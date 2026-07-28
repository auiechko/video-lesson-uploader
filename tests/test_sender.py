from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lesson_video_uploader.models import Lesson, SendStatus
from lesson_video_uploader.persistence import SQLiteSendItemRepository
from lesson_video_uploader.sender import ManualReviewRequired, TelethonLessonSender


def lesson(count: int = 3) -> Lesson:
    return Lesson(
        profile_id="profile-1",
        batch_id="batch-1",
        calendar_event_id="event-1",
        event_start=datetime(2026, 6, 12, 10),
        caption="12.06.2026 105813989 Ільяс 10р індив",
        ordered_video_paths=tuple(Path(f"video-{number}.mp4") for number in range(count)),
    )


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repository = SQLiteSendItemRepository(
            Path(self.temp_dir.name) / "uploader.sqlite3"
        )
        self.client = SimpleNamespace(send_file=AsyncMock())

    async def test_album_is_sent_in_one_telethon_call_and_all_ids_are_saved(self) -> None:
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
            SimpleNamespace(id=103, grouped_id=777),
        ]
        sender = TelethonLessonSender(self.client, self.repository)

        result = await sender.send(lesson(), target_peer=-100123)

        self.client.send_file.assert_awaited_once()
        call = self.client.send_file.await_args.kwargs
        self.assertEqual(call["entity"], -100123)
        self.assertEqual(call["file"], ["video-0.mp4", "video-1.mp4", "video-2.mp4"])
        self.assertEqual(call["caption"], "12.06.2026 105813989 Ільяс 10р індив")
        self.assertTrue(call["supports_streaming"])
        self.assertEqual(result.status, SendStatus.SENT)
        self.assertEqual(result.telegram_message_ids, (101, 102, 103))
        self.assertEqual(result.album_deliveries[0].telegram_album_group_id, 777)

    async def test_single_video_is_passed_as_regular_media_file(self) -> None:
        self.client.send_file.return_value = SimpleNamespace(id=101, grouped_id=None)
        sender = TelethonLessonSender(self.client, self.repository)

        await sender.send(lesson(1), target_peer="group")

        self.assertEqual(self.client.send_file.await_args.kwargs["file"], "video-0.mp4")

    async def test_eleven_videos_use_two_calls_and_collect_every_message_id(self) -> None:
        self.client.send_file.side_effect = [
            [SimpleNamespace(id=index, grouped_id=1) for index in range(1, 11)],
            SimpleNamespace(id=11, grouped_id=None),
        ]
        sender = TelethonLessonSender(self.client, self.repository)

        result = await sender.send(lesson(11), target_peer="group")

        self.assertEqual(self.client.send_file.await_count, 2)
        self.assertEqual(
            self.client.send_file.await_args_list[0].kwargs["caption"],
            "12.06.2026 105813989 Ільяс 10р індив (альбом 1/2)",
        )
        self.assertEqual(
            self.client.send_file.await_args_list[1].kwargs["caption"],
            "12.06.2026 105813989 Ільяс 10р індив (альбом 2/2)",
        )
        self.assertEqual(result.telegram_message_ids, tuple(range(1, 12)))
        self.assertEqual(result.status, SendStatus.SENT)

    async def test_eleven_videos_warn_before_any_telegram_call(self) -> None:
        calls: list[tuple[str, int]] = []
        self.client.send_file.side_effect = [
            [SimpleNamespace(id=index, grouped_id=1) for index in range(1, 11)],
            SimpleNamespace(id=11, grouped_id=None),
        ]
        sender = TelethonLessonSender(self.client, self.repository)

        await sender.send(
            lesson(11),
            target_peer="group",
            album_split_warning=lambda caption, count: calls.append((caption, count)),
        )

        self.assertEqual(
            calls,
            [("12.06.2026 105813989 Ільяс 10р індив", 2)],
        )

    async def test_sender_uses_configured_album_batch_template(self) -> None:
        self.client.send_file.side_effect = [
            [SimpleNamespace(id=index, grouped_id=1) for index in range(1, 11)],
            SimpleNamespace(id=11, grouped_id=None),
        ]
        sender = TelethonLessonSender(
            self.client,
            self.repository,
            album_batch_template=" [пакет {album_number}/{album_count}]",
        )

        await sender.send(lesson(11), target_peer="group")

        self.assertEqual(
            self.client.send_file.await_args_list[0].kwargs["caption"],
            "12.06.2026 105813989 Ільяс 10р індив [пакет 1/2]",
        )

    async def test_partial_result_is_not_marked_sent_and_is_not_retried(self) -> None:
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
        ]
        sender = TelethonLessonSender(self.client, self.repository)
        item = lesson(3)

        result = await sender.send(item, target_peer="group")

        self.assertEqual(result.status, SendStatus.PARTIALLY_CONFIRMED)
        with self.assertRaises(ManualReviewRequired):
            await sender.send(item, target_peer="group")
        self.client.send_file.assert_awaited_once()

    async def test_unknown_delivery_after_exception_is_not_automatically_retried(self) -> None:
        self.client.send_file.side_effect = TimeoutError("connection lost")
        sender = TelethonLessonSender(self.client, self.repository)
        item = lesson(2)

        with self.assertRaises(ManualReviewRequired):
            await sender.send(item, target_peer="group")

        saved = self.repository.get(item.identity)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.status, SendStatus.DELIVERY_UNKNOWN)
        with self.assertRaises(ManualReviewRequired):
            await sender.send(item, target_peer="group")
        self.client.send_file.assert_awaited_once()

    async def test_progress_is_reported_across_the_lesson_not_per_file(self) -> None:
        root = Path(self.temp_dir.name)
        sizes = (100, 300)
        paths = []
        for number, size in enumerate(sizes):
            path = root / f"part-{number}.mp4"
            path.write_bytes(b"x" * size)
            paths.append(path)
        item = Lesson(
            profile_id="profile-1",
            batch_id="batch-1",
            calendar_event_id="event-1",
            event_start=datetime(2026, 6, 12, 10),
            caption="12.06.2026 105813989 Ільяс 10р індив",
            ordered_video_paths=tuple(paths),
        )
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
        ]
        reported: list[tuple[int, int]] = []
        sender = TelethonLessonSender(self.client, self.repository)

        await sender.send(
            item,
            target_peer="group",
            progress_callback=lambda current, total: reported.append((current, total)),
        )

        telethon_callback = self.client.send_file.await_args.kwargs["progress_callback"]
        telethon_callback(50, 100)
        telethon_callback(100, 100)
        telethon_callback(150, 300)

        self.assertEqual(reported, [(50, 400), (100, 400), (250, 400)])

    async def test_successful_item_is_idempotent_on_next_run(self) -> None:
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
        ]
        sender = TelethonLessonSender(self.client, self.repository)
        item = lesson(2)

        first = await sender.send(item, target_peer="group")
        second = await sender.send(item, target_peer="group")

        self.assertEqual(second, first)
        self.client.send_file.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
