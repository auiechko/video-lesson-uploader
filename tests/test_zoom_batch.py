from __future__ import annotations

import unittest
from dataclasses import replace

from lesson_video_uploader.calendar_rules import CalendarEventStatus
from lesson_video_uploader.models import LessonSendMode
from lesson_video_uploader.zoom_batch import assemble_zoom_batch
from lesson_video_uploader.zoom_matching import (
    ZoomMatchStatus,
    match_zoom_segments,
)
from tests.test_zoom_matching import calendar_event, video_segment


class ZoomBatchAssemblyTests(unittest.TestCase):
    def test_multiple_confirmed_mp4_for_one_event_form_one_lesson_album(
        self,
    ) -> None:
        event = calendar_event(14, end_hour=16)
        first, second = match_zoom_segments(
            (
                video_segment(14, minute=2, duration_minutes=52),
                video_segment(
                    14,
                    minute=54,
                    duration_minutes=47,
                    sequence=2,
                ),
            ),
            (event,),
        )
        confirmed = (
            first,
            replace(
                second,
                status=ZoomMatchStatus.MANUALLY_CONFIRMED,
                event=event,
            ),
        )

        batch = assemble_zoom_batch(
            (event,),
            confirmed,
            profile_id="main",
            batch_id="2026-07-23",
        )

        self.assertEqual(len(batch.lessons), 1)
        self.assertEqual(batch.lessons[0].video_count, 2)
        self.assertEqual(
            batch.lessons[0].ordered_video_paths,
            (first.segment.path, second.segment.path),
        )
        self.assertNotIn("частина", batch.lessons[0].caption)

    def test_no_recording_event_becomes_ready_text_without_zoom_folder(
        self,
    ) -> None:
        text_event = replace(
            calendar_event(
                14,
                status=CalendarEventStatus.NO_RECORDING,
            ),
            is_no_recording=True,
        )

        batch = assemble_zoom_batch(
            (text_event,),
            (),
            profile_id="main",
            batch_id="2026-07-23",
        )

        self.assertEqual(len(batch.lessons), 1)
        self.assertEqual(
            batch.lessons[0].send_mode,
            LessonSendMode.TEXT_ONLY,
        )
        self.assertEqual(batch.lessons[0].ordered_video_paths, ())
        self.assertEqual(batch.missing_events, ())

    def test_conducted_event_without_candidate_is_reported_as_missing_folder(
        self,
    ) -> None:
        event = calendar_event(14)

        batch = assemble_zoom_batch(
            (event,),
            (),
            profile_id="main",
            batch_id="2026-07-23",
        )

        self.assertEqual(batch.lessons, ())
        self.assertEqual(batch.missing_events, (event,))

    def test_missing_video_can_be_resolved_as_text_only(self) -> None:
        event = calendar_event(14)

        batch = assemble_zoom_batch(
            (event,),
            (),
            profile_id="main",
            batch_id="2026-07-23",
            manual_text_event_ids=frozenset({event.event_id}),
        )

        self.assertEqual(batch.missing_events, ())
        self.assertEqual(len(batch.lessons), 1)
        self.assertEqual(
            batch.lessons[0].send_mode,
            LessonSendMode.TEXT_ONLY,
        )
        self.assertTrue(
            batch.lessons[0].caption.endswith("(без запису)")
        )

    def test_non_conducted_resolution_removes_missing_event(self) -> None:
        event = calendar_event(14)

        batch = assemble_zoom_batch(
            (event,),
            (),
            profile_id="main",
            batch_id="2026-07-23",
            ignored_event_ids=frozenset({event.event_id}),
        )

        self.assertEqual(batch.missing_events, ())
        self.assertEqual(batch.lessons, ())


if __name__ == "__main__":
    unittest.main()
