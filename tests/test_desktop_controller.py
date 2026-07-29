from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lesson_video_uploader.desktop_controller import (
    DesktopSettingsController,
    LessonForm,
    build_gui_manifest,
    create_lesson_from_form,
    form_from_lesson,
    parse_target_peer,
)
from lesson_video_uploader.models import Lesson


class FakeCredentialStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def get_secret(self) -> str | None:
        return self.value

    def set_secret(self, secret: str) -> None:
        self.value = secret

    def delete_secret(self) -> None:
        self.value = None


class DesktopSettingsControllerTests(unittest.TestCase):
    def test_saves_regular_settings_and_secret_in_separate_stores(self) -> None:
        credentials = FakeCredentialStore()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            controller = DesktopSettingsController(config_path, credentials)

            result = controller.save(
                api_id_text="123456",
                api_hash="top-secret",
                phone="+380991234567",
                session=".lesson-video-uploader/telegram",
            )

            config_text = config_path.read_text(encoding="utf-8")
        self.assertEqual(result.config.api_id, 123456)
        self.assertTrue(result.api_hash_saved)
        self.assertEqual(credentials.value, "top-secret")
        self.assertNotIn("top-secret", config_text)

    def test_blank_hash_preserves_an_existing_credential(self) -> None:
        credentials = FakeCredentialStore()
        credentials.value = "already-saved"
        with tempfile.TemporaryDirectory() as directory:
            controller = DesktopSettingsController(
                Path(directory) / "config.toml",
                credentials,
            )

            result = controller.save(
                api_id_text="123456",
                api_hash="",
                phone="+380991234567",
                session="telegram",
            )

        self.assertTrue(result.api_hash_saved)
        self.assertEqual(credentials.value, "already-saved")

    def test_invalid_api_id_is_rejected_without_saving_secret(self) -> None:
        credentials = FakeCredentialStore()
        with tempfile.TemporaryDirectory() as directory:
            controller = DesktopSettingsController(
                Path(directory) / "config.toml",
                credentials,
            )
            with self.assertRaisesRegex(ValueError, "API ID"):
                controller.save(
                    api_id_text="not-a-number",
                    api_hash="must-not-save",
                    phone="+380991234567",
                    session="telegram",
                )
        self.assertIsNone(credentials.value)

    def test_secret_is_required_before_telegram_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = DesktopSettingsController(
                Path(directory) / "config.toml",
                FakeCredentialStore(),
            )
            with self.assertRaisesRegex(ValueError, "API hash"):
                controller.require_api_hash()

    def test_google_calendar_settings_preserve_telegram_configuration(self) -> None:
        credentials = FakeCredentialStore()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            controller = DesktopSettingsController(config_path, credentials)
            controller.save(
                api_id_text="123456",
                api_hash="telegram-secret",
                phone="+380991234567",
                session="telegram-session",
            )

            result = controller.save_google_calendar(
                client_secrets="C:/google/credentials.json",
                calendar_id="primary",
                timezone_name="Europe/Kyiv",
                zoom_recordings_dir="D:/Zoom Recordings",
                automatic_time_tolerance_minutes=25,
                manual_time_search_window_minutes=150,
                next_lesson_overlap_tolerance_minutes=7,
                minimum_video_size_mb=6,
                video_stability_check_seconds=4,
            )

        self.assertEqual(result.config.api_id, 123456)
        self.assertTrue(Path(result.config.session).is_absolute())
        self.assertEqual(Path(result.config.session).name, "telegram")
        self.assertEqual(
            result.config.google_client_secrets,
            "C:/google/credentials.json",
        )
        self.assertEqual(
            result.config.zoom_recordings_dir,
            "D:/Zoom Recordings",
        )
        self.assertEqual(result.config.automatic_time_tolerance_minutes, 25)
        self.assertEqual(result.config.manual_time_search_window_minutes, 150)
        self.assertEqual(result.config.next_lesson_overlap_tolerance_minutes, 7)
        self.assertEqual(result.config.minimum_video_size_mb, 6)
        self.assertEqual(result.config.video_stability_check_seconds, 4)
        self.assertEqual(credentials.value, "telegram-secret")

    def test_environment_variable_is_not_used_instead_of_keyring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = DesktopSettingsController(
                Path(directory) / "config.toml",
                FakeCredentialStore(),
            )
            with self.assertRaisesRegex(ValueError, "API hash"):
                controller.require_api_hash()


class TargetPeerTests(unittest.TestCase):
    def test_numeric_peer_is_converted_to_integer(self) -> None:
        self.assertEqual(parse_target_peer(" -100123 "), -100123)

    def test_username_and_saved_messages_are_strings(self) -> None:
        self.assertEqual(parse_target_peer("@lessons"), "@lessons")
        self.assertEqual(parse_target_peer("me"), "me")

    def test_empty_peer_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "чат"):
            parse_target_peer(" ")


class LessonFormTests(unittest.TestCase):
    def test_builds_caption_and_preserves_file_picker_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "part-2.mp4", root / "part-1.mp4")
            for path in paths:
                path.touch()
            lesson = create_lesson_from_form(
                profile_id="main",
                batch_id="batch",
                form=LessonForm(
                    calendar_event_id="event-1",
                    event_start="2026-06-12 10:30",
                    student_id="105813989",
                    student_name="Ільяс",
                    lesson_label="10р індив",
                    duration_hours=2,
                    is_trial=True,
                    video_paths=paths,
                ),
            )
        self.assertEqual(
            lesson.caption,
            "12.06.2026 105813989 Ільяс 10р індив ДВІ ГОДИНИ (пробне)",
        )
        self.assertEqual(lesson.ordered_video_paths, paths)

    def test_manifest_uses_current_batch_fields_and_numeric_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one.mp4"
            path.touch()
            lesson = create_lesson_from_form(
                profile_id="old",
                batch_id="old",
                form=LessonForm(
                    calendar_event_id="event",
                    event_start="2026-06-12T10:30:00",
                    student_id="1",
                    student_name="Name",
                    lesson_label="lesson",
                    duration_hours=1,
                    is_trial=False,
                    video_paths=(path,),
                ),
            )

            manifest = build_gui_manifest(
                profile_id="main",
                batch_id="new-batch",
                target_peer="-100123",
                lessons=(lesson,),
            )

        self.assertEqual(manifest.target_peer, -100123)
        self.assertEqual(manifest.lessons[0].profile_id, "main")
        self.assertEqual(manifest.lessons[0].batch_id, "new-batch")

    def test_form_rejects_missing_or_non_mp4_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mov"
            path.touch()
            with self.assertRaisesRegex(ValueError, "MP4"):
                create_lesson_from_form(
                    profile_id="main",
                    batch_id="batch",
                    form=LessonForm(
                        calendar_event_id="event",
                        event_start="2026-06-12 10:30",
                        student_id="1",
                        student_name="Name",
                        lesson_label="lesson",
                        duration_hours=1,
                        is_trial=False,
                        video_paths=(path,),
                    ),
                )

    def test_lesson_can_be_pulled_back_into_the_editor_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "part-1.mp4", root / "part-2.mp4")
            for path in paths:
                path.touch()
            form = LessonForm(
                calendar_event_id="event-1",
                event_start="2026-06-12 10:30",
                student_id="105813989",
                student_name="Іван Петров",
                lesson_label="10р індив",
                duration_hours=2,
                is_trial=True,
                video_paths=paths,
            )
            lesson = create_lesson_from_form(
                profile_id="main",
                batch_id="batch",
                form=form,
            )

            restored = form_from_lesson(lesson)

        self.assertEqual(restored, form)

    def test_lesson_without_details_returns_blank_student_fields(self) -> None:
        lesson = Lesson(
            profile_id="main",
            batch_id="batch",
            calendar_event_id="event-1",
            event_start=datetime(2026, 6, 12, 10, 30),
            caption="написаний вручну caption",
            ordered_video_paths=(Path("one.mp4"),),
        )

        restored = form_from_lesson(lesson)

        self.assertEqual(restored.student_name, "")
        self.assertEqual(restored.duration_hours, 1)
        self.assertEqual(restored.event_start, "2026-06-12 10:30")
        self.assertEqual(restored.video_paths, (Path("one.mp4"),))

    def test_form_rejects_blank_student_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.touch()
            with self.assertRaisesRegex(ValueError, "учня"):
                create_lesson_from_form(
                    profile_id="main",
                    batch_id="batch",
                    form=LessonForm(
                        calendar_event_id="event",
                        event_start="2026-06-12 10:30",
                        student_id="",
                        student_name="",
                        lesson_label="",
                        duration_hours=1,
                        is_trial=False,
                        video_paths=(path,),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
