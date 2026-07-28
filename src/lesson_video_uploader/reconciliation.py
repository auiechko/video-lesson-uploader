from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .models import AlbumDelivery, Lesson, LessonSendMode, SendStatus
from .persistence import SQLiteSendItemRepository
from .planning import DEFAULT_ALBUM_BATCH_TEMPLATE, AlbumPlan, plan_albums


class MessageReader(Protocol):
    async def get_messages(
        self,
        entity: object,
        *,
        limit: int,
    ) -> Any: ...


class TelegramDeliveryReconciler:
    """Resolve uncertain sends without uploading any media."""

    def __init__(
        self,
        client: MessageReader,
        repository: SQLiteSendItemRepository,
        *,
        album_batch_template: str = DEFAULT_ALBUM_BATCH_TEMPLATE,
        time_tolerance: timedelta = timedelta(hours=2),
    ) -> None:
        self.client = client
        self.repository = repository
        self.album_batch_template = album_batch_template
        self.time_tolerance = time_tolerance

    async def reconcile(
        self,
        lesson: Lesson,
        *,
        target_peer: object,
        message_limit: int = 100,
    ) -> Lesson:
        current = self.repository.get(lesson.identity) or lesson
        if current.status is SendStatus.SENT:
            return current
        messages = list(
            await self.client.get_messages(target_peer, limit=message_limit)
        )
        if current.send_mode is LessonSendMode.TEXT_ONLY:
            return self._reconcile_text_only(current, messages)
        groups = self._group_messages(messages)
        plans = plan_albums(
            current,
            album_batch_template=self.album_batch_template,
        )

        exact_deliveries: list[AlbumDelivery] = []
        partial_deliveries: list[AlbumDelivery] = []
        latest_date: datetime | None = None
        ambiguous = False
        for plan in plans:
            exact = [
                group for group in groups
                if self._is_exact_match(group, plan, current.created_at)
            ]
            if len(exact) > 1:
                ambiguous = True
                break
            if len(exact) == 1:
                delivery = self._delivery(plan, exact[0])
                exact_deliveries.append(delivery)
                latest_date = self._latest_date(exact[0], latest_date)
                continue
            partial_matches = [
                group for group in groups
                if self._is_partial_match(group, plan, current.created_at)
            ]
            if len(partial_matches) == 1:
                partial_deliveries.append(
                    self._delivery(plan, partial_matches[0])
                )
                latest_date = self._latest_date(
                    partial_matches[0],
                    latest_date,
                )
            elif len(partial_matches) > 1:
                ambiguous = True
                break

        if not ambiguous and len(exact_deliveries) == len(plans):
            ids = tuple(
                message_id
                for delivery in exact_deliveries
                for message_id in delivery.telegram_message_ids
            )
            sent = replace(
                current,
                telegram_album_group_id=(
                    exact_deliveries[0].telegram_album_group_id
                    if len(exact_deliveries) == 1
                    else None
                ),
                telegram_message_ids=ids,
                album_deliveries=tuple(exact_deliveries),
                status=SendStatus.SENT,
                sent_at=latest_date or datetime.now(timezone.utc),
            )
            self.repository.save(sent)
            return sent

        found = exact_deliveries + partial_deliveries
        if not ambiguous and found:
            ids = tuple(
                message_id
                for delivery in found
                for message_id in delivery.telegram_message_ids
            )
            partial_lesson = replace(
                current,
                telegram_message_ids=ids,
                album_deliveries=tuple(found),
                status=SendStatus.PARTIALLY_CONFIRMED,
            )
            self.repository.save(partial_lesson)
            return partial_lesson

        unknown = replace(
            current,
            telegram_album_group_id=None,
            telegram_message_ids=(),
            album_deliveries=(),
            status=SendStatus.DELIVERY_UNKNOWN,
        )
        self.repository.save(unknown)
        return unknown

    def _reconcile_text_only(
        self,
        lesson: Lesson,
        messages: list[Any],
    ) -> Lesson:
        matches = []
        for message in messages:
            message_id = getattr(message, "id", None)
            text = getattr(message, "message", "")
            sent_at = getattr(message, "date", None)
            if (
                isinstance(message_id, int)
                and isinstance(text, str)
                and text.strip() == lesson.caption
                and isinstance(sent_at, datetime)
                and abs(sent_at - lesson.created_at) <= self.time_tolerance
            ):
                matches.append(message)
        if len(matches) == 1:
            message = matches[0]
            sent = replace(
                lesson,
                telegram_message_ids=(message.id,),
                status=SendStatus.SENT,
                sent_at=message.date,
            )
            self.repository.save(sent)
            return sent
        unknown = replace(
            lesson,
            telegram_message_ids=(),
            status=SendStatus.DELIVERY_UNKNOWN,
        )
        self.repository.save(unknown)
        return unknown

    @staticmethod
    def _group_messages(messages: list[Any]) -> list[list[Any]]:
        grouped: dict[tuple[str, int], list[Any]] = defaultdict(list)
        for message in messages:
            message_id = getattr(message, "id", None)
            if not isinstance(message_id, int):
                continue
            grouped_id = getattr(message, "grouped_id", None)
            key = (
                ("album", grouped_id)
                if isinstance(grouped_id, int)
                else ("message", message_id)
            )
            grouped[key].append(message)
        return [
            sorted(group, key=lambda message: message.id)
            for group in grouped.values()
        ]

    def _is_exact_match(
        self,
        messages: list[Any],
        plan: AlbumPlan,
        attempted_at: datetime,
    ) -> bool:
        actual = self._file_metadata(messages)
        expected = self._expected_metadata(plan.video_paths)
        return (
            len(messages) == len(plan.video_paths)
            and len(actual) == len(messages)
            and len(expected) == len(plan.video_paths)
            and self._common_fields_match(messages, plan, attempted_at)
            and actual == expected
        )

    def _is_partial_match(
        self,
        messages: list[Any],
        plan: AlbumPlan,
        attempted_at: datetime,
    ) -> bool:
        if not 0 < len(messages) < len(plan.video_paths):
            return False
        if not self._common_fields_match(messages, plan, attempted_at):
            return False
        actual = self._file_metadata(messages)
        expected = self._expected_metadata(plan.video_paths)
        return (
            len(actual) == len(messages)
            and len(expected) == len(plan.video_paths)
            and actual == expected[:len(actual)]
        )

    def _common_fields_match(
        self,
        messages: list[Any],
        plan: AlbumPlan,
        attempted_at: datetime,
    ) -> bool:
        captions = [
            caption for message in messages
            if (caption := self._caption(message))
        ]
        if captions != [plan.caption]:
            return False
        attempted = self._aware(attempted_at)
        dates = [
            self._aware(message.date)
            for message in messages
            if isinstance(getattr(message, "date", None), datetime)
        ]
        return (
            len(dates) == len(messages)
            and all(abs(date - attempted) <= self.time_tolerance for date in dates)
        )

    @staticmethod
    def _caption(message: Any) -> str:
        raw_text = getattr(message, "raw_text", None)
        if isinstance(raw_text, str):
            return raw_text
        value = getattr(message, "message", "")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _file_metadata(messages: list[Any]) -> tuple[tuple[str, int], ...]:
        result: list[tuple[str, int]] = []
        for message in messages:
            file = getattr(message, "file", None)
            name = getattr(file, "name", None)
            size = getattr(file, "size", None)
            if not isinstance(name, str) or not isinstance(size, int):
                return ()
            result.append((name, size))
        return tuple(result)

    @staticmethod
    def _expected_metadata(paths: tuple[Path, ...]) -> tuple[tuple[str, int], ...]:
        try:
            return tuple((path.name, path.stat().st_size) for path in paths)
        except OSError:
            return ()

    @staticmethod
    def _delivery(plan: AlbumPlan, messages: list[Any]) -> AlbumDelivery:
        group_ids = {
            message.grouped_id
            for message in messages
            if isinstance(getattr(message, "grouped_id", None), int)
        }
        return AlbumDelivery(
            album_number=plan.album_number,
            album_count=plan.album_count,
            telegram_album_group_id=(
                next(iter(group_ids)) if len(group_ids) == 1 else None
            ),
            telegram_message_ids=tuple(message.id for message in messages),
        )

    @classmethod
    def _latest_date(
        cls,
        messages: list[Any],
        current: datetime | None,
    ) -> datetime | None:
        dates = [
            cls._aware(message.date)
            for message in messages
            if isinstance(getattr(message, "date", None), datetime)
        ]
        candidates = dates + ([current] if current is not None else [])
        return max(candidates) if candidates else None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
