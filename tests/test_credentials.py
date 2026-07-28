from __future__ import annotations

import unittest

from lesson_video_uploader.credentials import resolve_api_hash


class FakeCredentialStore:
    def __init__(self, secret: str | None) -> None:
        self.secret = secret

    def get_secret(self) -> str | None:
        return self.secret

    def set_secret(self, secret: str) -> None:
        self.secret = secret

    def delete_secret(self) -> None:
        self.secret = None


class ResolveApiHashTests(unittest.TestCase):
    def test_reads_secret_from_store(self) -> None:
        secret = resolve_api_hash("main", FakeCredentialStore(" api-hash "))

        self.assertEqual(secret, "api-hash")

    def test_missing_secret_has_safe_message(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "захищеному сховищі",
        ):
            resolve_api_hash("main", FakeCredentialStore(None))


if __name__ == "__main__":
    unittest.main()
