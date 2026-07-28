from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lesson_video_uploader.desktop import (
    bind_api_hash_paste,
    is_ctrl_v_shortcut,
    paste_clipboard_into_entry,
)


class DesktopClipboardTests(unittest.TestCase):
    def test_ctrl_v_uses_physical_keycode_with_ukrainian_layout(self) -> None:
        event = SimpleNamespace(
            state=0x0004,
            keycode=86,
            keysym="Cyrillic_em",
        )

        self.assertTrue(
            is_ctrl_v_shortcut(
                state=event.state,
                keycode=event.keycode,
            )
        )

    def test_clipboard_replaces_selected_hash_text(self) -> None:
        entry = Mock()

        result = paste_clipboard_into_entry(
            entry,
            clipboard_get=lambda: "telegram-api-hash",
        )

        self.assertTrue(result)
        entry.delete.assert_called_once_with("sel.first", "sel.last")
        entry.insert.assert_called_once_with("insert", "telegram-api-hash")

    def test_empty_clipboard_does_not_change_entry(self) -> None:
        entry = Mock()

        result = paste_clipboard_into_entry(
            entry,
            clipboard_get=lambda: "",
        )

        self.assertFalse(result)
        entry.delete.assert_not_called()
        entry.insert.assert_not_called()

    def test_windows_clipboard_history_paste_event_is_bound(self) -> None:
        entry = Mock()
        physical_handler = Mock()
        virtual_paste_handler = Mock()

        bind_api_hash_paste(
            entry,
            physical_handler=physical_handler,
            virtual_paste_handler=virtual_paste_handler,
        )

        self.assertEqual(
            entry.bind.call_args_list,
            [
                unittest.mock.call(
                    "<Control-KeyPress>",
                    physical_handler,
                    add="+",
                ),
                unittest.mock.call(
                    "<<Paste>>",
                    virtual_paste_handler,
                    add="+",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
