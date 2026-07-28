from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lesson_video_uploader.manifest import (
    UploadManifest,
    load_manifest,
    render_manifest_preview,
    save_manifest,
)
from lesson_video_uploader.models import Lesson
from datetime import datetime


class ManifestTests(unittest.TestCase):
    def test_manifest_builds_one_lesson_with_chronologically_ordered_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "later.mp4").touch()
            (root / "first.mp4").touch()
            path = root / "batch.json"
            path.write_text(json.dumps({
                "profile_id": "profile-1",
                "batch_id": "batch-1",
                "target_peer": -100123,
                "lessons": [{
                    "calendar_event_id": "event-1",
                    "event_start": "2026-06-12T10:00:00+03:00",
                    "student_id": "105813989",
                    "student_name": "Ільяс",
                    "lesson_label": "10р індив",
                    "duration_hours": 2,
                    "videos": [
                        {"path": "later.mp4", "order": 2},
                        {"path": "first.mp4", "order": 1},
                    ],
                }],
            }, ensure_ascii=False), encoding="utf-8")

            manifest = load_manifest(path)

        self.assertEqual(len(manifest.lessons), 1)
        lesson = manifest.lessons[0]
        self.assertEqual(
            tuple(item.name for item in lesson.ordered_video_paths),
            ("first.mp4", "later.mp4"),
        )
        self.assertEqual(
            lesson.caption,
            "12.06.2026 105813989 Ільяс 10р індив ДВІ ГОДИНИ",
        )

    def test_preview_is_one_row_with_expandable_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.mp4", "two.mp4"):
                (root / name).touch()
            path = root / "batch.json"
            path.write_text(json.dumps({
                "profile_id": "profile-1",
                "batch_id": "batch-1",
                "target_peer": "lesson-group",
                "lessons": [{
                    "calendar_event_id": "event-1",
                    "event_start": "2026-06-12T10:00:00",
                    "student_id": "105813989",
                    "student_name": "Ільяс",
                    "lesson_label": "10р індив",
                    "videos": ["one.mp4", "two.mp4"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            manifest = load_manifest(path)

            collapsed = render_manifest_preview(manifest, expand=False)
            expanded = render_manifest_preview(manifest, expand=True)

        self.assertEqual(
            collapsed,
            "12.06.2026 | Ільяс | 2 відео | Один Telegram-альбом",
        )
        self.assertIn("  - one.mp4", expanded)
        self.assertIn("  - two.mp4", expanded)

    def test_non_mp4_video_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video.mov").touch()
            path = root / "batch.json"
            path.write_text(json.dumps({
                "profile_id": "p",
                "batch_id": "b",
                "target_peer": "group",
                "lessons": [{
                    "calendar_event_id": "e",
                    "event_start": "2026-06-12T10:00:00",
                    "student_id": "1",
                    "student_name": "Name",
                    "lesson_label": "lesson",
                    "videos": ["video.mov"],
                }],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "MP4"):
                load_manifest(path)

    def test_gui_manifest_round_trip_preserves_exact_caption_and_video_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "one.mp4", root / "two.mp4")
            for path in paths:
                path.touch()
            manifest = UploadManifest(
                profile_id="main",
                batch_id="batch-1",
                target_peer="me",
                lessons=(Lesson(
                    profile_id="main",
                    batch_id="batch-1",
                    calendar_event_id="event-1",
                    event_start=datetime(2026, 6, 12, 10),
                    caption="12.06.2026 105813989 Ільяс 10р індив (пробне)",
                    ordered_video_paths=paths,
                ),),
            )
            output = root / "saved-batch.json"

            save_manifest(manifest, output)
            loaded = load_manifest(output)

        self.assertEqual(loaded.lessons[0].caption, manifest.lessons[0].caption)
        self.assertEqual(
            tuple(path.name for path in loaded.lessons[0].ordered_video_paths),
            ("one.mp4", "two.mp4"),
        )


if __name__ == "__main__":
    unittest.main()
