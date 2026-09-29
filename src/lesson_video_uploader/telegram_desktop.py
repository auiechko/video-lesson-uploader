from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
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
from .telegram_runtime import create_telegram_client


class TelegramLoginRequired(RuntimeError):
    pass


class TelegramConnectionUnavailable(RuntimeError):
    pass


class LoginResult(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
    CANCELLED = "CANCELLED"


_CODE_DELIVERY = {
    "App": "у застосунок Telegram — чат «Telegram» на іншому пристрої, "
    "де ви вже увійшли",
    "Sms": "SMS",
    "SmsWord": "SMS (слово замість цифр)",
    "SmsPhrase": "SMS (фраза замість цифр)",
    "FirebaseSms": "SMS",
    "FragmentSms": "SMS через Fragment",
    "Call": "голосовим дзвінком",
    "FlashCall": "дзвінком-скиданням (код — цифри номера, що дзвонив)",
    "MissedCall": "пропущеним дзвінком (код — останні цифри номера)",
    "EmailCode": "на пошту",
    "SetUpEmailRequired": "нікуди: Telegram вимагає спершу прив'язати "
    "пошту для входу в офіційному застосунку",
}


def describe_code_type(code_type: object | None) -> str | None:
    """Human-readable channel for auth.SentCodeType*/auth.CodeType*."""
    if code_type is None:
        return None
    name = type(code_type).__name__
    key = name.removeprefix("SentCodeType").removeprefix("CodeType")
    description = _CODE_DELIVERY.get(key)
    if description is None:
        return f"невідомим способом ({name})"
    email_pattern = getattr(code_type, "email_pattern", None)
    if email_pattern:
        description = f"{description} {email_pattern}"
    return description


@dataclass(frozen=True)
class CodeRequest:
    phone_code_hash: str
    delivery: str
    next_delivery: str | None = None


def _code_request(sent: Any) -> CodeRequest:
    if type(sent).__name__ == "SentCodePaymentRequired":
        raise ValueError(
            "Telegram вимагає оплату за надсилання коду на цей номер. "
            "Увійдіть через офіційний застосунок Telegram і повторіть."
        )
    return CodeRequest(
        phone_code_hash=sent.phone_code_hash,
        delivery=(
            describe_code_type(getattr(sent, "type", None))
            or "невідомим способом"
        ),
        next_delivery=describe_code_type(getattr(sent, "next_type", None)),
    )


class DesktopTelethonClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def is_user_authorized(self) -> bool: ...
    async def get_entity(self, entity: object) -> object: ...


ClientFactory = Callable[..., DesktopTelethonClient]
ConnectedOperation = Callable[[], Awaitable[Any]]


def _is_disconnected_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return (
        "cannot send requests while disconnected" in message
        or "not connected" in message
        or "connection is closed" in message
    )


async def _safe_disconnect(client: DesktopTelethonClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


async def _ensure_connected(client: DesktopTelethonClient) -> None:
    if client.is_connected():
        return
    try:
        await client.connect()
    except Exception as error:
        raise TelegramConnectionUnavailable(
            "Не вдалося підключитися до Telegram. Перевірте інтернет "
            "і повторіть дію."
        ) from error
    if not client.is_connected():
        raise TelegramConnectionUnavailable(
            "Telegram не встановив з’єднання. Перевірте інтернет "
            "і повторіть дію."
        )


async def _run_connected(
    client: DesktopTelethonClient,
    operation: ConnectedOperation,
) -> Any:
    """Retry one safe pre-upload Telegram request after reconnecting."""
    for attempt in range(2):
        await _ensure_connected(client)
        try:
            return await operation()
        except Exception as error:
            if not _is_disconnected_error(error):
                raise
            if attempt == 1:
                raise TelegramConnectionUnavailable(
                    "З’єднання з Telegram втрачено. Перевірте інтернет "
                    "і повторіть дію."
                ) from error
            await _safe_disconnect(client)
    raise AssertionError("unreachable")


def telethon_components() -> tuple[ClientFactory, type[BaseException]]:
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Telethon не встановлено. Повторіть: .venv\\Scripts\\python.exe -m pip install -e ."
        ) from error
    return TelegramClient, SessionPasswordNeededError


class TelegramAuthService:
    def __init__(
        self,
        client_factory: ClientFactory,
        *,
        password_required_error: type[BaseException],
    ) -> None:
        self.client_factory = client_factory
        self.password_required_error = password_required_error

    def _client(
        self,
        config: AppConfig,
        api_hash: str,
        profile_id: str,
    ) -> DesktopTelethonClient:
        return create_telegram_client(
            self.client_factory,
            profile_id=profile_id,
            api_id=config.api_id,
            api_hash=api_hash,
        )

    async def request_code(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        profile_id: str = "main",
    ) -> CodeRequest:
        if not config.phone:
            raise ValueError("Спочатку збережіть номер телефону Telegram")
        client = self._client(config, api_hash, profile_id)
        try:
            sent = await _run_connected(
                client,
                lambda: client.send_code_request(config.phone),  # type: ignore[attr-defined]
            )
            return _code_request(sent)
        finally:
            await _safe_disconnect(client)

    async def resend_code(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        phone_code_hash: str,
        profile_id: str = "main",
    ) -> CodeRequest:
        """Ask Telegram to deliver the pending code via its next channel."""
        from telethon.tl.functions.auth import ResendCodeRequest

        client = self._client(config, api_hash, profile_id)
        try:
            sent = await _run_connected(
                client,
                lambda: client(  # type: ignore[operator]
                    ResendCodeRequest(config.phone, phone_code_hash)
                ),
            )
            return _code_request(sent)
        finally:
            await _safe_disconnect(client)

    async def qr_login(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        on_qr: Callable[[str], object],
        cancelled: Callable[[], bool] = lambda: False,
        profile_id: str = "main",
        max_wait: float = 300.0,
        poll_interval: float = 0.5,
    ) -> LoginResult:
        """Log in by scanning a tg://login QR code from another device.

        ``on_qr`` receives every new QR URL (tokens expire in ~30 s and are
        recreated). ``cancelled`` is polled so the UI can abort the wait.
        """
        client = self._client(config, api_hash, profile_id)
        try:
            if await _run_connected(client, client.is_user_authorized):
                return LoginResult.AUTHORIZED
            qr = await _run_connected(client, client.qr_login)  # type: ignore[attr-defined]
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max_wait
            while True:
                on_qr(qr.url)
                waiter = asyncio.ensure_future(qr.wait())
                while not waiter.done():
                    if cancelled() or loop.time() >= deadline:
                        waiter.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await waiter
                        if cancelled():
                            return LoginResult.CANCELLED
                        raise TimeoutError(
                            "QR-код не відскановано вчасно. "
                            "Спробуйте ще раз."
                        )
                    await asyncio.wait({waiter}, timeout=poll_interval)
                try:
                    waiter.result()
                except asyncio.TimeoutError:
                    await qr.recreate()
                    continue
                except self.password_required_error:
                    return LoginResult.PASSWORD_REQUIRED
                return LoginResult.AUTHORIZED
        finally:
            await _safe_disconnect(client)

    async def verify_code(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        code: str,
        phone_code_hash: str,
        profile_id: str = "main",
    ) -> LoginResult:
        client = self._client(config, api_hash, profile_id)
        try:
            try:
                await _run_connected(
                    client,
                    lambda: client.sign_in(  # type: ignore[attr-defined]
                        phone=config.phone,
                        code=code.strip(),
                        phone_code_hash=phone_code_hash,
                    ),
                )
            except self.password_required_error:
                return LoginResult.PASSWORD_REQUIRED
            return LoginResult.AUTHORIZED
        finally:
            await _safe_disconnect(client)

    async def verify_password(
        self,
        config: AppConfig,
        api_hash: str,
        *,
        password: str,
        profile_id: str = "main",
    ) -> LoginResult:
        client = self._client(config, api_hash, profile_id)
        try:
            await _run_connected(
                client,
                lambda: client.sign_in(password=password),  # type: ignore[attr-defined]
            )
            return LoginResult.AUTHORIZED
        finally:
            await _safe_disconnect(client)


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

    def _client(
        self,
        config: AppConfig,
        api_hash: str,
        profile_id: str,
    ) -> DesktopTelethonClient:
        return create_telegram_client(
            self.client_factory,
            profile_id=profile_id,
            api_id=config.api_id,
            api_hash=api_hash,
        )

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
        repository = SQLiteSendItemRepository(self.database_path)
        blocked_lessons = [
            current
            for lesson in manifest.lessons
            if (
                (current := repository.get(lesson.identity))
                is not None
                and current.status in {
                    SendStatus.UPLOADING,
                    SendStatus.DELIVERY_UNKNOWN,
                    SendStatus.PARTIALLY_CONFIRMED,
                }
            )
        ]
        if blocked_lessons:
            first = blocked_lessons[0]
            raise ManualReviewRequired(
                "Надсилання заблоковано: "
                f"{len(blocked_lessons)} урок(и) мають невідому або "
                "незавершену доставку. Натисніть «Перевірити невідому "
                "доставку», а для повного повторного надсилання — "
                f"«Скинути всю історію пакета». Перший урок: {first.caption}"
            )
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
                for lesson in manifest.lessons:
                    repository.save(replace(
                        lesson,
                        status=SendStatus.BATCH_REVALIDATION_REQUIRED,
                    ))
                raise
        client = self._client(config, api_hash, manifest.profile_id)
        try:
            if not await _run_connected(
                client,
                client.is_user_authorized,
            ):
                raise TelegramLoginRequired(
                    "Спочатку натисніть «Увійти в Telegram»"
                )
            try:
                await _run_connected(
                    client,
                    lambda: client.get_entity(target_peer),
                )
            except TelegramConnectionUnavailable:
                raise
            except Exception as error:
                raise ValueError(
                    f"Telegram-чат недоступний: {target_peer}"
                ) from error
            sender = TelethonLessonSender(
                client,  # type: ignore[arg-type]
                repository,
                album_batch_template=config.album_batch,
            )
            results: list[Lesson] = []
            for lesson in manifest.lessons:
                existing = repository.get(lesson.identity)
                if (
                    status_callback
                    and existing is not None
                    and existing.status is SendStatus.SENT
                ):
                    status_callback(
                        f"Пропущено, вже надіслано: {lesson.caption}"
                    )
                await _ensure_connected(client)
                if status_callback and (
                    existing is None
                    or existing.status is not SendStatus.SENT
                ):
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
            await _safe_disconnect(client)

    async def reconcile_manifest(
        self,
        manifest: UploadManifest,
        config: AppConfig,
        api_hash: str,
        *,
        target_peer: object,
        status_callback: StatusCallback | None = None,
    ) -> tuple[Lesson, ...]:
        repository = SQLiteSendItemRepository(self.database_path)
        current_lessons = tuple(
            repository.get(lesson.identity) or lesson
            for lesson in manifest.lessons
        )
        uncertain_statuses = {
            SendStatus.UPLOADING,
            SendStatus.DELIVERY_UNKNOWN,
            SendStatus.PARTIALLY_CONFIRMED,
        }
        if not any(
            lesson.status in uncertain_statuses
            for lesson in current_lessons
        ):
            if status_callback:
                for lesson in current_lessons:
                    status_callback(
                        f"{lesson.caption}: {lesson.status.value}"
                    )
            return current_lessons
        client = self._client(config, api_hash, manifest.profile_id)
        try:
            if not await _run_connected(
                client,
                client.is_user_authorized,
            ):
                raise TelegramLoginRequired(
                    "Спочатку натисніть «Увійти в Telegram»"
                )
            reconciler = TelegramDeliveryReconciler(
                client,  # type: ignore[arg-type]
                repository,
                album_batch_template=config.album_batch,
            )
            results: list[Lesson] = []
            for lesson in manifest.lessons:
                result = await _run_connected(
                    client,
                    partial(
                        reconciler.reconcile,
                        lesson,
                        target_peer=target_peer,
                    ),
                )
                results.append(result)
                if status_callback:
                    status_callback(f"{lesson.caption}: {result.status.value}")
            return tuple(results)
        finally:
            await _safe_disconnect(client)
