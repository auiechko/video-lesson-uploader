from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from lesson_video_uploader.telegram_runtime import (
    create_telegram_client,
    telegram_session_path,
)


class SecretSubclass(str):
    pass


class TelegramRuntimeTests(unittest.TestCase):
    def test_profile_session_path_is_absolute_and_has_no_session_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = telegram_session_path(
                "teacher-main",
                base_dir=Path(directory),
            )

        self.assertTrue(path.is_absolute())
        self.assertEqual(
            path.parts[-4:],
            ("profiles", "teacher-main", "telegram", "telegram"),
        )
        self.assertNotEqual(path.suffix, ".session")

    def test_session_directory_exists_before_telethon_factory_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = telegram_session_path(
                "main",
                base_dir=Path(directory),
            )
            observed_parent_states: list[bool] = []

            def factory(**kwargs):
                observed_parent_states.append(
                    Path(kwargs["session"]).parent.is_dir()
                )
                return object()

            create_telegram_client(
                factory,
                profile_id="main",
                api_id="123456",
                api_hash=SecretSubclass("api-hash"),
                base_dir=Path(directory),
            )

        self.assertEqual(observed_parent_states, [True])
        self.assertEqual(expected.name, "telegram")

    def test_telegram_client_receives_plain_int_and_str_values(self) -> None:
        factory = Mock(return_value=object())
        with tempfile.TemporaryDirectory() as directory:
            create_telegram_client(
                factory,
                profile_id="main",
                api_id="123456",
                api_hash=SecretSubclass("api-hash"),
                base_dir=Path(directory),
            )

        kwargs = factory.call_args.kwargs
        self.assertIs(type(kwargs["api_id"]), int)
        self.assertIs(type(kwargs["api_hash"]), str)
        self.assertIsInstance(kwargs["session"], Path)

    def test_invalid_api_id_and_blank_hash_are_rejected(self) -> None:
        factory = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "API ID"):
                create_telegram_client(
                    factory,
                    profile_id="main",
                    api_id=0,
                    api_hash="hash",
                    base_dir=Path(directory),
                )
            with self.assertRaisesRegex(ValueError, "API hash"):
                create_telegram_client(
                    factory,
                    profile_id="main",
                    api_id=123,
                    api_hash=" ",
                    base_dir=Path(directory),
                )

        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
