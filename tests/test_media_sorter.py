import os
import re
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from media_sorter import (
    EXACT_VIDEO_LANE,
    NEAR_VIDEO_LANE,
    PHOTO_LANE,
    DigestCache,
    DuplicateGroup,
    MediaItem,
    RunManifest,
    ScanRequest,
    automatic_exclude_names,
    build_photo_groups,
    build_photo_scan_command,
    exact_video_keeper,
    find_dups_command_prefix,
    library_workspace,
    normalize_vdf_report,
    parse_quoted_groups,
    photo_exclude_patterns,
    photo_keeper,
    quarantine_groups,
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

    def test_identical_photo_keeper_avoids_copy_names(self) -> None:
        # " - Copy" sorts before "." so a plain path tie-break kept the copy.
        items = [
            item(Path("IMG_2311 - Copy.heic"), PHOTO_LANE, 1_000, 5, 200, 200),
            item(Path("IMG_2311.heic"), PHOTO_LANE, 1_000, 5, 200, 200),
            item(Path("IMG_2311 (1).heic"), PHOTO_LANE, 1_000, 5, 200, 200),
        ]

        self.assertEqual(photo_keeper(items).path, Path("IMG_2311.heic"))

    def test_exact_video_keeper_avoids_copy_names_before_age(self) -> None:
        items = [
            item(Path("clip - Copy.mp4"), EXACT_VIDEO_LANE, 10, 1),
            item(Path("clip.mp4"), EXACT_VIDEO_LANE, 10, 2),
        ]

        self.assertEqual(exact_video_keeper(items).path, Path("clip.mp4"))


class FindDupsOutputTests(unittest.TestCase):
    def test_quoted_groups_round_trip_shell_special_names(self) -> None:
        groups = [
            [r"C:\Mom & Dad's 100% pics\a^b & c.jpg", r"C:\Kids' Photos\ਪੰਜਾਬੀ photo.jpg"],
            [r"C:\Phone Pics\one photo.jpg", r"C:\Phone Pics\two.jpg", r"C:\x\(1).jpg"],
        ]
        output = [" ".join(shlex.quote(path) for path in group) for group in groups]

        self.assertEqual(parse_quoted_groups(output), [[Path(path) for path in group] for group in groups])

    def test_quoted_group_parser_skips_noise_and_duplicates(self) -> None:
        line = "'C:\\a.jpg' 'C:\\b.jpg'"

        self.assertEqual(parse_quoted_groups(["", line, line, "'unbalanced", "'C:\\single.jpg'"]), [[Path(r"C:\a.jpg"), Path(r"C:\b.jpg")]])

    def test_photo_command_reports_groups_on_stdout_without_a_shell(self) -> None:
        request = ScanRequest(Path(r"C:\Photos"), quarantine_root=Path(r"C:\Photos\quarantine"))

        command = build_photo_scan_command(request, Path("hashes.json"), ["python", "-m", "duplicate_images.duplicate"])

        self.assertEqual(command[:3], ["python", "-m", "duplicate_images.duplicate"])
        self.assertEqual(command[-2:], ["--on-equal", "quote"])
        self.assertNotIn("--exec", command)

    def test_find_dups_runs_as_module_of_current_interpreter(self) -> None:
        # A venv's find-dups.exe launcher breaks once the project folder moves.
        self.assertEqual(find_dups_command_prefix(), [sys.executable, "-m", "duplicate_images.duplicate"])


class ExcludePatternTests(unittest.TestCase):
    """find-dups applies re.search() to each file's full parent path."""

    def excluded(self, patterns: tuple[str, ...], parent: Path) -> bool:
        return any(re.search(pattern, str(parent)) for pattern in patterns)

    def test_internal_quarantine_and_system_folders_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve() / "library"
            quarantine = source / "library_quarantine"
            patterns = photo_exclude_patterns(source, quarantine)

            self.assertTrue(self.excluded(patterns, quarantine / "photos" / "album"))
            self.assertTrue(self.excluded(patterns, source / "$RECYCLE.BIN" / "S-1-5"))
            self.assertTrue(self.excluded(patterns, source / "System Volume Information"))
            self.assertFalse(self.excluded(patterns, source / "album"))
            self.assertFalse(self.excluded(patterns, source / "album" / "library_quarantine_notes"))

    def test_external_quarantine_is_not_a_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve() / "library"

            patterns = photo_exclude_patterns(source, source.parent / "library_quarantine")

            self.assertEqual(len(patterns), 2)


class PhotoGroupTests(unittest.TestCase):
    def test_matches_split_into_identical_and_similar_groups(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = Image.new("RGB", (64, 64), "#3567e8")
            original = root / "IMG_1.jpg"
            copy = root / "IMG_1 - Copy.jpg"
            resent = root / "WhatsApp Image.jpg"
            image.save(original)
            copy.write_bytes(original.read_bytes())
            image.resize((32, 32)).save(resent)

            groups = build_photo_groups([[copy, original, resent]], DigestCache(root / "digests.json"))

            exact = [group for group in groups if group.exact]
            similar = [group for group in groups if not group.exact]
            self.assertEqual(len(exact), 1)
            self.assertEqual(exact[0].keeper_path, original)
            self.assertEqual([entry.path for entry in exact[0].duplicates], [copy])
            self.assertEqual(len(similar), 1)
            self.assertEqual({entry.path for entry in similar[0].items}, {original, resent})
            self.assertEqual(similar[0].keeper_path, original)
            self.assertNotEqual(exact[0].id, similar[0].id)

    def test_similar_only_match_produces_no_exact_group(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = Image.new("RGB", (64, 64), "#3567e8")
            first, second = root / "0R6A8174.JPG", root / "0R6A8175.JPG"
            image.save(first, quality=95)
            image.save(second, quality=60)

            groups = build_photo_groups([[first, second]], DigestCache(root / "digests.json"))

            self.assertEqual([group.exact for group in groups], [False])

    def test_digest_cache_reuses_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "a.jpg"
            path.write_bytes(b"bytes")
            cache = DigestCache(root / "digests.json")
            first = cache.digest(path)
            cache.save()

            with patch("media_sorter.hash_file", side_effect=AssertionError("re-hashed")):
                self.assertEqual(DigestCache(root / "digests.json").digest(path), first)


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

    def test_overlapping_groups_never_quarantine_every_copy(self) -> None:
        # Exact and near video lanes can pick opposite keepers for the same pair.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "library"
            source.mkdir()
            first = source / "a.mp4"
            second = source / "b.mp4"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            items = [item(first, EXACT_VIDEO_LANE, 4, 1), item(second, EXACT_VIDEO_LANE, 4, 2)]
            exact = DuplicateGroup("exact", EXACT_VIDEO_LANE, items, first, exact=True)
            near = DuplicateGroup("near", NEAR_VIDEO_LANE, list(items), second)

            manifest = quarantine_groups(source, root / "quarantine", [exact, near])

            self.assertEqual([entry.status for entry in manifest.entries], ["moved", "skipped"])
            self.assertTrue(first.exists())

    def test_missing_keeper_skips_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "library"
            source.mkdir()
            duplicate = source / "copy.jpg"
            duplicate.write_text("copy", encoding="utf-8")
            group = DuplicateGroup("photos-test", PHOTO_LANE, [item(source / "gone.jpg", PHOTO_LANE, 4, 1), item(duplicate, PHOTO_LANE, 4, 2)], source / "gone.jpg")

            manifest = quarantine_groups(source, root / "quarantine", [group])

            self.assertEqual(manifest.entries[0].status, "skipped")
            self.assertTrue(duplicate.exists())

    def test_interrupted_run_is_recoverable_from_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "library"
            source.mkdir()
            kept = source / "kept.jpg"
            kept.write_text("original", encoding="utf-8")
            duplicates = []
            for index in range(3):
                duplicate = source / f"copy{index}.jpg"
                duplicate.write_text("copy", encoding="utf-8")
                duplicates.append(item(duplicate, PHOTO_LANE, 4, 2))
            group = DuplicateGroup("photos-test", PHOTO_LANE, [item(kept, PHOTO_LANE, 8, 1), *duplicates], kept)
            moves = []

            def crash_on_third(source_path: str, destination: str) -> None:
                if len(moves) == 2:
                    raise KeyboardInterrupt
                moves.append(destination)
                os.replace(source_path, destination)

            with self.assertRaises(KeyboardInterrupt):
                quarantine_groups(source, root / "quarantine", [group], move_file=crash_on_third)

            manifest_file = next((root / "quarantine" / ".duplicate-media-sorter" / "runs").glob("*.json"))
            recovered = RunManifest.load(manifest_file)
            self.assertEqual([entry.status for entry in recovered.entries], ["moved", "moved", "pending"])
            restore_entries(recovered, {entry.id for entry in recovered.entries})
            self.assertTrue(all((source / f"copy{index}.jpg").exists() for index in range(3)))
            self.assertEqual(RunManifest.load(manifest_file).entries[0].status, "restored")


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
