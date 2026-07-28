from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .calendar_rules import CalendarEventSnapshot


class SendStatus(StrEnum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    SENT = "SENT"
    FAILED = "FAILED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    BATCH_REVALIDATION_REQUIRED = "BATCH_REVALIDATION_REQUIRED"


class LessonSendMode(StrEnum):
    MEDIA = "MEDIA"
    TEXT_ONLY = "TEXT_ONLY"


@dataclass(frozen=True, slots=True)
class LessonDetails:
    """The fields a generated caption was built from.

    Kept beside the finished caption so a saved batch can be reopened in the
    editor, and so a preview can name the student without re-parsing text.
    """

    student_id: str
    student_name: str
    lesson_label: str
    duration_hours: int = 1
    is_trial: bool = False
    student_age: int | None = None
    calendar_status: str = "NORMAL"
    is_no_recording: bool = False
    is_transferred: bool = False


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
    send_mode: LessonSendMode = LessonSendMode.MEDIA
    telegram_album_group_id: int | None = None
    telegram_message_ids: tuple[int, ...] = ()
    status: SendStatus = SendStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    album_deliveries: tuple[AlbumDelivery, ...] = ()
    details: LessonDetails | None = None
    calendar_snapshot: CalendarEventSnapshot | None = None

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id is required")
        if not self.batch_id.strip():
            raise ValueError("batch_id is required")
        if not self.calendar_event_id.strip():
            raise ValueError("calendar_event_id is required")
        if not self.caption.strip():
            raise ValueError("caption is required")
        if (
            self.send_mode is LessonSendMode.MEDIA
            and not self.ordered_video_paths
        ):
            raise ValueError("a lesson must contain at least one video")
        if (
            self.send_mode is LessonSendMode.TEXT_ONLY
            and self.ordered_video_paths
        ):
            raise ValueError("a text-only lesson cannot contain video paths")
        if len(set(self.ordered_video_paths)) != len(self.ordered_video_paths):
            raise ValueError("a lesson cannot contain duplicate video paths")

    @property
    def identity(self) -> str:
        return "\x1f".join((self.profile_id, self.batch_id, self.calendar_event_id))

    @property
    def video_count(self) -> int:
        return len(self.ordered_video_paths)
