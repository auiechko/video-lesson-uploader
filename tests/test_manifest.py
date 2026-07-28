from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lesson_video_uploader.calendar_rules import CalendarEventSnapshot
from lesson_video_uploader.manifest import (
    UploadManifest,
    load_manifest,
    render_manifest_preview,
    save_manifest,
)
from lesson_video_uploader.models import (
    Lesson,
    LessonDetails,
    LessonSendMode,
)


class ManifestTests(unittest.TestCase):
    def test_text_only_calendar_lesson_loads_without_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "batch.json"
            manifest_path.write_text(
                json.dumps({
                    "profile_id": "main",
                    "batch_id": "batch",
                    "target_peer": "me",
                    "lessons": [{
                        "calendar_event_id": "event-text",
                        "event_start": "2026-06-12T18:00:00+03:00",
                        "caption": (
                            "12.06.2026 105853087 Святослав "
                            "14р індив (без запису)"
                        ),
                        "send_mode": "TEXT_ONLY",
                        "videos": [],
                    }],
                }),
                encoding="utf-8",
            )

            loaded = load_manifest(manifest_path)

        self.assertEqual(
            loaded.lessons[0].send_mode,
            LessonSendMode.TEXT_ONLY,
        )
        self.assertEqual(loaded.lessons[0].ordered_video_paths, ())

    def test_calendar_snapshot_survives_saved_manifest_round_trip(self) -> None:
        snapshot = CalendarEventSnapshot(
            event_id="event-text",
            summary="105853087 Наталія (Святослав 15) Учко ТГ",
            student_id="105853087",
            student_name="Святослав",
            student_age=15,
            local_date="2026-06-12",
            start="2026-06-12T18:00:00+03:00",
            end="2026-06-12T19:00:00+03:00",
            duration_minutes=60,
            status="NO_RECORDING",
            is_trial=False,
            is_no_recording=True,
            is_transferred=False,
            is_cancelled=False,
            is_pause=False,
        )
        lesson = Lesson(
            profile_id="main",
            batch_id="batch",
            calendar_event_id="event-text",
            event_start=datetime.fromisoformat(snapshot.start),
            caption=(
                "12.06.2026 105853087 Святослав "
                "15р індив (без запису)"
            ),
            ordered_video_paths=(),
            send_mode=LessonSendMode.TEXT_ONLY,
            calendar_snapshot=snapshot,
            details=LessonDetails(
                student_id="105853087",
                student_name="Святослав",
                lesson_label="15р індив",
                student_age=15,
                calendar_status="NO_RECORDING",
                is_no_recording=True,
            ),
        )
        manifest = UploadManifest("main", "batch", "me", (lesson,))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"

            save_manifest(manifest, path)
            loaded = load_manifest(path)

        restored = loaded.lessons[0]
        self.assertEqual(restored.calendar_snapshot, snapshot)
        self.assertEqual(restored.send_mode, LessonSendMode.TEXT_ONLY)
        self.assertEqual(restored.details, lesson.details)

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

    def test_saved_batch_can_be_reopened_in_the_lesson_editor(self) -> None:
        details = LessonDetails(
            student_id="105813989",
            student_name="Іван Петров",
            lesson_label="10р індив",
            duration_hours=2,
            is_trial=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "one.mp4"
            video.touch()
            manifest = UploadManifest(
                profile_id="main",
                batch_id="batch-1",
                target_peer="me",
                lessons=(Lesson(
                    profile_id="main",
                    batch_id="batch-1",
                    calendar_event_id="event-1",
                    event_start=datetime(2026, 6, 12, 10),
                    caption="12.06.2026 105813989 Іван Петров 10р індив ДВІ ГОДИНИ (пробне)",
                    ordered_video_paths=(video,),
                    details=details,
                ),),
            )
            output = root / "saved-batch.json"

            save_manifest(manifest, output)
            loaded = load_manifest(output)

        self.assertEqual(loaded.lessons[0].details, details)

    def test_two_videos_claiming_the_same_position_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one.mp4", "two.mp4"):
                (root / name).touch()
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
                    "videos": [
                        {"path": "one.mp4", "order": 1},
                        {"path": "two.mp4", "order": 1},
                    ],
                }]}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "order"):
                load_manifest(path)

    def test_lesson_without_a_caption_still_requires_the_student_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.mp4").touch()
            path = root / "batch.json"
            path.write_text(json.dumps({
                "profile_id": "p",
                "batch_id": "b",
                "target_peer": "group",
                "lessons": [{
                    "calendar_event_id": "e",
                    "event_start": "2026-06-12T10:00:00",
                    "videos": ["one.mp4"],
                }],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "student_id"):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
