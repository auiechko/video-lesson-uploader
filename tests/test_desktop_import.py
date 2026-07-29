from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from lesson_video_uploader.calendar_rules import CalendarEventSnapshot
from lesson_video_uploader.models import (
    Lesson,
    LessonDetails,
    SendStatus,
)
from lesson_video_uploader.persistence import SQLiteSendItemRepository
from lesson_video_uploader.workflow import (
    WorkflowState,
    WorkflowStateMachine,
)


class DesktopImportTests(unittest.TestCase):
    def test_desktop_entry_point_imports_without_creating_a_window(self) -> None:
        from lesson_video_uploader.desktop import DesktopApplication, main

        self.assertTrue(callable(main))
        self.assertTrue(callable(DesktopApplication))
        for method_name in (
            "_connect_google_calendar",
            "_load_google_events",
            "_import_google_event",
            "_disconnect_google_calendar",
        ):
            self.assertTrue(callable(getattr(DesktopApplication, method_name)))

    def test_edit_keeps_the_lesson_in_preview_until_changes_are_saved(
        self,
    ) -> None:
        from lesson_video_uploader.desktop import DesktopApplication

        lesson = Lesson(
            profile_id="main",
            batch_id="batch",
            calendar_event_id="event-1",
            event_start=datetime(2026, 7, 29, 10, 0),
            caption="29.07.2026 1 Учень 10р індив",
            ordered_video_paths=(Path("lesson.mp4"),),
            details=LessonDetails(
                student_id="1",
                student_name="Учень",
                lesson_label="10р індив",
                duration_hours=1,
                is_trial=False,
            ),
        )
        app = DesktopApplication.__new__(DesktopApplication)
        app.lesson_tree = MagicMock()
        app.lesson_tree.selection.return_value = ("0",)
        app.lessons = [lesson]
        app.pending_video_paths = []
        app.add_lesson_button = MagicMock()
        for variable_name in (
            "event_id_var",
            "start_var",
            "student_id_var",
            "student_name_var",
            "lesson_label_var",
            "duration_var",
            "trial_var",
            "no_recording_var",
        ):
            setattr(app, variable_name, MagicMock())
        app._refresh_pending_files = MagicMock()
        app._log = MagicMock()

        app._edit_lesson()

        self.assertEqual(app.lessons, [lesson])
        self.assertEqual(app.editing_calendar_event_id, "event-1")
        app.add_lesson_button.configure.assert_called_once_with(
            text="Зберегти зміни уроку"
        )

    def test_selected_preview_file_can_be_opened(self) -> None:
        from lesson_video_uploader.desktop import DesktopApplication

        paths = (Path("first.mp4"), Path("second.mp4"))
        lesson = Lesson(
            profile_id="main",
            batch_id="batch",
            calendar_event_id="event-1",
            event_start=datetime(2026, 7, 29, 10, 0),
            caption="lesson",
            ordered_video_paths=paths,
        )
        app = DesktopApplication.__new__(DesktopApplication)
        app.lesson_tree = MagicMock()
        app.lesson_tree.selection.return_value = ("0",)
        app.preview_files = MagicMock()
        app.preview_files.curselection.return_value = (1,)
        app.lessons = [lesson]
        app._open_local_path = MagicMock()
        app._show_error = MagicMock()

        app._open_selected_lesson_file()

        app._open_local_path.assert_called_once_with(paths[1])
        app._show_error.assert_not_called()

    @patch("lesson_video_uploader.desktop.messagebox.showinfo")
    @patch(
        "lesson_video_uploader.desktop.messagebox.askyesno",
        return_value=True,
    )
    def test_reset_incomplete_history_preserves_sent_and_restores_send(
        self,
        _ask_yes_no: MagicMock,
        show_info: MagicMock,
    ) -> None:
        from lesson_video_uploader.desktop import DesktopApplication

        snapshot = CalendarEventSnapshot(
            event_id="event-1",
            summary="1 Менеджер (Учень 10) Учко ТГ",
            student_id="1",
            student_name="Учень",
            student_age=10,
            local_date="2026-07-29",
            start="2026-07-29T10:00:00+03:00",
            end="2026-07-29T11:00:00+03:00",
            duration_minutes=60,
            status="NORMAL",
            is_trial=False,
            is_no_recording=False,
            is_transferred=False,
            is_cancelled=False,
            is_pause=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "lesson.mp4"
            video.write_bytes(b"video")
            lesson = Lesson(
                profile_id="main",
                batch_id="batch",
                calendar_event_id="event-1",
                event_start=datetime(2026, 7, 29, 10),
                caption="29.07.2026 1 Учень 10р індив",
                ordered_video_paths=(video,),
                calendar_snapshot=snapshot,
            )
            database = root / "deliveries.sqlite3"
            repository = SQLiteSendItemRepository(database)
            repository.save(Lesson(
                profile_id=lesson.profile_id,
                batch_id=lesson.batch_id,
                calendar_event_id=lesson.calendar_event_id,
                event_start=lesson.event_start,
                caption=lesson.caption,
                ordered_video_paths=lesson.ordered_video_paths,
                status=SendStatus.DELIVERY_UNKNOWN,
                calendar_snapshot=snapshot,
            ))
            app = DesktopApplication.__new__(DesktopApplication)
            app.profile_var = MagicMock()
            app.profile_var.get.return_value = "main"
            app.batch_var = MagicMock()
            app.batch_var.get.return_value = "batch"
            app.database_path = database
            app.lessons = [lesson]
            app.workflow = WorkflowStateMachine()
            app._apply_workflow_state = MagicMock()
            app._show_error = MagicMock()
            app._log = MagicMock()

            app._reset_incomplete_delivery_history()

            restored = repository.get(lesson.identity)

        self.assertEqual(restored.status, SendStatus.PENDING)
        self.assertEqual(app.workflow.state, WorkflowState.BATCH_READY)
        app._show_error.assert_not_called()
        show_info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
