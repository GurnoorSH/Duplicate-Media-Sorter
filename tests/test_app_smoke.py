import importlib.util
import os
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
class DesktopAppSmokeTests(unittest.TestCase):
    def test_main_window_constructs_offscreen(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from media_sorter_app import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        self.assertEqual(window.windowTitle(), "Duplicate Media Sorter")
        window.close()
        app.quit()


if __name__ == "__main__":
    unittest.main()

