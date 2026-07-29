from __future__ import annotations

import unittest
from pathlib import Path

from lesson_video_uploader.media_tools import bundled_ffmpeg_path


class MediaRuntimeTests(unittest.TestCase):
    def test_bundled_ffmpeg_is_available_for_probe_preview_and_split(self) -> None:
        executable = Path(bundled_ffmpeg_path())

        self.assertTrue(executable.is_file())


if __name__ == "__main__":
    unittest.main()
