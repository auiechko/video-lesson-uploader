from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from lesson_video_uploader.models import Lesson, LessonVideo
from lesson_video_uploader.planning import (
    build_caption,
    build_preview,
    group_videos_by_lesson,
    plan_albums,
)


EVENT_START = datetime(2026, 6, 12, 10, 0)
BASE_CAPTION = "12.06.2026 105813989 Ільяс 10р індив"


def make_lesson(
    count: int,
    *,
    event_id: str = "event-1",
    duration_hours: int = 1,
    is_trial: bool = False,
) -> Lesson:
    return Lesson(
        profile_id="profile-1",
        batch_id="batch-1",
        calendar_event_id=event_id,
        event_start=EVENT_START,
        caption=build_caption(
            date=EVENT_START.date(),
            student_id="105813989",
            student_name="Ільяс",
            lesson_label="10р індив",
            duration_hours=duration_hours,
            is_trial=is_trial,
        ),
        ordered_video_paths=tuple(
            Path(f"video_part_{index:02}.mp4") for index in range(1, count + 1)
        ),
    )


class CaptionTests(unittest.TestCase):
    def test_regular_lesson_has_no_part_suffix(self) -> None:
        self.assertEqual(build_caption(
            date=EVENT_START.date(),
            student_id="105813989",
            student_name="Ільяс",
            lesson_label="10р індив",
        ), BASE_CAPTION)

    def test_two_hour_lesson_uses_duration_suffix(self) -> None:
        self.assertEqual(make_lesson(3, duration_hours=2).caption, f"{BASE_CAPTION} ДВІ ГОДИНИ")

    def test_three_hour_lesson_uses_duration_suffix(self) -> None:
        self.assertEqual(make_lesson(2, duration_hours=3).caption, f"{BASE_CAPTION} ТРИ ГОДИНИ")

    def test_trial_lesson_appends_trial_after_duration(self) -> None:
        self.assertEqual(
            make_lesson(2, duration_hours=2, is_trial=True).caption,
            f"{BASE_CAPTION} ДВІ ГОДИНИ (пробне)",
        )


class AlbumPlanningTests(unittest.TestCase):
    def test_one_video_is_one_regular_media_message(self) -> None:
        plans = plan_albums(make_lesson(1))
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].video_paths, (Path("video_part_01.mp4"),))
        self.assertEqual(plans[0].caption, BASE_CAPTION)
        self.assertFalse(plans[0].is_album)

    def test_two_videos_are_one_album_with_one_shared_caption(self) -> None:
        plans = plan_albums(make_lesson(2))
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0].video_paths), 2)
        self.assertEqual(plans[0].caption, BASE_CAPTION)
        self.assertNotIn("частина", plans[0].caption)
        self.assertTrue(plans[0].is_album)

    def test_ten_videos_fit_one_album_without_batch_suffix(self) -> None:
        plans = plan_albums(make_lesson(10))
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0].video_paths), 10)
        self.assertEqual(plans[0].caption, BASE_CAPTION)

    def test_eleven_videos_are_split_into_two_captioned_batches(self) -> None:
        plans = plan_albums(make_lesson(11))
        self.assertEqual([len(plan.video_paths) for plan in plans], [10, 1])
        self.assertEqual(plans[0].caption, f"{BASE_CAPTION} (альбом 1/2)")
        self.assertEqual(plans[1].caption, f"{BASE_CAPTION} (альбом 2/2)")

    def test_video_order_is_preserved_across_batches(self) -> None:
        lesson = make_lesson(11)
        paths = [path for plan in plan_albums(lesson) for path in plan.video_paths]
        self.assertEqual(paths, list(lesson.ordered_video_paths))

    def test_empty_lesson_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one video"):
            make_lesson(0)


class GroupingAndPreviewTests(unittest.TestCase):
    def test_segments_from_different_calendar_events_are_separate_lessons(self) -> None:
        videos = [
            LessonVideo("event-b", EVENT_START, Path("b-2.mp4"), order=2),
            LessonVideo("event-a", EVENT_START, Path("a-1.mp4"), order=1),
            LessonVideo("event-b", EVENT_START, Path("b-1.mp4"), order=1),
        ]
        lessons = group_videos_by_lesson(
            videos,
            profile_id="profile-1",
            batch_id="batch-1",
            captions={"event-a": "Святослав", "event-b": "Ільяс"},
        )
        self.assertEqual(len(lessons), 2)
        by_event = {lesson.calendar_event_id: lesson for lesson in lessons}
        self.assertEqual(by_event["event-a"].ordered_video_paths, (Path("a-1.mp4"),))
        self.assertEqual(
            by_event["event-b"].ordered_video_paths,
            (Path("b-1.mp4"), Path("b-2.mp4")),
        )

    def test_preview_has_one_expandable_row_per_lesson(self) -> None:
        row = build_preview(make_lesson(3))
        self.assertEqual(row.summary, "12.06.2026 | Ільяс | 3 відео | Один Telegram-альбом")
        self.assertEqual(len(row.video_paths), 3)


if __name__ == "__main__":
    unittest.main()
