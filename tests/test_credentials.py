from __future__ import annotations

import ctypes
import importlib
import os
import sys
import unittest
from unittest.mock import patch

from lesson_video_uploader.credentials import WindowsCredentialStore, resolve_api_hash


class _RejectWintypes:
    """Fail ``ctypes.wintypes`` the way a non-Windows interpreter does.

    On Linux the module raises at import time, because ``VARIANT_BOOL`` is
    declared with ``_type_ = "v"`` and that type code only exists in the
    Windows build of ``_ctypes``.
    """

    MESSAGE = "class must define a '_type_' attribute"

    def find_spec(self, name: str, path: object = None, target: object = None):
        if name == "ctypes.wintypes":
            raise ValueError(self.MESSAGE)
        return None


class CredentialsImportTests(unittest.TestCase):
    def test_module_imports_where_ctypes_wintypes_is_unavailable(self) -> None:
        """CI runs on Linux, so importing this module must not need wintypes."""
        blocker = _RejectWintypes()
        saved_module = sys.modules.pop("lesson_video_uploader.credentials")
        saved_wintypes = sys.modules.pop("ctypes.wintypes", None)
        had_attribute = hasattr(ctypes, "wintypes")
        if had_attribute:
            del ctypes.wintypes
        sys.meta_path.insert(0, blocker)
        try:
            module = importlib.import_module("lesson_video_uploader.credentials")
            self.assertTrue(callable(module.WindowsCredentialStore))
        finally:
            sys.meta_path.remove(blocker)
            sys.modules["lesson_video_uploader.credentials"] = saved_module
            if saved_wintypes is not None:
                sys.modules["ctypes.wintypes"] = saved_wintypes
                ctypes.wintypes = saved_wintypes

    def test_non_windows_platform_reports_a_clear_error(self) -> None:
        store = WindowsCredentialStore()
        with patch("lesson_video_uploader.credentials.os.name", "posix"):
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                store.get_secret()


class FakeCredentialStore:
    def __init__(self, secret: str | None = None) -> None:
        self.value = secret

    def get_secret(self) -> str | None:
        return self.value

    def set_secret(self, secret: str) -> None:
        self.value = secret

    def delete_secret(self) -> None:
        self.value = None


class ResolveApiHashTests(unittest.TestCase):
    """The CLI and the GUI must accept the same secret from either store."""

    def test_environment_variable_takes_precedence(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_API_HASH": " from-env "}, clear=True):
            secret = resolve_api_hash(
                "TELEGRAM_API_HASH",
                FakeCredentialStore("from-vault"),
            )
        self.assertEqual(secret, "from-env")

    def test_credential_manager_is_used_when_the_variable_is_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            secret = resolve_api_hash(
                "TELEGRAM_API_HASH",
                FakeCredentialStore("from-vault"),
            )
        self.assertEqual(secret, "from-vault")

    def test_missing_secret_names_both_places_to_put_it(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                resolve_api_hash("TELEGRAM_API_HASH", FakeCredentialStore(None))
        message = str(caught.exception)
        self.assertIn("API hash", message)
        self.assertIn("TELEGRAM_API_HASH", message)
        self.assertIn("Credential Manager", message)

    def test_vault_is_not_consulted_on_non_windows_platforms(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("lesson_video_uploader.credentials.os.name", "posix"):
                with self.assertRaises(ValueError):
                    resolve_api_hash("TELEGRAM_API_HASH")


if __name__ == "__main__":
    unittest.main()
