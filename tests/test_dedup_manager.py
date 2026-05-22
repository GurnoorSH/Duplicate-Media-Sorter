from pathlib import Path
import unittest

from dedup_manager import DedupConfig, build_find_dups_command, default_quarantine_dir


class BuildCommandTests(unittest.TestCase):
    def test_dry_run_builds_grouped_print_command_with_cache(self) -> None:
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
        self.assertEqual(command[-2:], ["--on-equal", "print"])
        self.assertIn("hashes.json", command)

    def test_quarantine_moves_smallest_and_recreates_paths(self) -> None:
        config = DedupConfig(
            target=Path(r"C:\Photos\Library"),
            cache=Path("hashes.json"),
            mode="quarantine",
            quarantine_dir=Path(r"D:\Quarantine Folder"),
        )

        command = build_find_dups_command(config)

        self.assertIn("--on-equal", command)
        self.assertIn("move-smallest", command)
        self.assertIn("--move-to", command)
        self.assertIn(str(Path(r"D:\Quarantine Folder")), command)
        self.assertIn("--move-recreate-path", command)

    def test_delete_uses_delete_smallest(self) -> None:
        config = DedupConfig(
            target=Path(r"C:\Photos\Library"),
            cache=Path("hashes.json"),
            mode="delete",
        )

        command = build_find_dups_command(config)

        self.assertEqual(command[-2:], ["--on-equal", "delete-smallest"])

    def test_default_quarantine_is_sibling_of_target(self) -> None:
        target = Path(r"C:\Photos\Library")

        quarantine = default_quarantine_dir(target)

        self.assertEqual(quarantine.name, "Library_quarantine")


if __name__ == "__main__":
    unittest.main()
