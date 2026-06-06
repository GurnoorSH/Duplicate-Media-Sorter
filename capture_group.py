#!/usr/bin/env python3
"""Append one duplicate-image group to a JSONL file.

This helper is called by duplicate_images through its ``--exec`` action.  Each
path arrives as its own argument, so spaces and apostrophes remain intact.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence


def decode_windows_shlex_args(paths: Sequence[str]) -> list[str]:
    """Undo POSIX quoting after duplicate_images passes it through cmd.exe.

    duplicate_images uses ``shlex.quote`` before ``shell=True``. On Windows,
    cmd.exe keeps single quotes as text and separates the enclosed path at
    spaces. Reassemble those pieces before writing the structured group.
    """

    decoded: list[str] = []
    pending: list[str] = []
    for value in paths:
        if pending:
            pending.append(value)
            if value.endswith("'"):
                decoded.append(_decode_windows_shlex_value(" ".join(pending)))
                pending = []
        elif value.startswith("'"):
            if len(value) > 1 and value.endswith("'"):
                decoded.append(_decode_windows_shlex_value(value))
            else:
                pending = [value]
        else:
            decoded.append(value)
    if pending:
        decoded.append(_decode_windows_shlex_value(" ".join(pending)))
    return decoded


def _decode_windows_shlex_value(value: str) -> str:
    if len(value) > 1 and value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    return value.replace("'''", "'")


def write_group(output: Path, paths: Sequence[str]) -> None:
    if os.name == "nt":
        paths = decode_windows_shlex_args(paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(list(paths), ensure_ascii=False) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("paths", nargs="+")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    write_group(args.output, args.paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
