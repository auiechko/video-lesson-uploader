from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .models import AlbumDelivery, Lesson, SendStatus


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
                    sent_at TEXT
                )
                """)

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
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                    INSERT INTO send_items (
                        identity, profile_id, batch_id, calendar_event_id, event_start,
                        caption, ordered_video_paths, video_count,
                        telegram_album_group_id, telegram_message_ids,
                        album_deliveries, status, created_at, sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        sent_at=excluded.sent_at
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
        )
