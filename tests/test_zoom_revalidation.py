from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lesson_video_uploader.calendar_rules import parse_calendar_event
from lesson_video_uploader.google_calendar import GoogleCalendarEvent
from lesson_video_uploader.zoom_matching import (
    ZoomMatchResult,
    ZoomMatchStatus,
)
from lesson_video_uploader.zoom_recordings import (
    Mp4Metadata,
    VideoTimeConfidence,
    VideoTimeMethod,
    ZoomVideoSegment,
)
from lesson_video_uploader.zoom_revalidation import (
    ZoomSourceRevalidationError,
    validate_zoom_sources,
)


class ZoomSourceRevalidationTests(unittest.TestCase):
    def _result(self, path: Path) -> ZoomMatchResult:
        start = datetime(2026, 7, 23, 14, tzinfo=timezone.utc)
        event = parse_calendar_event(
            GoogleCalendarEvent(
                id="event",
                calendar_id="lessons",
                summary="105853087 Наталія (Святослав 14) Учко ТГ",
                description="",
                start=start,
                end=start + timedelta(hours=1),
            )
        )
        return ZoomMatchResult(
            segment=ZoomVideoSegment(
                source_folder=path.parent,
                path=path,
                sequence_number=1,
                duration_seconds=3600,
                estimated_start=start,
                estimated_end=start + timedelta(hours=1),
                time_method=VideoTimeMethod.ZOOM_FOLDER_START,
                confidence=VideoTimeConfidence.HIGH,
                file_size=path.stat().st_size,
            ),
            status=ZoomMatchStatus.AUTO_MATCHED,
            event=event,
            candidates=(),
        )

    def test_unchanged_source_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            result = self._result(path)

            validate_zoom_sources(
                (result,),
                metadata_probe=lambda _path: Mp4Metadata(
                    duration_seconds=3600,
                    has_video_stream=True,
                ),
            )

    def test_changed_size_invalidates_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            result = self._result(path)
            path.write_bytes(b"changed video")

            with self.assertRaises(ZoomSourceRevalidationError) as raised:
                validate_zoom_sources(
                    (result,),
                    metadata_probe=lambda _path: Mp4Metadata(
                        duration_seconds=3600,
                        has_video_stream=True,
                    ),
                )

        self.assertIn("size", raised.exception.changes[str(path)])

    def test_changed_duration_invalidates_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            result = self._result(path)

            with self.assertRaises(ZoomSourceRevalidationError) as raised:
                validate_zoom_sources(
                    (result,),
                    metadata_probe=lambda _path: Mp4Metadata(
                        duration_seconds=3500,
                        has_video_stream=True,
                    ),
                )

        self.assertIn("duration", raised.exception.changes[str(path)])

    def test_missing_generated_segment_invalidates_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video")
            result = self._result(path)
            path.unlink()

            with self.assertRaises(ZoomSourceRevalidationError):
                validate_zoom_sources(
                    (result,),
                    metadata_probe=lambda _path: Mp4Metadata(
                        duration_seconds=3600,
                        has_video_stream=True,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
