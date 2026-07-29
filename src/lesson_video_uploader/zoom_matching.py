from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable

from .calendar_rules import ParsedCalendarEvent
from .zoom_recordings import ZoomVideoSegment


class ZoomMatchStatus(StrEnum):
    ZOOM_DATETIME_PARSE_ERROR = "ZOOM_DATETIME_PARSE_ERROR"
    AUTO_MATCHED = "AUTO_MATCHED"
    TIME_CONFIRMATION_REQUIRED = "TIME_CONFIRMATION_REQUIRED"
    MANUAL_SELECTION_REQUIRED = "MANUAL_SELECTION_REQUIRED"
    VIDEO_OVERLAPS_NEXT_EVENT = "VIDEO_OVERLAPS_NEXT_EVENT"
    MULTIPLE_VIDEOS_REVIEW_REQUIRED = "MULTIPLE_VIDEOS_REVIEW_REQUIRED"
    VIDEO_SPLIT_REQUIRED = "VIDEO_SPLIT_REQUIRED"
    MANUALLY_CONFIRMED = "MANUALLY_CONFIRMED"
    READY_VIDEO = "READY_VIDEO"
    READY_ALBUM = "READY_ALBUM"
    READY_TEXT = "READY_TEXT"
    BATCH_REVALIDATION_REQUIRED = "BATCH_REVALIDATION_REQUIRED"
    ZOOM_FOLDER_NOT_FOUND = "ZOOM_FOLDER_NOT_FOUND"
    CALENDAR_EVENT_NOT_FOUND = "CALENDAR_EVENT_NOT_FOUND"
    VIDEO_DURATION_UNKNOWN = "VIDEO_DURATION_UNKNOWN"
    VIDEO_SPLIT_FAILED = "VIDEO_SPLIT_FAILED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    ERROR = "ERROR"
    DEFERRED = "DEFERRED"
    NOT_A_LESSON = "NOT_A_LESSON"
    SKIPPED_BY_USER = "SKIPPED_BY_USER"


@dataclass(frozen=True, slots=True)
class ZoomMatchSettings:
    automatic_time_tolerance_minutes: int = 30
    manual_time_search_window_minutes: int = 180
    calendar_conflict_tolerance_minutes: int = 10
    next_lesson_overlap_tolerance_minutes: int = 10

    def __post_init__(self) -> None:
        values = (
            self.automatic_time_tolerance_minutes,
            self.manual_time_search_window_minutes,
            self.calendar_conflict_tolerance_minutes,
            self.next_lesson_overlap_tolerance_minutes,
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in values
        ):
            raise ValueError("Zoom matching tolerances must be non-negative integers")


@dataclass(frozen=True, slots=True)
class CandidateDiagnostic:
    event: ParsedCalendarEvent
    start_difference_minutes: float
    overlap_seconds: float
    overlap_ratio: float
    score: int
    rejection_reason: str = ""


@dataclass(frozen=True, slots=True)
class ZoomMatchResult:
    segment: ZoomVideoSegment
    status: ZoomMatchStatus
    event: ParsedCalendarEvent | None
    candidates: tuple[CandidateDiagnostic, ...]
    next_event: ParsedCalendarEvent | None = None
    start_difference_minutes: float | None = None
    next_overlap_seconds: float = 0
    split_offset_seconds: float | None = None
    reason: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status in {
            ZoomMatchStatus.AUTO_MATCHED,
            ZoomMatchStatus.MANUALLY_CONFIRMED,
            ZoomMatchStatus.READY_VIDEO,
            ZoomMatchStatus.READY_ALBUM,
            ZoomMatchStatus.READY_TEXT,
            ZoomMatchStatus.NOT_A_LESSON,
            ZoomMatchStatus.SKIPPED_BY_USER,
        }


def match_zoom_segments(
    segments: Iterable[ZoomVideoSegment],
    events: Iterable[ParsedCalendarEvent],
    *,
    settings: ZoomMatchSettings | None = None,
) -> tuple[ZoomMatchResult, ...]:
    active_settings = settings or ZoomMatchSettings()
    conducted = tuple(
        sorted(
            (event for event in events if event.requires_video),
            key=lambda event: (event.start, event.event_id),
        )
    )
    assigned_event_ids: set[str] = set()
    results: list[ZoomMatchResult] = []
    for segment in sorted(
        segments,
        key=lambda item: (
            item.estimated_start,
            item.source_folder,
            item.sequence_number,
        ),
    ):
        result = _match_one(
            segment,
            conducted,
            assigned_event_ids,
            active_settings,
        )
        results.append(result)
        if result.status is ZoomMatchStatus.AUTO_MATCHED and result.event:
            assigned_event_ids.add(result.event.event_id)
    return tuple(results)


def batch_matching_ready(results: Iterable[ZoomMatchResult]) -> bool:
    materialized = tuple(results)
    return bool(materialized) and all(result.is_resolved for result in materialized)


def _match_one(
    segment: ZoomVideoSegment,
    events: tuple[ParsedCalendarEvent, ...],
    assigned_event_ids: set[str],
    settings: ZoomMatchSettings,
) -> ZoomMatchResult:
    diagnostics = tuple(
        _diagnostic(segment, event)
        for event in events
        if _minutes_apart(segment.estimated_start, event.start)
        <= settings.manual_time_search_window_minutes
    )
    automatic = tuple(
        candidate
        for candidate in diagnostics
        if (
            candidate.event.start.date() == segment.estimated_start.date()
            and candidate.start_difference_minutes
            <= settings.automatic_time_tolerance_minutes
        )
    )
    if len(automatic) > 1:
        ranked = tuple(
            sorted(
                automatic,
                key=lambda item: (
                    item.start_difference_minutes,
                    -item.score,
                    item.event.event_id,
                ),
            )
        )
        same_slot = any(
            abs(
                (
                    second.event.start - first.event.start
                ).total_seconds()
            )
            <= settings.calendar_conflict_tolerance_minutes * 60
            for index, first in enumerate(ranked)
            for second in ranked[index + 1 :]
        )
        equally_near = (
            ranked[0].start_difference_minutes
            == ranked[1].start_difference_minutes
        )
        if same_slot or equally_near:
            return ZoomMatchResult(
                segment=segment,
                status=ZoomMatchStatus.MANUAL_SELECTION_REQUIRED,
                event=None,
                candidates=automatic,
                reason=(
                    "У межах одного Calendar-слота є кілька "
                    "проведених подій."
                ),
            )
        automatic = (ranked[0],)
    if not automatic:
        nearest = min(
            diagnostics,
            key=lambda item: (
                item.start_difference_minutes,
                -item.score,
                item.event.event_id,
            ),
            default=None,
        )
        return ZoomMatchResult(
            segment=segment,
            status=ZoomMatchStatus.TIME_CONFIRMATION_REQUIRED,
            event=nearest.event if nearest else None,
            candidates=diagnostics,
            start_difference_minutes=(
                nearest.start_difference_minutes if nearest else None
            ),
            reason=(
                "Немає однозначного проведеного уроку в межах "
                f"±{settings.automatic_time_tolerance_minutes} хв."
            ),
        )

    selected = automatic[0]
    event = selected.event
    if event.event_id in assigned_event_ids:
        return ZoomMatchResult(
            segment=segment,
            status=ZoomMatchStatus.MULTIPLE_VIDEOS_REVIEW_REQUIRED,
            event=event,
            candidates=automatic,
            start_difference_minutes=selected.start_difference_minutes,
            reason="Calendar event уже зайнята іншим відеосегментом.",
        )

    other_overlaps = [
        candidate
        for candidate in diagnostics
        if (
            candidate.event.event_id != event.event_id
            and candidate.overlap_seconds > 0
        )
    ]
    if segment.sequence_number > 1 and other_overlaps:
        return ZoomMatchResult(
            segment=segment,
            status=ZoomMatchStatus.MULTIPLE_VIDEOS_REVIEW_REQUIRED,
            event=event,
            candidates=tuple((selected, *other_overlaps)),
            start_difference_minutes=selected.start_difference_minutes,
            reason=(
                "Окремий MP4-сегмент перетинає більше одного проведеного уроку."
            ),
        )

    next_event = next(
        (
            candidate
            for candidate in events
            if (
                candidate.start > event.start
                and candidate.event_id != event.event_id
            )
        ),
        None,
    )
    next_overlap = (
        _interval_overlap_seconds(
            segment.estimated_start,
            segment.estimated_end,
            next_event.start,
            next_event.end,
        )
        if next_event is not None
        else 0
    )
    if (
        next_event is not None
        and next_overlap
        > settings.next_lesson_overlap_tolerance_minutes * 60
    ):
        return ZoomMatchResult(
            segment=segment,
            status=ZoomMatchStatus.VIDEO_OVERLAPS_NEXT_EVENT,
            event=event,
            candidates=automatic,
            next_event=next_event,
            start_difference_minutes=selected.start_difference_minutes,
            next_overlap_seconds=next_overlap,
            split_offset_seconds=(
                next_event.start - segment.estimated_start
            ).total_seconds(),
            reason=(
                "Відео суттєво переходить на наступний проведений урок."
            ),
        )

    return ZoomMatchResult(
        segment=segment,
        status=ZoomMatchStatus.AUTO_MATCHED,
        event=event,
        candidates=automatic,
        next_event=next_event,
        start_difference_minutes=selected.start_difference_minutes,
        next_overlap_seconds=next_overlap,
        reason=(
            f"Zoom почався о {segment.estimated_start:%H:%M}; "
            f"урок — о {event.start:%H:%M}; різниця "
            f"{selected.start_difference_minutes:g} хв. Інших "
            "однозначних кандидатів немає."
        ),
    )


def _diagnostic(
    segment: ZoomVideoSegment,
    event: ParsedCalendarEvent,
) -> CandidateDiagnostic:
    difference = _minutes_apart(segment.estimated_start, event.start)
    overlap = _interval_overlap_seconds(
        segment.estimated_start,
        segment.estimated_end,
        event.start,
        event.end,
    )
    event_duration = max(0, (event.end - event.start).total_seconds())
    shorter_duration = min(segment.duration_seconds, event_duration)
    ratio = overlap / shorter_duration if shorter_duration else 0
    score = (
        (100 if difference <= 10 else 70 if difference <= 30 else 20)
        + round(ratio * 20)
    )
    return CandidateDiagnostic(
        event=event,
        start_difference_minutes=difference,
        overlap_seconds=overlap,
        overlap_ratio=ratio,
        score=score,
    )


def _minutes_apart(first: datetime, second: datetime) -> float:
    if first.tzinfo is None or second.tzinfo is None:
        raise ValueError("Zoom and Calendar datetimes must be timezone-aware")
    return abs((first - second).total_seconds()) / 60


def _interval_overlap_seconds(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
) -> float:
    return max(
        0,
        (min(first_end, second_end) - max(first_start, second_start)).total_seconds(),
    )
