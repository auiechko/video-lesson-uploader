from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lesson_video_uploader.cli import _delivery_summary, main, render_dialogs
from lesson_video_uploader.models import Lesson, SendStatus
from lesson_video_uploader.sender import ManualReviewRequired


class CliTests(unittest.TestCase):
    def test_preview_command_prints_one_row_and_expanded_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.mp4").touch()
            (root / "two.mp4").touch()
            manifest = root / "batch.json"
            manifest.write_text(json.dumps({
                "profile_id": "profile-1",
                "batch_id": "batch-1",
                "target_peer": "group",
                "lessons": [{
                    "calendar_event_id": "event-1",
                    "event_start": "2026-06-12T10:00:00",
                    "student_id": "105813989",
                    "student_name": "Ільяс",
                    "lesson_label": "10р індив",
                    "videos": ["one.mp4", "two.mp4"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                result = main(["preview", str(manifest), "--expand"])

        self.assertEqual(result, 0)
        self.assertIn(
            "12.06.2026 | Ільяс | 2 відео | Один Telegram-альбом",
            output.getvalue(),
        )
        self.assertIn("  - one.mp4", output.getvalue())

    def test_invalid_manifest_returns_nonzero_without_traceback(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = main(["preview", "missing.json"])
        self.assertEqual(result, 2)
        self.assertIn("Помилка:", error.getvalue())

    def test_unconfirmed_delivery_cannot_be_rendered_as_success(self) -> None:
        item = Lesson(
            profile_id="p",
            batch_id="b",
            calendar_event_id="e",
            event_start=datetime(2026, 6, 12, 10),
            caption="caption",
            ordered_video_paths=(Path("one.mp4"), Path("two.mp4")),
            status=SendStatus.PARTIALLY_CONFIRMED,
            telegram_message_ids=(101,),
        )
        with self.assertRaises(ManualReviewRequired):
            _delivery_summary(item)

    def test_reconcile_command_runs_read_only_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.mp4").touch()
            manifest = root / "batch.json"
            manifest.write_text(json.dumps({
                "profile_id": "profile-1",
                "batch_id": "batch-1",
                "target_peer": "group",
                "lessons": [{
                    "calendar_event_id": "event-1",
                    "event_start": "2026-06-12T10:00:00",
                    "student_id": "1",
                    "student_name": "Ільяс",
                    "lesson_label": "урок",
                    "videos": ["one.mp4"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            database = root / "deliveries.sqlite3"

            with patch(
                "lesson_video_uploader.cli._reconcile",
                new_callable=AsyncMock,
                create=True,
            ) as reconcile:
                result = main([
                    "reconcile",
                    str(manifest),
                    "--database",
                    str(database),
                ])

        self.assertEqual(result, 0)
        reconcile.assert_awaited_once()


class ChatsCommandTests(unittest.TestCase):
    def test_dialogs_are_rendered_as_pastable_target_peer_values(self) -> None:
        output = render_dialogs([
            SimpleNamespace(id=-1001234567890, name="Уроки"),
            SimpleNamespace(id=777000, name="Telegram"),
        ])

        self.assertIn("-1001234567890\tУроки", output)
        self.assertIn("777000\tTelegram", output)

    def test_empty_dialog_list_says_so_instead_of_printing_a_bare_header(self) -> None:
        self.assertEqual(render_dialogs([]), "Доступних чатів не знайдено.")

    def test_chats_command_needs_no_manifest(self) -> None:
        with patch(
            "lesson_video_uploader.cli._chats",
            new_callable=AsyncMock,
            create=True,
        ) as chats:
            with redirect_stdout(io.StringIO()):
                result = main(["chats"])

        self.assertEqual(result, 0)
        chats.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
