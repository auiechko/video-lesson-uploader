from __future__ import annotations

import unittest

from lesson_video_uploader.progress import LessonUploadProgress


class LessonUploadProgressTests(unittest.TestCase):
    def test_position_covers_every_video_not_only_the_current_one(self) -> None:
        progress = LessonUploadProgress((100, 200, 300))

        self.assertEqual(progress.observe(50, 100), (50, 600))
        self.assertEqual(progress.observe(100, 100), (100, 600))
        self.assertEqual(progress.observe(100, 200), (200, 600))
        self.assertEqual(progress.observe(200, 200), (300, 600))
        self.assertEqual(progress.observe(300, 300), (600, 600))

    def test_videos_of_equal_size_are_not_mistaken_for_one_another(self) -> None:
        progress = LessonUploadProgress((100, 100))

        self.assertEqual(progress.observe(100, 100), (100, 200))
        self.assertEqual(progress.observe(100, 100), (200, 200))

    def test_progress_is_capped_at_the_lesson_total(self) -> None:
        progress = LessonUploadProgress((100,))

        progress.observe(100, 100)

        self.assertEqual(progress.observe(100, 100), (100, 100))

    def test_impossible_sizes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "size"):
            LessonUploadProgress((100, 0))
        with self.assertRaisesRegex(ValueError, "video"):
            LessonUploadProgress(())

    def test_a_zero_byte_report_cannot_divide_the_caller_by_zero(self) -> None:
        progress = LessonUploadProgress((100,))
        with self.assertRaisesRegex(ValueError, "total"):
            progress.observe(0, 0)


if __name__ == "__main__":
    unittest.main()
