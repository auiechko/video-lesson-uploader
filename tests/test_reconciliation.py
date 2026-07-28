from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lesson_video_uploader.models import Lesson, LessonSendMode, SendStatus
from lesson_video_uploader.persistence import SQLiteSendItemRepository
from lesson_video_uploader.reconciliation import TelegramDeliveryReconciler


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_message_is_reconciled_by_exact_caption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSendItemRepository(
                Path(directory) / "deliveries.sqlite3"
            )
            lesson = Lesson(
                profile_id="main",
                batch_id="batch",
                calendar_event_id="text-event",
                event_start=datetime(2026, 6, 12, 10),
                caption=(
                    "12.06.2026 105813989 Ільяс "
                    "10р індив (без запису)"
                ),
                ordered_video_paths=(),
                send_mode=LessonSendMode.TEXT_ONLY,
                status=SendStatus.DELIVERY_UNKNOWN,
            )
            repository.save(lesson)
            message = SimpleNamespace(
                id=501,
                grouped_id=None,
                message=lesson.caption,
                date=datetime.now(timezone.utc),
                file=None,
            )
            client = SimpleNamespace(
                get_messages=AsyncMock(return_value=[message])
            )
            reconciler = TelegramDeliveryReconciler(client, repository)

            result = await reconciler.reconcile(
                lesson,
                target_peer="group",
            )

        self.assertEqual(result.status, SendStatus.SENT)
        self.assertEqual(result.telegram_message_ids, (501,))

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.paths = (root / "one.mp4", root / "two.mp4", root / "three.mp4")
        for index, path in enumerate(self.paths, start=1):
            path.write_bytes(b"x" * index)
        self.lesson = Lesson(
            profile_id="profile-1",
            batch_id="batch-1",
            calendar_event_id="event-1",
            event_start=datetime(2026, 6, 12, 10),
            caption="12.06.2026 105813989 Ільяс 10р індив",
            ordered_video_paths=self.paths,
            status=SendStatus.DELIVERY_UNKNOWN,
        )
        self.repository = SQLiteSendItemRepository(root / "deliveries.sqlite3")
        self.repository.save(self.lesson)
        self.client = SimpleNamespace(get_messages=AsyncMock())

    def message(
        self,
        message_id: int,
        path: Path,
        *,
        grouped_id: int = 777,
        caption: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            grouped_id=grouped_id,
            message=caption,
            date=datetime.now(timezone.utc),
            file=SimpleNamespace(name=path.name, size=path.stat().st_size),
        )

    async def test_exact_album_match_marks_lesson_sent_with_all_ids(self) -> None:
        self.client.get_messages.return_value = [
            self.message(103, self.paths[2]),
            self.message(102, self.paths[1]),
            self.message(101, self.paths[0], caption=self.lesson.caption),
        ]
        reconciler = TelegramDeliveryReconciler(self.client, self.repository)

        result = await reconciler.reconcile(self.lesson, target_peer="group")

        self.client.get_messages.assert_awaited_once_with("group", limit=100)
        self.assertEqual(result.status, SendStatus.SENT)
        self.assertEqual(result.telegram_message_ids, (101, 102, 103))
        self.assertEqual(result.telegram_album_group_id, 777)

    async def test_incomplete_album_stays_partially_confirmed(self) -> None:
        self.client.get_messages.return_value = [
            self.message(102, self.paths[1]),
            self.message(101, self.paths[0], caption=self.lesson.caption),
        ]
        reconciler = TelegramDeliveryReconciler(self.client, self.repository)

        result = await reconciler.reconcile(self.lesson, target_peer="group")

        self.assertEqual(result.status, SendStatus.PARTIALLY_CONFIRMED)
        self.assertEqual(result.telegram_message_ids, (101, 102))

    async def test_ambiguous_duplicate_album_is_not_assumed_sent(self) -> None:
        first = [
            self.message(101 + index, path, grouped_id=777, caption=(
                self.lesson.caption if index == 0 else ""
            ))
            for index, path in enumerate(self.paths)
        ]
        second = [
            self.message(201 + index, path, grouped_id=888, caption=(
                self.lesson.caption if index == 0 else ""
            ))
            for index, path in enumerate(self.paths)
        ]
        self.client.get_messages.return_value = first + second
        reconciler = TelegramDeliveryReconciler(self.client, self.repository)

        result = await reconciler.reconcile(self.lesson, target_peer="group")

        self.assertEqual(result.status, SendStatus.DELIVERY_UNKNOWN)
        self.assertEqual(result.telegram_message_ids, ())

    async def test_messages_with_wrong_sizes_are_not_confirmed(self) -> None:
        messages = [
            self.message(101 + index, path, caption=(
                self.lesson.caption if index == 0 else ""
            ))
            for index, path in enumerate(self.paths)
        ]
        messages[1].file.size = 999
        self.client.get_messages.return_value = messages
        reconciler = TelegramDeliveryReconciler(self.client, self.repository)

        result = await reconciler.reconcile(self.lesson, target_peer="group")

        self.assertEqual(result.status, SendStatus.DELIVERY_UNKNOWN)

    async def test_missing_metadata_cannot_create_a_false_exact_match(self) -> None:
        for path in self.paths:
            path.unlink()
        self.client.get_messages.return_value = [
            SimpleNamespace(
                id=101 + index,
                grouped_id=777,
                message=self.lesson.caption if index == 0 else "",
                date=datetime.now(timezone.utc),
                file=None,
            )
            for index in range(3)
        ]
        reconciler = TelegramDeliveryReconciler(self.client, self.repository)

        result = await reconciler.reconcile(self.lesson, target_peer="group")

        self.assertEqual(result.status, SendStatus.DELIVERY_UNKNOWN)

    async def test_sent_lesson_is_returned_without_another_lookup(self) -> None:
        sent = replace(
            self.lesson,
            status=SendStatus.SENT,
            telegram_message_ids=(101, 102, 103),
        )
        self.repository.save(sent)
        reconciler = TelegramDeliveryReconciler(self.client, self.repository)

        result = await reconciler.reconcile(sent, target_peer="group")

        self.assertEqual(result, sent)
        self.client.get_messages.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
