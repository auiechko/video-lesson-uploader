from __future__ import annotations

import unittest
from pathlib import Path

from lesson_video_uploader.progress import AlbumProgress


class AlbumProgressTests(unittest.TestCase):
    def test_aggregates_current_file_and_total_album_progress(self) -> None:
        progress = AlbumProgress(
            student_name="Ільяс",
            video_paths=(Path("one.mp4"), Path("two.mp4"), Path("three.mp4")),
            video_sizes=(100, 200, 300),
        )

        progress.start_file(1)
        snapshot = progress.update(100, 200)

        self.assertEqual(snapshot.video_number, 2)
        self.assertEqual(snapshot.video_count, 3)
        self.assertEqual(snapshot.file_percent, 50)
        self.assertEqual(snapshot.total_percent, 33)

    def test_completed_message_reports_one_album(self) -> None:
        progress = AlbumProgress(
            student_name="Ільяс",
            video_paths=(Path("one.mp4"), Path("two.mp4")),
            video_sizes=(100, 100),
        )
        self.assertEqual(progress.completion_message(), "Надіслано 2 відео одним альбомом.")

    def test_mismatched_sizes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "size"):
            AlbumProgress(
                student_name="Ільяс",
                video_paths=(Path("one.mp4"),),
                video_sizes=(),
            )


if __name__ == "__main__":
    unittest.main()
