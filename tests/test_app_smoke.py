import importlib.util
import os
import unittest
from pathlib import Path


def make_group(index: int, exact: bool):
    from media_sorter import PHOTO_LANE, DuplicateGroup, MediaItem

    keeper = Path(f"C:/library/{index}.jpg")
    copy = Path(f"C:/library/{index} - Copy.jpg")
    items = [MediaItem(keeper, PHOTO_LANE, 10, 1), MediaItem(copy, PHOTO_LANE, 10, 1)]
    return DuplicateGroup(f"group-{index}-{exact}", PHOTO_LANE, items, keeper, exact=exact)


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
class DesktopAppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_constructs_offscreen(self) -> None:
        from media_sorter_app import MainWindow

        window = MainWindow()
        self.assertEqual(window.windowTitle(), "Duplicate Media Sorter")
        window.close()

    def test_only_identical_groups_are_preselected(self) -> None:
        from media_sorter_app import default_selections

        exact, similar = make_group(1, True), make_group(2, False)

        selections = default_selections([exact, similar])

        self.assertEqual(selections[exact.id], {str(exact.items[1].path)})
        self.assertEqual(selections[similar.id], set())

    def test_empty_files_are_preselected_without_a_keeper_choice(self) -> None:
        from media_sorter import ZERO_FILLED_LANE, DuplicateGroup, MediaItem, ScanRequest, ScanResult
        from media_sorter_app import GroupCard, ReviewPage, default_selections

        empty = DuplicateGroup("empty-1", ZERO_FILLED_LANE, [MediaItem(Path("C:/library/dead.mp4"), ZERO_FILLED_LANE, 0, 1)], None, exact=True)

        self.assertEqual(default_selections([empty])[empty.id], {str(empty.items[0].path)})
        page = ReviewPage()
        page.set_result(ScanResult(ScanRequest(Path("C:/library")), [empty]))
        page.lanes.setCurrentRow(page.lanes.count() - 1)
        cards = [page.group_layout.itemAt(index).widget() for index in range(page.group_layout.count())]
        card = next(card for card in cards if isinstance(card, GroupCard))
        tile = next(iter(card.tiles.values()))
        self.assertTrue(tile.keeper_radio.isHidden())
        self.assertTrue(tile.move_checkbox.isChecked())
        self.assertIn("Empty files  1", page.lanes.item(page.lanes.count() - 1).text())

    def test_review_page_renders_one_page_of_groups(self) -> None:
        from media_sorter import ScanRequest, ScanResult
        from media_sorter_app import GROUPS_PER_PAGE, GroupCard, ReviewPage

        groups = [make_group(index, True) for index in range(GROUPS_PER_PAGE * 3 + 1)]
        page = ReviewPage()
        page.set_result(ScanResult(ScanRequest(Path("C:/library")), groups))

        cards = [page.group_layout.itemAt(index).widget() for index in range(page.group_layout.count())]
        self.assertEqual(sum(isinstance(card, GroupCard) for card in cards), GROUPS_PER_PAGE)
        self.assertEqual(page.pager.pages, 4)
        self.assertIn(f"{len(groups)} files selected", page.summary.text())
        page.thumbnail_pool.clear()
        page.thumbnail_pool.waitForDone()

    def test_quarantine_releases_previewed_video(self) -> None:
        # A loaded preview keeps the file open, and Windows then refuses to
        # move it (WinError 32), so quarantine must unload it first.
        from unittest.mock import patch

        from PySide6.QtWidgets import QMessageBox

        from media_sorter import ScanRequest, ScanResult
        from media_sorter_app import MainWindow

        window = MainWindow()
        group = make_group(1, True)
        window.scan_result = ScanResult(ScanRequest(Path("C:/library")), [group])
        window.review_page.preview_video(group.items[1].path, group.lane)
        self.assertFalse(window.review_page.player.source().isEmpty())

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), patch.object(window, "_start_worker"):
            window.start_quarantine({group.id: {str(group.items[1].path)}})

        self.assertTrue(window.review_page.player.source().isEmpty())
        window.close()


if __name__ == "__main__":
    unittest.main()
