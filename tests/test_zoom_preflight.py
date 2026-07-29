from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from lesson_video_uploader.zoom_preflight import (
    PreflightSettings,
    ZoomPreflightService,
    ZoomPreflightStatus,
)
from lesson_video_uploader.zoom_recordings import Mp4Metadata


class ZoomPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = PreflightSettings(
            minimum_video_size_mb=0,
            video_stability_check_seconds=0,
        )

    @staticmethod
    def _ready_probe(_path: Path) -> Mp4Metadata:
        return Mp4Metadata(
            duration_seconds=3600,
            creation_time=datetime.now(timezone.utc),
            has_video_stream=True,
        )

    def test_service_files_without_mp4_are_unrendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            (folder / "recording.conf").touch()
            (folder / "audio123.m4a").touch()

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
            ).check(root, date(2026, 7, 23), date(2026, 7, 23), self.settings)

        self.assertEqual(
            result.folders[0].status,
            ZoomPreflightStatus.UNRENDERED_RECORDING,
        )
        self.assertFalse(result.is_passed)

    def test_changing_mp4_size_is_rendering_in_progress(self) -> None:
        sizes = iter((100, 200))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            video = folder / "video123.mp4"
            video.write_bytes(b"video")

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
                size_reader=lambda _path: next(sizes),
            ).check(root, date(2026, 7, 23), date(2026, 7, 23), self.settings)

        self.assertEqual(
            result.folders[0].status,
            ZoomPreflightStatus.RENDERING_IN_PROGRESS,
        )

    def test_stable_mp4_with_video_stream_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            (folder / "video123.mp4").write_bytes(b"video")

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
            ).check(root, date(2026, 7, 23), date(2026, 7, 23), self.settings)

        self.assertTrue(result.is_passed)
        self.assertEqual(
            result.folders[0].status,
            ZoomPreflightStatus.READY,
        )

    def test_onedrive_placeholder_is_not_available_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            (folder / "video123.mp4").write_bytes(b"placeholder")

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
                local_availability_checker=lambda _path: False,
            ).check(root, date(2026, 7, 23), date(2026, 7, 23), self.settings)

        self.assertEqual(
            result.folders[0].status,
            ZoomPreflightStatus.VIDEO_NOT_AVAILABLE_LOCALLY,
        )

    def test_stable_mp4_without_video_stream_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            (folder / "video123.mp4").write_bytes(b"audio-only")

            result = ZoomPreflightService(
                metadata_probe=lambda _path: Mp4Metadata(
                    duration_seconds=60,
                    has_video_stream=False,
                ),
            ).check(root, date(2026, 7, 23), date(2026, 7, 23), self.settings)

        self.assertEqual(
            result.folders[0].status,
            ZoomPreflightStatus.INVALID_VIDEO,
        )

    def test_locked_mp4_is_rendering_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            (folder / "video123.mp4").write_bytes(b"video")

            result = ZoomPreflightService(
                metadata_probe=lambda _path: (_ for _ in ()).throw(
                    PermissionError("locked")
                ),
            ).check(
                root,
                date(2026, 7, 23),
                date(2026, 7, 23),
                self.settings,
            )

        self.assertEqual(
            result.folders[0].status,
            ZoomPreflightStatus.RENDERING_IN_PROGRESS,
        )

    def test_all_folders_are_scanned_even_when_one_is_unrendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in range(1, 11):
                folder = (
                    root
                    / f"2026-07-{day:02} 14.00.00 Personal Meeting Room"
                )
                folder.mkdir()
                if day == 10:
                    (folder / "double_click_to_convert_01.zoom").touch()
                else:
                    (folder / f"video{day}.mp4").write_bytes(b"video")

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
            ).check(root, date(2026, 7, 1), date(2026, 7, 10), self.settings)

        self.assertEqual(len(result.folders), 10)
        self.assertEqual(len(result.ready_folders), 9)
        self.assertEqual(len(result.blocking_folders), 1)
        self.assertFalse(result.is_passed)

    def test_recheck_only_blocked_folder_can_become_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 14.02.10 Personal Meeting Room"
            folder.mkdir()
            artifact = folder / "double_click_to_convert_01.zoom"
            artifact.touch()
            service = ZoomPreflightService(metadata_probe=self._ready_probe)
            first = service.check(
                root,
                date(2026, 7, 23),
                date(2026, 7, 23),
                self.settings,
            )
            artifact.unlink()
            (folder / "video123.mp4").write_bytes(b"video")

            second = service.recheck_blocked(first, self.settings)

        self.assertTrue(second.is_passed)
        self.assertEqual(
            second.folders[0].status,
            ZoomPreflightStatus.READY,
        )

    def test_folders_outside_selected_dates_are_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in (22, 23, 24):
                folder = root / f"2026-07-{day} 14.00.00 Meeting"
                folder.mkdir()
                (folder / "video.mp4").write_bytes(b"video")

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
            ).check(root, date(2026, 7, 23), date(2026, 7, 23), self.settings)

        self.assertEqual(len(result.folders), 1)
        self.assertEqual(result.folders[0].start.date(), date(2026, 7, 23))

    def test_manual_folder_start_includes_nonstandard_folder_name(self) -> None:
        manual_start = datetime(
            2026,
            7,
            23,
            14,
            2,
            10,
            tzinfo=timezone.utc,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "renamed Zoom recording"
            folder.mkdir()
            (folder / "video123.mp4").write_bytes(b"video")

            result = ZoomPreflightService(
                metadata_probe=self._ready_probe,
            ).check(
                root,
                date(2026, 7, 23),
                date(2026, 7, 23),
                self.settings,
                manual_folder_starts={folder: manual_start},
            )

        self.assertEqual(len(result.folders), 1)
        self.assertEqual(result.folders[0].folder, folder)
        self.assertEqual(result.folders[0].start, manual_start)


if __name__ == "__main__":
    unittest.main()
