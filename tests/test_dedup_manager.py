from pathlib import Path
import unittest

from dedup_manager import DedupConfig, build_find_dups_command, default_quarantine_dir, parse_args


class BuildCommandTests(unittest.TestCase):
    def test_dry_run_builds_grouped_quote_command_with_cache(self) -> None:
        config = DedupConfig(
            target=Path(r"C:\Photos\Phone Exports"),
            cache=Path("hashes.json"),
            mode="dry-run",
        )

        command = build_find_dups_command(config)

        self.assertEqual(command[0], "find-dups")
        self.assertIn(str(Path(r"C:\Photos\Phone Exports")), command)
        self.assertIn("--parallel", command)
        self.assertIn("--progress", command)
        self.assertIn("--group", command)
        self.assertEqual(command[-2:], ["--on-equal", "quote"])
        self.assertIn("hashes.json", command)

    def test_every_mode_only_reports_groups_from_find_dups(self) -> None:
        # find-dups' move/delete actions touch one file per group and cannot
        # tell identical copies from similar shots, so the wrapper acts itself.
        for mode in ("dry-run", "quarantine", "delete"):
            config = DedupConfig(
                target=Path(r"C:\Photos\Library"),
                cache=Path("hashes.json"),
                mode=mode,
                quarantine_dir=Path(r"D:\Quarantine Folder"),
            )

            command = build_find_dups_command(config, ["python", "-m", "duplicate_images.duplicate"])

            self.assertEqual(command[:3], ["python", "-m", "duplicate_images.duplicate"])
            self.assertEqual(command[-2:], ["--on-equal", "quote"])
            self.assertNotIn("--move-to", command)

    def test_default_quarantine_is_sibling_of_target(self) -> None:
        target = Path(r"C:\Photos\Library")

        quarantine = default_quarantine_dir(target)

        self.assertEqual(quarantine.name, "Library_quarantine")

    def test_similar_groups_need_explicit_opt_in(self) -> None:
        target = str(Path(__file__).parent)

        self.assertFalse(parse_args([target]).include_similar)
        self.assertTrue(parse_args([target, "--include-similar"]).include_similar)


if __name__ == "__main__":
    unittest.main()
