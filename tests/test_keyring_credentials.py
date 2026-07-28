from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lesson_video_uploader.credentials import (
    KeyringSecretStore,
    SecretStoreError,
)


class FakeKeyringError(Exception):
    pass


class FakePasswordDeleteError(FakeKeyringError):
    pass


def backend() -> Mock:
    result = Mock()
    result.errors = SimpleNamespace(
        KeyringError=FakeKeyringError,
        PasswordDeleteError=FakePasswordDeleteError,
    )
    return result


class KeyringSecretStoreTests(unittest.TestCase):
    def test_telegram_api_hash_is_written_under_profile_account(self) -> None:
        keyring_backend = backend()
        store = KeyringSecretStore(backend=keyring_backend)

        store.set_telegram_api_hash("teacher-main", "  api-hash  ")

        keyring_backend.set_password.assert_called_once_with(
            "LessonVideoUploader",
            "teacher-main:telegram_api_hash",
            "api-hash",
        )

    def test_telegram_api_hash_is_read_from_profile_account(self) -> None:
        keyring_backend = backend()
        keyring_backend.get_password.return_value = "api-hash"
        store = KeyringSecretStore(backend=keyring_backend)

        result = store.get_telegram_api_hash("teacher-main")

        self.assertEqual(result, "api-hash")
        keyring_backend.get_password.assert_called_once_with(
            "LessonVideoUploader",
            "teacher-main:telegram_api_hash",
        )

    def test_blank_telegram_api_hash_is_rejected_before_keyring_call(self) -> None:
        keyring_backend = backend()
        store = KeyringSecretStore(backend=keyring_backend)

        with self.assertRaisesRegex(ValueError, "не може бути порожнім"):
            store.set_telegram_api_hash("main", "   ")

        keyring_backend.set_password.assert_not_called()

    def test_missing_password_can_be_deleted_idempotently(self) -> None:
        keyring_backend = backend()
        keyring_backend.delete_password.side_effect = FakePasswordDeleteError()
        store = KeyringSecretStore(backend=keyring_backend)

        store.delete_telegram_api_hash("main")

    def test_keyring_failure_has_safe_ukrainian_message(self) -> None:
        keyring_backend = backend()
        keyring_backend.get_password.side_effect = FakeKeyringError(
            "backend internals"
        )
        store = KeyringSecretStore(backend=keyring_backend)

        with self.assertRaisesRegex(
            SecretStoreError,
            "захищеного сховища",
        ) as captured:
            store.get_telegram_api_hash("main")

        self.assertNotIn("backend internals", str(captured.exception))

    def test_generic_adapter_can_store_google_oauth_token(self) -> None:
        keyring_backend = backend()
        store = KeyringSecretStore(
            profile_id="main",
            credential_name="google_calendar_oauth",
            backend=keyring_backend,
        )

        store.set_secret("oauth-json")

        keyring_backend.set_password.assert_called_once_with(
            "LessonVideoUploader",
            "main:google_calendar_oauth",
            "oauth-json",
        )


if __name__ == "__main__":
    unittest.main()
