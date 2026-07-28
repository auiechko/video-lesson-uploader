from __future__ import annotations

import ctypes
import importlib
import sys
import unittest
from unittest.mock import patch

from lesson_video_uploader.credentials import WindowsCredentialStore


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


if __name__ == "__main__":
    unittest.main()
