from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .calendar_rules import (
    BatchRevalidationRequired,
    CalendarEventSnapshot,
)
from .config import AppConfig
from .manifest import UploadManifest
from .models import Lesson, SendStatus
from .persistence import SQLiteSendItemRepository
from .reconciliation import TelegramDeliveryReconciler
from .sender import ManualReviewRequired, TelethonLessonSender


class TelegramLoginRequired(RuntimeError):
    pass


class LoginResult(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    PASSWORD_REQUIRED = "PASSWORD_REQUIRED"


class DesktopTelethonClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_user_authorized(self) -> bool: ...


ClientFactory = Callable[[str, int, str], DesktopTelethonClient]


def telethon_components() -> tuple[ClientFactory, type[BaseException]]:
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Telethon не встановлено. Повторіть: .venv\\Scripts\\python.exe -m pip install -e ."
        ) from error
    return TelegramClient, SessionPasswordNeededError


def _validate_credentials(config: AppConfig, api_hash: str) -> tuple[int, str]:
    if config.api_id is None or config.api_id <= 0:
        raise ValueError("Спочатку збережіть Telegram API ID")
    if not api_hash:
        raise ValueError("Спочатку збережіть Telegram API hash")
    return config.api_id, api_hash


def _ensure_session_parent(session: str) -> None:
    parent = Path(session).expanduser().parent
    if parent != Path("."):
        parent.mkdir(parents=True, exist_ok=True)


class TelegramAuthService:
    def __init__(
        self,
        client_factory: ClientFactory,
        *,
        password_required_error: type[BaseException],
    ) -> None:
        self.client_factory = client_factory
        self.password_required_error = password_required_error

    def _client(self, config: AppConfig, api_hash: str) -> DesktopTelethonClient:
        api_id, secret = _validate_credentials(config, api_hash)
        _ensure_session_parent(config.session)
        return self.client_factory(config.session, api_id, secret)

    async def request_code(self, config: AppConfig, api_hash: str) -> str:
        if not config.phone:
            raise ValueError("Спочатку збережіть номер телефону Telegram")
        client = self._client(config, api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(config.phone)  # type: ignore[attr-defined]
            return sent.phone_code_hash
        finally:
            await client.disconnect()

    async def verify_code(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        code: str,
        phone_code_hash: str,
    ) -> LoginResult:
        client = self._client(config, api_hash)
        await client.connect()
        try:
            try:
                await client.sign_in(  # type: ignore[attr-defined]
                    phone=config.phone,
                    code=code.strip(),
                    phone_code_hash=phone_code_hash,
                )
            except self.password_required_error:
                return LoginResult.PASSWORD_REQUIRED
            return LoginResult.AUTHORIZED
        finally:
            await client.disconnect()

    async def verify_password(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        password: str,
    ) -> LoginResult:
        client = self._client(config, api_hash)
        await client.connect()
        try:
            await client.sign_in(password=password)  # type: ignore[attr-defined]
            return LoginResult.AUTHORIZED
        finally:
            await client.disconnect()


StatusCallback = Callable[[str], object]
ProgressCallback = Callable[[int, int], object]
CalendarRevalidator = Callable[
    [Mapping[str, CalendarEventSnapshot]],
    Awaitable[None],
]


class TelegramDesktopService:
    def __init__(
        self,
        client_factory: ClientFactory,
        database_path: Path,
    ) -> None:
        self.client_factory = client_factory
        self.database_path = database_path

    def _client(self, config: AppConfig, api_hash: str) -> DesktopTelethonClient:
        api_id, secret = _validate_credentials(config, api_hash)
        _ensure_session_parent(config.session)
        return self.client_factory(config.session, api_id, secret)

    async def send_manifest(
        self,
        manifest: UploadManifest,
        config: AppConfig,
        api_hash: str,
        *,
        target_peer: object,
        status_callback: StatusCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        calendar_revalidator: CalendarRevalidator | None = None,
    ) -> tuple[Lesson, ...]:
        snapshots = {
            lesson.calendar_event_id: lesson.calendar_snapshot
            for lesson in manifest.lessons
            if lesson.calendar_snapshot is not None
        }
        if snapshots:
            try:
                if calendar_revalidator is None:
                    raise BatchRevalidationRequired({
                        event_id: {
                            "validation": (
                                "Calendar snapshot",
                                "revalidator is not configured",
                            )
                        }
                        for event_id in snapshots
                    })
                await calendar_revalidator(snapshots)
            except BatchRevalidationRequired:
                repository = SQLiteSendItemRepository(self.database_path)
                for lesson in manifest.lessons:
                    repository.save(replace(
                        lesson,
                        status=SendStatus.BATCH_REVALIDATION_REQUIRED,
                    ))
                raise
        client = self._client(config, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramLoginRequired(
                    "Спочатку натисніть «Увійти в Telegram»"
                )
            repository = SQLiteSendItemRepository(self.database_path)
            sender = TelethonLessonSender(
                client,  # type: ignore[arg-type]
                repository,
                album_batch_template=config.album_batch,
            )
            results: list[Lesson] = []
            for lesson in manifest.lessons:
                if status_callback:
                    status_callback(f"Надсилання: {lesson.caption}")
                result = await sender.send(
                    lesson,
                    target_peer=target_peer,
                    progress_callback=progress_callback,
                    album_split_warning=(
                        (
                            lambda caption, count: status_callback(
                                f"Увага: {caption} буде розділено на {count} альбоми"
                            )
                        )
                        if status_callback
                        else None
                    ),
                )
                if result.status is not SendStatus.SENT:
                    raise ManualReviewRequired(
                        f"Статус {result.status.value}; автоматичний повтор заборонено"
                    )
                results.append(result)
            return tuple(results)
        finally:
            await client.disconnect()

    async def reconcile_manifest(
        self,
        manifest: UploadManifest,
        config: AppConfig,
        api_hash: str,
        *,
        target_peer: object,
        status_callback: StatusCallback | None = None,
    ) -> tuple[Lesson, ...]:
        client = self._client(config, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramLoginRequired(
                    "Спочатку натисніть «Увійти в Telegram»"
                )
            repository = SQLiteSendItemRepository(self.database_path)
            reconciler = TelegramDeliveryReconciler(
                client,  # type: ignore[arg-type]
                repository,
                album_batch_template=config.album_batch,
            )
            results: list[Lesson] = []
            for lesson in manifest.lessons:
                result = await reconciler.reconcile(
                    lesson,
                    target_peer=target_peer,
                )
                results.append(result)
                if status_callback:
                    status_callback(f"{lesson.caption}: {result.status.value}")
            return tuple(results)
        finally:
            await client.disconnect()
