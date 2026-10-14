#!/usr/bin/env python3
"""Shared services for the Duplicate Media Sorter desktop app.

The existing command-line tools remain useful on their own.  This module adds
structured scan results, review-friendly keeper suggestions, and reversible
quarantine manifests for the desktop interface.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from video_exact_dedup import (
    VideoRecord,
    VideoScanCancelled,
    duplicate_groups,
    hash_file,
    load_cache,
    save_cache,
    scan_videos,
)


MANIFEST_VERSION = 1
MANIFEST_SAVE_INTERVAL = 250
DEFAULT_FIND_DUPS = "find-dups"
FIND_DUPS_MODULE = "duplicate_images.duplicate"
# Names such as "IMG_1 - Copy", "IMG_1 - Copy (2)", "IMG_1 (1)", "IMG_1_copy" or
# our own "IMG_1__duplicate_1" mark a file as the copy rather than the original.
COPY_NAME_PATTERN = re.compile(r"(?:\s*-\s*copy(?:\s*\(\d+\))?|\s*\(\d+\)|[\s_]copy|__duplicate_\d+)$", re.IGNORECASE)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
PATH_SEPARATOR_PATTERN = r"[\\/]"
PHOTO_LANE = "photos"
EXACT_VIDEO_LANE = "videos_exact"
NEAR_VIDEO_LANE = "videos_near"
LANE_FOLDERS = {
    PHOTO_LANE: Path("photos"),
    EXACT_VIDEO_LANE: Path("videos") / "exact",
    NEAR_VIDEO_LANE: Path("videos") / "near",
}
SYSTEM_EXCLUDE_DIRS = ("$RECYCLE.BIN", "System Volume Information")
ProgressCallback = Callable[["ScanProgress"], None]


class ScanCancelled(RuntimeError):
    """Raised when a scan is cancelled by the caller."""


@dataclass
class ScanProgress:
    stage: str
    message: str
    current: int | None = None
    total: int | None = None
    percent: int | None = None
    warning: bool = False


@dataclass
class MediaItem:
    path: Path
    lane: str
    size: int
    modified_ns: int
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    bitrate_kbps: int | None = None
    metadata_error: str | None = None

    @property
    def pixel_area(self) -> int:
        return (self.width or 0) * (self.height or 0)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["path"] = str(self.path)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "MediaItem":
        data = dict(raw)
        data["path"] = Path(str(data["path"]))
        return cls(**data)  # type: ignore[arg-type]


@dataclass
class DuplicateGroup:
    id: str
    lane: str
    items: list[MediaItem]
    keeper_path: Path
    # True when every file in the group has identical bytes. Groups that only
    # look alike (re-encodes, burst frames, edits) are False and need review.
    exact: bool = False

    @property
    def keeper(self) -> MediaItem:
        for item in self.items:
            if paths_equal(item.path, self.keeper_path):
                return item
        raise ValueError(f"keeper is not part of duplicate group {self.id}")

    @property
    def duplicates(self) -> list[MediaItem]:
        return [item for item in self.items if not paths_equal(item.path, self.keeper_path)]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(item.size for item in self.duplicates)

    def set_keeper(self, path: Path) -> None:
        if not any(paths_equal(item.path, path) for item in self.items):
            raise ValueError(f"keeper is not part of duplicate group {self.id}: {path}")
        self.keeper_path = path

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "lane": self.lane,
            "keeper_path": str(self.keeper_path),
            "items": [item.to_dict() for item in self.items],
            "exact": self.exact,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "DuplicateGroup":
        return cls(
            id=str(raw["id"]),
            lane=str(raw["lane"]),
            keeper_path=Path(str(raw["keeper_path"])),
            items=[MediaItem.from_dict(item) for item in raw["items"]],  # type: ignore[arg-type]
            exact=bool(raw.get("exact", False)),
        )


@dataclass
class ScanRequest:
    source_root: Path
    quarantine_root: Path | None = None
    include_photos: bool = True
    include_exact_videos: bool = True
    include_near_videos: bool = False
    photo_algorithm: str = "phash"
    photo_hash_size: int = 8
    find_dups_executable: str = "find-dups"
    vdf_executable: Path | None = None
    ffmpeg_executable: Path | None = None
    ffprobe_executable: Path | None = None

    @property
    def effective_quarantine_root(self) -> Path:
        return self.quarantine_root or default_quarantine_dir(self.source_root)


@dataclass
class ScanResult:
    request: ScanRequest
    groups: list[DuplicateGroup]
    warnings: list[str] = field(default_factory=list)

    def groups_for_lane(self, lane: str) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.lane == lane]


@dataclass
class ManifestEntry:
    id: str
    group_id: str
    lane: str
    keeper_path: str
    original_path: str
    moved_path: str
    metadata: dict[str, object]
    status: str = "pending"
    error: str | None = None


@dataclass
class RunManifest:
    version: int
    run_id: str
    created_at: str
    source_root: str
    quarantine_root: str
    entries: list[ManifestEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return manifest_path(Path(self.quarantine_root), self.run_id)

    @property
    def journal_path(self) -> Path:
        return journal_path_for(self.path)

    def save(self) -> Path:
        """Write the full manifest and clear the journal it now contains."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temporary.replace(path)
        self.journal_path.unlink(missing_ok=True)
        return path

    def record(self, entry: ManifestEntry) -> None:
        """Append one entry's current state to the journal.

        Rewriting the whole manifest after every move is quadratic for large
        runs, so per-file progress goes to an append-only journal and the full
        manifest is saved periodically. ``load`` replays the journal, so a run
        interrupted between saves is still fully recoverable.
        """
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry)) + "\n")
            handle.flush()

    @classmethod
    def load(cls, path: Path) -> "RunManifest":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version: {raw.get('version')}")
        entries = {entry["id"]: ManifestEntry(**entry) for entry in raw.get("entries", [])}
        journal = journal_path_for(path)
        if journal.exists():
            for line in journal.read_text(encoding="utf-8").splitlines():
                try:
                    entry = ManifestEntry(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue  # A torn final line from an interrupted write.
                entries[entry.id] = entry
        raw["entries"] = list(entries.values())
        return cls(**raw)


@dataclass(frozen=True)
class VdfTools:
    available: bool
    reason: str
    vdf: Path | None = None
    ffmpeg: Path | None = None
    ffprobe: Path | None = None


def emit(callback: ProgressCallback | None, progress: ScanProgress) -> None:
    if callback is not None:
        callback(progress)


def path_key(path: Path) -> str:
    # abspath rather than resolve(): resolve() opens each file on Windows,
    # which adds up across tens of thousands of comparisons.
    return os.path.normcase(os.path.abspath(path))


def paths_equal(first: Path, second: Path) -> bool:
    return path_key(first) == path_key(second)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def default_quarantine_dir(source_root: Path) -> Path:
    source_root = source_root.expanduser().resolve()
    name = source_root.stem if source_root.is_file() else source_root.name
    return source_root.parent / f"{name or 'media_library'}_quarantine"


def local_app_data_dir() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "share"


def library_workspace(source_root: Path) -> Path:
    root = str(source_root.expanduser().resolve())
    digest = hashlib.sha256(os.path.normcase(root).encode("utf-8")).hexdigest()[:16]
    return local_app_data_dir() / "DuplicateMediaSorter" / "libraries" / digest


def manifest_path(quarantine_root: Path, run_id: str) -> Path:
    return runs_dir(quarantine_root) / f"{run_id}.json"


def journal_path_for(manifest: Path) -> Path:
    return manifest.with_suffix(".journal.jsonl")


def runs_dir(quarantine_root: Path) -> Path:
    return quarantine_root / ".duplicate-media-sorter" / "runs"


def list_run_manifests(quarantine_root: Path) -> list[Path]:
    runs = runs_dir(quarantine_root)
    if not runs.exists():
        return []
    return sorted(runs.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)


def automatic_exclude_names(source_root: Path, quarantine_root: Path) -> tuple[str, ...]:
    names = list(SYSTEM_EXCLUDE_DIRS)
    if is_within(quarantine_root, source_root):
        names.append(quarantine_root.name)
    return tuple(dict.fromkeys(names))


def photo_exclude_patterns(source_root: Path, quarantine_root: Path) -> tuple[str, ...]:
    """Regexes for find-dups ``--exclude-dir``.

    find-dups applies ``re.search`` to each file's full parent path, so system
    folders are matched as whole path components and an internal quarantine
    folder is matched by its absolute path.
    """
    separator = PATH_SEPARATOR_PATTERN
    patterns = [rf"(?i)(?:^|{separator}){re.escape(name)}(?:{separator}|$)" for name in SYSTEM_EXCLUDE_DIRS]
    if is_within(quarantine_root, source_root):
        patterns.append(rf"(?i)^{re.escape(str(quarantine_root.resolve()))}(?:{separator}|$)")
    return tuple(patterns)


def group_id(lane: str, paths: Iterable[Path], kind: str = "") -> str:
    normalized = sorted(path_key(path) for path in paths)
    digest = hashlib.sha256((lane + kind + "\0" + "\0".join(normalized)).encode("utf-8")).hexdigest()[:16]
    return f"{lane}-{digest}"


def media_item_for_file(path: Path, lane: str) -> MediaItem:
    stat = path.stat()
    return MediaItem(path=path, lane=lane, size=stat.st_size, modified_ns=stat.st_mtime_ns)


@functools.cache
def register_heif_support() -> None:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return
    register_heif_opener()


def photo_item(path: Path) -> MediaItem:
    item = media_item_for_file(path, PHOTO_LANE)
    register_heif_support()
    try:
        from PIL import Image

        with Image.open(path) as image:
            item.width, item.height = image.size
    except Exception as exc:  # Pillow deliberately reports several error types.
        item.metadata_error = str(exc)
    return item


def copy_name_penalty(path: Path) -> int:
    return 1 if COPY_NAME_PATTERN.search(path.stem) else 0


def photo_keeper(items: Sequence[MediaItem]) -> MediaItem:
    return min(
        items,
        key=lambda item: (
            -item.pixel_area,
            -item.size,
            copy_name_penalty(item.path),
            item.modified_ns,
            os.path.normcase(str(item.path)),
        ),
    )


def exact_video_keeper(items: Sequence[MediaItem]) -> MediaItem:
    return min(items, key=lambda item: (copy_name_penalty(item.path), item.modified_ns, os.path.normcase(str(item.path))))


def near_video_keeper(items: Sequence[MediaItem]) -> MediaItem:
    return min(
        items,
        key=lambda item: (
            -item.pixel_area,
            -(item.bitrate_kbps or 0),
            -item.size,
            copy_name_penalty(item.path),
            os.path.normcase(str(item.path)),
        ),
    )


def find_dups_command_prefix(configured: str = DEFAULT_FIND_DUPS) -> list[str] | None:
    """Return the argv prefix that launches find-dups, or None if unavailable.

    The default runs the installed module with this interpreter. Console-script
    launchers such as ``.venv\\Scripts\\find-dups.exe`` embed the interpreter
    path from install time and fail silently once the project folder moves.
    """
    if configured == DEFAULT_FIND_DUPS and importlib.util.find_spec("duplicate_images") is not None:
        return [sys.executable, "-m", FIND_DUPS_MODULE]
    discovered = shutil.which(configured)
    if discovered:
        return [discovered]
    configured_path = Path(configured)
    if configured_path.exists():
        return [str(configured_path.resolve())]
    return None


def build_photo_scan_command(
    request: ScanRequest,
    cache_path: Path,
    find_dups: Sequence[str] = (DEFAULT_FIND_DUPS,),
) -> list[str]:
    command = [
        *find_dups,
        str(request.source_root),
        "--parallel",
        "--progress",
        "--algorithm",
        request.photo_algorithm,
        "--hash-size",
        str(request.photo_hash_size),
        "--hash-db",
        str(cache_path),
        "--group",
    ]
    patterns = photo_exclude_patterns(request.source_root, request.effective_quarantine_root)
    if patterns:
        command.append("--exclude-dir")
        command.extend(patterns)
    # "quote" prints each group as one line of shell-quoted paths on stdout.
    # The previous "--on-equal exec" route ran every group through cmd.exe,
    # where '&', '%' or '^' in a file name broke parsing or ran commands.
    command.extend(["--on-equal", "quote"])
    return command


def find_dups_environment(extra_pythonpath: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # Paths are printed on stdout; force UTF-8 so non-ASCII names survive the pipe.
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_pythonpath is not None:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{extra_pythonpath}{os.pathsep}{existing}" if existing else str(extra_pythonpath)
    return env


def stream_process(
    command: Sequence[str],
    callback: ProgressCallback | None,
    cancel_event: threading.Event | None,
    stage: str,
    env: dict[str, str] | None = None,
    stdout_lines: list[str] | None = None,
) -> int:
    """Run a command, forwarding its output as progress.

    When ``stdout_lines`` is given, stdout is collected there as data and only
    stderr is forwarded as progress.
    """
    emit(callback, ScanProgress(stage, subprocess.list2cmdline(list(command))))
    capture = stdout_lines is not None
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture else subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def collect_output(stream, name: str) -> None:  # type: ignore[no-untyped-def]
        for line in stream:
            output_queue.put((name, line.rstrip("\r\n")))
        output_queue.put((name, None))

    streams = [(process.stdout, "stdout")]
    if capture:
        streams.append((process.stderr, "stderr"))
    threads = [threading.Thread(target=collect_output, args=stream, daemon=True) for stream in streams]
    for thread in threads:
        thread.start()

    def close() -> None:
        for thread in threads:
            thread.join(timeout=1)
        for stream, _name in streams:
            if stream is not None:
                stream.close()

    open_streams = len(streams)
    while open_streams:
        if cancel_event is not None and cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            close()
            raise ScanCancelled("scan cancelled")
        try:
            name, line = output_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            open_streams -= 1
        elif capture and name == "stdout":
            stdout_lines.append(line)  # type: ignore[union-attr]
        else:
            line = ANSI_ESCAPE.sub("", line).strip()
            if line:
                emit(callback, ScanProgress(stage, line, warning="WARNING" in line))
    return_code = process.wait()
    close()
    return return_code


def parse_quoted_groups(lines: Iterable[str]) -> list[list[Path]]:
    """Parse ``find-dups --on-equal quote`` output: one shell-quoted group per line."""
    groups: list[list[Path]] = []
    seen: set[tuple[str, ...]] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            paths = [Path(value) for value in shlex.split(line, posix=True)]
        except ValueError:
            continue  # Not a group line.
        if len(paths) < 2:
            continue
        key = tuple(sorted(os.path.normcase(str(path)) for path in paths))
        if key not in seen:
            groups.append(paths)
            seen.add(key)
    return groups


class DigestCache:
    """SHA-256 digests keyed by resolved path and invalidated by size or mtime."""

    algorithm = "sha256"

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.entries = load_cache(path)
        except (OSError, ValueError):
            self.entries = {}
        self.dirty = False

    def digest(self, path: Path) -> str:
        stat = path.stat()
        key = str(path.resolve())
        cached = self.entries.get(key)
        if (
            cached
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("algorithm") == self.algorithm
        ):
            return str(cached["digest"])
        value = hash_file(path, self.algorithm)
        self.entries[key] = {"algorithm": self.algorithm, "digest": value, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        self.dirty = True
        return value

    def save(self) -> None:
        if self.dirty:
            save_cache(self.path, self.entries)
            self.dirty = False


def split_identical(items: Sequence[MediaItem], digests: DigestCache) -> list[list[MediaItem]]:
    """Partition files into sets with identical bytes; unique files stay alone."""
    by_size: dict[int, list[MediaItem]] = defaultdict(list)
    for item in items:
        by_size[item.size].append(item)
    contents: list[list[MediaItem]] = []
    for same_size in by_size.values():
        if len(same_size) == 1:
            contents.append(same_size)
            continue
        by_digest: dict[str, list[MediaItem]] = defaultdict(list)
        for item in same_size:
            try:
                key = digests.digest(item.path)
            except OSError:
                key = f"unreadable:{item.path}"
            by_digest[key].append(item)
        contents.extend(by_digest.values())
    return contents


def build_photo_groups(
    captured: Sequence[Sequence[Path]],
    digests: DigestCache,
    callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[DuplicateGroup]:
    """Turn perceptual-hash matches into exact and similar review groups.

    Each match is split by content: files with identical bytes become an exact
    group, and when a match holds more than one distinct image, one
    representative of each forms a similar group that needs human review.
    """
    total = sum(len(paths) for paths in captured)
    current = 0
    groups: list[DuplicateGroup] = []
    for paths in captured:
        items: list[MediaItem] = []
        for path in paths:
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled("scan cancelled")
            current += 1
            emit(callback, ScanProgress("photo_metadata", f"Reading photo metadata: {path.name}", current, total, percent(current, total)))
            try:
                items.append(photo_item(path))
            except OSError as exc:
                emit(callback, ScanProgress("photo_metadata", f"Could not inspect {path}: {exc}", warning=True))
        if len(items) < 2:
            continue
        contents = split_identical(items, digests)
        for copies in contents:
            if len(copies) > 1:
                keeper = photo_keeper(copies)
                groups.append(DuplicateGroup(group_id(PHOTO_LANE, (item.path for item in copies), "exact"), PHOTO_LANE, copies, keeper.path, exact=True))
        if len(contents) > 1:
            representatives = [photo_keeper(copies) for copies in contents]
            keeper = photo_keeper(representatives)
            groups.append(DuplicateGroup(group_id(PHOTO_LANE, (item.path for item in representatives), "similar"), PHOTO_LANE, representatives, keeper.path))
    digests.save()
    return groups


def scan_photos(
    request: ScanRequest,
    callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    warnings: list[str] | None = None,
) -> list[DuplicateGroup]:
    find_dups = find_dups_command_prefix(request.find_dups_executable)
    if find_dups is None:
        raise RuntimeError(
            f"Could not find '{request.find_dups_executable}'. "
            "Install dependencies with: python -m pip install -r requirements.txt"
        )

    workspace = library_workspace(request.source_root)
    workspace.mkdir(parents=True, exist_ok=True)
    cache_path = workspace / "photo_hashes.json"
    command = build_photo_scan_command(request, cache_path, find_dups)
    from dedup_manager import ensure_heif_support, find_heic_files, write_heif_bootstrap

    bootstrap = None
    if find_heic_files(request.source_root):
        ensure_heif_support(required=True)
        bootstrap = write_heif_bootstrap(workspace / ".dedup_runtime")

    def forward(progress: ScanProgress) -> None:
        if progress.warning and warnings is not None:
            warnings.append(progress.message)
        emit(callback, progress)

    stdout: list[str] = []
    return_code = stream_process(command, forward, cancel_event, "photos", find_dups_environment(bootstrap), stdout)
    if return_code != 0:
        raise RuntimeError(f"find-dups exited with status {return_code}")

    quarantine_root = request.effective_quarantine_root
    captured = []
    for paths in parse_quoted_groups(stdout):
        kept = [path for path in paths if not is_within(path, quarantine_root)]
        if len(kept) > 1:
            captured.append(kept)
    return build_photo_groups(captured, DigestCache(workspace / "photo_digests.json"), callback, cancel_event)


def exact_video_groups(records: Sequence[VideoRecord]) -> list[DuplicateGroup]:
    groups: list[DuplicateGroup] = []
    for records_group in duplicate_groups(records):
        items = [media_item_for_file(record.path, EXACT_VIDEO_LANE) for record in records_group]
        keeper = exact_video_keeper(items)
        groups.append(DuplicateGroup(group_id(EXACT_VIDEO_LANE, (item.path for item in items)), EXACT_VIDEO_LANE, items, keeper.path, exact=True))
    return groups


def scan_exact_videos(
    request: ScanRequest,
    callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[DuplicateGroup]:
    workspace = library_workspace(request.source_root)
    workspace.mkdir(parents=True, exist_ok=True)

    def on_video_progress(current: int, total: int, message: str) -> None:
        emit(callback, ScanProgress("videos_exact", message, current, total, percent(current, total)))

    try:
        records = scan_videos(
            request.source_root,
            workspace / "video_exact_hashes.json",
            "sha256",
            progress_callback=on_video_progress,
            cancel_event=cancel_event,
            exclude_dirs=automatic_exclude_names(request.source_root, request.effective_quarantine_root),
        )
    except VideoScanCancelled as exc:
        raise ScanCancelled(str(exc)) from exc
    return exact_video_groups(records)


def find_tool(explicit: Path | None, name: str, project_root: Path | None = None) -> Path | None:
    if explicit is not None and explicit.exists():
        return explicit.resolve()
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered)
    if project_root is not None:
        matches = sorted((project_root / "tools").rglob(name))
        if matches:
            return matches[0]
    return None


def probe_vdf_tools(request: ScanRequest | None = None, project_root: Path | None = None) -> VdfTools:
    request = request or ScanRequest(Path.cwd())
    project_root = project_root or Path(__file__).parent
    vdf = find_tool(request.vdf_executable, "vdf-cli.exe" if os.name == "nt" else "vdf-cli", project_root)
    ffmpeg = find_tool(request.ffmpeg_executable, "ffmpeg.exe" if os.name == "nt" else "ffmpeg", project_root)
    ffprobe = find_tool(request.ffprobe_executable, "ffprobe.exe" if os.name == "nt" else "ffprobe", project_root)
    missing = [name for name, tool in (("VDF", vdf), ("FFmpeg", ffmpeg), ("FFprobe", ffprobe)) if tool is None]
    if missing:
        return VdfTools(False, f"Optional near-video scan unavailable. Missing: {', '.join(missing)}.", vdf, ffmpeg, ffprobe)
    return VdfTools(True, "Near-video scan available.", vdf, ffmpeg, ffprobe)


def detect_vdf_input_flag(vdf: Path) -> str:
    result = subprocess.run([str(vdf), "scan-and-compare", "--help"], capture_output=True, text=True, errors="replace", check=False)
    help_text = f"{result.stdout}\n{result.stderr}"
    return "--include" if "--include" in help_text else "-i"


def vdf_command(request: ScanRequest, tools: VdfTools, report_path: Path, db_path: Path, input_flag: str) -> list[str]:
    if tools.vdf is None:
        raise RuntimeError(tools.reason)
    return [
        str(tools.vdf),
        "scan-and-compare",
        input_flag,
        str(request.source_root),
        "--db",
        str(db_path),
        "--threshold",
        "5",
        "--percent",
        "96",
        "--parallelism",
        "1",
        "-f",
        "json",
        "-o",
        str(report_path),
    ]


def first_value(raw: dict[str, object], keys: Sequence[str], default: object = None) -> object:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


def integer_value(raw: object) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_frame_size(raw: object) -> tuple[int | None, int | None]:
    if isinstance(raw, str):
        match = re.search(r"(\d+)\s*[xX]\s*(\d+)", raw)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def vdf_item(raw: dict[str, object]) -> MediaItem:
    path = Path(str(first_value(raw, ("Path", "path", "FilePath", "filePath", "File"))))
    stat = path.stat() if path.exists() else None
    width = integer_value(first_value(raw, ("Width", "width")))
    height = integer_value(first_value(raw, ("Height", "height")))
    if not width or not height:
        width, height = parse_frame_size(first_value(raw, ("FrameSize", "frameSize")))
    return MediaItem(
        path=path,
        lane=NEAR_VIDEO_LANE,
        size=integer_value(first_value(raw, ("SizeLong", "size", "Size"), stat.st_size if stat else 0)) or 0,
        modified_ns=stat.st_mtime_ns if stat else 0,
        width=width,
        height=height,
        duration_seconds=float(first_value(raw, ("Duration", "duration"), 0) or 0),
        bitrate_kbps=integer_value(first_value(raw, ("BitRateKbs", "bitrateKbps", "BitrateKbps"))),
    )


def vdf_group_values(raw: object) -> list[object]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("Groups", "groups", "DuplicateGroups", "duplicateGroups", "Results", "results"):
            values = raw.get(key)
            if isinstance(values, list):
                return values
    raise ValueError("VDF report does not contain a duplicate-group list")


def vdf_group_items(raw: object) -> list[dict[str, object]]:
    if isinstance(raw, list):
        return [value for value in raw if isinstance(value, dict)]
    if isinstance(raw, dict):
        for key in ("Files", "files", "Items", "items", "Duplicates", "duplicates"):
            values = raw.get(key)
            if isinstance(values, list):
                return [value for value in values if isinstance(value, dict)]
    return []


def normalize_vdf_report(raw: object) -> list[DuplicateGroup]:
    groups: list[DuplicateGroup] = []
    for value in vdf_group_values(raw):
        items = [vdf_item(item) for item in vdf_group_items(value)]
        if len(items) > 1:
            keeper = near_video_keeper(items)
            groups.append(DuplicateGroup(group_id(NEAR_VIDEO_LANE, (item.path for item in items)), NEAR_VIDEO_LANE, items, keeper.path))
    return groups


def scan_near_videos(
    request: ScanRequest,
    callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[DuplicateGroup]:
    tools = probe_vdf_tools(request)
    if not tools.available:
        raise RuntimeError(tools.reason)
    workspace = library_workspace(request.source_root)
    db_path = workspace / "vdf_db"
    report_path = workspace / "video_near_vdf_report.json"
    db_path.mkdir(parents=True, exist_ok=True)
    input_flag = detect_vdf_input_flag(tools.vdf)  # type: ignore[arg-type]
    env = os.environ.copy()
    assert tools.ffmpeg is not None
    env["PATH"] = f"{tools.ffmpeg.parent}{os.pathsep}{env.get('PATH', '')}"
    result = stream_process(vdf_command(request, tools, report_path, db_path, input_flag), callback, cancel_event, "videos_near", env)
    if result != 0:
        raise RuntimeError(f"VDF exited with status {result}")
    return normalize_vdf_report(json.loads(report_path.read_text(encoding="utf-8")))


def scan_library(
    request: ScanRequest,
    callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> ScanResult:
    request.source_root = request.source_root.expanduser().resolve()
    if not request.source_root.is_dir():
        raise ValueError(f"Source folder does not exist: {request.source_root}")
    request.quarantine_root = request.effective_quarantine_root.expanduser().resolve()
    groups: list[DuplicateGroup] = []
    warnings: list[str] = []
    if request.include_photos:
        emit(callback, ScanProgress("photos", "Scanning photos"))
        groups.extend(scan_photos(request, callback, cancel_event, warnings))
    if request.include_exact_videos:
        emit(callback, ScanProgress("videos_exact", "Scanning exact videos"))
        groups.extend(scan_exact_videos(request, callback, cancel_event))
    if request.include_near_videos:
        emit(callback, ScanProgress("videos_near", "Scanning near-duplicate videos"))
        groups.extend(scan_near_videos(request, callback, cancel_event))
    emit(callback, ScanProgress("complete", f"Found {len(groups)} duplicate groups", percent=100))
    return ScanResult(request, groups, warnings)


def relative_source_path(path: Path, source_root: Path) -> Path:
    try:
        return path.resolve().relative_to(source_root.resolve())
    except ValueError:
        drive = path.drive.rstrip(":\\/") or "external"
        return Path(drive, *path.resolve().parts[1:])


def unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    for number in range(1, 100_000):
        candidate = destination.with_name(f"{destination.stem}__duplicate_{number}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find an unused quarantine path for {destination}")


def new_manifest(source_root: Path, quarantine_root: Path) -> RunManifest:
    return RunManifest(
        version=MANIFEST_VERSION,
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
        created_at=datetime.now(timezone.utc).isoformat(),
        source_root=str(source_root.resolve()),
        quarantine_root=str(quarantine_root.resolve()),
    )


def quarantine_groups(
    source_root: Path,
    quarantine_root: Path,
    groups: Sequence[DuplicateGroup],
    selected_paths: dict[str, set[str]] | None = None,
    callback: ProgressCallback | None = None,
    move_file: Callable[[str, str], object] = shutil.move,
) -> RunManifest:
    manifest = new_manifest(source_root, quarantine_root)
    candidates: list[tuple[DuplicateGroup, MediaItem]] = []
    for group in groups:
        allowed = selected_paths.get(group.id) if selected_paths is not None else None
        for item in group.duplicates:
            if allowed is None or str(item.path) in allowed:
                candidates.append((group, item))
    manifest.save()
    moved: set[str] = set()
    last_save = time.monotonic()
    total = len(candidates)
    for current, (group, item) in enumerate(candidates, start=1):
        entry = ManifestEntry(
            id=uuid.uuid4().hex,
            group_id=group.id,
            lane=group.lane,
            keeper_path=str(group.keeper_path),
            original_path=str(item.path),
            moved_path="",
            metadata=item.to_dict(),
        )
        manifest.entries.append(entry)
        # Never quarantine the last copy: the kept original must still be in
        # place. It can be gone when an overlapping group (exact and near
        # video lanes) already moved it, or when the scan is stale.
        if path_key(group.keeper_path) in moved or not group.keeper_path.exists():
            entry.status = "skipped"
            entry.error = f"Kept original is no longer at {group.keeper_path}"
        elif not item.path.exists():
            entry.status = "skipped"
            entry.error = f"File is no longer at {item.path}"
        if entry.status == "skipped":
            manifest.record(entry)
            emit(callback, ScanProgress("quarantine", f"Skipped {item.path.name}: {entry.error}", current, total, percent(current, total), warning=True))
            continue

        relative = relative_source_path(item.path, source_root)
        destination = unique_destination(quarantine_root / LANE_FOLDERS[group.lane] / relative)
        entry.moved_path = str(destination)
        manifest.record(entry)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            move_file(str(item.path), str(destination))
            entry.status = "moved"
            moved.add(path_key(item.path))
            emit(callback, ScanProgress("quarantine", f"Moved {item.path.name}", current, total, percent(current, total)))
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)
            manifest.errors.append(f"Could not move {item.path}: {exc}")
            emit(callback, ScanProgress("quarantine", manifest.errors[-1], current, total, percent(current, total), warning=True))
        manifest.record(entry)
        if current % MANIFEST_SAVE_INTERVAL == 0 or time.monotonic() - last_save > 30:
            manifest.save()
            last_save = time.monotonic()
    manifest.save()
    return manifest


RESTORABLE_STATUSES = ("moved", "restore-blocked", "restore-error")


def restore_entries(
    manifest: RunManifest,
    entry_ids: set[str],
    callback: ProgressCallback | None = None,
    move_file: Callable[[str, str], object] = shutil.move,
) -> RunManifest:
    entries = [entry for entry in manifest.entries if entry.id in entry_ids and entry.status in RESTORABLE_STATUSES]
    total = len(entries)
    for current, entry in enumerate(entries, start=1):
        source = Path(entry.moved_path)
        destination = Path(entry.original_path)
        if destination.exists():
            entry.status = "restore-blocked"
            entry.error = f"Original path is occupied: {destination}"
            emit(callback, ScanProgress("restore", entry.error, current, total, percent(current, total), warning=True))
        elif not source.exists():
            entry.status = "missing"
            entry.error = f"Quarantined file is missing: {source}"
            emit(callback, ScanProgress("restore", entry.error, current, total, percent(current, total), warning=True))
        else:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                move_file(str(source), str(destination))
                entry.status = "restored"
                entry.error = None
                emit(callback, ScanProgress("restore", f"Restored {destination.name}", current, total, percent(current, total)))
            except Exception as exc:
                # The file is still in quarantine, so keep it restorable.
                entry.status = "restore-error"
                entry.error = str(exc)
                manifest.errors.append(f"Could not restore {source}: {exc}")
                emit(callback, ScanProgress("restore", manifest.errors[-1], current, total, percent(current, total), warning=True))
        manifest.record(entry)
        if current % MANIFEST_SAVE_INTERVAL == 0:
            manifest.save()
    manifest.save()
    return manifest


def thumbnail_path(item: MediaItem, size: tuple[int, int] = (280, 180)) -> Path | None:
    thumbnail_dir = local_app_data_dir() / "DuplicateMediaSorter" / "thumbnails"
    digest = hashlib.sha256(f"{item.path.resolve()}\0{item.modified_ns}\0{size}".encode("utf-8")).hexdigest()
    output = thumbnail_dir / f"{digest}.jpg"
    if output.exists():
        return output
    register_heif_support()
    try:
        from PIL import Image, ImageOps

        thumbnail_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(item.path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(size)
            canvas = Image.new("RGB", size, "#eef2f6")
            position = ((size[0] - image.width) // 2, (size[1] - image.height) // 2)
            canvas.paste(image, position)
            canvas.save(output, "JPEG", quality=86)
        return output
    except Exception:
        return None


def percent(current: int, total: int) -> int | None:
    return round(current * 100 / total) if total else None
