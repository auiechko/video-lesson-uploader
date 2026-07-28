from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from lesson_video_uploader.models import Lesson, LessonSendMode


class LessonSendModeTests(unittest.TestCase):
    def test_media_lesson_still_requires_at_least_one_video(self) -> None:
        with self.assertRaisesRegex(ValueError, "video"):
            Lesson(
                profile_id="main",
                batch_id="batch",
                calendar_event_id="event",
                event_start=datetime(2026, 6, 12, 10),
                caption="caption",
                ordered_video_paths=(),
            )

    def test_text_only_lesson_requires_no_video(self) -> None:
        lesson = Lesson(
            profile_id="main",
            batch_id="batch",
            calendar_event_id="event",
            event_start=datetime(2026, 6, 12, 10),
            caption="caption (без запису)",
            ordered_video_paths=(),
            send_mode=LessonSendMode.TEXT_ONLY,
        )

        self.assertEqual(lesson.video_count, 0)

    def test_text_only_lesson_rejects_accidental_video_attachment(self) -> None:
        with self.assertRaisesRegex(ValueError, "text-only"):
            Lesson(
                profile_id="main",
                batch_id="batch",
                calendar_event_id="event",
                event_start=datetime(2026, 6, 12, 10),
                caption="caption (без запису)",
                ordered_video_paths=(Path("unexpected.mp4"),),
                send_mode=LessonSendMode.TEXT_ONLY,
            )


if __name__ == "__main__":
    unittest.main()
