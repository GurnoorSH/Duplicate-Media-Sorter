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


if __name__ == "__main__":
    unittest.main()
