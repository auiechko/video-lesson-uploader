from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from lesson_video_uploader.models import AlbumDelivery, Lesson, SendStatus
from lesson_video_uploader.persistence import SQLiteSendItemRepository


class PersistenceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
