#!/usr/bin/env python3
"""
Exact duplicate video finder/quarantine tool.

This handles only byte-for-byte duplicate video files. It intentionally does
not try to detect visually similar re-encodes; that is a separate near-duplicate
step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

if TYPE_CHECKING:
    from media_sorter import DuplicateGroup


VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}
DEFAULT_CACHE = Path("video_exact_hashes.json")
BUFFER_SIZE = 1024 * 1024


class VideoScanCancelled(RuntimeError):
    """Raised when a caller cancels a video scan."""


@dataclass(frozen=True)
class VideoRecord:
    path: Path
    size: int
    digest: str


def iter_video_files(root: Path, exclude_dirs: Sequence[str] = ()) -> Iterable[Path]:
    def on_walk_error(error: OSError) -> None:
        print(f"Warning: could not read directory: {error}", file=sys.stderr)

    excluded = {name.casefold() for name in exclude_dirs}
    for directory, dirs, files in os.walk(root, onerror=on_walk_error):
        dirs[:] = [name for name in dirs if name.casefold() not in excluded]
        directory_path = Path(directory)
        for filename in files:
            candidate = directory_path / filename
            if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                yield candidate


def file_fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def load_cache(cache_path: Path) -> dict[str, dict[str, object]]:
    if not cache_path.exists():
        return {}
    with cache_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        return {}
    return raw


def save_cache(cache_path: Path, cache: dict[str, dict[str, object]]) -> None:
    if cache_path.parent != Path("."):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2, sort_keys=True)


def hash_file(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(BUFFER_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def scan_videos(
    root: Path,
    cache_path: Path,
    algorithm: str,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    exclude_dirs: Sequence[str] = (),
) -> list[VideoRecord]:
    cache = load_cache(cache_path)
    next_cache: dict[str, dict[str, object]] = {}
    records: list[VideoRecord] = []
    video_files = list(iter_video_files(root, exclude_dirs))
    print(f"Found {len(video_files)} video files under {root}")

    for index, path in enumerate(video_files, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise VideoScanCancelled("video scan cancelled")
        try:
            size, mtime_ns = file_fingerprint(path)
            key = str(path.resolve())
            cached = cache.get(key)
            digest: str
            if (
                cached
                and cached.get("size") == size
                and cached.get("mtime_ns") == mtime_ns
                and cached.get("algorithm") == algorithm
            ):
                digest = str(cached["digest"])
            else:
                digest = hash_file(path, algorithm)

            next_cache[key] = {
                "algorithm": algorithm,
                "digest": digest,
                "mtime_ns": mtime_ns,
                "size": size,
            }
            records.append(VideoRecord(path=path, size=size, digest=digest))
        except OSError as exc:
            print(f"Warning: could not hash {path}: {exc}", file=sys.stderr)

        if index % 100 == 0 or index == len(video_files):
            print(f"Hashed {index}/{len(video_files)} videos", flush=True)
        if progress_callback is not None:
            progress_callback(index, len(video_files), f"Hashed {index}/{len(video_files)} videos")

    save_cache(cache_path, next_cache)
    return records


def duplicate_groups(records: Sequence[VideoRecord]) -> list[list[VideoRecord]]:
    buckets: dict[tuple[int, str], list[VideoRecord]] = {}
    for record in records:
        buckets.setdefault((record.size, record.digest), []).append(record)
    return [sorted(group, key=lambda item: str(item.path).lower()) for group in buckets.values() if len(group) > 1]


def write_report(report_path: Path, groups: Sequence["DuplicateGroup"], empty: Sequence["DuplicateGroup"] = ()) -> None:
    if report_path.parent != Path("."):
        report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        handle.write(f"Exact duplicate video groups: {len(groups)}\n")
        for index, group in enumerate(groups, start=1):
            size = group.items[0].size
            handle.write(
                f"\nGroup {index}: {len(group.items)} files, size={size}, "
                f"reclaimable={group.reclaimable_bytes}, total={size * len(group.items)}\n"
            )
            handle.write(f"KEEP {group.keeper_path}\n")
            for item in group.duplicates:
                handle.write(f"MOVE {item.path}\n")
        handle.write(f"\nEmpty or zero-filled videos (no data, every byte is zero): {len(empty)}\n")
        for group in empty:
            handle.write(f"MOVE {group.items[0].path}  size={group.items[0].size}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find and quarantine byte-for-byte duplicate videos.")
    parser.add_argument("target", type=Path)
    parser.add_argument("--mode", choices=("dry-run", "quarantine"), default="dry-run")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=Path("video_exact_report.txt"))
    parser.add_argument("--quarantine-dir", type=Path, default=None)
    # SHAKE digests need an explicit length for hexdigest(), so they are excluded.
    algorithms = sorted(name for name in hashlib.algorithms_guaranteed if not name.startswith("shake_"))
    parser.add_argument("--algorithm", choices=algorithms, default="sha256")
    parser.add_argument("--dry-run-moves", action="store_true", help="Print quarantine moves without moving files.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    # media_sorter imports this module, so import it lazily to avoid a cycle.
    from media_sorter import drop_groups_with, exact_video_groups, find_empty_media, photo_exclude_patterns, quarantine_groups

    args = parse_args(argv)
    target = args.target.expanduser().resolve()
    if not target.exists():
        print(f"Error: target does not exist: {target}", file=sys.stderr)
        return 1

    quarantine_dir = (args.quarantine_dir or target.parent / f"{target.name}_video_quarantine_exact").expanduser().resolve()
    exclude = (quarantine_dir.name,) if quarantine_dir.is_relative_to(target) else ()
    records = scan_videos(target, args.cache, args.algorithm, exclude_dirs=exclude)
    # Empty or zero-filled videos hold no data: report them on their own and
    # never keep one as the original of an exact group.
    empty = find_empty_media(target, VIDEO_EXTENSIONS, photo_exclude_patterns(target, quarantine_dir))
    groups = drop_groups_with(exact_video_groups(records), (group.items[0].path for group in empty))
    write_report(args.report, groups, empty)
    print(f"Exact duplicate groups: {len(groups)}")
    print(f"Empty or zero-filled videos: {len(empty)}")
    print(f"Report: {args.report}")

    if args.mode == "quarantine":
        if args.dry_run_moves:
            count = sum(len(group.duplicates) for group in [*groups, *empty])
            print(f"Would move {count} video files to {quarantine_dir} (see MOVE lines in the report)")
            return 0
        manifest = quarantine_groups(target, quarantine_dir, [*groups, *empty])
        statuses = [entry.status for entry in manifest.entries]
        print(
            f"Moved {statuses.count('moved')} video files to {quarantine_dir}, "
            f"skipped {statuses.count('skipped')}, failed {statuses.count('error')}. Restore manifest: {manifest.path}"
        )
        return 1 if statuses.count("error") else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
