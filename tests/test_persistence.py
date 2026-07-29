from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from lesson_video_uploader.calendar_rules import (
    CancellationSource,
    build_calendar_snapshot,
    parse_calendar_event,
)
from lesson_video_uploader.google_calendar import GoogleCalendarEvent
from lesson_video_uploader.models import (
    AlbumDelivery,
    Lesson,
    LessonDetails,
    LessonSendMode,
    SendStatus,
)
from lesson_video_uploader.persistence import SQLiteSendItemRepository


class PersistenceTests(unittest.TestCase):
    def test_existing_database_is_migrated_without_deleting_send_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.execute("""
                    CREATE TABLE send_items (
                        identity TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        calendar_event_id TEXT NOT NULL,
                        event_start TEXT NOT NULL,
                        caption TEXT NOT NULL,
                        ordered_video_paths TEXT NOT NULL,
                        video_count INTEGER NOT NULL,
                        telegram_album_group_id INTEGER,
                        telegram_message_ids TEXT NOT NULL,
                        album_deliveries TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        sent_at TEXT
                    )
                    """)
                    connection.execute(
                        """
                        INSERT INTO send_items VALUES (
                            'old', 'main', 'batch', 'event',
                            '2026-06-12T10:00:00', 'caption',
                            '["one.mp4"]', 1, NULL, '[]', '[]',
                            'PENDING', '2026-06-12T09:00:00+00:00', NULL
                        )
                        """
                    )

            _repository = SQLiteSendItemRepository(path)
            with closing(sqlite3.connect(path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(send_items)"
                    )
                }
                old_count = connection.execute(
                    "SELECT COUNT(*) FROM send_items WHERE identity = 'old'"
                ).fetchone()[0]

        self.assertTrue(
            {"send_mode", "lesson_details", "calendar_snapshot"}
            <= columns
        )
        self.assertEqual(old_count, 1)

    def test_cancelled_calendar_event_is_kept_for_reporting(self) -> None:
        kyiv = ZoneInfo("Europe/Kyiv")
        parsed = parse_calendar_event(GoogleCalendarEvent(
            id="cancelled-event",
            calendar_id="lessons",
            summary=(
                "ВП викладач 105853087 Наталія "
                "(Святослав 14) Учко ТГ"
            ),
            description="",
            start=datetime(2026, 6, 12, 18, tzinfo=kyiv),
            end=datetime(2026, 6, 12, 19, tzinfo=kyiv),
        ))
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(
                Path(directory) / "db.sqlite3"
            )

            repository.save_calendar_event_report(parsed)
            restored = repository.get_calendar_event_report(
                "lessons",
                "cancelled-event",
            )

        self.assertEqual(restored.status, parsed.status)
        self.assertEqual(
            restored.cancellation_source,
            CancellationSource.TEACHER,
        )
        self.assertFalse(restored.requires_video)

    def test_current_calendar_name_and_age_replace_old_local_report(self) -> None:
        kyiv = ZoneInfo("Europe/Kyiv")

        def current(summary: str):
            return parse_calendar_event(GoogleCalendarEvent(
                id="same-event",
                calendar_id="lessons",
                summary=summary,
                description="",
                start=datetime(2026, 6, 12, 18, tzinfo=kyiv),
                end=datetime(2026, 6, 12, 19, tzinfo=kyiv),
            ))

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(
                Path(directory) / "db.sqlite3"
            )
            repository.save_calendar_event_report(current(
                "105853087 Наталія (Святослав 14) Учко ТГ"
            ))

            repository.save_calendar_event_report(current(
                "105853087 Наталія (Святик 15 років) Учко ТГ"
            ))
            restored = repository.get_calendar_event_report(
                "lessons",
                "same-event",
            )

        self.assertEqual(restored.student_name, "Святик")
        self.assertEqual(restored.student_age, 15)

    def test_calendar_report_preserves_recurring_occurrence_metadata(self) -> None:
        kyiv = ZoneInfo("Europe/Kyiv")
        parsed = parse_calendar_event(
            GoogleCalendarEvent(
                id="instance-1",
                calendar_id="lessons",
                summary=(
                    "105853087 Наталія (Святослав 14) Учко ТГ"
                ),
                description="",
                start=datetime(2026, 7, 29, 19, tzinfo=kyiv),
                end=datetime(2026, 7, 29, 20, tzinfo=kyiv),
                recurring_event_id="series-1",
                original_start=datetime(2026, 7, 29, 19, tzinfo=kyiv),
                event_timezone="Europe/Kyiv",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(
                Path(directory) / "db.sqlite3"
            )

            repository.save_calendar_event_report(parsed)
            restored = repository.get_calendar_event_report(
                "lessons",
                "instance-1",
            )

        self.assertEqual(restored.recurring_event_id, "series-1")
        self.assertEqual(
            restored.original_start_utc,
            datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(restored.event_timezone, "Europe/Kyiv")

    def test_round_trip_preserves_ordered_paths_and_every_message_id(self) -> None:
        lesson = Lesson(
            profile_id="profile-1",
            batch_id="batch-1",
            calendar_event_id="event-1",
            event_start=datetime(2026, 6, 12, 10),
            caption="caption",
            ordered_video_paths=(Path("second.mp4"), Path("first.mp4")),
        )
        sent = replace(
            lesson,
            status=SendStatus.SENT,
            telegram_message_ids=(41, 42),
            album_deliveries=(AlbumDelivery(1, 1, 9001, (41, 42)),),
            sent_at=datetime(2026, 6, 12, 11, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(Path(directory) / "db.sqlite3")
            repository.save(sent)

            loaded = repository.get(lesson.identity)

        self.assertEqual(loaded, sent)
        self.assertEqual(loaded.ordered_video_paths, (Path("second.mp4"), Path("first.mp4")))
        self.assertEqual(loaded.telegram_message_ids, (41, 42))

    def test_reset_incomplete_deliveries_preserves_sent_and_other_batches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(
                Path(directory) / "db.sqlite3"
            )

            def item(
                event_id: str,
                *,
                batch_id: str = "batch-1",
                status: SendStatus,
            ) -> Lesson:
                return Lesson(
                    profile_id="profile-1",
                    batch_id=batch_id,
                    calendar_event_id=event_id,
                    event_start=datetime(2026, 6, 12, 10),
                    caption=event_id,
                    ordered_video_paths=(Path(f"{event_id}.mp4"),),
                    telegram_message_ids=(101,),
                    album_deliveries=(
                        AlbumDelivery(1, 1, 777, (101,)),
                    ),
                    status=status,
                )

            unknown = item(
                "unknown",
                status=SendStatus.DELIVERY_UNKNOWN,
            )
            partial = item(
                "partial",
                status=SendStatus.PARTIALLY_CONFIRMED,
            )
            sent = item("sent", status=SendStatus.SENT)
            other_batch = item(
                "other",
                batch_id="batch-2",
                status=SendStatus.DELIVERY_UNKNOWN,
            )
            for lesson in (unknown, partial, sent, other_batch):
                repository.save(lesson)

            reset_count = repository.reset_incomplete_deliveries(
                "profile-1",
                "batch-1",
            )

            reset_unknown = repository.get(unknown.identity)
            reset_partial = repository.get(partial.identity)
            preserved_sent = repository.get(sent.identity)
            preserved_other = repository.get(other_batch.identity)

        self.assertEqual(reset_count, 2)
        self.assertEqual(reset_unknown.status, SendStatus.PENDING)
        self.assertEqual(reset_unknown.telegram_message_ids, ())
        self.assertEqual(reset_unknown.album_deliveries, ())
        self.assertEqual(reset_partial.status, SendStatus.PENDING)
        self.assertEqual(preserved_sent.status, SendStatus.SENT)
        self.assertEqual(preserved_sent.telegram_message_ids, (101,))
        self.assertEqual(
            preserved_other.status,
            SendStatus.DELIVERY_UNKNOWN,
        )

    def test_round_trip_preserves_text_mode_details_and_calendar_snapshot(self) -> None:
        kyiv = ZoneInfo("Europe/Kyiv")
        parsed = parse_calendar_event(GoogleCalendarEvent(
            id="event-text",
            calendar_id="lessons",
            summary=(
                "Перенос 105853087 Наталія (Святослав 15 років) "
                "Учко ТГ (пробне) (без запису)"
            ),
            description="",
            start=datetime(2026, 6, 12, 18, tzinfo=kyiv),
            end=datetime(2026, 6, 12, 19, tzinfo=kyiv),
        ))
        lesson = Lesson(
            profile_id="profile-1",
            batch_id="batch-1",
            calendar_event_id=parsed.event_id,
            event_start=parsed.start,
            caption=parsed.caption,
            ordered_video_paths=(),
            send_mode=LessonSendMode.TEXT_ONLY,
            calendar_snapshot=build_calendar_snapshot(parsed),
            details=LessonDetails(
                student_id=parsed.student_id,
                student_name=parsed.student_name,
                lesson_label=parsed.lesson_label,
                duration_hours=parsed.duration_hours,
                is_trial=True,
                student_age=15,
                calendar_status=parsed.status.value,
                is_no_recording=True,
                is_transferred=True,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(Path(directory) / "db.sqlite3")
            repository.save(lesson)

            loaded = repository.get(lesson.identity)

        self.assertEqual(loaded, lesson)

    def test_backup_creates_readable_sqlite_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = SQLiteSendItemRepository(root / "db.sqlite3")
            lesson = Lesson(
                profile_id="profile-1",
                batch_id="batch-1",
                calendar_event_id="event-1",
                event_start=datetime(2026, 6, 12, 10),
                caption="caption",
                ordered_video_paths=(Path("video.mp4"),),
            )
            repository.save(lesson)

            backup = repository.create_backup(root / "backups")
            restored = SQLiteSendItemRepository(backup).get(lesson.identity)

        self.assertIsNotNone(restored)
        self.assertTrue(backup.name.startswith("deliveries-"))
        self.assertEqual(backup.suffix, ".sqlite3")


if __name__ == "__main__":
    unittest.main()
