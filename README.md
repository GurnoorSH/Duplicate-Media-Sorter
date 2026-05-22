# Duplicate Media Sorter

Safety-first wrapper for cleaning a consolidated photo library with the
`duplicate_images` `find-dups` CLI.

The implementation follows the supplied spec and the upstream
[`duplicate_images` documentation](https://github.com/lene/DuplicateImages):
grouped duplicate detection, cached perceptual hashes, parallel hashing,
progress output, and smallest-file actions to preserve the highest-quality copy.

## Setup

Use Python 3.9 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

The dependency file includes:

- `duplicate_images>=0.11.0`
- `pillow-heif>=1.0.0`

`pillow-heif` is required so HEIC/HEIF files from phones can be decoded by
Pillow. When HEIC files are detected, `dedup_manager.py` registers the HEIF
opener in the current process and injects a tiny `sitecustomize.py` bootstrap so
the `find-dups` subprocess registers it too.

## Stage 1: scan and cache

```powershell
python .\dedup_manager.py "D:\Photos\Consolidated Library" `
  --mode dry-run `
  --cache ".\photo_hashes.json"
```

This prints duplicate groups and creates or reuses the hash cache. No files are
moved or deleted.

## Stage 2: quarantine smaller duplicates

```powershell
python .\dedup_manager.py "D:\Photos\Consolidated Library" `
  --mode quarantine `
  --cache ".\photo_hashes.json" `
  --quarantine-dir "D:\Photos\Duplicate Quarantine"
```

This uses `--on-equal move-smallest` and `--move-recreate-path`, preserving the
source folder structure under the quarantine directory.

## Stage 3: auto-purge smaller duplicates

```powershell
python .\dedup_manager.py "D:\Photos\Consolidated Library" `
  --mode delete `
  --cache ".\photo_hashes.json"
```

Delete mode is destructive. The wrapper requires typing `DELETE` before it runs.
For non-interactive automation, pass `--yes` only after validating a dry-run and
preferably a quarantine run.

## Useful options

- `--algorithm phash`: balanced default. `dhash` can be faster for layout-like
  similarity.
- `--hash-size 8`: tune this before considering `--max-distance`; grouped mode
  intentionally avoids `--max-distance` because upstream documents it as
  incompatible with `--group` and much slower on large libraries.
- `--exclude-dir REGEX`: repeat to skip generated or archival folders.
- `--parallel-workers N`: limit CPU worker count; otherwise `find-dups` uses all
  free cores.
- `--print-command-only`: validate paths and show the exact `find-dups` command.

Paths are passed to subprocess as argument lists rather than shell strings, so
names such as `WhatsApp Image 2026...jpeg` and folders with spaces are handled
without extra quoting.
