from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .google_calendar import GoogleCalendarEvent
from .planning import build_caption


class CalendarEventStatus(StrEnum):
    NORMAL = "NORMAL"
    TRIAL = "TRIAL"
    NO_RECORDING = "NO_RECORDING"
    TRANSFERRED = "TRANSFERRED"
    TRANSFERRED_TRIAL = "TRANSFERRED_TRIAL"
    TRANSFERRED_NO_RECORDING = "TRANSFERRED_NO_RECORDING"
    IGNORED_PAUSE = "IGNORED_PAUSE"
    IGNORED_CANCELLED = "IGNORED_CANCELLED"
    PARSE_ERROR = "PARSE_ERROR"
    MANUAL_SELECTION_REQUIRED = "MANUAL_SELECTION_REQUIRED"


class CancellationSource(StrEnum):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    OTHER = "OTHER"


_SPACE_PATTERN = re.compile(r"\s+")
_PAUSE_PREFIX = re.compile(r"^\s*\(?\s*пауза\b", re.IGNORECASE)
_CANCELLATION_PREFIX = re.compile(r"^\s*вп(?:\s|$)", re.IGNORECASE)
_TRANSFER_PREFIX = re.compile(r"^\s*перенос(?:\s|$)", re.IGNORECASE)
_TRIAL_MARKER = re.compile(r"\(?\s*пробн\w*\s*\)?", re.IGNORECASE)
_NO_RECORDING_MARKER = re.compile(
    r"\(?\s*без\s+запису\s*\)?",
    re.IGNORECASE,
)
_STUDENT_GROUP = re.compile(
    r"^\s*(?P<name>.+?)\s+"
    r"(?P<age>\d{1,2})\s*(?:р(?:оків)?|років)?\s*$",
    re.IGNORECASE,
)
@dataclass(frozen=True, slots=True)
class ParsedCalendarEvent:
    event_id: str
    calendar_id: str
    original_summary: str
    student_id: str
    student_name: str
    student_age: int | None
    lesson_type: str
    start: datetime
    end: datetime
    status: CalendarEventStatus
    cancellation_source: CancellationSource | None = None
    is_trial: bool = False
    is_no_recording: bool = False
    is_transferred: bool = False
    parse_error: str = ""

    @property
    def is_conducted(self) -> bool:
        return self.status not in {
            CalendarEventStatus.IGNORED_CANCELLED,
            CalendarEventStatus.IGNORED_PAUSE,
            CalendarEventStatus.PARSE_ERROR,
            CalendarEventStatus.MANUAL_SELECTION_REQUIRED,
        }

    @property
    def is_text_only(self) -> bool:
        return self.is_conducted and self.is_no_recording

    @property
    def requires_video(self) -> bool:
        return self.is_conducted and not self.is_no_recording

    @property
    def is_split_boundary(self) -> bool:
        return self.requires_video

    @property
    def duration_hours(self) -> int:
        seconds = max(0, int((self.end - self.start).total_seconds()))
        return min(3, max(1, int((seconds + 1800) // 3600)))

    @property
    def lesson_label(self) -> str:
        if self.student_age is None:
            return self.lesson_type
        return f"{self.student_age}р {self.lesson_type}".strip()

    @property
    def caption(self) -> str:
        if not self.is_conducted:
            return ""
        caption = build_caption(
            date=self.start.date(),
            student_id=self.student_id,
            student_name=self.student_name,
            lesson_label=self.lesson_label,
            duration_hours=self.duration_hours,
            is_trial=self.is_trial,
        )
        return (
            f"{caption} (без запису)"
            if self.is_no_recording
            else caption
        )


@dataclass(frozen=True, slots=True)
class CalendarSlotResolution:
    start: datetime
    status: CalendarEventStatus | None
    selected: ParsedCalendarEvent | None
    candidates: tuple[ParsedCalendarEvent, ...]
    text_only: tuple[ParsedCalendarEvent, ...]
    ignored: tuple[ParsedCalendarEvent, ...]
    parse_errors: tuple[ParsedCalendarEvent, ...]


@dataclass(frozen=True, slots=True)
class CalendarEventSnapshot:
    event_id: str
    summary: str
    student_id: str
    student_name: str
    student_age: int | None
    local_date: str
    start: str
    end: str
    duration_minutes: int
    status: str
    is_trial: bool
    is_no_recording: bool
    is_transferred: bool
    is_cancelled: bool
    is_pause: bool
    calendar_id: str = "primary"


class BatchRevalidationRequired(RuntimeError):
    def __init__(
        self,
        changes: Mapping[str, Mapping[str, tuple[object, object]]],
    ) -> None:
        self.changes = {
            event_id: dict(event_changes)
            for event_id, event_changes in changes.items()
        }
        details = []
        for event_id in sorted(self.changes):
            rendered = ", ".join(
                f"{field}: {old} → {new}"
                for field, (old, new) in self.changes[event_id].items()
            )
            details.append(f"{event_id}: {rendered}")
        super().__init__(
            "BATCH_REVALIDATION_REQUIRED: Google Calendar змінився.\n"
            + "\n".join(details)
        )


def parse_calendar_event(
    event: GoogleCalendarEvent,
    *,
    timezone_name: str = "Europe/Kyiv",
) -> ParsedCalendarEvent:
    timezone = _timezone(timezone_name)
    start = _local_datetime(event.start, timezone)
    end = _local_datetime(event.end, timezone)
    summary = _normalize_text(event.summary)
    is_pause = bool(_PAUSE_PREFIX.match(summary))
    is_cancelled = bool(_CANCELLATION_PREFIX.match(summary))
    is_transferred = bool(_TRANSFER_PREFIX.match(summary))
    is_trial = bool(_TRIAL_MARKER.search(summary))
    is_no_recording = bool(_NO_RECORDING_MARKER.search(summary))
    cancellation_source: CancellationSource | None = None

    content = summary
    if is_pause:
        content = _from_first_student_id(content)
    elif is_cancelled:
        content, cancellation_source = _strip_cancellation_prefix(content)
    elif is_transferred:
        content = _TRANSFER_PREFIX.sub("", content, count=1).strip()

    content = _TRIAL_MARKER.sub(" ", content)
    content = _NO_RECORDING_MARKER.sub(" ", content)
    content = _normalize_text(content)
    student_id, student_name, student_age, lesson_type, error = (
        _parse_student_fields(content)
    )

    if is_pause:
        status = CalendarEventStatus.IGNORED_PAUSE
    elif is_cancelled:
        status = CalendarEventStatus.IGNORED_CANCELLED
    elif error:
        status = CalendarEventStatus.PARSE_ERROR
    elif is_transferred and is_no_recording:
        status = CalendarEventStatus.TRANSFERRED_NO_RECORDING
    elif is_transferred and is_trial:
        status = CalendarEventStatus.TRANSFERRED_TRIAL
    elif is_transferred:
        status = CalendarEventStatus.TRANSFERRED
    elif is_no_recording:
        status = CalendarEventStatus.NO_RECORDING
    elif is_trial:
        status = CalendarEventStatus.TRIAL
    else:
        status = CalendarEventStatus.NORMAL

    return ParsedCalendarEvent(
        event_id=event.id,
        calendar_id=event.calendar_id,
        original_summary=summary,
        student_id=student_id,
        student_name=student_name,
        student_age=student_age,
        lesson_type=lesson_type,
        start=start,
        end=end,
        status=status,
        cancellation_source=cancellation_source,
        is_trial=is_trial,
        is_no_recording=is_no_recording,
        is_transferred=is_transferred,
        parse_error=error,
    )


def resolve_calendar_slots(
    events: Iterable[ParsedCalendarEvent],
    *,
    tolerance_minutes: int = 10,
) -> tuple[CalendarSlotResolution, ...]:
    if tolerance_minutes < 0:
        raise ValueError("calendar conflict tolerance cannot be negative")
    ordered = sorted(events, key=lambda item: (item.start, item.event_id))
    buckets: list[list[ParsedCalendarEvent]] = []
    tolerance = timedelta(minutes=tolerance_minutes)
    for item in ordered:
        if not buckets or item.start - buckets[-1][0].start > tolerance:
            buckets.append([item])
        else:
            buckets[-1].append(item)
    return tuple(_resolve_slot(bucket) for bucket in buckets)


def _resolve_slot(events: list[ParsedCalendarEvent]) -> CalendarSlotResolution:
    ignored = tuple(
        item
        for item in events
        if item.status in {
            CalendarEventStatus.IGNORED_CANCELLED,
            CalendarEventStatus.IGNORED_PAUSE,
        }
    )
    parse_errors = tuple(
        item
        for item in events
        if item.status is CalendarEventStatus.PARSE_ERROR
    )
    text_only = tuple(item for item in events if item.is_text_only)
    candidates = tuple(item for item in events if item.requires_video)
    if len(candidates) >= 2:
        status = CalendarEventStatus.MANUAL_SELECTION_REQUIRED
        selected = None
    elif len(candidates) == 1:
        status = candidates[0].status
        selected = candidates[0]
    elif len(text_only) == 1:
        status = text_only[0].status
        selected = text_only[0]
    else:
        status = None
        selected = None
    return CalendarSlotResolution(
        start=events[0].start,
        status=status,
        selected=selected,
        candidates=candidates,
        text_only=text_only,
        ignored=ignored,
        parse_errors=parse_errors,
    )


def build_calendar_snapshot(
    event: ParsedCalendarEvent,
) -> CalendarEventSnapshot:
    return CalendarEventSnapshot(
        event_id=event.event_id,
        summary=event.original_summary,
        student_id=event.student_id,
        student_name=event.student_name,
        student_age=event.student_age,
        local_date=event.start.date().isoformat(),
        start=event.start.isoformat(),
        end=event.end.isoformat(),
        duration_minutes=int((event.end - event.start).total_seconds() // 60),
        status=event.status.value,
        is_trial=event.is_trial,
        is_no_recording=event.is_no_recording,
        is_transferred=event.is_transferred,
        is_cancelled=event.status is CalendarEventStatus.IGNORED_CANCELLED,
        is_pause=event.status is CalendarEventStatus.IGNORED_PAUSE,
        calendar_id=event.calendar_id,
    )


def validate_calendar_snapshots(
    expected: Mapping[str, CalendarEventSnapshot],
    current: Mapping[str, ParsedCalendarEvent],
) -> None:
    changes: dict[str, dict[str, tuple[object, object]]] = {}
    for event_id, old_snapshot in expected.items():
        current_event = current.get(event_id)
        if current_event is None:
            changes[event_id] = {"event": (old_snapshot, None)}
            continue
        new_snapshot = build_calendar_snapshot(current_event)
        event_changes: dict[str, tuple[object, object]] = {}
        for field in fields(CalendarEventSnapshot):
            if field.name == "event_id":
                continue
            old_value = getattr(old_snapshot, field.name)
            new_value = getattr(new_snapshot, field.name)
            if old_value != new_value:
                event_changes[field.name] = (old_value, new_value)
        if event_changes:
            changes[event_id] = event_changes
    if changes:
        raise BatchRevalidationRequired(changes)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Невідомий часовий пояс: {name}") from error


def _local_datetime(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _normalize_text(value: str) -> str:
    return _SPACE_PATTERN.sub(" ", value).strip()


def _from_first_student_id(value: str) -> str:
    match = re.search(r"\b\d{5,}\b", value)
    return value[match.start():] if match else ""


def _strip_cancellation_prefix(
    value: str,
) -> tuple[str, CancellationSource]:
    remainder = _CANCELLATION_PREFIX.sub("", value, count=1).strip()
    id_match = re.search(r"\b\d{5,}\b", remainder)
    source_text = (
        remainder[:id_match.start()].strip().casefold()
        if id_match
        else remainder.casefold()
    )
    source = {
        "учень": CancellationSource.STUDENT,
        "викладач": CancellationSource.TEACHER,
    }.get(source_text, CancellationSource.OTHER)
    return (
        remainder[id_match.start():].strip() if id_match else "",
        source,
    )


def _parse_student_fields(
    value: str,
) -> tuple[str, str, int | None, str, str]:
    id_match = re.search(r"\b\d{5,}\b", value)
    if id_match is None:
        return "", "", None, "", "Не знайдено ID учня"
    student_id = id_match.group(0)
    student_group: re.Match[str] | None = None
    student_name = ""
    student_age: int | None = None
    for group in re.finditer(r"\(([^()]*)\)", value):
        details = _STUDENT_GROUP.match(group.group(1))
        if details:
            student_group = group
            student_name = _normalize_text(details.group("name"))
            student_age = int(details.group("age"))
            break
    if student_group is None:
        return student_id, "", None, "", "Не знайдено ім’я та вік у дужках"
    return student_id, student_name, student_age, "індив", ""
