from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .calendar_rules import (
    CalendarEventSnapshot,
    CalendarEventStatus,
    CancellationSource,
    ParsedCalendarEvent,
)
from .models import (
    AlbumDelivery,
    Lesson,
    LessonDetails,
    LessonSendMode,
    SendStatus,
)


class SQLiteSendItemRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                CREATE TABLE IF NOT EXISTS send_items (
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
                    sent_at TEXT,
                    send_mode TEXT NOT NULL DEFAULT 'MEDIA',
                    lesson_details TEXT,
                    calendar_snapshot TEXT
                )
                """)
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(send_items)"
                    ).fetchall()
                }
                migrations = {
                    "send_mode": (
                        "ALTER TABLE send_items ADD COLUMN "
                        "send_mode TEXT NOT NULL DEFAULT 'MEDIA'"
                    ),
                    "lesson_details": (
                        "ALTER TABLE send_items ADD COLUMN lesson_details TEXT"
                    ),
                    "calendar_snapshot": (
                        "ALTER TABLE send_items ADD COLUMN calendar_snapshot TEXT"
                    ),
                }
                for column, statement in migrations.items():
                    if column not in columns:
                        connection.execute(statement)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS calendar_event_reports (
                        calendar_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        original_summary TEXT NOT NULL,
                        student_id TEXT NOT NULL,
                        student_name TEXT NOT NULL,
                        student_age INTEGER,
                        lesson_type TEXT NOT NULL,
                        start TEXT NOT NULL,
                        end TEXT NOT NULL,
                        status TEXT NOT NULL,
                        cancellation_source TEXT,
                        is_trial INTEGER NOT NULL,
                        is_no_recording INTEGER NOT NULL,
                        is_transferred INTEGER NOT NULL,
                        parse_error TEXT NOT NULL,
                        PRIMARY KEY (calendar_id, event_id)
                    )
                """)

    def save_calendar_event_report(
        self,
        event: ParsedCalendarEvent,
    ) -> None:
        values = (
            event.calendar_id,
            event.event_id,
            event.original_summary,
            event.student_id,
            event.student_name,
            event.student_age,
            event.lesson_type,
            event.start.isoformat(),
            event.end.isoformat(),
            event.status.value,
            (
                event.cancellation_source.value
                if event.cancellation_source is not None
                else None
            ),
            event.is_trial,
            event.is_no_recording,
            event.is_transferred,
            event.parse_error,
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                    INSERT INTO calendar_event_reports (
                        calendar_id, event_id, original_summary, student_id,
                        student_name, student_age, lesson_type, start, end,
                        status, cancellation_source, is_trial,
                        is_no_recording, is_transferred, parse_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(calendar_id, event_id) DO UPDATE SET
                        original_summary=excluded.original_summary,
                        student_id=excluded.student_id,
                        student_name=excluded.student_name,
                        student_age=excluded.student_age,
                        lesson_type=excluded.lesson_type,
                        start=excluded.start,
                        end=excluded.end,
                        status=excluded.status,
                        cancellation_source=excluded.cancellation_source,
                        is_trial=excluded.is_trial,
                        is_no_recording=excluded.is_no_recording,
                        is_transferred=excluded.is_transferred,
                        parse_error=excluded.parse_error
                """, values)

    def get_calendar_event_report(
        self,
        calendar_id: str,
        event_id: str,
    ) -> ParsedCalendarEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM calendar_event_reports
                WHERE calendar_id = ? AND event_id = ?
                """,
                (calendar_id, event_id),
            ).fetchone()
        if row is None:
            return None
        return ParsedCalendarEvent(
            event_id=row["event_id"],
            calendar_id=row["calendar_id"],
            original_summary=row["original_summary"],
            student_id=row["student_id"],
            student_name=row["student_name"],
            student_age=row["student_age"],
            lesson_type=row["lesson_type"],
            start=datetime.fromisoformat(row["start"]),
            end=datetime.fromisoformat(row["end"]),
            status=CalendarEventStatus(row["status"]),
            cancellation_source=(
                CancellationSource(row["cancellation_source"])
                if row["cancellation_source"] is not None
                else None
            ),
            is_trial=bool(row["is_trial"]),
            is_no_recording=bool(row["is_no_recording"]),
            is_transferred=bool(row["is_transferred"]),
            parse_error=row["parse_error"],
        )

    def save(self, lesson: Lesson) -> None:
        deliveries = [
            {
                "album_number": delivery.album_number,
                "album_count": delivery.album_count,
                "telegram_album_group_id": delivery.telegram_album_group_id,
                "telegram_message_ids": list(delivery.telegram_message_ids),
            }
            for delivery in lesson.album_deliveries
        ]
        values = (
            lesson.identity,
            lesson.profile_id,
            lesson.batch_id,
            lesson.calendar_event_id,
            lesson.event_start.isoformat(),
            lesson.caption,
            json.dumps([str(path) for path in lesson.ordered_video_paths]),
            lesson.video_count,
            lesson.telegram_album_group_id,
            json.dumps(lesson.telegram_message_ids),
            json.dumps(deliveries),
            lesson.status.value,
            lesson.created_at.isoformat(),
            lesson.sent_at.isoformat() if lesson.sent_at else None,
            lesson.send_mode.value,
            (
                json.dumps(asdict(lesson.details), ensure_ascii=False)
                if lesson.details is not None
                else None
            ),
            (
                json.dumps(asdict(lesson.calendar_snapshot), ensure_ascii=False)
                if lesson.calendar_snapshot is not None
                else None
            ),
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                    INSERT INTO send_items (
                        identity, profile_id, batch_id, calendar_event_id, event_start,
                        caption, ordered_video_paths, video_count,
                        telegram_album_group_id, telegram_message_ids,
                        album_deliveries, status, created_at, sent_at,
                        send_mode, lesson_details, calendar_snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET
                        profile_id=excluded.profile_id,
                        batch_id=excluded.batch_id,
                        calendar_event_id=excluded.calendar_event_id,
                        event_start=excluded.event_start,
                        caption=excluded.caption,
                        ordered_video_paths=excluded.ordered_video_paths,
                        video_count=excluded.video_count,
                        telegram_album_group_id=excluded.telegram_album_group_id,
                        telegram_message_ids=excluded.telegram_message_ids,
                        album_deliveries=excluded.album_deliveries,
                        status=excluded.status,
                        created_at=excluded.created_at,
                        sent_at=excluded.sent_at,
                        send_mode=excluded.send_mode,
                        lesson_details=excluded.lesson_details,
                        calendar_snapshot=excluded.calendar_snapshot
                """, values)

    def get(self, identity: str) -> Lesson | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM send_items WHERE identity = ?",
                (identity,),
            ).fetchone()
        if row is None:
            return None
        deliveries_data = json.loads(row["album_deliveries"])
        return Lesson(
            profile_id=row["profile_id"],
            batch_id=row["batch_id"],
            calendar_event_id=row["calendar_event_id"],
            event_start=datetime.fromisoformat(row["event_start"]),
            caption=row["caption"],
            ordered_video_paths=tuple(
                Path(path) for path in json.loads(row["ordered_video_paths"])
            ),
            send_mode=LessonSendMode(row["send_mode"]),
            telegram_album_group_id=row["telegram_album_group_id"],
            telegram_message_ids=tuple(json.loads(row["telegram_message_ids"])),
            status=SendStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            sent_at=(
                datetime.fromisoformat(row["sent_at"])
                if row["sent_at"] is not None
                else None
            ),
            album_deliveries=tuple(
                AlbumDelivery(
                    album_number=item["album_number"],
                    album_count=item["album_count"],
                    telegram_album_group_id=item["telegram_album_group_id"],
                    telegram_message_ids=tuple(item["telegram_message_ids"]),
                )
                for item in deliveries_data
            ),
            details=(
                LessonDetails(**json.loads(row["lesson_details"]))
                if row["lesson_details"] is not None
                else None
            ),
            calendar_snapshot=(
                CalendarEventSnapshot(**json.loads(row["calendar_snapshot"]))
                if row["calendar_snapshot"] is not None
                else None
            ),
        )
