# Duplicate Media Sorter

Safety-first tools for finding duplicate media in large, nested phone/photo
archives.

The project now handles two media lanes:

- Images: perceptual duplicate detection through the `duplicate_images`
  `find-dups` CLI, wrapped by `dedup_manager.py`.
- Videos: exact byte-for-byte duplicate detection through
  `video_exact_dedup.py`, plus near-duplicate detection through Video Duplicate
  Finder (VDF).

The default workflow is scan, review, quarantine. Delete mode exists only for
the image wrapper and should be treated as a final optional step after the
quarantine output has been verified.

## Setup

Use Python 3.9 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

The Python dependency file includes:

- `duplicate_images>=0.11.0`
- `pillow-heif>=1.0.0`

`pillow-heif` lets Pillow decode HEIC/HEIF files from phones. When HEIC files
are detected, `dedup_manager.py` registers the HEIF opener in the current
process and injects a small `sitecustomize.py` bootstrap so the `find-dups`
subprocess registers it too.

For near-duplicate video scans, install VDF CLI and FFmpeg shared binaries under
`tools/`:

- `tools\vdf\cli\vdf-cli.exe`
- `tools\vdf\ffmpeg-shared\...\bin\ffmpeg.exe`

The `tools/` directory is ignored by git because these are downloaded runtime
artifacts.

## Image Workflow

### Stage 1: scan and cache

```powershell
python .\dedup_manager.py "D:\Photos\Consolidated Library" `
  --mode dry-run `
  --cache ".\photo_hashes.json"
```

This prints duplicate groups and creates or reuses the hash cache. No files are
moved or deleted.

### Stage 2: quarantine smaller duplicates

```powershell
python .\dedup_manager.py "D:\Photos\Consolidated Library" `
  --mode quarantine `
  --cache ".\photo_hashes.json" `
  --quarantine-dir "D:\Photos\Duplicate Image Quarantine"
```

This uses `--on-equal move-smallest` and `--move-recreate-path`, preserving the
source folder structure under the quarantine directory. The original library
keeps the larger/better-ranked copy.

### Stage 3: optional image delete

```powershell
python .\dedup_manager.py "D:\Photos\Consolidated Library" `
  --mode delete `
  --cache ".\photo_hashes.json"
```

Delete mode is destructive. The wrapper requires typing `DELETE` before it runs.
For non-interactive automation, pass `--yes` only after validating a dry-run and
preferably a quarantine run.

## Exact Video Workflow

`video_exact_dedup.py` finds byte-for-byte duplicate videos with SHA-256. It
does not try to identify visually similar re-encodes; that is the VDF phase.

Supported video extensions include:

`.3g2`, `.3gp`, `.avi`, `.flv`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`,
`.mpeg`, `.mpg`, `.mts`, `.ts`, `.vob`, `.webm`, `.wmv`.

### Stage 1: exact-video dry run

```powershell
python .\video_exact_dedup.py "D:\Photos\Consolidated Library" `
  --mode dry-run `
  --cache ".\video_exact_hashes.json" `
  --report ".\video_exact_dry_run_report.txt"
```

### Stage 2: exact-video quarantine

```powershell
python .\video_exact_dedup.py "D:\Photos\Consolidated Library" `
  --mode quarantine `
  --cache ".\video_exact_hashes.json" `
  --report ".\video_exact_quarantine_report.txt" `
  --quarantine-dir "D:\Photos\Duplicate Video Exact Quarantine"
```

The script keeps one copy from each exact group and moves the duplicate copies
into the quarantine folder, preserving their source-relative paths. It does not
delete files.

## Near-Duplicate Video Workflow

Near-video detection is done with VDF because re-encoded videos do not share the
same bytes. VDF compares sampled video frames and writes duplicate groups to
JSON.

Use a fresh, absolute database folder for verification runs. Relative `--db`
paths can be easy to misread because VDF may fall back to its default database
beside the CLI.

```powershell
$ffmpegBin = "C:\Path\To\Duplicate Media Sorter\tools\vdf\ffmpeg-shared\ffmpeg-8.1.1-full_build-shared\bin"
$db = "C:\Path\To\Duplicate Media Sorter\vdf_video_db_clean"
$env:PATH = "$ffmpegBin;$env:PATH"
New-Item -ItemType Directory -Force -Path $db | Out-Null

.\tools\vdf\cli\vdf-cli.exe scan-and-compare `
  -i "D:\Photos\Consolidated Library" `
  --db $db `
  --threshold 5 `
  --percent 96 `
  --parallelism 1 `
  -f json `
  -o ".\video_near_vdf_report.json" `
  *> ".\video_near_vdf_console.log"
```

For safety, do not use VDF `--delete` or `--delete-permanent`. Review the JSON
groups first, choose the keeper by quality, and move only the lower-quality
copies into a separate near-video quarantine folder.

The selection rule used for the `Phone Pics` run was:

1. Keep the highest `FrameSizeInt`.
2. If tied, keep the highest `BitRateKbs`.
3. If still tied, keep the largest `SizeLong`.
4. Move the remaining item(s) in that group to near-video quarantine.

PowerShell may label VDF's normal stderr progress output as `NativeCommandError`
inside a redirected log. Treat the process exit code and the JSON report as the
source of truth. A successful clean run should exit `0`, write
`Results written to: ...`, and produce a valid JSON array.

## Useful Options

- `--algorithm phash`: balanced default for image matching. `dhash` can be
  faster for layout-like similarity.
- `--hash-size 8`: tune this before considering `--max-distance`; grouped image
  mode intentionally avoids `--max-distance` because upstream documents it as
  incompatible with `--group` and much slower on large libraries.
- `--exclude-dir REGEX`: repeat to skip generated or archival folders.
- `--parallel-workers N`: limit image hashing CPU worker count; otherwise
  `find-dups` uses all free cores.
- `--print-command-only`: validate image paths and show the exact `find-dups`
  command.
- VDF `--percent 96`: strict near-video threshold used in the current workflow.
  Lower values may find more candidates but increase false positives.

Paths are passed to subprocesses as argument lists rather than shell strings, so
names such as `WhatsApp Image 2026...jpeg` and folders with spaces are handled
without extra quoting.

## Recent Phone Pics Run

The process was run against:

`C:\Users\gurno\Desktop\Phone Pics`

Final state after the video passes:

- Exact video quarantine:
  `C:\Users\gurno\Desktop\Phone Pics_video_quarantine_exact`
- Near-video quarantine:
  `C:\Users\gurno\Desktop\Phone Pics_video_quarantine_near`
- Exact post-quarantine report: `0` exact duplicate video groups.
- Clean VDF post-quarantine report: `[]`, meaning no near-video groups remained
  at the 96 percent threshold.
- No delete phase was run.
