from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lesson_video_uploader.calendar_rules import (
    CalendarEventStatus,
    ParsedCalendarEvent,
)
from lesson_video_uploader.zoom_matching import (
    ZoomMatchSettings,
    ZoomMatchStatus,
    batch_matching_ready,
    match_zoom_segments,
)
from lesson_video_uploader.zoom_recordings import (
    VideoTimeConfidence,
    VideoTimeMethod,
    ZoomVideoSegment,
)

KYIV = ZoneInfo("Europe/Kyiv")
DAY = datetime(2026, 7, 23, tzinfo=KYIV)


def calendar_event(
    hour: int,
    *,
    minute: int = 0,
    end_hour: int | None = None,
    end_minute: int = 0,
    event_id: str = "event",
    status: CalendarEventStatus = CalendarEventStatus.NORMAL,
) -> ParsedCalendarEvent:
    start = DAY.replace(hour=hour, minute=minute)
    end = DAY.replace(
        hour=end_hour if end_hour is not None else hour + 1,
        minute=end_minute,
    )
    return ParsedCalendarEvent(
        event_id=event_id,
        calendar_id="lessons",
        original_summary=f"105813989 Teacher (Ільяс 10) {event_id}",
        student_id="105813989",
        student_name="Ільяс",
        student_age=10,
        lesson_type="індив",
        start=start,
        end=end,
        status=status,
        is_transferred=status in {
            CalendarEventStatus.TRANSFERRED,
            CalendarEventStatus.TRANSFERRED_TRIAL,
            CalendarEventStatus.TRANSFERRED_NO_RECORDING,
        },
    )


def video_segment(
    hour: int,
    *,
    minute: int = 0,
    second: int = 0,
    duration_minutes: int = 48,
    sequence: int = 1,
) -> ZoomVideoSegment:
    start = DAY.replace(hour=hour, minute=minute, second=second)
    return ZoomVideoSegment(
        source_folder=Path("zoom-folder"),
        path=Path(f"video-{sequence}.mp4"),
        sequence_number=sequence,
        duration_seconds=duration_minutes * 60,
        estimated_start=start,
        estimated_end=start + timedelta(minutes=duration_minutes),
        time_method=VideoTimeMethod.ZOOM_FOLDER_START,
        confidence=VideoTimeConfidence.HIGH,
        file_size=1000 + sequence,
    )


class ZoomMatchingTests(unittest.TestCase):
    def test_automatic_start_differences_up_to_thirty_minutes(self) -> None:
        examples = (
            (14, 6, 14),
            (13, 45, 14),
            (15, 18, 15),
        )
        for video_hour, video_minute, calendar_hour in examples:
            with self.subTest(
                video_hour=video_hour,
                video_minute=video_minute,
            ):
                result = match_zoom_segments(
                    (video_segment(video_hour, minute=video_minute),),
                    (calendar_event(calendar_hour),),
                )[0]

                self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)
                self.assertEqual(result.event.event_id, "event")

    def test_start_differences_over_thirty_minutes_require_confirmation(self) -> None:
        for video_hour, video_minute, calendar_hour in (
            (13, 2, 14),
            (16, 2, 15),
        ):
            with self.subTest(video_hour=video_hour):
                result = match_zoom_segments(
                    (video_segment(video_hour, minute=video_minute),),
                    (calendar_event(calendar_hour),),
                )[0]

                self.assertEqual(
                    result.status,
                    ZoomMatchStatus.TIME_CONFIRMATION_REQUIRED,
                )

    def test_shorter_video_has_no_duration_conflict(self) -> None:
        result = match_zoom_segments(
            (video_segment(14, minute=3, duration_minutes=48),),
            (calendar_event(14),),
        )[0]

        self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)

    def test_long_video_without_next_lesson_has_no_overlap_conflict(self) -> None:
        result = match_zoom_segments(
            (video_segment(14, minute=2, duration_minutes=85),),
            (calendar_event(14),),
        )[0]

        self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)

    def test_long_video_overlapping_next_lesson_requires_review(self) -> None:
        result = match_zoom_segments(
            (video_segment(14, minute=2, second=10, duration_minutes=85),),
            (
                calendar_event(14, event_id="current"),
                calendar_event(15, event_id="next"),
            ),
        )[0]

        self.assertEqual(
            result.status,
            ZoomMatchStatus.VIDEO_OVERLAPS_NEXT_EVENT,
        )
        self.assertEqual(result.next_event.event_id, "next")
        self.assertEqual(result.next_overlap_seconds, 27 * 60 + 10)
        self.assertEqual(result.split_offset_seconds, 57 * 60 + 50)

    def test_two_normal_events_in_one_slot_require_manual_selection(self) -> None:
        result = match_zoom_segments(
            (video_segment(18, minute=2),),
            (
                calendar_event(18, event_id="first"),
                calendar_event(18, event_id="second"),
            ),
        )[0]

        self.assertEqual(
            result.status,
            ZoomMatchStatus.MANUAL_SELECTION_REQUIRED,
        )
        self.assertEqual(
            {candidate.event.event_id for candidate in result.candidates},
            {"first", "second"},
        )

    def test_nearest_event_is_selected_when_candidates_are_not_one_slot(
        self,
    ) -> None:
        result = match_zoom_segments(
            (video_segment(18, minute=1, duration_minutes=5),),
            (
                calendar_event(18, event_id="nearest"),
                calendar_event(
                    18,
                    minute=12,
                    end_hour=19,
                    end_minute=12,
                    event_id="later",
                ),
            ),
            settings=ZoomMatchSettings(
                calendar_conflict_tolerance_minutes=10,
            ),
        )[0]

        self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)
        self.assertEqual(result.event.event_id, "nearest")

    def test_cancelled_event_is_filtered_before_automatic_match(self) -> None:
        result = match_zoom_segments(
            (video_segment(18, minute=2),),
            (
                calendar_event(
                    18,
                    event_id="cancelled",
                    status=CalendarEventStatus.IGNORED_CANCELLED,
                ),
                calendar_event(18, event_id="normal"),
            ),
        )[0]

        self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)
        self.assertEqual(result.event.event_id, "normal")

    def test_transfer_and_normal_in_one_slot_require_manual_selection(self) -> None:
        result = match_zoom_segments(
            (video_segment(18, minute=2),),
            (
                calendar_event(
                    18,
                    event_id="transfer",
                    status=CalendarEventStatus.TRANSFERRED,
                ),
                calendar_event(18, event_id="normal"),
            ),
        )[0]

        self.assertEqual(
            result.status,
            ZoomMatchStatus.MANUAL_SELECTION_REQUIRED,
        )

    def test_single_three_hour_event_does_not_create_split_boundary(self) -> None:
        result = match_zoom_segments(
            (video_segment(14, minute=2, duration_minutes=170),),
            (calendar_event(14, end_hour=17),),
        )[0]

        self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)
        self.assertIsNone(result.next_event)

    def test_second_mp4_crossing_two_lessons_requires_manual_review(self) -> None:
        result = match_zoom_segments(
            (
                video_segment(17, minute=2, duration_minutes=52),
                video_segment(
                    17,
                    minute=54,
                    duration_minutes=47,
                    sequence=2,
                ),
            ),
            (
                calendar_event(17, event_id="first"),
                calendar_event(18, event_id="second"),
            ),
        )[1]

        self.assertEqual(
            result.status,
            ZoomMatchStatus.MULTIPLE_VIDEOS_REVIEW_REQUIRED,
        )

    def test_batch_readiness_requires_every_result_to_be_resolved(self) -> None:
        ready = match_zoom_segments(
            (video_segment(14, minute=6),),
            (calendar_event(14),),
        )
        unresolved = match_zoom_segments(
            (video_segment(13, minute=2),),
            (calendar_event(14),),
        )

        self.assertTrue(batch_matching_ready(ready))
        self.assertFalse(batch_matching_ready(unresolved))

    def test_custom_automatic_tolerance_is_inclusive(self) -> None:
        result = match_zoom_segments(
            (video_segment(13, minute=40),),
            (calendar_event(14),),
            settings=ZoomMatchSettings(
                automatic_time_tolerance_minutes=20,
            ),
        )[0]

        self.assertEqual(result.status, ZoomMatchStatus.AUTO_MATCHED)


if __name__ == "__main__":
    unittest.main()
