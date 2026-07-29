from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .calendar_rules import (
    ParsedCalendarEvent,
    build_calendar_snapshot,
)
from .models import Lesson, LessonDetails, LessonSendMode
from .zoom_matching import (
    ZoomMatchResult,
    ZoomMatchStatus,
)

_ASSIGNED_STATUSES = {
    ZoomMatchStatus.AUTO_MATCHED,
    ZoomMatchStatus.MANUALLY_CONFIRMED,
    ZoomMatchStatus.READY_VIDEO,
    ZoomMatchStatus.READY_ALBUM,
}


@dataclass(frozen=True, slots=True)
class ZoomBatchAssembly:
    lessons: tuple[Lesson, ...]
    missing_events: tuple[ParsedCalendarEvent, ...]
    unresolved_results: tuple[ZoomMatchResult, ...]


def assemble_zoom_batch(
    events: tuple[ParsedCalendarEvent, ...],
    results: tuple[ZoomMatchResult, ...],
    *,
    profile_id: str,
    batch_id: str,
    manual_text_event_ids: frozenset[str] = frozenset(),
    ignored_event_ids: frozenset[str] = frozenset(),
) -> ZoomBatchAssembly:
    event_by_id = {event.event_id: event for event in events}
    assigned: dict[str, list[ZoomMatchResult]] = defaultdict(list)
    unresolved: list[ZoomMatchResult] = []
    candidate_event_ids: set[str] = set()
    for result in results:
        candidate_event_ids.update(
            candidate.event.event_id
            for candidate in result.candidates
        )
        if (
            result.status in _ASSIGNED_STATUSES
            and result.event is not None
        ):
            assigned[result.event.event_id].append(result)
        elif not result.is_resolved:
            unresolved.append(result)

    lessons: list[Lesson] = []
    for event_id, assigned_results in assigned.items():
        event = event_by_id[event_id]
        ordered = sorted(
            assigned_results,
            key=lambda result: (
                result.segment.estimated_start,
                result.segment.sequence_number,
            ),
        )
        lessons.append(
            _lesson(
                event,
                profile_id=profile_id,
                batch_id=batch_id,
                video_paths=tuple(
                    result.segment.path for result in ordered
                ),
            )
        )
    for event in events:
        if event.event_id in ignored_event_ids:
            continue
        if (
            event.is_text_only
            or event.event_id in manual_text_event_ids
        ):
            lessons.append(
                _lesson(
                    event,
                    profile_id=profile_id,
                    batch_id=batch_id,
                    video_paths=(),
                    force_text_only=(
                        event.event_id in manual_text_event_ids
                    ),
                )
            )

    assigned_event_ids = set(assigned)
    missing_events = tuple(
        event
        for event in events
        if (
            event.requires_video
            and event.event_id not in assigned_event_ids
            and event.event_id not in candidate_event_ids
            and event.event_id not in manual_text_event_ids
            and event.event_id not in ignored_event_ids
        )
    )
    lessons.sort(key=lambda lesson: (lesson.event_start, lesson.calendar_event_id))
    return ZoomBatchAssembly(
        lessons=tuple(lessons),
        missing_events=missing_events,
        unresolved_results=tuple(unresolved),
    )


def _lesson(
    event: ParsedCalendarEvent,
    *,
    profile_id: str,
    batch_id: str,
    video_paths: tuple[Path, ...],
    force_text_only: bool = False,
) -> Lesson:
    text_only = event.is_text_only or force_text_only
    return Lesson(
        profile_id=profile_id,
        batch_id=batch_id,
        calendar_event_id=event.event_id,
        event_start=event.start,
        caption=(
            f"{event.caption} (без запису)"
            if force_text_only and not event.is_no_recording
            else event.caption
        ),
        ordered_video_paths=video_paths,
        send_mode=(
            LessonSendMode.TEXT_ONLY if text_only else LessonSendMode.MEDIA
        ),
        calendar_snapshot=build_calendar_snapshot(event),
        details=LessonDetails(
            student_id=event.student_id,
            student_name=event.student_name,
            lesson_label=event.lesson_label,
            duration_hours=event.duration_hours,
            is_trial=event.is_trial,
            student_age=event.student_age,
            calendar_status=event.status.value,
            is_no_recording=event.is_no_recording,
            is_transferred=event.is_transferred,
        ),
    )
