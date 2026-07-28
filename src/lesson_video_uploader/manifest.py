from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Lesson, LessonDetails
from .planning import build_caption, build_preview


@dataclass(frozen=True, slots=True)
class UploadManifest:
    profile_id: str
    batch_id: str
    target_peer: int | str
    lessons: tuple[Lesson, ...]


def _required_string(mapping: dict[str, Any], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _video_paths(lesson: dict[str, Any], root: Path) -> tuple[Path, ...]:
    raw_videos = lesson.get("videos")
    if not isinstance(raw_videos, list) or not raw_videos:
        raise ValueError("each lesson must have at least one video")
    ordered: list[tuple[int, Path]] = []
    for default_order, item in enumerate(raw_videos, start=1):
        if isinstance(item, str):
            raw_path = item
            order = default_order
        elif isinstance(item, dict):
            raw_path = _required_string(item, "path")
            order = item.get("order", default_order)
            if not isinstance(order, int):
                raise ValueError("video order must be an integer")
        else:
            raise ValueError("videos must contain paths or path/order objects")
        path = Path(raw_path)
        if path.suffix.lower() != ".mp4":
            raise ValueError(f"only MP4 videos are supported: {path}")
        resolved = path if path.is_absolute() else root / path
        if not resolved.is_file():
            raise ValueError(f"video does not exist: {resolved}")
        ordered.append((order, resolved.resolve()))
    orders = [order for order, _ in ordered]
    if len(set(orders)) != len(orders):
        # Chronology is the whole point of a lesson, so a tie would otherwise
        # be broken silently by filename and could reorder a recording.
        raise ValueError("each video in a lesson needs its own order")
    ordered.sort(key=lambda value: (value[0], value[1].name))
    return tuple(path for _, path in ordered)


_DETAIL_KEYS = ("student_id", "student_name", "lesson_label")


def _lesson_details(
    lesson: dict[str, Any],
    *,
    duration_hours: int,
    is_trial: bool,
    required: bool,
) -> LessonDetails | None:
    """Read the fields a caption is generated from.

    They stay optional when the lesson carries a ready-made caption, which is
    what a batch saved from the GUI looks like.
    """
    if not required and not any(key in lesson for key in _DETAIL_KEYS):
        return None
    return LessonDetails(
        student_id=_required_string(lesson, "student_id"),
        student_name=_required_string(lesson, "student_name"),
        lesson_label=_required_string(lesson, "lesson_label"),
        duration_hours=duration_hours,
        is_trial=is_trial,
    )


def load_manifest(path: Path) -> UploadManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid manifest JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be an object")
    profile_id = _required_string(raw, "profile_id")
    batch_id = _required_string(raw, "batch_id")
    target_peer = raw.get("target_peer")
    if not isinstance(target_peer, (int, str)) or isinstance(target_peer, bool):
        raise ValueError("target_peer must be an integer or string")
    raw_lessons = raw.get("lessons")
    if not isinstance(raw_lessons, list) or not raw_lessons:
        raise ValueError("manifest must contain at least one lesson")

    lessons: list[Lesson] = []
    event_ids: set[str] = set()
    for raw_lesson in raw_lessons:
        if not isinstance(raw_lesson, dict):
            raise ValueError("each lesson must be an object")
        event_id = _required_string(raw_lesson, "calendar_event_id")
        if event_id in event_ids:
            raise ValueError(
                f"calendar event {event_id!r} must be represented by one lesson item"
            )
        event_ids.add(event_id)
        try:
            event_start = datetime.fromisoformat(
                _required_string(raw_lesson, "event_start")
            )
        except ValueError as error:
            raise ValueError(f"invalid event_start for {event_id!r}") from error
        duration_hours = raw_lesson.get("duration_hours", 1)
        if not isinstance(duration_hours, int):
            raise ValueError("duration_hours must be an integer")
        is_trial = raw_lesson.get("is_trial", False)
        if not isinstance(is_trial, bool):
            raise ValueError("is_trial must be true or false")
        explicit_caption = raw_lesson.get("caption")
        if explicit_caption is not None and (
            not isinstance(explicit_caption, str) or not explicit_caption.strip()
        ):
            raise ValueError("caption must be a non-empty string")
        details = _lesson_details(
            raw_lesson,
            duration_hours=duration_hours,
            is_trial=is_trial,
            required=explicit_caption is None,
        )
        caption = (
            explicit_caption.strip()
            if isinstance(explicit_caption, str)
            else build_caption(
                date=event_start.date(),
                student_id=details.student_id,
                student_name=details.student_name,
                lesson_label=details.lesson_label,
                duration_hours=duration_hours,
                is_trial=is_trial,
            )
        )
        lessons.append(Lesson(
            profile_id=profile_id,
            batch_id=batch_id,
            calendar_event_id=event_id,
            event_start=event_start,
            caption=caption,
            ordered_video_paths=_video_paths(raw_lesson, path.parent),
            details=details,
        ))
    lessons.sort(key=lambda lesson: (lesson.event_start, lesson.calendar_event_id))
    return UploadManifest(
        profile_id=profile_id,
        batch_id=batch_id,
        target_peer=target_peer,
        lessons=tuple(lessons),
    )


def save_manifest(manifest: UploadManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lessons = []
    for lesson in manifest.lessons:
        videos = [
            {
                "path": os.path.relpath(video_path, path.parent),
                "order": index,
            }
            for index, video_path in enumerate(lesson.ordered_video_paths, start=1)
        ]
        entry: dict[str, Any] = {
            "calendar_event_id": lesson.calendar_event_id,
            "event_start": lesson.event_start.isoformat(),
        }
        if lesson.details is not None:
            entry.update({
                "student_id": lesson.details.student_id,
                "student_name": lesson.details.student_name,
                "lesson_label": lesson.details.lesson_label,
                "duration_hours": lesson.details.duration_hours,
                "is_trial": lesson.details.is_trial,
            })
        # Written even when the fields above could rebuild it, so the exact
        # text that goes to Telegram survives any later caption change.
        entry["caption"] = lesson.caption
        entry["videos"] = videos
        lessons.append(entry)
    payload = {
        "profile_id": manifest.profile_id,
        "batch_id": manifest.batch_id,
        "target_peer": manifest.target_peer,
        "lessons": lessons,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def render_manifest_preview(
    manifest: UploadManifest,
    *,
    expand: bool,
) -> str:
    lines: list[str] = []
    for lesson in manifest.lessons:
        row = build_preview(lesson)
        lines.append(row.summary)
        if expand:
            lines.extend(f"  - {path.name}" for path in row.video_paths)
    return "\n".join(lines)
