import os
import tempfile
import unittest
from pathlib import Path

from media_sorter import ScanRequest, find_dups_command_prefix, quarantine_groups, restore_entries, scan_library


def find_dups_available() -> bool:
    return find_dups_command_prefix() is not None


@unittest.skipUnless(find_dups_available(), "find-dups is not installed")
class MixedMediaIntegrationTests(unittest.TestCase):
    def test_scan_quarantine_and_restore_photo_and_exact_video_duplicates(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            os.environ["LOCALAPPDATA"] = str(root / "appdata")
            source = root / "Phone Pics"
            source.mkdir()
            image = Image.new("RGB", (64, 64), "#3567e8")
            image.save(source / "original photo.jpg")
            image.save(source / "Mom's copy.jpg")
            (source / "original video.mp4").write_bytes(b"example video bytes")
            (source / "copy video.mp4").write_bytes(b"example video bytes")

            result = scan_library(ScanRequest(source))

            self.assertEqual(sorted((group.lane, len(group.items)) for group in result.groups), [("photos", 2), ("videos_exact", 2)])

            manifest = quarantine_groups(source, result.request.effective_quarantine_root, result.groups)

            self.assertEqual([entry.status for entry in manifest.entries], ["moved", "moved"])

            restore_entries(manifest, {entry.id for entry in manifest.entries})

            self.assertEqual([entry.status for entry in manifest.entries], ["restored", "restored"])


@unittest.skipUnless(find_dups_available(), "find-dups is not installed")
class CommandLineQuarantineTests(unittest.TestCase):
    def test_photo_cli_quarantines_every_identical_copy_and_leaves_similar(self) -> None:
        from PIL import Image

        import dedup_manager
        from media_sorter import RunManifest

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "Pics"
            source.mkdir()
            image = Image.new("RGB", (64, 64), "#3567e8")
            image.save(source / "IMG_1.jpg")
            for name in ("IMG_1 - Copy.jpg", "IMG_1 (1).jpg"):
                (source / name).write_bytes((source / "IMG_1.jpg").read_bytes())
            image.resize((32, 32)).save(source / "resized.jpg")

            code = dedup_manager.main([str(source), "--mode", "quarantine", "--cache", str(root / "hashes.json"), "--no-progress"])

            self.assertEqual(code, 0)
            self.assertEqual(sorted(path.name for path in source.iterdir()), ["IMG_1.jpg", "resized.jpg"])
            manifest = RunManifest.load(next((root / "Pics_quarantine" / ".duplicate-media-sorter" / "runs").glob("*.json")))
            self.assertEqual([entry.status for entry in manifest.entries], ["moved", "moved"])

    def test_video_cli_writes_restorable_manifest(self) -> None:
        import video_exact_dedup
        from media_sorter import RunManifest, restore_entries

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "Pics"
            (source / "nested").mkdir(parents=True)
            (source / "clip.mp4").write_bytes(b"video bytes")
            (source / "nested" / "clip - Copy.mp4").write_bytes(b"video bytes")

            code = video_exact_dedup.main(
                [str(source), "--mode", "quarantine", "--cache", str(root / "cache.json"), "--report", str(root / "report.txt")]
            )

            self.assertEqual(code, 0)
            self.assertIn("KEEP " + str(source / "clip.mp4"), (root / "report.txt").read_text(encoding="utf-8"))
            quarantine = root / "Pics_video_quarantine_exact"
            moved = quarantine / "videos" / "exact" / "nested" / "clip - Copy.mp4"
            self.assertTrue(moved.exists())
            manifest = RunManifest.load(next((quarantine / ".duplicate-media-sorter" / "runs").glob("*.json")))
            restore_entries(manifest, {entry.id for entry in manifest.entries})
            self.assertTrue((source / "nested" / "clip - Copy.mp4").exists())


if __name__ == "__main__":
    unittest.main()

