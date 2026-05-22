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
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


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


@dataclass(frozen=True)
class VideoRecord:
    path: Path
    size: int
    digest: str


def iter_video_files(root: Path) -> Iterable[Path]:
    def on_walk_error(error: OSError) -> None:
        print(f"Warning: could not read directory: {error}", file=sys.stderr)

    for directory, _dirs, files in os.walk(root, onerror=on_walk_error):
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


def scan_videos(root: Path, cache_path: Path, algorithm: str) -> list[VideoRecord]:
    cache = load_cache(cache_path)
    next_cache: dict[str, dict[str, object]] = {}
    records: list[VideoRecord] = []
    video_files = list(iter_video_files(root))
    print(f"Found {len(video_files)} video files under {root}")

    for index, path in enumerate(video_files, start=1):
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

    save_cache(cache_path, next_cache)
    return records


def duplicate_groups(records: Sequence[VideoRecord]) -> list[list[VideoRecord]]:
    buckets: dict[tuple[int, str], list[VideoRecord]] = {}
    for record in records:
        buckets.setdefault((record.size, record.digest), []).append(record)
    return [sorted(group, key=lambda item: str(item.path).lower()) for group in buckets.values() if len(group) > 1]


def relative_quarantine_path(path: Path) -> Path:
    resolved = path.resolve()
    parts = list(resolved.parts)
    if parts and parts[0].endswith("\\"):
        parts = parts[1:]
    return Path(*parts)


def unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    parent = destination.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}__duplicate_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def write_report(report_path: Path, groups: Sequence[Sequence[VideoRecord]]) -> None:
    if report_path.parent != Path("."):
        report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        handle.write(f"Exact duplicate video groups: {len(groups)}\n")
        for index, group in enumerate(groups, start=1):
            total_size = sum(record.size for record in group)
            reclaimable = sum(record.size for record in group[1:])
            handle.write(f"\nGroup {index}: {len(group)} files, size={group[0].size}, reclaimable={reclaimable}, total={total_size}\n")
            handle.write(f"KEEP {group[0].path}\n")
            for record in group[1:]:
                handle.write(f"MOVE {record.path}\n")


def quarantine_groups(groups: Sequence[Sequence[VideoRecord]], quarantine_dir: Path, dry_run: bool) -> int:
    moved = 0
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    for group in groups:
        for record in group[1:]:
            destination = unique_destination(quarantine_dir / relative_quarantine_path(record.path))
            print(f"Move {record.path} -> {destination}")
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(record.path), str(destination))
            moved += 1
    return moved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find and quarantine byte-for-byte duplicate videos.")
    parser.add_argument("target", type=Path)
    parser.add_argument("--mode", choices=("dry-run", "quarantine"), default="dry-run")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=Path("video_exact_report.txt"))
    parser.add_argument("--quarantine-dir", type=Path, default=None)
    parser.add_argument("--algorithm", choices=hashlib.algorithms_available, default="sha256")
    parser.add_argument("--dry-run-moves", action="store_true", help="Print quarantine moves without moving files.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    target = args.target.expanduser()
    if not target.exists():
        print(f"Error: target does not exist: {target}", file=sys.stderr)
        return 1

    records = scan_videos(target, args.cache, args.algorithm)
    groups = duplicate_groups(records)
    write_report(args.report, groups)
    print(f"Exact duplicate groups: {len(groups)}")
    print(f"Report: {args.report}")

    if args.mode == "quarantine":
        quarantine_dir = args.quarantine_dir
        if quarantine_dir is None:
            quarantine_dir = target.parent / f"{target.name}_video_quarantine_exact"
        moved = quarantine_groups(groups, quarantine_dir, args.dry_run_moves)
        action = "Would move" if args.dry_run_moves else "Moved"
        print(f"{action} {moved} exact duplicate video files to {quarantine_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
