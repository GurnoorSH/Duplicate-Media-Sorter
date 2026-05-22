# Developer Specification: Automated Photo Library Deduplication Engine

## 1. Context & Architectural Goal

You are building an automated pipeline to clean up a consolidated photo library containing images loaded from 3 different phones across nested directory structures.

Target Media formats: JPEG, PNG, and HEIC.

Safety constraints: Apple Live Photos are not present in this dataset (meaning video/still sidecar coordination is not required).

Core engine: The pipeline must utilize the PyPI duplicate_images command-line utility (find-dups).

Processing goals: The automation script must parse nested folders, cache perceptual hashes to prevent re-computation , cluster visually similar matches, and execute safety-first actions (such as dry-runs, moving to quarantine, or retaining the highest-quality version).

## 2. Environment Setup & Dependencies

The agent must generate an initialization block or a requirements.txt incorporating:

```text
duplicate_images>=0.11.0
pillow-heif>=1.0.0
```

HEIC Format Decryption: pillow-heif must be installed alongside the library to ensure Pillow can open and decode modern iOS/Android HEIC formats.

## 3. CLI Options & Parameter Specifications

The wrapper script must construct and execute find-dups CLI commands using these highly optimized flag mappings:

### A. Processing & Performance Options

Multithreading: Apply --parallel to utilize all free CPU cores during hash computation.

Progress Tracking: Apply --progress to render interactive progress bars for both parsing and duplicate lookup.

Caching (Crucial for multi-device scale): Include --hash-db {cache_file} to save and read computed hashes in JSON or Pickle format. This prevents re-analyzing unaltered images on subsequent runs.

### B. Similarity Hashing Parameters

Algorithm Selection (--algorithm): Use phash (default) for balanced structural transformations, or dhash for rapid gradient and layout consistency.

Precision Control (--hash-size): Defaults to 8. Tuning this parameter regulates the precision of the visual fingerprint.

Distance Thresholding (--max-distance):

Warning: Setting --max-distance to any value greater than $0$ changes the matching lookup complexity from $O(N)$ to $O(N^2)$ (slow exhaustive search).

Incompatibility: The --max-distance flag is mutually exclusive and conflicts with the --group parameter.

Design Rule: If processing massive libraries, recommend using --hash-size alone to tune thresholding without breaking $O(N)$ lookup efficiency.

### C. Grouping and Quality Preserving Actions (--on-equal)

To process duplicates collectively rather than just evaluating A-B pairs, instruct find-dups to act on groups :

Grouping Flag: Use --group to consolidate all visually identical copies into single evaluation clusters.

Quality Preservation:

Set --on-equal delete-smallest (or d<) to automatically identify matching clusters, evaluate their exact byte sizes, and purge the smaller (likely compressed or down-sampled WhatsApp) variants while preserving the highest-quality original.

Alternatively, for high safety, use --on-equal move-smallest (or m<) in conjunction with --move-to {quarantine_dir} to quarantine smaller duplicates instead of deleting them outright.

## 4. Wrapper Script Architecture

The developer agent should write a robust Python script (e.g., dedup_manager.py) that acts as a wrapper around the subprocess CLI call. The script must support three distinct execution stages:

### Stage 1: Scan & Cache (Dry Run)

Scan target directories, build/load the --hash-db, find duplicate groups, and print them to stdout. No files are deleted or moved.

### Stage 2: Quarantine Action

Perform the visual matching search and move the lower-quality/smallest duplicates into a dedicated quarantine directory using --on-equal move-smallest --move-to {quarantine_dir}.

### Stage 3: Auto-Purge (Destructive)

Perform matching and execute atomic deletions of duplicate images using --on-equal delete-smallest.

## 5. Python Automation Blueprint for the Agent

The agent should implement the automation logic following this core Python implementation structure:

```Python
import os
import subprocess
import argparse
import sys
import shutil

def run_deduplication(target_dir, cache_db, mode, quarantine_dir=None, algorithm="phash", hash_size=8):
    """
    Executes and automates the find-dups CLI based on specification parameters.
    """
    # Ensure target directory exists
    if not os.path.exists(target_dir):
        print(f"Error: Target directory '{target_dir}' does not exist.")
        sys.exit(1)

    # Base arguments for high performance and logging 
    cmd = [
        "find-dups", 
        target_dir,
        "--parallel",
        "--progress",
        "--algorithm", algorithm,
        "--hash-size", str(hash_size),
        "--hash-db", cache_db
    ]

    # Mode configuration and action mapping
    if mode == "dry-run":
        # Group duplicates and print them without executing changes
        cmd.extend(["--group", "--on-equal", "print"])
        print("â–¶ Running in DRY-RUN mode. Visualizing duplicate groups...")

    elif mode == "quarantine":
        if not quarantine_dir:
            print("Error: Quarantine directory must be specified for quarantine mode.")
            sys.exit(1)
        os.makedirs(quarantine_dir, exist_ok=True)
        # Move the smallest files in the matched groups to quarantine
        cmd.extend([
            "--group", 
            "--on-equal", "move-smallest", 
            "--move-to", quarantine_dir,
            "--move-recreate-path" # Optional: recreates source path tree in quarantine )
        print(f"â–¶ Running in QUARANTINE mode. Moving duplicates to: {quarantine_dir}")

    elif mode == "delete":
        # Destructive action: Keeps the largest/best copy and purges the rest
        cmd.extend(["--group", "--on-equal", "delete-smallest"])
        
        # Double safety confirmation [3]
        confirm = input("âš ï¸ WARNING: Destructive deletion mode selected. Are you sure? (y/N): ")
        if confirm.lower()!= 'y':
            print("Operation aborted by user.")
            sys.exit(0)
        print("â–¶ Running in DESTRUCTIVE mode. Purging duplicate files...")

    # Execute the CLI command
    try:
        print(f"Executing: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=True, text=True)
        print("Process completed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"CLI Error encountered during processing: {e}", file=sys.stderr)
        sys.exit(e.returncode)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Device Image Deduplicator Wrapper")
    parser.add_argument("target", help="The consolidated photos root folder")
    parser.add_argument("--cache", default="photo_hashes.json", help="Path to write/read hash DB ")
    parser.add_argument("--mode", choices=["dry-run", "quarantine", "delete"], default="dry-run")
    parser.add_argument("--quarantine-dir", default="./quarantine_folder")
    parser.add_argument("--algorithm", default="phash", choices=["phash", "dhash", "ahash", "whash"])
    parser.add_argument("--hash-size", type=int, default=8)
    
    args = parser.parse_args()
    run_deduplication(
        target_dir=args.target,
        cache_db=args.cache,
        mode=args.mode,
        quarantine_dir=args.quarantine_dir,
        algorithm=args.algorithm,
        hash_size=args.hash_size
    )
```

## 6. Acceptance Criteria for the Coding Agent

Ensure your agent satisfies the following conditions during validation:

Dependency Validation: The generated code must call register_heif_opener() or verify pillow-heif presence if executing any helper tests containing HEIC format extensions.

Path Sanitization: The CLI execution must robustly handle filenames containing whitespace, special characters, and typical phone-export naming structures (e.g. WhatsApp Image 2026...jpeg).

Graceful Error Handling: If an image file within the directory is corrupted or unreadable, the command should log the anomaly but continue processing the rest of the repository.

Cache Preservation: Ensure that running a "dry-run" first successfully populates the .json hash file, and that subsequent executions load directly from this file without scanning the images again.
