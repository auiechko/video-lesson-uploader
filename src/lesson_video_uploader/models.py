from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class SendStatus(StrEnum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    SENT = "SENT"
    FAILED = "FAILED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"


@dataclass(frozen=True, slots=True)
class LessonVideo:
    calendar_event_id: str
    event_start: datetime
    path: Path
    order: int


@dataclass(frozen=True, slots=True)
class AlbumDelivery:
    album_number: int
    album_count: int
    telegram_album_group_id: int | None
    telegram_message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Lesson:
    """One logical Telegram send item representing one calendar lesson."""

    profile_id: str
    batch_id: str
    calendar_event_id: str
    event_start: datetime
    caption: str
    ordered_video_paths: tuple[Path, ...]
    telegram_album_group_id: int | None = None
    telegram_message_ids: tuple[int, ...] = ()
    status: SendStatus = SendStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    album_deliveries: tuple[AlbumDelivery, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id is required")
        if not self.batch_id.strip():
            raise ValueError("batch_id is required")
        if not self.calendar_event_id.strip():
            raise ValueError("calendar_event_id is required")
        if not self.caption.strip():
            raise ValueError("caption is required")
        if not self.ordered_video_paths:
            raise ValueError("a lesson must contain at least one video")
        if len(set(self.ordered_video_paths)) != len(self.ordered_video_paths):
            raise ValueError("a lesson cannot contain duplicate video paths")

    @property
    def identity(self) -> str:
        return "\x1f".join((self.profile_id, self.batch_id, self.calendar_event_id))

    @property
    def video_count(self) -> int:
        return len(self.ordered_video_paths)
