from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lesson_video_uploader.debug_logging import write_debug_exception


class DebugLoggingTests(unittest.TestCase):
    def test_traceback_is_written_without_api_hash_value(self) -> None:
        secret = "telegram-api-hash-must-not-leak"
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "debug.log"
            try:
                raise RuntimeError(f"keyring failure near {secret}")
            except RuntimeError as error:
                write_debug_exception(
                    error,
                    secrets=(secret,),
                    log_path=log_path,
                )

            text = log_path.read_text(encoding="utf-8")

        self.assertIn("Traceback", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
