from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from lesson_video_uploader.calendar_rules import (
    BatchRevalidationRequired,
    CalendarEventSnapshot,
)
from lesson_video_uploader.config import AppConfig
from lesson_video_uploader.manifest import UploadManifest
from lesson_video_uploader.models import Lesson, SendStatus
from lesson_video_uploader.persistence import SQLiteSendItemRepository
from lesson_video_uploader.telegram_desktop import (
    LoginResult,
    TelegramAuthService,
    TelegramConnectionUnavailable,
    TelegramDesktopService,
    TelegramLoginRequired,
)


class PasswordNeeded(Exception):
    pass


class FakeClient:
    def __init__(self) -> None:
        self.connected = False

        async def connect() -> None:
            self.connected = True

        async def disconnect() -> None:
            self.connected = False

        self.connect = AsyncMock(side_effect=connect)
        self.disconnect = AsyncMock(side_effect=disconnect)
        self.is_connected = Mock(side_effect=lambda: self.connected)
        self.is_user_authorized = AsyncMock(return_value=True)
        self.send_code_request = AsyncMock(
            return_value=SimpleNamespace(phone_code_hash="phone-hash")
        )
        self.sign_in = AsyncMock()
        self.send_file = AsyncMock()
        self.get_messages = AsyncMock(return_value=[])
        self.get_entity = AsyncMock(return_value=SimpleNamespace(id=999))


class TelegramAuthServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_code_returns_hash_and_always_disconnects(self) -> None:
        client = FakeClient()
        service = TelegramAuthService(
            lambda **_: client,
            password_required_error=PasswordNeeded,
        )

        result = await service.request_code(
            AppConfig(api_id=123, phone="+380991234567"),
            "api-hash",
        )

        self.assertEqual(result, "phone-hash")
        client.send_code_request.assert_awaited_once_with("+380991234567")
        client.disconnect.assert_awaited_once()

    async def test_verify_code_reports_two_factor_requirement(self) -> None:
        client = FakeClient()
        client.sign_in.side_effect = PasswordNeeded()
        service = TelegramAuthService(
            lambda **_: client,
            password_required_error=PasswordNeeded,
        )

        result = await service.verify_code(
            AppConfig(api_id=123, phone="+380991234567"),
            "api-hash",
            code="12345",
            phone_code_hash="phone-hash",
        )

        self.assertEqual(result, LoginResult.PASSWORD_REQUIRED)
        client.disconnect.assert_awaited_once()

    async def test_verify_password_returns_authorized(self) -> None:
        client = FakeClient()
        service = TelegramAuthService(
            lambda **_: client,
            password_required_error=PasswordNeeded,
        )

        result = await service.verify_password(
            AppConfig(api_id=123, phone="+380991234567"),
            "api-hash",
            password="2fa-secret",
        )

        self.assertEqual(result, LoginResult.AUTHORIZED)
        client.sign_in.assert_awaited_once_with(password="2fa-secret")


class TelegramDesktopServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        paths = (root / "one.mp4", root / "two.mp4")
        for path in paths:
            path.touch()
        self.lesson = Lesson(
            profile_id="main",
            batch_id="batch",
            calendar_event_id="event",
            event_start=datetime(2026, 6, 12, 10),
            caption="12.06.2026 1 Ільяс урок",
            ordered_video_paths=paths,
        )
        self.manifest = UploadManifest(
            profile_id="main",
            batch_id="batch",
            target_peer="me",
            lessons=(self.lesson,),
        )
        self.client = FakeClient()
        self.config = AppConfig(api_id=123)

    async def test_send_requires_prior_gui_login(self) -> None:
        self.client.is_user_authorized.return_value = False
        service = TelegramDesktopService(
            lambda **_: self.client,
            Path(self.temp_dir.name) / "db.sqlite3",
        )

        with self.assertRaises(TelegramLoginRequired):
            await service.send_manifest(
                self.manifest,
                self.config,
                "api-hash",
                target_peer="me",
            )

        self.client.send_file.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_send_returns_sent_lesson_and_uses_gui_target_override(self) -> None:
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
        ]
        service = TelegramDesktopService(
            lambda **_: self.client,
            Path(self.temp_dir.name) / "db.sqlite3",
        )

        results = await service.send_manifest(
            self.manifest,
            self.config,
            "api-hash",
            target_peer=-100999,
        )

        self.assertEqual(results[0].status, SendStatus.SENT)
        self.assertEqual(
            self.client.send_file.await_args.kwargs["entity"],
            -100999,
        )
        self.client.get_entity.assert_awaited_once_with(-100999)
        self.client.disconnect.assert_awaited_once()

    async def test_unavailable_target_blocks_upload(self) -> None:
        self.client.get_entity.side_effect = ValueError("chat not found")
        service = TelegramDesktopService(
            lambda **_: self.client,
            Path(self.temp_dir.name) / "db.sqlite3",
        )

        with self.assertRaisesRegex(
            ValueError,
            "Telegram-чат недоступний",
        ):
            await service.send_manifest(
                self.manifest,
                self.config,
                "api-hash",
                target_peer="missing-chat",
            )

        self.client.send_file.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_disconnected_preflight_request_reconnects_once(self) -> None:
        self.client.is_user_authorized.side_effect = [
            ConnectionError("Cannot send requests while disconnected"),
            True,
        ]
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
        ]
        service = TelegramDesktopService(
            lambda **_: self.client,
            Path(self.temp_dir.name) / "db.sqlite3",
        )

        results = await service.send_manifest(
            self.manifest,
            self.config,
            "api-hash",
            target_peer="me",
        )

        self.assertEqual(results[0].status, SendStatus.SENT)
        self.assertEqual(self.client.connect.await_count, 2)
        self.assertEqual(self.client.is_user_authorized.await_count, 2)
        self.client.send_file.assert_awaited_once()

    async def test_repeated_disconnect_uses_clear_ukrainian_error(self) -> None:
        self.client.is_user_authorized.side_effect = ConnectionError(
            "Cannot send requests while disconnected"
        )
        service = TelegramDesktopService(
            lambda **_: self.client,
            Path(self.temp_dir.name) / "db.sqlite3",
        )

        with self.assertRaisesRegex(
            TelegramConnectionUnavailable,
            "З’єднання з Telegram втрачено",
        ):
            await service.send_manifest(
                self.manifest,
                self.config,
                "api-hash",
                target_peer="me",
            )

        self.assertEqual(self.client.connect.await_count, 2)
        self.client.send_file.assert_not_awaited()

    async def test_calendar_change_blocks_batch_before_telegram_upload(self) -> None:
        snapshot = CalendarEventSnapshot(
            event_id="event",
            summary="105813989 Сервер Османов (Ільяс 10) Учко ТГ",
            student_id="105813989",
            student_name="Ільяс",
            student_age=10,
            local_date="2026-06-12",
            start="2026-06-12T10:00:00+03:00",
            end="2026-06-12T11:00:00+03:00",
            duration_minutes=60,
            status="NORMAL",
            is_trial=False,
            is_no_recording=False,
            is_transferred=False,
            is_cancelled=False,
            is_pause=False,
        )
        lesson = replace(self.lesson, calendar_snapshot=snapshot)
        manifest = UploadManifest("main", "batch", "me", (lesson,))
        revalidation_error = BatchRevalidationRequired({
            "event": {"status": ("NORMAL", "IGNORED_CANCELLED")}
        })
        revalidator = AsyncMock(side_effect=revalidation_error)
        database = Path(self.temp_dir.name) / "db.sqlite3"
        service = TelegramDesktopService(lambda **_: self.client, database)

        with self.assertRaises(BatchRevalidationRequired):
            await service.send_manifest(
                manifest,
                self.config,
                "api-hash",
                target_peer="me",
                calendar_revalidator=revalidator,
            )

        self.client.connect.assert_not_awaited()
        self.client.send_file.assert_not_awaited()
        saved = SQLiteSendItemRepository(database).get(lesson.identity)
        self.assertEqual(
            saved.status,
            SendStatus.BATCH_REVALIDATION_REQUIRED,
        )

    async def test_unchanged_calendar_is_revalidated_before_upload(self) -> None:
        snapshot = CalendarEventSnapshot(
            event_id="event",
            summary="105813989 Сервер Османов (Ільяс 10) Учко ТГ",
            student_id="105813989",
            student_name="Ільяс",
            student_age=10,
            local_date="2026-06-12",
            start="2026-06-12T10:00:00+03:00",
            end="2026-06-12T11:00:00+03:00",
            duration_minutes=60,
            status="NORMAL",
            is_trial=False,
            is_no_recording=False,
            is_transferred=False,
            is_cancelled=False,
            is_pause=False,
        )
        lesson = replace(self.lesson, calendar_snapshot=snapshot)
        manifest = UploadManifest("main", "batch", "me", (lesson,))
        revalidator = AsyncMock(return_value=None)
        self.client.send_file.return_value = [
            SimpleNamespace(id=101, grouped_id=777),
            SimpleNamespace(id=102, grouped_id=777),
        ]
        service = TelegramDesktopService(
            lambda **_: self.client,
            Path(self.temp_dir.name) / "db.sqlite3",
        )

        await service.send_manifest(
            manifest,
            self.config,
            "api-hash",
            target_peer="me",
            calendar_revalidator=revalidator,
        )

        revalidator.assert_awaited_once_with({"event": snapshot})
        self.client.send_file.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
