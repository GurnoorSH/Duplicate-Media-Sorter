import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from capture_group import decode_windows_shlex_args, write_group
from media_sorter import (
    EXACT_VIDEO_LANE,
    NEAR_VIDEO_LANE,
    PHOTO_LANE,
    DuplicateGroup,
    MediaItem,
    RunManifest,
    ScanRequest,
    automatic_exclude_names,
    build_photo_scan_command,
    capture_command,
    exact_video_keeper,
    library_workspace,
    normalize_vdf_report,
    photo_keeper,
    quarantine_groups,
    read_jsonl_groups,
    restore_entries,
    unique_destination,
)
from video_exact_dedup import VideoScanCancelled, duplicate_groups, scan_videos


def item(path: Path, lane: str, size: int, modified_ns: int, width: int | None = None, height: int | None = None) -> MediaItem:
    return MediaItem(path, lane, size, modified_ns, width, height)


class KeeperSelectionTests(unittest.TestCase):
    def test_photo_keeper_prefers_resolution_then_size_then_oldest(self) -> None:
        items = [
            item(Path("small.jpg"), PHOTO_LANE, 900, 1, 100, 100),
            item(Path("new.jpg"), PHOTO_LANE, 1_000, 3, 200, 200),
            item(Path("old.jpg"), PHOTO_LANE, 1_000, 2, 200, 200),
        ]

        self.assertEqual(photo_keeper(items).path, Path("old.jpg"))

    def test_exact_video_keeper_prefers_oldest(self) -> None:
        items = [
            item(Path("new.mp4"), EXACT_VIDEO_LANE, 10, 20),
            item(Path("old.mp4"), EXACT_VIDEO_LANE, 10, 10),
        ]

        self.assertEqual(exact_video_keeper(items).path, Path("old.mp4"))


class CaptureTests(unittest.TestCase):
    def test_capture_helper_preserves_spaces_and_apostrophes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "groups.jsonl"
            paths = [r"C:\Phone Pics\one photo.jpg", r"C:\Phone Pics\Mom's copy.jpg"]

            write_group(output, paths)

            self.assertEqual(read_jsonl_groups(output), [[Path(value) for value in paths]])

    def test_windows_capture_decoder_repairs_upstream_posix_quoting(self) -> None:
        split_paths = [
            r"'C:\Phone",
            r"Pics\Mom'''s",
            r"copy.jpg'",
            r"'C:\Phone",
            r"Pics\original",
            r"photo.jpg'",
        ]

        self.assertEqual(
            decode_windows_shlex_args(split_paths),
            [r"C:\Phone Pics\Mom's copy.jpg", r"C:\Phone Pics\original photo.jpg"],
        )

    def test_capture_command_uses_group_wildcard(self) -> None:
        command = capture_command(Path(r"C:\Temp Folder\groups.jsonl"), Path(r"C:\App Folder\capture_group.py"))

        self.assertIn("capture_group.py", command)
        self.assertTrue(command.endswith("{*}"))

    def test_photo_command_includes_structured_exec_capture_and_exclusions(self) -> None:
        request = ScanRequest(Path(r"C:\Photos"), quarantine_root=Path(r"C:\Photos\quarantine"))

        command = build_photo_scan_command(request, Path("groups.jsonl"), Path("hashes.json"))

        self.assertIn("--on-equal", command)
        self.assertIn("exec", command)
        self.assertIn("--exec", command)
        self.assertIn(r"^\$RECYCLE\.BIN$", command)
        self.assertIn(r"^System\ Volume\ Information$", command)
        self.assertIn(r"^quarantine$", command)


class WorkspaceTests(unittest.TestCase):
    def test_library_workspace_uses_local_app_data_and_stable_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            first = library_workspace(Path("."))
            second = library_workspace(Path("."))

            self.assertEqual(first, second)
            self.assertEqual(first.parent, Path(temporary) / "DuplicateMediaSorter" / "libraries")

    def test_exclusions_only_add_quarantine_when_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "library"
            source.mkdir()

            internal = automatic_exclude_names(source, source / "quarantine")
            external = automatic_exclude_names(source, source.parent / "quarantine")

            self.assertIn("quarantine", internal)
            self.assertNotIn("quarantine", external)
            self.assertIn("$RECYCLE.BIN", internal)


class QuarantineTests(unittest.TestCase):
    def test_unique_destination_adds_suffix_for_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "photo.jpg"
            destination.write_text("occupied", encoding="utf-8")

            self.assertEqual(unique_destination(destination).name, "photo__duplicate_1.jpg")

    def test_quarantine_manifest_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "library"
            quarantine = root / "quarantine"
            kept = source / "album" / "kept.jpg"
            duplicate = source / "album" / "copy.jpg"
            duplicate.parent.mkdir(parents=True)
            kept.write_text("original", encoding="utf-8")
            duplicate.write_text("copy", encoding="utf-8")
            group = DuplicateGroup(
                "photos-test",
                PHOTO_LANE,
                [item(kept, PHOTO_LANE, 8, 1), item(duplicate, PHOTO_LANE, 4, 2)],
                kept,
            )

            manifest = quarantine_groups(source, quarantine, [group])

            self.assertFalse(duplicate.exists())
            self.assertTrue(manifest.path.exists())
            saved = RunManifest.load(manifest.path)
            self.assertEqual(saved.entries[0].status, "moved")
            self.assertTrue(Path(saved.entries[0].moved_path).exists())

            restore_entries(saved, {saved.entries[0].id})

            self.assertTrue(duplicate.exists())
            self.assertEqual(saved.entries[0].status, "restored")

    def test_restore_blocks_when_original_path_is_occupied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "library"
            quarantine = root / "quarantine"
            kept = source / "kept.jpg"
            duplicate = source / "copy.jpg"
            source.mkdir()
            kept.write_text("original", encoding="utf-8")
            duplicate.write_text("copy", encoding="utf-8")
            group = DuplicateGroup(
                "photos-test",
                PHOTO_LANE,
                [item(kept, PHOTO_LANE, 8, 1), item(duplicate, PHOTO_LANE, 4, 2)],
                kept,
            )
            manifest = quarantine_groups(source, quarantine, [group])
            duplicate.write_text("replacement", encoding="utf-8")

            restore_entries(manifest, {manifest.entries[0].id})

            self.assertEqual(manifest.entries[0].status, "restore-blocked")
            self.assertEqual(duplicate.read_text(encoding="utf-8"), "replacement")

    def test_manifest_records_move_error_for_interrupted_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "library"
            quarantine = root / "quarantine"
            kept = source / "kept.jpg"
            duplicate = source / "copy.jpg"
            source.mkdir()
            kept.write_text("original", encoding="utf-8")
            duplicate.write_text("copy", encoding="utf-8")
            group = DuplicateGroup(
                "photos-test",
                PHOTO_LANE,
                [item(kept, PHOTO_LANE, 8, 1), item(duplicate, PHOTO_LANE, 4, 2)],
                kept,
            )

            manifest = quarantine_groups(source, quarantine, [group], move_file=lambda _source, _destination: (_ for _ in ()).throw(OSError("stopped")))

            self.assertEqual(manifest.entries[0].status, "error")
            self.assertIn("stopped", RunManifest.load(manifest.path).entries[0].error)


class VideoTests(unittest.TestCase):
    def test_exact_video_scan_groups_copied_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.mp4"
            second = root / "nested" / "second.mp4"
            second.parent.mkdir()
            first.write_bytes(b"same video bytes")
            second.write_bytes(b"same video bytes")

            groups = duplicate_groups(scan_videos(root, root / "cache.json", "sha256"))

            self.assertEqual(len(groups), 1)
            self.assertEqual({record.path for record in groups[0]}, {first, second})

    def test_exact_video_scan_honors_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.mp4").write_bytes(b"video")
            cancelled = threading.Event()
            cancelled.set()

            with self.assertRaises(VideoScanCancelled):
                scan_videos(root, root / "cache.json", "sha256", cancel_event=cancelled)

    def test_vdf_normalization_uses_quality_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            small = root / "small.mp4"
            large = root / "large.mp4"
            small.write_bytes(b"x")
            large.write_bytes(b"xx")
            report = [
                {
                    "Files": [
                        {"Path": str(small), "FrameSize": "1280x720", "BitRateKbs": 900, "SizeLong": 1},
                        {"Path": str(large), "FrameSize": "1920x1080", "BitRateKbs": 1200, "SizeLong": 2},
                    ]
                }
            ]

            groups = normalize_vdf_report(report)

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].lane, NEAR_VIDEO_LANE)
            self.assertEqual(groups[0].keeper_path, large)


if __name__ == "__main__":
    unittest.main()
