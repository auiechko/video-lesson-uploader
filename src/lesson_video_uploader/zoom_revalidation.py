from __future__ import annotations

import math
from collections.abc import Iterable

from .media_tools import probe_mp4
from .models import Lesson, LessonSendMode
from .zoom_matching import ZoomMatchResult
from .zoom_recordings import MetadataProbe


class ZoomSourceRevalidationError(RuntimeError):
    def __init__(self, changes: dict[str, str]) -> None:
        self.changes = changes
        details = "\n".join(
            f"- {path}: {reason}"
            for path, reason in sorted(changes.items())
        )
        super().__init__(
            "Відеофайли змінилися після підготовки batch:\n"
            f"{details}"
        )


def validate_manifest_video_files(
    lessons: Iterable[Lesson],
    *,
    metadata_probe: MetadataProbe = probe_mp4,
) -> None:
    """Validate current media when resuming a saved batch after restart."""
    changes: dict[str, str] = {}
    seen_paths: set[str] = set()
    for lesson in lessons:
        if lesson.send_mode is LessonSendMode.TEXT_ONLY:
            continue
        for path in lesson.ordered_video_paths:
            path_key = str(path)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            if not path.is_file():
                changes[path_key] = "file is missing"
                continue
            try:
                if path.stat().st_size <= 0:
                    changes[path_key] = "file is empty"
                    continue
                metadata = metadata_probe(path)
            except (OSError, RuntimeError, ValueError) as error:
                changes[path_key] = f"ffprobe failed: {error}"
                continue
            if not metadata.has_video_stream:
                changes[path_key] = "video stream is missing"
                continue
            if metadata.duration_seconds <= 0:
                changes[path_key] = "video duration is unavailable"
    if changes:
        raise ZoomSourceRevalidationError(changes)


def validate_zoom_sources(
    results: Iterable[ZoomMatchResult],
    *,
    metadata_probe: MetadataProbe = probe_mp4,
    duration_tolerance_seconds: float = 1,
) -> None:
    changes: dict[str, str] = {}
    seen_paths: set[str] = set()
    for result in results:
        if not result.is_resolved or result.event is None:
            continue
        segment = result.segment
        path_key = str(segment.path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        if not segment.path.is_file():
            changes[path_key] = "file is missing"
            continue
        current_size = segment.path.stat().st_size
        if current_size != segment.file_size:
            changes[path_key] = (
                f"size changed from {segment.file_size} to {current_size}"
            )
            continue
        try:
            metadata = metadata_probe(segment.path)
        except (OSError, RuntimeError, ValueError) as error:
            changes[path_key] = f"ffprobe failed: {error}"
            continue
        if not metadata.has_video_stream:
            changes[path_key] = "video stream is missing"
            continue
        if not math.isclose(
            metadata.duration_seconds,
            segment.duration_seconds,
            abs_tol=duration_tolerance_seconds,
        ):
            changes[path_key] = (
                "duration changed from "
                f"{segment.duration_seconds:g} to "
                f"{metadata.duration_seconds:g} seconds"
            )
    if changes:
        raise ZoomSourceRevalidationError(changes)
