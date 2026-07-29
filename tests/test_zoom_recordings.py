from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lesson_video_uploader.zoom_recordings import (
    Mp4Metadata,
    VideoTimeMethod,
    ZoomFolderStatus,
    ZoomRecordingAmbiguous,
    ZoomRecordingCatalog,
    ZoomRecordingNotFound,
    ZoomRecordingPendingConversion,
    default_zoom_recordings_dir,
)


class ZoomRecordingCatalogTests(unittest.TestCase):
    def test_matches_zoom_folder_by_nearest_start_and_orders_all_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = (
                root
                / "2026-07-29 19.02.12 Уєчко Артем's Personal Meeting Room"
            )
            recording.mkdir()
            second = recording / "video_part_2.mp4"
            first = recording / "video_part_1.mp4"
            second.touch()
            first.touch()
            os.utime(first, (100, 100))
            os.utime(second, (200, 200))
            (recording / "audio.m4a").touch()
            (root / "not-a-zoom-folder").mkdir()

            catalog = ZoomRecordingCatalog.scan(
                root,
                timezone_name="Europe/Kyiv",
            )
            match = catalog.match(
                datetime(2026, 7, 29, 19, 0, tzinfo=ZoneInfo("Europe/Kyiv")),
                tolerance_minutes=15,
            )

        self.assertEqual(match.folder, recording)
        self.assertEqual(match.start.hour, 19)
        self.assertEqual(match.delta_seconds, 132)
        self.assertEqual(match.video_paths, (first, second))

    def test_reports_unconverted_zoom_recording_instead_of_missing_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "2026-07-29 20.08.22 Personal Meeting Room"
            recording.mkdir()
            (recording / "double_click_to_convert_01.zoom").touch()
            catalog = ZoomRecordingCatalog.scan(root)

            with self.assertRaises(ZoomRecordingPendingConversion) as raised:
                catalog.match(
                    datetime(
                        2026,
                        7,
                        29,
                        20,
                        0,
                        tzinfo=ZoneInfo("Europe/Kyiv"),
                    ),
                    tolerance_minutes=15,
                )

        self.assertEqual(raised.exception.folder, recording)

    def test_rejects_missing_and_equally_close_recordings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for timestamp in ("18.55.00", "19.05.00"):
                folder = root / f"2026-07-29 {timestamp} Personal Meeting Room"
                folder.mkdir()
                (folder / "video.mp4").touch()
            catalog = ZoomRecordingCatalog.scan(root)
            event_start = datetime(
                2026,
                7,
                29,
                19,
                0,
                tzinfo=ZoneInfo("Europe/Kyiv"),
            )

            with self.assertRaises(ZoomRecordingAmbiguous):
                catalog.match(event_start, tolerance_minutes=10)
            with self.assertRaises(ZoomRecordingNotFound):
                catalog.match(
                    event_start.replace(hour=21),
                    tolerance_minutes=10,
                )

    def test_detects_ukrainian_onedrive_zoom_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            expected = home / "OneDrive" / "Документи" / "Zoom"
            expected.mkdir(parents=True)

            detected = default_zoom_recordings_dir(home=home)

        self.assertEqual(detected, expected)

    def test_multiple_mp4_segments_use_duration_to_estimate_unknown_starts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 17.02.00 Personal Meeting Room"
            folder.mkdir()
            first = folder / "zoom_0.mp4"
            second = folder / "zoom_1.mp4"
            first.touch()
            second.touch()
            metadata = {
                first: Mp4Metadata(duration_seconds=52 * 60),
                second: Mp4Metadata(duration_seconds=47 * 60),
            }

            catalog = ZoomRecordingCatalog.scan(
                root,
                metadata_probe=metadata.__getitem__,
            )
            segments = catalog.folders[0].segments

        self.assertEqual(len(segments), 2)
        self.assertEqual(
            segments[0].time_method,
            VideoTimeMethod.ZOOM_FOLDER_START,
        )
        self.assertEqual(
            segments[1].time_method,
            VideoTimeMethod.SEQUENTIAL_DURATION,
        )
        self.assertEqual(
            segments[1].estimated_start,
            segments[0].estimated_end,
        )
        self.assertEqual(segments[1].duration_seconds, 47 * 60)

    def test_invalid_folder_is_reported_and_manual_datetime_can_restore_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "renamed Zoom folder"
            folder.mkdir()
            video = folder / "video.mp4"
            video.touch()

            invalid = ZoomRecordingCatalog.scan(
                root,
                metadata_probe=lambda _path: Mp4Metadata(60),
            )
            manual_start = datetime(
                2026,
                7,
                23,
                14,
                2,
                10,
                tzinfo=ZoneInfo("Europe/Kyiv"),
            )
            restored = ZoomRecordingCatalog.scan(
                root,
                metadata_probe=lambda _path: Mp4Metadata(60),
                manual_folder_starts={
                    str(folder.resolve()): manual_start,
                },
            )

        self.assertEqual(
            invalid.issues[0].status,
            ZoomFolderStatus.ZOOM_DATETIME_PARSE_ERROR,
        )
        self.assertEqual(restored.folders[0].start, manual_start)
        self.assertEqual(
            restored.folders[0].start_method,
            VideoTimeMethod.MANUALLY_CONFIRMED,
        )
        self.assertEqual(restored.folders[0].segments[0].path, video)

    def test_reliable_mp4_creation_time_orders_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 17.02.00 Personal Meeting Room"
            folder.mkdir()
            later_name = folder / "video_1.mp4"
            earlier_name = folder / "video_2.mp4"
            later_name.touch()
            earlier_name.touch()
            start = datetime(
                2026,
                7,
                23,
                17,
                2,
                tzinfo=ZoneInfo("Europe/Kyiv"),
            )
            metadata = {
                earlier_name: Mp4Metadata(
                    duration_seconds=60,
                    creation_time=start,
                ),
                later_name: Mp4Metadata(
                    duration_seconds=60,
                    creation_time=start + timedelta(minutes=1),
                ),
            }

            catalog = ZoomRecordingCatalog.scan(
                root,
                metadata_probe=metadata.__getitem__,
            )

        self.assertEqual(
            tuple(segment.path for segment in catalog.folders[0].segments),
            (earlier_name, later_name),
        )

    def test_unreliable_late_metadata_falls_back_to_sequential_duration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026-07-23 17.02.00 Personal Meeting Room"
            folder.mkdir()
            first = folder / "video_1.mp4"
            second = folder / "video_2.mp4"
            first.touch()
            second.touch()
            folder_start = datetime(
                2026,
                7,
                23,
                17,
                2,
                tzinfo=ZoneInfo("Europe/Kyiv"),
            )
            metadata = {
                first: Mp4Metadata(duration_seconds=60),
                second: Mp4Metadata(
                    duration_seconds=60,
                    creation_time=folder_start + timedelta(hours=3),
                ),
            }

            catalog = ZoomRecordingCatalog.scan(
                root,
                metadata_probe=metadata.__getitem__,
            )
            second_segment = catalog.folders[0].segments[1]

        self.assertEqual(
            second_segment.time_method,
            VideoTimeMethod.SEQUENTIAL_DURATION,
        )
        self.assertEqual(
            second_segment.estimated_start,
            folder_start + timedelta(minutes=1),
        )


if __name__ == "__main__":
    unittest.main()
