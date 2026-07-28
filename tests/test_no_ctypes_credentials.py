from __future__ import annotations

import unittest
from pathlib import Path


class NoCtypesCredentialStorageTests(unittest.TestCase):
    def test_source_has_no_custom_windows_credential_manager_ctypes(self) -> None:
        source_root = Path(__file__).parents[1] / "src"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in source_root.rglob("*.py")
        )

        for forbidden in (
            "ctypes.c_byte",
            "CredentialBlob",
            "CredWriteW",
            "CredReadW",
            "WindowsCredentialStore",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
