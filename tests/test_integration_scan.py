import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from media_sorter import ScanRequest, quarantine_groups, restore_entries, scan_library


def find_dups_available() -> bool:
    executable = "find-dups.exe" if os.name == "nt" else "find-dups"
    return shutil.which("find-dups") is not None or Path(sys.executable).with_name(executable).exists()


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


if __name__ == "__main__":
    unittest.main()

