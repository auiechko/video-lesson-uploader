from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from .models import Lesson, LessonVideo


TELEGRAM_ALBUM_LIMIT = 10
DEFAULT_ALBUM_BATCH_TEMPLATE = " (альбом {album_number}/{album_count})"


@dataclass(frozen=True, slots=True)
class AlbumPlan:
    album_number: int
    album_count: int
    video_paths: tuple[Path, ...]
    caption: str

    @property
    def is_album(self) -> bool:
        return len(self.video_paths) > 1


@dataclass(frozen=True, slots=True)
class PreviewRow:
    summary: str
    video_paths: tuple[Path, ...]


def build_caption(
    *,
    date: date,
    student_id: str,
    student_name: str,
    lesson_label: str,
    duration_hours: int = 1,
    is_trial: bool = False,
) -> str:
    if duration_hours not in (1, 2, 3):
        raise ValueError("duration_hours must be 1, 2, or 3")
    base = f"{date:%d.%m.%Y} {student_id.strip()} {student_name.strip()} {lesson_label.strip()}"
    duration_suffix = {1: "", 2: " ДВІ ГОДИНИ", 3: " ТРИ ГОДИНИ"}[duration_hours]
    trial_suffix = " (пробне)" if is_trial else ""
    return f"{base}{duration_suffix}{trial_suffix}"


def plan_albums(
    lesson: Lesson,
    *,
    album_batch_template: str = DEFAULT_ALBUM_BATCH_TEMPLATE,
) -> tuple[AlbumPlan, ...]:
    paths = lesson.ordered_video_paths
    batches = tuple(
        paths[offset:offset + TELEGRAM_ALBUM_LIMIT]
        for offset in range(0, len(paths), TELEGRAM_ALBUM_LIMIT)
    )
    album_count = len(batches)
    return tuple(
        AlbumPlan(
            album_number=index,
            album_count=album_count,
            video_paths=batch,
            caption=lesson.caption + (
                album_batch_template.format(
                    album_number=index,
                    album_count=album_count,
                )
                if album_count > 1
                else ""
            ),
        )
        for index, batch in enumerate(batches, start=1)
    )


def group_videos_by_lesson(
    videos: Iterable[LessonVideo],
    *,
    profile_id: str,
    batch_id: str,
    captions: Mapping[str, str],
) -> tuple[Lesson, ...]:
    grouped: dict[str, list[LessonVideo]] = defaultdict(list)
    for video in videos:
        grouped[video.calendar_event_id].append(video)

    lessons: list[Lesson] = []
    for event_id, event_videos in grouped.items():
        try:
            caption = captions[event_id]
        except KeyError as error:
            raise ValueError(f"missing caption for calendar event {event_id!r}") from error
        starts = {video.event_start for video in event_videos}
        if len(starts) != 1:
            raise ValueError(f"calendar event {event_id!r} has inconsistent start times")
        ordered = sorted(event_videos, key=lambda video: (video.order, video.path.name))
        lessons.append(Lesson(
            profile_id=profile_id,
            batch_id=batch_id,
            calendar_event_id=event_id,
            event_start=ordered[0].event_start,
            caption=caption,
            ordered_video_paths=tuple(video.path for video in ordered),
        ))
    return tuple(sorted(lessons, key=lambda item: (item.event_start, item.calendar_event_id)))


def build_preview(lesson: Lesson) -> PreviewRow:
    caption_parts = lesson.caption.split()
    student_name = caption_parts[2] if len(caption_parts) >= 3 else lesson.caption
    album_count = (lesson.video_count + TELEGRAM_ALBUM_LIMIT - 1) // TELEGRAM_ALBUM_LIMIT
    album_label = (
        "Один Telegram-альбом"
        if album_count == 1
        else f"{album_count} Telegram-альбоми"
    )
    return PreviewRow(
        summary=(
            f"{lesson.event_start:%d.%m.%Y} | {student_name} | "
            f"{lesson.video_count} відео | {album_label}"
        ),
        video_paths=lesson.ordered_video_paths,
    )
