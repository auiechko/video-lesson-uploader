from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import AlbumDelivery, Lesson, SendStatus
from .persistence import SQLiteSendItemRepository
from .planning import DEFAULT_ALBUM_BATCH_TEMPLATE, plan_albums
from .progress import LessonUploadProgress


class ManualReviewRequired(RuntimeError):
    """Delivery cannot safely be retried without checking Telegram first."""


class TelethonClient(Protocol):
    async def send_file(self, **kwargs: Any) -> Any: ...


ProgressCallback = Callable[[int, int], object]
AlbumSplitWarning = Callable[[str, int], object]


def _lesson_progress(
    lesson: Lesson,
    callback: ProgressCallback | None,
) -> ProgressCallback | None:
    """Rebase Telethon's per-file reports onto the whole lesson.

    Falls back to the raw callback when the sizes cannot be read, so a lesson
    still uploads if a file turns unreadable between validation and send.
    """
    if callback is None:
        return None
    try:
        sizes = tuple(path.stat().st_size for path in lesson.ordered_video_paths)
    except OSError:
        return callback
    if any(size <= 0 for size in sizes):
        return callback
    tracker = LessonUploadProgress(sizes)

    def report(current: int, total: int) -> None:
        callback(*tracker.observe(current, total))

    return report


class TelethonLessonSender:
    def __init__(
        self,
        client: TelethonClient,
        repository: SQLiteSendItemRepository,
        *,
        album_batch_template: str = DEFAULT_ALBUM_BATCH_TEMPLATE,
    ) -> None:
        self.client = client
        self.repository = repository
        self.album_batch_template = album_batch_template

    async def send(
        self,
        lesson: Lesson,
        *,
        target_peer: object,
        progress_callback: ProgressCallback | None = None,
        album_split_warning: AlbumSplitWarning | None = None,
    ) -> Lesson:
        existing = self.repository.get(lesson.identity)
        if existing is not None:
            if existing.status is SendStatus.SENT:
                return existing
            if existing.status in {
                SendStatus.DELIVERY_UNKNOWN,
                SendStatus.PARTIALLY_CONFIRMED,
            }:
                raise ManualReviewRequired(
                    "Check recent Telegram messages before retrying this lesson."
                )
            if existing.status is SendStatus.UPLOADING:
                unknown = replace(existing, status=SendStatus.DELIVERY_UNKNOWN)
                self.repository.save(unknown)
                raise ManualReviewRequired(
                    "A previous upload was interrupted; verify Telegram manually."
                )

        plans = plan_albums(
            lesson,
            album_batch_template=self.album_batch_template,
        )
        if len(plans) > 1 and album_split_warning is not None:
            album_split_warning(lesson.caption, len(plans))
        report_progress = _lesson_progress(lesson, progress_callback)

        uploading = replace(lesson, status=SendStatus.UPLOADING)
        self.repository.save(uploading)
        message_ids: list[int] = []
        deliveries: list[AlbumDelivery] = []

        try:
            for plan in plans:
                file_argument: str | list[str]
                if len(plan.video_paths) == 1:
                    file_argument = str(plan.video_paths[0])
                else:
                    file_argument = [str(path) for path in plan.video_paths]
                response = await self.client.send_file(
                    entity=target_peer,
                    file=file_argument,
                    caption=plan.caption,
                    supports_streaming=True,
                    progress_callback=report_progress,
                )
                messages = (
                    list(response)
                    if isinstance(response, (list, tuple))
                    else [response]
                )
                batch_ids = tuple(
                    message.id
                    for message in messages
                    if isinstance(getattr(message, "id", None), int)
                )
                group_ids = {
                    message.grouped_id
                    for message in messages
                    if getattr(message, "grouped_id", None) is not None
                }
                group_id = next(iter(group_ids)) if len(group_ids) == 1 else None
                message_ids.extend(batch_ids)
                deliveries.append(AlbumDelivery(
                    album_number=plan.album_number,
                    album_count=plan.album_count,
                    telegram_album_group_id=group_id,
                    telegram_message_ids=batch_ids,
                ))
                if len(batch_ids) != len(plan.video_paths):
                    return self._save_unconfirmed(
                        lesson,
                        message_ids=message_ids,
                        deliveries=deliveries,
                    )
        except Exception as error:
            self._save_unconfirmed(
                lesson,
                message_ids=message_ids,
                deliveries=deliveries,
            )
            raise ManualReviewRequired(
                "Telegram delivery is unknown; inspect recent messages before retrying."
            ) from error

        sent = replace(
            lesson,
            telegram_album_group_id=(
                deliveries[0].telegram_album_group_id
                if len(deliveries) == 1
                else None
            ),
            telegram_message_ids=tuple(message_ids),
            album_deliveries=tuple(deliveries),
            status=SendStatus.SENT,
            sent_at=datetime.now(timezone.utc),
        )
        self.repository.save(sent)
        return sent

    def _save_unconfirmed(
        self,
        lesson: Lesson,
        *,
        message_ids: list[int],
        deliveries: list[AlbumDelivery],
    ) -> Lesson:
        status = (
            SendStatus.PARTIALLY_CONFIRMED
            if message_ids
            else SendStatus.DELIVERY_UNKNOWN
        )
        result = replace(
            lesson,
            telegram_message_ids=tuple(message_ids),
            album_deliveries=tuple(deliveries),
            status=status,
        )
        self.repository.save(result)
        return result
