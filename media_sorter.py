#!/usr/bin/env python3
"""Shared services for the Duplicate Media Sorter desktop app.

The existing command-line tools remain useful on their own.  This module adds
structured scan results, review-friendly keeper suggestions, and reversible
quarantine manifests for the desktop interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from video_exact_dedup import VideoScanCancelled, duplicate_groups, scan_videos


MANIFEST_VERSION = 1
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
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "DuplicateGroup":
        return cls(
            id=str(raw["id"]),
            lane=str(raw["lane"]),
            keeper_path=Path(str(raw["keeper_path"])),
            items=[MediaItem.from_dict(item) for item in raw["items"]],  # type: ignore[arg-type]
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

    def save(self) -> Path:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    @classmethod
    def load(cls, path: Path) -> "RunManifest":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version: {raw.get('version')}")
        raw["entries"] = [ManifestEntry(**entry) for entry in raw.get("entries", [])]
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


def paths_equal(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


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
    return quarantine_root / ".duplicate-media-sorter" / "runs" / f"{run_id}.json"


def list_run_manifests(quarantine_root: Path) -> list[Path]:
    runs = quarantine_root / ".duplicate-media-sorter" / "runs"
    if not runs.exists():
        return []
    return sorted(runs.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)


def automatic_exclude_names(source_root: Path, quarantine_root: Path) -> tuple[str, ...]:
    names = list(SYSTEM_EXCLUDE_DIRS)
    try:
        quarantine_root.resolve().relative_to(source_root.resolve())
    except ValueError:
        pass
    else:
        names.append(quarantine_root.name)
    return tuple(dict.fromkeys(names))


def photo_exclude_patterns(source_root: Path, quarantine_root: Path) -> tuple[str, ...]:
    return tuple(f"^{re.escape(name)}$" for name in automatic_exclude_names(source_root, quarantine_root))


def group_id(lane: str, paths: Iterable[Path]) -> str:
    normalized = sorted(os.path.normcase(str(path.resolve())) for path in paths)
    digest = hashlib.sha256((lane + "\0" + "\0".join(normalized)).encode("utf-8")).hexdigest()[:16]
    return f"{lane}-{digest}"


def media_item_for_file(path: Path, lane: str) -> MediaItem:
    stat = path.stat()
    return MediaItem(path=path, lane=lane, size=stat.st_size, modified_ns=stat.st_mtime_ns)


def photo_item(path: Path) -> MediaItem:
    item = media_item_for_file(path, PHOTO_LANE)
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass

    try:
        from PIL import Image

        with Image.open(path) as image:
            item.width, item.height = image.size
    except Exception as exc:  # Pillow deliberately reports several error types.
        item.metadata_error = str(exc)
    return item


def photo_keeper(items: Sequence[MediaItem]) -> MediaItem:
    return min(
        items,
        key=lambda item: (
            -item.pixel_area,
            -item.size,
            item.modified_ns,
            os.path.normcase(str(item.path)),
        ),
    )


def exact_video_keeper(items: Sequence[MediaItem]) -> MediaItem:
    return min(items, key=lambda item: (item.modified_ns, os.path.normcase(str(item.path))))


def near_video_keeper(items: Sequence[MediaItem]) -> MediaItem:
    return min(
        items,
        key=lambda item: (
            -item.pixel_area,
            -(item.bitrate_kbps or 0),
            -item.size,
            os.path.normcase(str(item.path)),
        ),
    )


def capture_command(output_path: Path, capture_script: Path | None = None) -> str:
    script = capture_script or Path(__file__).with_name("capture_group.py")
    base = subprocess.list2cmdline([sys.executable, str(script), "--output", str(output_path)])
    return f"{base} {{*}}"


def build_photo_scan_command(
    request: ScanRequest,
    group_output: Path,
    cache_path: Path,
    capture_script: Path | None = None,
) -> list[str]:
    command = [
        request.find_dups_executable,
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
    command.extend(["--on-equal", "exec", "--exec", capture_command(group_output, capture_script)])
    return command


def find_find_dups_executable(configured: str) -> str | None:
    discovered = shutil.which(configured)
    if discovered:
        return discovered
    configured_path = Path(configured)
    if configured_path.exists():
        return str(configured_path.resolve())
    sibling_name = "find-dups.exe" if os.name == "nt" else "find-dups"
    sibling = Path(sys.executable).with_name(sibling_name)
    return str(sibling) if sibling.exists() else None


def stream_process(
    command: Sequence[str],
    callback: ProgressCallback | None,
    cancel_event: threading.Event | None,
    stage: str,
    env: dict[str, str] | None = None,
) -> int:
    emit(callback, ScanProgress(stage, subprocess.list2cmdline(list(command))))
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=env,
    )
    output_queue: queue.Queue[str | None] = queue.Queue()

    def collect_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output_queue.put(line.rstrip())
        output_queue.put(None)

    output_thread = threading.Thread(target=collect_output, daemon=True)
    output_thread.start()
    stream_closed = False
    while not stream_closed:
        if cancel_event is not None and cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            output_thread.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            raise ScanCancelled("scan cancelled")
        try:
            line = output_queue.get(timeout=0.1)
        except queue.Empty:
            if process.poll() is not None:
                continue
            continue
        if line is None:
            stream_closed = True
        elif line:
            emit(callback, ScanProgress(stage, line))
    return_code = process.wait()
    output_thread.join(timeout=1)
    if process.stdout is not None:
        process.stdout.close()
    return return_code


def read_jsonl_groups(path: Path) -> list[list[Path]]:
    if not path.exists():
        return []
    groups: list[list[Path]] = []
    seen: set[tuple[str, ...]] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, list) or len(raw) < 2:
            raise ValueError(f"invalid captured photo group on line {line_number}")
        paths = [Path(str(value)) for value in raw]
        key = tuple(sorted(os.path.normcase(str(value.resolve())) for value in paths))
        if key not in seen:
            groups.append(paths)
            seen.add(key)
    return groups


def scan_photos(
    request: ScanRequest,
    callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[DuplicateGroup]:
    executable = find_find_dups_executable(request.find_dups_executable)
    if executable is None:
        raise RuntimeError(
            f"Could not find '{request.find_dups_executable}'. "
            "Install dependencies with: python -m pip install -r requirements.txt"
        )

    workspace = library_workspace(request.source_root)
    workspace.mkdir(parents=True, exist_ok=True)
    group_output = workspace / "photo_groups.jsonl"
    group_output.unlink(missing_ok=True)
    cache_path = workspace / "photo_hashes.json"
    executable_request = ScanRequest(**{**request.__dict__, "find_dups_executable": executable})
    command = build_photo_scan_command(executable_request, group_output, cache_path)
    env = None
    from dedup_manager import ensure_heif_support, env_with_pythonpath, find_heic_files, write_heif_bootstrap

    if find_heic_files(request.source_root):
        ensure_heif_support(required=True)
        env = env_with_pythonpath(write_heif_bootstrap(workspace / ".dedup_runtime"))
    return_code = stream_process(command, callback, cancel_event, "photos", env)
    if return_code != 0:
        raise RuntimeError(f"find-dups exited with status {return_code}")

    captured = read_jsonl_groups(group_output)
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
        if len(items) > 1:
            keeper = photo_keeper(items)
            groups.append(DuplicateGroup(group_id(PHOTO_LANE, (item.path for item in items)), PHOTO_LANE, items, keeper.path))
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

    groups: list[DuplicateGroup] = []
    for records_group in duplicate_groups(records):
        items = [media_item_for_file(record.path, EXACT_VIDEO_LANE) for record in records_group]
        keeper = exact_video_keeper(items)
        groups.append(DuplicateGroup(group_id(EXACT_VIDEO_LANE, (item.path for item in items)), EXACT_VIDEO_LANE, items, keeper.path))
    return groups


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
        groups.extend(scan_photos(request, callback, cancel_event))
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
    total = len(candidates)
    for current, (group, item) in enumerate(candidates, start=1):
        relative = relative_source_path(item.path, source_root)
        destination = unique_destination(quarantine_root / LANE_FOLDERS[group.lane] / relative)
        entry = ManifestEntry(
            id=uuid.uuid4().hex,
            group_id=group.id,
            lane=group.lane,
            keeper_path=str(group.keeper_path),
            original_path=str(item.path),
            moved_path=str(destination),
            metadata=item.to_dict(),
        )
        manifest.entries.append(entry)
        manifest.save()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            move_file(str(item.path), str(destination))
            entry.status = "moved"
            emit(callback, ScanProgress("quarantine", f"Moved {item.path.name}", current, total, percent(current, total)))
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)
            manifest.errors.append(f"Could not move {item.path}: {exc}")
            emit(callback, ScanProgress("quarantine", manifest.errors[-1], current, total, percent(current, total), warning=True))
        manifest.save()
    return manifest


def restore_entries(
    manifest: RunManifest,
    entry_ids: set[str],
    callback: ProgressCallback | None = None,
    move_file: Callable[[str, str], object] = shutil.move,
) -> RunManifest:
    entries = [entry for entry in manifest.entries if entry.id in entry_ids and entry.status in ("moved", "restore-blocked")]
    total = len(entries)
    for current, entry in enumerate(entries, start=1):
        source = Path(entry.moved_path)
        destination = Path(entry.original_path)
        if destination.exists():
            entry.status = "restore-blocked"
            entry.error = f"Original path is occupied: {destination}"
            emit(callback, ScanProgress("restore", entry.error, current, total, percent(current, total), warning=True))
            manifest.save()
            continue
        if not source.exists():
            entry.status = "missing"
            entry.error = f"Quarantined file is missing: {source}"
            emit(callback, ScanProgress("restore", entry.error, current, total, percent(current, total), warning=True))
            manifest.save()
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            move_file(str(source), str(destination))
            entry.status = "restored"
            entry.error = None
            emit(callback, ScanProgress("restore", f"Restored {destination.name}", current, total, percent(current, total)))
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)
            manifest.errors.append(f"Could not restore {source}: {exc}")
            emit(callback, ScanProgress("restore", manifest.errors[-1], current, total, percent(current, total), warning=True))
        manifest.save()
    return manifest


def thumbnail_path(item: MediaItem, size: tuple[int, int] = (280, 180)) -> Path | None:
    workspace = library_workspace(item.path.parent)
    thumbnail_dir = workspace / "thumbnails"
    digest = hashlib.sha256(f"{item.path.resolve()}\0{item.modified_ns}\0{size}".encode("utf-8")).hexdigest()
    output = thumbnail_dir / f"{digest}.jpg"
    if output.exists():
        return output
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass
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
