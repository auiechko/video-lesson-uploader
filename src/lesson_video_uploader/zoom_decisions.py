from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .calendar_rules import ParsedCalendarEvent
from .zoom_recordings import ZoomVideoSegment


class AssignmentType(StrEnum):
    AUTO_MATCHED = "AUTO_MATCHED"
    MANUALLY_CONFIRMED_TIME = "MANUALLY_CONFIRMED_TIME"
    MANUALLY_SELECTED_EVENT = "MANUALLY_SELECTED_EVENT"
    MANUALLY_ENTERED_STUDENT = "MANUALLY_ENTERED_STUDENT"
    CURRENT_EVENT_FULL_VIDEO = "CURRENT_EVENT_FULL_VIDEO"
    NEXT_EVENT = "NEXT_EVENT"
    SPLIT_BETWEEN_EVENTS = "SPLIT_BETWEEN_EVENTS"
    NOT_A_LESSON = "NOT_A_LESSON"
    SKIPPED = "SKIPPED"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    normalized_path: str
    filename: str
    file_size: int
    duration_seconds: float
    zoom_start: datetime
    partial_hash: str = ""

    @classmethod
    def from_segment(cls, segment: ZoomVideoSegment) -> FileIdentity:
        return cls(
            normalized_path=normalize_file_path(segment.path),
            filename=segment.path.name,
            file_size=segment.file_size,
            duration_seconds=segment.duration_seconds,
            zoom_start=segment.estimated_start,
            partial_hash=partial_file_hash(segment.path),
        )


@dataclass(frozen=True, slots=True)
class ZoomAssignmentDecision:
    file_identity: FileIdentity
    calendar_event_id: str
    event_start_utc: datetime
    assignment_type: AssignmentType
    confirmed_by: str
    confirmed_at: datetime
    reason: str
    manual_split_point_seconds: float | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.file_identity.zoom_start, "Zoom start"),
            (self.event_start_utc, "Calendar event start"),
            (self.confirmed_at, "Confirmation datetime"),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    def is_reusable(
        self,
        segment: ZoomVideoSegment,
        event: ParsedCalendarEvent,
    ) -> bool:
        current = FileIdentity.from_segment(segment)
        event_start_utc = event.start.astimezone(timezone.utc)
        return (
            event.requires_video
            and self.calendar_event_id == event.event_id
            and self.event_start_utc == event_start_utc
            and self.file_identity.normalized_path == current.normalized_path
            and self.file_identity.filename == current.filename
            and self.file_identity.file_size == current.file_size
            and abs(
                self.file_identity.duration_seconds
                - current.duration_seconds
            )
            < 0.01
            and self.file_identity.zoom_start == current.zoom_start
            and (
                not self.file_identity.partial_hash
                or self.file_identity.partial_hash == current.partial_hash
            )
        )


def normalize_file_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def partial_file_hash(path: Path, *, chunk_size: int = 64 * 1024) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as file:
        digest.update(file.read(chunk_size))
        if size > chunk_size:
            file.seek(max(0, size - chunk_size))
            digest.update(file.read(chunk_size))
    return digest.hexdigest()
