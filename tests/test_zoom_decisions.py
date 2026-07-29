from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from lesson_video_uploader.calendar_rules import (
    CalendarEventStatus,
    ParsedCalendarEvent,
)
from lesson_video_uploader.persistence import SQLiteSendItemRepository
from lesson_video_uploader.zoom_decisions import (
    AssignmentType,
    FileIdentity,
    ZoomAssignmentDecision,
    normalize_file_path,
)
from lesson_video_uploader.zoom_recordings import (
    VideoTimeConfidence,
    VideoTimeMethod,
    ZoomVideoSegment,
)

KYIV = ZoneInfo("Europe/Kyiv")


def segment(path: Path) -> ZoomVideoSegment:
    start = datetime(2026, 7, 23, 14, 2, 10, tzinfo=KYIV)
    return ZoomVideoSegment(
        source_folder=path.parent,
        path=path,
        sequence_number=1,
        duration_seconds=3600,
        estimated_start=start,
        estimated_end=start + timedelta(hours=1),
        time_method=VideoTimeMethod.ZOOM_FOLDER_START,
        confidence=VideoTimeConfidence.HIGH,
        file_size=path.stat().st_size,
    )


def event(
    *,
    status: CalendarEventStatus = CalendarEventStatus.NORMAL,
) -> ParsedCalendarEvent:
    start = datetime(2026, 7, 23, 14, tzinfo=KYIV)
    return ParsedCalendarEvent(
        event_id="event-1",
        calendar_id="lessons",
        original_summary="105813989 Teacher (Ільяс 10)",
        student_id="105813989",
        student_name="Ільяс",
        student_age=10,
        lesson_type="індив",
        start=start,
        end=start + timedelta(hours=1),
        status=status,
    )


class ZoomDecisionPersistenceTests(unittest.TestCase):
    def test_unchanged_file_and_calendar_event_reuse_manual_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"unchanged")
            item = segment(video)
            calendar = event()
            repository = SQLiteSendItemRepository(root / "state.sqlite3")
            decision = ZoomAssignmentDecision(
                file_identity=FileIdentity.from_segment(item),
                calendar_event_id=calendar.event_id,
                event_start_utc=calendar.start.astimezone(timezone.utc),
                assignment_type=AssignmentType.MANUALLY_CONFIRMED_TIME,
                confirmed_by="main",
                confirmed_at=datetime.now(timezone.utc),
                reason="Урок почався раніше за домовленістю",
            )

            repository.save_zoom_decision(decision)
            loaded = repository.get_valid_zoom_decision(item, calendar)

        self.assertEqual(loaded, decision)

    def test_size_or_duration_change_invalidates_saved_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"original")
            original = segment(video)
            calendar = event()
            repository = SQLiteSendItemRepository(root / "state.sqlite3")
            decision = ZoomAssignmentDecision(
                file_identity=FileIdentity.from_segment(original),
                calendar_event_id=calendar.event_id,
                event_start_utc=calendar.start.astimezone(timezone.utc),
                assignment_type=AssignmentType.MANUALLY_SELECTED_EVENT,
                confirmed_by="main",
                confirmed_at=datetime.now(timezone.utc),
                reason="manual",
            )
            repository.save_zoom_decision(decision)

            video.write_bytes(b"changed-size")
            changed_size = replace(original, file_size=video.stat().st_size)
            changed_duration = replace(original, duration_seconds=3590)

            self.assertIsNone(
                repository.get_valid_zoom_decision(changed_size, calendar)
            )
            repository.save_zoom_decision(decision)
            self.assertIsNone(
                repository.get_valid_zoom_decision(changed_duration, calendar)
            )

    def test_cancelled_calendar_event_invalidates_saved_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            item = segment(video)
            calendar = event()
            repository = SQLiteSendItemRepository(root / "state.sqlite3")
            repository.save_zoom_decision(
                ZoomAssignmentDecision(
                    file_identity=FileIdentity.from_segment(item),
                    calendar_event_id=calendar.event_id,
                    event_start_utc=calendar.start.astimezone(timezone.utc),
                    assignment_type=AssignmentType.MANUALLY_SELECTED_EVENT,
                    confirmed_by="main",
                    confirmed_at=datetime.now(timezone.utc),
                    reason="manual",
                )
            )

            loaded = repository.get_valid_zoom_decision(
                item,
                event(status=CalendarEventStatus.IGNORED_CANCELLED),
            )

        self.assertIsNone(loaded)

    def test_manual_folder_datetime_round_trip_is_timezone_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = SQLiteSendItemRepository(root / "state.sqlite3")
            folder = root / "renamed recording"
            start = datetime(2026, 7, 23, 14, 2, 10, tzinfo=KYIV)

            repository.save_zoom_folder_start(folder, start)
            loaded = repository.get_zoom_folder_starts()

        self.assertEqual(loaded[normalize_file_path(folder)], start)
        self.assertIsNotNone(
            loaded[normalize_file_path(folder)].tzinfo
        )


if __name__ == "__main__":
    unittest.main()
