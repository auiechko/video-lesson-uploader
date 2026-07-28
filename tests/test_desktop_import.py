from __future__ import annotations

import unittest


class DesktopImportTests(unittest.TestCase):
    def test_desktop_entry_point_imports_without_creating_a_window(self) -> None:
        from lesson_video_uploader.desktop import DesktopApplication, main

        self.assertTrue(callable(main))
        self.assertTrue(callable(DesktopApplication))


if __name__ == "__main__":
    unittest.main()
