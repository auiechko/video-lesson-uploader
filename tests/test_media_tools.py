from __future__ import annotations

import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from subprocess import CompletedProcess

from lesson_video_uploader.media_tools import (
    boundary_preview_offsets,
    general_preview_offsets,
    parse_ffmpeg_metadata,
    probe_mp4,
    split_mp4,
    subprocess_window_options,
)


class MediaToolsTests(unittest.TestCase):
    def test_parses_duration_and_timezone_aware_creation_time(self) -> None:
        metadata = parse_ffmpeg_metadata(
            """
            creation_time   : 2026-07-23T11:02:10.000000Z
            Duration: 00:52:00.50, start: 0.000000, bitrate: 1000 kb/s
            Stream #0:0: Video: h264
            """
        )

        self.assertEqual(metadata.duration_seconds, 3120.5)
        self.assertEqual(metadata.creation_time.tzinfo, timezone.utc)
        self.assertTrue(metadata.has_video_stream)

    def test_audio_only_metadata_has_no_video_stream(self) -> None:
        metadata = parse_ffmpeg_metadata(
            """
            Duration: 00:10:00.00, start: 0.000000, bitrate: 128 kb/s
            Stream #0:0: Audio: aac
            """
        )

        self.assertFalse(metadata.has_video_stream)

    def test_preview_offsets_follow_general_and_boundary_rules(self) -> None:
        self.assertEqual(
            general_preview_offsets(1000),
            (100.0, 350.0, 600.0, 850.0),
        )
        self.assertEqual(
            boundary_preview_offsets(1000, boundary_seconds=300),
            (210, 285, 315, 390),
        )

    def test_split_planner_creates_two_outputs_at_requested_boundary(self) -> None:
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).touch()
            return CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.touch()

            outputs = split_mp4(
                source,
                split_offset_seconds=57 * 60 + 50,
                output_dir=root / "split",
                ffmpeg_path="ffmpeg",
                runner=runner,
            )

            self.assertTrue(all(path.is_file() for path in outputs))

        self.assertEqual(len(commands), 2)
        self.assertIn("3470.000", commands[0])
        self.assertIn("3470.000", commands[1])

    def test_windows_ffmpeg_processes_are_created_without_console_window(
        self,
    ) -> None:
        options = subprocess_window_options(platform_name="nt")

        self.assertEqual(
            int(options["creationflags"]) & 0x08000000,
            0x08000000,
        )

    def test_probe_passes_hidden_window_options_to_runner_on_windows(
        self,
    ) -> None:
        received_kwargs: dict[str, object] = {}

        def runner(command, **kwargs):
            received_kwargs.update(kwargs)
            return CompletedProcess(
                command,
                0,
                "",
                (
                    "Duration: 00:01:00.00\n"
                    "Stream #0:0: Video: h264"
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.touch()

            probe_mp4(
                video,
                ffmpeg_path="ffmpeg",
                runner=runner,
            )

        if subprocess_window_options():
            self.assertEqual(
                int(received_kwargs["creationflags"]) & 0x08000000,
                0x08000000,
            )


if __name__ == "__main__":
    unittest.main()
