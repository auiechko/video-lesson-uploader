from __future__ import annotations

import importlib
import unittest

from telethon.crypto import aes as telethon_aes


class TelethonAccelerationTests(unittest.TestCase):
    def test_native_cryptg_backend_is_available_and_round_trips(self) -> None:
        cryptg = importlib.import_module("cryptg")
        plain_text = b"lesson-video" + (b"\0" * 4)
        key = bytes(range(32))
        iv = bytes(range(32, 64))

        encrypted = telethon_aes.AES.encrypt_ige(plain_text, key, iv)

        self.assertIs(telethon_aes.cryptg, cryptg)
        self.assertNotEqual(encrypted, plain_text)
        self.assertEqual(
            telethon_aes.AES.decrypt_ige(encrypted, key, iv),
            plain_text,
        )


if __name__ == "__main__":
    unittest.main()
