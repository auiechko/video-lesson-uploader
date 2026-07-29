from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lesson_video_uploader.config import (
    DEFAULT_ALBUM_BATCH_TEMPLATE,
    AppConfig,
    load_config,
    save_config,
)


class ConfigTests(unittest.TestCase):
    def test_default_config_has_album_batch_and_no_part_template(self) -> None:
        config = load_config()
        self.assertEqual(config.album_batch, DEFAULT_ALBUM_BATCH_TEMPLATE)
        self.assertEqual(config.calendar_conflict_tolerance_minutes, 10)
        self.assertEqual(config.automatic_time_tolerance_minutes, 30)
        self.assertEqual(config.manual_time_search_window_minutes, 180)
        self.assertEqual(config.next_lesson_overlap_tolerance_minutes, 10)
        self.assertEqual(config.minimum_video_size_mb, 5)
        self.assertEqual(config.video_stability_check_seconds, 5)
        self.assertTrue(config.zoom_recordings_dir)
        self.assertFalse(hasattr(config, "part"))

    def test_custom_album_batch_template_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[caption]\nalbum_batch = " [пакет {album_number}/{album_count}]"\n',
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(
            config.album_batch,
            " [пакет {album_number}/{album_count}]",
        )

    def test_telegram_settings_are_loaded_without_storing_api_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[telegram]\n"
                "api_id = 123456\n"
                'session = "sessions/uploader"\n',
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.api_id, 123456)
        self.assertFalse(hasattr(config, "api_hash"))
        self.assertEqual(config.session, "sessions/uploader")

    def test_invalid_album_template_is_rejected_early(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[caption]\nalbum_batch = "пакет"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "album_number"):
                load_config(path)

    def test_saved_gui_config_never_contains_api_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save_config(path, AppConfig(
                api_id=123456,
                session=".lesson-video-uploader/telegram",
                phone="+380991234567",
            ))

            text = path.read_text(encoding="utf-8")
            loaded = load_config(path)

        self.assertNotIn("api_hash =", text)
        self.assertNotIn("api_hash_env", text)
        self.assertEqual(loaded.api_id, 123456)
        self.assertEqual(loaded.phone, "+380991234567")

    def test_google_calendar_settings_round_trip_without_oauth_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save_config(path, AppConfig(
                google_client_secrets="C:/secrets/google-credentials.json",
                google_calendar_id="lessons@example.com",
                google_timezone="Europe/Kyiv",
                calendar_conflict_tolerance_minutes=15,
            ))

            text = path.read_text(encoding="utf-8")
            loaded = load_config(path)

        self.assertEqual(
            loaded.google_client_secrets,
            "C:/secrets/google-credentials.json",
        )
        self.assertEqual(loaded.google_calendar_id, "lessons@example.com")
        self.assertEqual(loaded.google_timezone, "Europe/Kyiv")
        self.assertEqual(loaded.calendar_conflict_tolerance_minutes, 15)
        self.assertNotIn("refresh_token", text)

    def test_negative_calendar_conflict_tolerance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[google_calendar]\n"
                "calendar_conflict_tolerance_minutes = -1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "tolerance"):
                load_config(path)

    def test_zoom_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save_config(
                path,
                AppConfig(
                    zoom_recordings_dir="C:/Users/teacher/Documents/Zoom",
                    automatic_time_tolerance_minutes=17,
                    manual_time_search_window_minutes=120,
                    next_lesson_overlap_tolerance_minutes=8,
                    minimum_video_size_mb=7.5,
                    video_stability_check_seconds=3,
                ),
            )

            text = path.read_text(encoding="utf-8")
            loaded = load_config(path)

        self.assertIn("[zoom]", text)
        self.assertEqual(
            loaded.zoom_recordings_dir,
            "C:/Users/teacher/Documents/Zoom",
        )
        self.assertEqual(loaded.automatic_time_tolerance_minutes, 17)
        self.assertEqual(loaded.manual_time_search_window_minutes, 120)
        self.assertEqual(loaded.next_lesson_overlap_tolerance_minutes, 8)
        self.assertEqual(loaded.minimum_video_size_mb, 7.5)
        self.assertEqual(loaded.video_stability_check_seconds, 3)

    def test_negative_zoom_tolerance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[zoom]\nautomatic_time_tolerance_minutes = -1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "zoom.automatic_time_tolerance",
            ):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
