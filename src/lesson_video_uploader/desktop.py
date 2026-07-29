from __future__ import annotations

import asyncio
import os
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Coroutine, Mapping

from .calendar_rules import (
    BatchRevalidationRequired,
    CalendarEventSnapshot,
    ParsedCalendarEvent,
    build_calendar_snapshot,
    parse_calendar_event,
    validate_calendar_snapshots,
)
from .credentials import KeyringSecretStore
from .debug_logging import write_debug_exception
from .desktop_controller import (
    DesktopSettingsController,
    LessonForm,
    build_gui_manifest,
    create_lesson_from_form,
    form_from_lesson,
)
from .google_calendar import (
    CalendarEventNotSendable,
    GoogleCalendarEvent,
    GoogleCalendarInfo,
    GoogleCalendarService,
    GoogleOAuthManager,
    calendar_event_to_lesson_form,
)
from .manifest import UploadManifest, load_manifest, save_manifest
from .media_tools import (
    boundary_preview_offsets,
    extract_preview_frames,
    general_preview_offsets,
    probe_mp4,
    split_mp4,
)
from .models import Lesson, LessonSendMode, SendStatus
from .persistence import SQLiteSendItemRepository
from .planning import plan_albums
from .telegram_desktop import (
    LoginResult,
    TelegramAuthService,
    TelegramDesktopService,
    telethon_components,
)
from .workflow import (
    WorkflowState,
    WorkflowStateMachine,
    WorkflowTransitionError,
)
from .zoom_batch import assemble_zoom_batch
from .zoom_decisions import (
    AssignmentType,
    FileIdentity,
    ZoomAssignmentDecision,
)
from .zoom_matching import (
    ZoomMatchResult,
    ZoomMatchSettings,
    ZoomMatchStatus,
    match_zoom_segments,
)
from .zoom_preflight import (
    PreflightFolderResult,
    PreflightSettings,
    ZoomPreflightResult,
    ZoomPreflightService,
)
from .zoom_recordings import (
    VideoTimeConfidence,
    VideoTimeMethod,
    ZoomFolderIssue,
    ZoomRecordingCatalog,
    ZoomVideoSegment,
)
from .zoom_revalidation import validate_zoom_sources

APP_TITLE = "Lesson Video Uploader"
CONTROL_KEY_MASK = 0x0004
VIRTUAL_KEY_V = 86


def _format_seconds(value: float | None) -> str:
    total = max(0, round(value or 0))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _parse_offset(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError("Час має формат ГГ:ХХ:СС")
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError as error:
        raise ValueError("Час має містити лише числа") from error
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Некоректна точка розрізання")
    return float(hours * 3600 + minutes * 60 + seconds)


def is_ctrl_v_shortcut(*, state: int | str, keycode: int) -> bool:
    try:
        normalized_state = int(state)
    except ValueError:
        return False
    return (
        bool(normalized_state & CONTROL_KEY_MASK)
        and keycode == VIRTUAL_KEY_V
    )


def paste_clipboard_into_entry(
    entry: ttk.Entry,
    *,
    clipboard_get: Callable[[], str],
) -> bool:
    try:
        text = clipboard_get()
    except tk.TclError:
        return False
    if not text:
        return False
    try:
        entry.delete("sel.first", "sel.last")
    except tk.TclError:
        pass
    entry.insert(tk.INSERT, text)
    return True


def bind_api_hash_paste(
    entry: ttk.Entry,
    *,
    physical_handler: Callable[[tk.Event[tk.Misc]], str | None],
    virtual_paste_handler: Callable[[tk.Event[tk.Misc]], str],
) -> None:
    entry.bind("<Control-KeyPress>", physical_handler, add="+")
    entry.bind("<<Paste>>", virtual_paste_handler, add="+")


class DesktopApplication:
    def __init__(self, root: tk.Tk, *, workspace: Path | None = None) -> None:
        self.root = root
        self.workspace = (workspace or Path.cwd()).resolve()
        self.config_path = self.workspace / "config.toml"
        self.database_path = (
            self.workspace / ".lesson-video-uploader" / "deliveries.sqlite3"
        )
        self.settings = DesktopSettingsController(
            self.config_path,
            KeyringSecretStore(),
        )
        self.calendar_report_repository = SQLiteSendItemRepository(
            self.database_path
        )
        self.google_token_store = KeyringSecretStore(
            profile_id="main",
            credential_name="google_calendar_oauth",
        )
        self.google_service: GoogleCalendarService | None = None
        self.google_events: list[GoogleCalendarEvent] = []
        self.google_calendar_by_label: dict[str, GoogleCalendarInfo] = {}
        self.pending_calendar_form: LessonForm | None = None
        self.lessons: list[Lesson] = []
        self.pending_video_paths: list[Path] = []
        self.workflow = WorkflowStateMachine()
        self.preflight_service = ZoomPreflightService()
        self.preflight_result: ZoomPreflightResult | None = None
        self.parsed_calendar_events: tuple[ParsedCalendarEvent, ...] = ()
        self.zoom_match_results: list[ZoomMatchResult] = []
        self.unresolved_zoom_results: list[ZoomMatchResult] = []
        self.missing_zoom_events: list[ParsedCalendarEvent] = []
        self.zoom_catalog_issues: list[ZoomFolderIssue] = []
        self.manual_text_event_ids: set[str] = set()
        self.ignored_event_ids: set[str] = set()
        self.google_tree_event_index: dict[str, int] = {}
        self.busy = False
        self.action_buttons: list[ttk.Button] = []

        self._configure_window()
        self._create_variables()
        self._build_layout()
        self._load_settings()

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1120x780")
        self.root.minsize(940, 680)
        self.root.option_add("*tearOff", False)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", foreground="#52606d")
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 10))
        style.configure("Treeview", rowheight=30)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))

    def _create_variables(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self.profile_var = tk.StringVar(value="main")
        self.batch_var = tk.StringVar(value=today)
        self.target_var = tk.StringVar(value="me")
        self.event_id_var = tk.StringVar(value=f"event-{datetime.now():%Y%m%d-%H%M}")
        self.start_var = tk.StringVar(value=f"{today} {datetime.now():%H:%M}")
        self.student_id_var = tk.StringVar()
        self.student_name_var = tk.StringVar()
        self.lesson_label_var = tk.StringVar(value="10р індив")
        self.duration_var = tk.StringVar(value="1")
        self.trial_var = tk.BooleanVar(value=False)
        self.no_recording_var = tk.BooleanVar(value=False)
        self.api_id_var = tk.StringVar()
        self.api_hash_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        self.session_var = tk.StringVar()
        self.secret_status_var = tk.StringVar(value="API hash ще не збережений")
        self.status_var = tk.StringVar(value="Готово")
        self.progress_var = tk.DoubleVar(value=0)
        self.google_credentials_var = tk.StringVar()
        self.google_calendar_var = tk.StringVar(value="primary")
        self.google_timezone_var = tk.StringVar(value="Europe/Kyiv")
        self.google_from_var = tk.StringVar(value=today)
        self.google_to_var = tk.StringVar(value=today)
        self.google_status_var = tk.StringVar(value="Google Calendar не підключений")
        self.zoom_recordings_dir_var = tk.StringVar()
        self.automatic_tolerance_var = tk.StringVar(value="30")
        self.manual_window_var = tk.StringVar(value="180")
        self.next_overlap_var = tk.StringVar(value="10")
        self.minimum_video_size_var = tk.StringVar(value="5")
        self.video_stability_var = tk.StringVar(value="5")
        self.workflow_step_var = tk.StringVar(value=self.workflow.step_label)
        self.unresolved_count_var = tk.StringVar(
            value="Невирішених питань: 0"
        )

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            outer,
            text=(
                "Один Calendar event → один логічний урок → "
                "один Telegram-альбом"
            ),
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 14))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.send_tab = ttk.Frame(self.notebook, padding=14)
        google_tab = ttk.Frame(self.notebook, padding=14)
        settings_tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(self.send_tab, text="  Уроки та надсилання  ")
        self.notebook.add(google_tab, text="  Google Calendar і Zoom  ")
        self.notebook.add(settings_tab, text="  Telegram і безпека  ")
        self._build_send_tab(self.send_tab)
        self._build_google_tab(google_tab)
        self._build_settings_tab(settings_tab)

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(12, 0))
        ttk.Progressbar(
            footer,
            variable=self.progress_var,
            maximum=100,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(footer, textvariable=self.status_var, width=42).pack(
            side=tk.LEFT, padx=(12, 0)
        )

    def _build_send_tab(self, parent: ttk.Frame) -> None:
        batch = ttk.LabelFrame(parent, text="Пакет", padding=10)
        batch.pack(fill=tk.X)
        self._labeled_entry(batch, "Profile ID", self.profile_var, 0, 0)
        self._labeled_entry(batch, "Batch ID", self.batch_var, 0, 2)
        self._labeled_entry(
            batch,
            "Telegram-чат",
            self.target_var,
            0,
            4,
            width=24,
        )
        ttk.Button(batch, text="Відкрити пакет…", command=self._load_batch).grid(
            row=0, column=6, padx=(16, 4), pady=4
        )
        ttk.Button(batch, text="Зберегти пакет…", command=self._save_batch).grid(
            row=0, column=7, padx=4, pady=4
        )
        for column in (1, 3, 5):
            batch.columnconfigure(column, weight=1)

        body = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

        editor = ttk.LabelFrame(body, text="Додати урок", padding=12)
        overview = ttk.LabelFrame(body, text="Preview пакета", padding=10)
        body.add(editor, weight=2)
        body.add(overview, weight=3)
        self._build_lesson_editor(editor)
        self._build_preview(overview)

        actions = ttk.Frame(parent)
        actions.pack(fill=tk.X, pady=(12, 0))
        self.send_button = ttk.Button(
            actions,
            text="Надіслати в Telegram",
            style="Primary.TButton",
            command=self._send_package,
        )
        self.send_button.pack(side=tk.LEFT)
        reconcile_button = ttk.Button(
            actions,
            text="Перевірити невідому доставку",
            command=self._reconcile_package,
        )
        reconcile_button.pack(side=tk.LEFT, padx=8)
        self.action_buttons.extend((self.send_button, reconcile_button))

        self.log = ScrolledText(
            parent,
            height=6,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        self.log.pack(fill=tk.X, pady=(10, 0))

    def _build_lesson_editor(
        self,
        parent: ttk.Frame | ttk.LabelFrame,
    ) -> None:
        self._labeled_entry(
            parent, "Calendar event ID", self.event_id_var, 0, 0, columnspan=3
        )
        self._labeled_entry(
            parent, "Початок (РРРР-ММ-ДД ГГ:ХХ)", self.start_var, 1, 0,
            columnspan=3,
        )
        self._labeled_entry(parent, "ID учня", self.student_id_var, 2, 0)
        self._labeled_entry(parent, "Ім’я", self.student_name_var, 2, 2)
        self._labeled_entry(
            parent, "Опис уроку", self.lesson_label_var, 3, 0, columnspan=3
        )

        ttk.Label(parent, text="Тривалість").grid(
            row=4, column=0, sticky=tk.W, pady=5
        )
        ttk.Combobox(
            parent,
            textvariable=self.duration_var,
            values=("1", "2", "3"),
            state="readonly",
            width=7,
        ).grid(row=4, column=1, sticky=tk.W, pady=5)
        ttk.Checkbutton(
            parent,
            text="Пробне заняття",
            variable=self.trial_var,
        ).grid(row=4, column=2, sticky=tk.W, padx=(10, 0))
        ttk.Checkbutton(
            parent,
            text="Без запису",
            variable=self.no_recording_var,
        ).grid(row=4, column=3, sticky=tk.W, padx=(10, 0))

        ttk.Label(parent, text="MP4 у хронологічному порядку").grid(
            row=5, column=0, columnspan=4, sticky=tk.W, pady=(10, 4)
        )
        files_frame = ttk.Frame(parent)
        files_frame.grid(row=6, column=0, columnspan=4, sticky=tk.NSEW)
        self.pending_files = tk.Listbox(
            files_frame,
            height=7,
            selectmode=tk.SINGLE,
            font=("Segoe UI", 9),
        )
        self.pending_files.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        file_buttons = ttk.Frame(files_frame)
        file_buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0))
        ttk.Button(file_buttons, text="Додати…", command=self._pick_videos).pack(
            fill=tk.X, pady=(0, 4)
        )
        ttk.Button(file_buttons, text="Вище", command=lambda: self._move_video(-1)).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(file_buttons, text="Нижче", command=lambda: self._move_video(1)).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(file_buttons, text="Прибрати", command=self._remove_video).pack(
            fill=tk.X, pady=2
        )

        ttk.Button(
            parent,
            text="Додати урок до пакета",
            style="Primary.TButton",
            command=self._add_lesson,
        ).grid(row=7, column=0, columnspan=4, sticky=tk.EW, pady=(12, 0))
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(3, weight=1)
        parent.rowconfigure(6, weight=1)

    def _build_preview(
        self,
        parent: ttk.Frame | ttk.LabelFrame,
    ) -> None:
        columns = ("date", "caption", "videos", "telegram")
        self.lesson_tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.lesson_tree.heading("date", text="Дата")
        self.lesson_tree.heading("caption", text="Caption")
        self.lesson_tree.heading("videos", text="Відео")
        self.lesson_tree.heading("telegram", text="Telegram")
        self.lesson_tree.column("date", width=86, stretch=False)
        self.lesson_tree.column("caption", width=300)
        self.lesson_tree.column("videos", width=65, anchor=tk.CENTER, stretch=False)
        self.lesson_tree.column("telegram", width=105, anchor=tk.CENTER, stretch=False)
        self.lesson_tree.pack(fill=tk.BOTH, expand=True)
        self.lesson_tree.bind("<<TreeviewSelect>>", self._show_selected_files)

        ttk.Label(parent, text="Файли вибраного уроку").pack(
            anchor=tk.W, pady=(10, 4)
        )
        self.preview_files = tk.Listbox(parent, height=7, font=("Segoe UI", 9))
        self.preview_files.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(parent)
        buttons.pack(anchor=tk.E, pady=(8, 0))
        ttk.Button(
            buttons,
            text="Редагувати вибраний урок",
            command=self._edit_lesson,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            buttons,
            text="Видалити вибраний урок",
            command=self._remove_lesson,
        ).pack(side=tk.LEFT)

    def _build_google_tab(self, parent: ttk.Frame) -> None:
        connection = ttk.LabelFrame(
            parent,
            text="Підключення Google Calendar",
            padding=10,
        )
        connection.pack(fill=tk.X)
        ttk.Label(connection, text="OAuth credentials.json").grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 6),
            pady=5,
        )
        ttk.Entry(
            connection,
            textvariable=self.google_credentials_var,
        ).grid(row=0, column=1, sticky=tk.EW, pady=5)
        browse_button = ttk.Button(
            connection,
            text="Вибрати файл…",
            command=self._pick_google_credentials,
        )
        browse_button.grid(row=0, column=2, padx=(8, 0), pady=5)
        connect_button = ttk.Button(
            connection,
            text="Підключити Google",
            style="Primary.TButton",
            command=self._connect_google_calendar,
        )
        connect_button.grid(row=1, column=1, sticky=tk.W, pady=(8, 0))
        disconnect_button = ttk.Button(
            connection,
            text="Відключити",
            command=self._disconnect_google_calendar,
        )
        disconnect_button.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))
        ttk.Label(
            connection,
            textvariable=self.google_status_var,
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(10, 0))
        connection.columnconfigure(1, weight=1)

        filters = ttk.LabelFrame(parent, text="Події", padding=10)
        filters.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(filters, text="Календар").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=5
        )
        self.google_calendar_combo = ttk.Combobox(
            filters,
            textvariable=self.google_calendar_var,
            state="normal",
        )
        self.google_calendar_combo.grid(
            row=0,
            column=1,
            columnspan=3,
            sticky=tk.EW,
            pady=5,
        )
        self._labeled_entry(filters, "Від", self.google_from_var, 1, 0)
        self._labeled_entry(filters, "До", self.google_to_var, 1, 2)
        self._labeled_entry(
            filters,
            "Часовий пояс",
            self.google_timezone_var,
            2,
            0,
            width=24,
        )
        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)

        zoom = ttk.LabelFrame(
            parent,
            text="Локальні записи Zoom",
            padding=10,
        )
        zoom.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(zoom, text="Папка записів Zoom").grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 6),
            pady=5,
        )
        ttk.Entry(
            zoom,
            textvariable=self.zoom_recordings_dir_var,
        ).grid(
            row=0,
            column=1,
            columnspan=5,
            sticky=tk.EW,
            pady=5,
        )
        zoom_browse_button = ttk.Button(
            zoom,
            text="Вибрати папку…",
            command=self._pick_zoom_recordings_dir,
        )
        zoom_browse_button.grid(row=0, column=6, padx=(8, 0), pady=5)
        self._labeled_entry(
            zoom,
            "Авто, хв",
            self.automatic_tolerance_var,
            1,
            0,
            width=7,
        )
        self._labeled_entry(
            zoom,
            "Ручний пошук, хв",
            self.manual_window_var,
            1,
            2,
            width=7,
        )
        self._labeled_entry(
            zoom,
            "Overlap, хв",
            self.next_overlap_var,
            1,
            4,
            width=7,
        )
        self._labeled_entry(
            zoom,
            "Мін. MP4, МБ",
            self.minimum_video_size_var,
            2,
            0,
            width=7,
        )
        self._labeled_entry(
            zoom,
            "Стабільність, с",
            self.video_stability_var,
            2,
            2,
            width=7,
        )
        zoom.columnconfigure(1, weight=1)
        zoom.columnconfigure(3, weight=1)
        zoom.columnconfigure(5, weight=1)

        workflow = ttk.LabelFrame(
            parent,
            text="Підготовка batch",
            padding=10,
        )
        workflow.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            workflow,
            textvariable=self.workflow_step_var,
            style="Subtitle.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            workflow,
            textvariable=self.unresolved_count_var,
        ).pack(side=tk.LEFT, padx=(16, 0))
        self.preflight_button = ttk.Button(
            workflow,
            text="Перевірити конвертацію",
            style="Primary.TButton",
            command=self._start_zoom_preflight,
        )
        self.preflight_button.pack(side=tk.RIGHT)
        self.matching_button = ttk.Button(
            workflow,
            text="Перевірити відповідність",
            command=self._load_google_events,
        )
        self.matching_button.pack(side=tk.RIGHT, padx=6)
        self.next_problem_button = ttk.Button(
            workflow,
            text="Перейти до наступної проблеми",
            command=self._resolve_next_zoom_problem,
        )
        self.next_problem_button.pack(side=tk.RIGHT, padx=6)
        self.change_decision_button = ttk.Button(
            workflow,
            text="Змінити рішення",
            command=self._change_selected_zoom_decision,
        )
        self.change_decision_button.pack(side=tk.RIGHT, padx=6)
        self.open_problems_button = ttk.Button(
            workflow,
            text="Відкрити проблемні папки",
            command=self._show_preflight_problems,
        )
        self.open_problems_button.pack(side=tk.RIGHT, padx=6)

        columns = (
            "zoom_start",
            "calendar_start",
            "calendar_end",
            "difference",
            "video_end",
            "next_event",
            "overlap",
            "method",
            "status",
            "student",
            "event_id",
        )
        self.google_event_tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=11,
        )
        self.google_event_tree.heading("zoom_start", text="Zoom start")
        self.google_event_tree.heading("calendar_start", text="Calendar start")
        self.google_event_tree.heading("calendar_end", text="Calendar end")
        self.google_event_tree.heading("difference", text="Різниця")
        self.google_event_tree.heading("video_end", text="Video end")
        self.google_event_tree.heading("next_event", text="Наступний урок")
        self.google_event_tree.heading("overlap", text="Overlap")
        self.google_event_tree.heading("method", text="Метод")
        self.google_event_tree.heading("status", text="Статус")
        self.google_event_tree.heading("student", text="Учень")
        self.google_event_tree.heading("event_id", text="Google event ID")
        for column in columns:
            self.google_event_tree.column(
                column,
                width=125 if column not in {"status", "student"} else 180,
                stretch=column in {"status", "student"},
            )
        self.google_event_tree.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.google_event_tree.bind(
            "<Double-1>",
            lambda _event: self._import_google_event(),
        )
        import_button = ttk.Button(
            parent,
            text="Імпортувати вибрану подію в урок",
            style="Primary.TButton",
            command=self._import_google_event,
        )
        import_button.pack(anchor=tk.E, pady=(10, 0))
        self.action_buttons.extend(
            (
                browse_button,
                connect_button,
                disconnect_button,
                zoom_browse_button,
                self.preflight_button,
                self.matching_button,
                self.next_problem_button,
                self.change_decision_button,
                self.open_problems_button,
                import_button,
            )
        )
        self._apply_workflow_state()

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        intro = ttk.Label(
            parent,
            text=(
                "API ID зберігається у config.toml. API hash зберігається "
                "через системний keyring і не потрапляє в Git."
            ),
            wraplength=820,
            style="Subtitle.TLabel",
        )
        intro.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 16))
        self._labeled_entry(parent, "Telegram API ID", self.api_id_var, 1, 0)
        self.api_hash_entry = self._labeled_entry(
            parent,
            "Telegram API hash",
            self.api_hash_var,
            2,
            0,
            show="•",
        )
        bind_api_hash_paste(
            self.api_hash_entry,
            physical_handler=self._handle_api_hash_keypress,
            virtual_paste_handler=self._handle_api_hash_virtual_paste,
        )
        ttk.Label(parent, textvariable=self.secret_status_var).grid(
            row=2, column=2, sticky=tk.W, padx=(12, 0)
        )
        ttk.Button(
            parent,
            text="Вставити",
            command=self._paste_api_hash,
        ).grid(row=2, column=3, sticky=tk.W, padx=(10, 0))
        self._labeled_entry(parent, "Номер телефону", self.phone_var, 3, 0)
        self._labeled_entry(
            parent,
            "Telethon session (автоматично)",
            self.session_var,
            4,
            0,
            width=48,
        )
        ttk.Separator(parent).grid(
            row=5, column=0, columnspan=3, sticky=tk.EW, pady=18
        )
        save_button = ttk.Button(
            parent,
            text="Зберегти налаштування",
            style="Primary.TButton",
            command=self._save_settings,
        )
        save_button.grid(row=6, column=0, sticky=tk.W)
        login_button = ttk.Button(
            parent,
            text="Увійти в Telegram",
            command=self._start_login,
        )
        login_button.grid(row=6, column=1, sticky=tk.W, padx=(10, 0))
        self.action_buttons.extend((save_button, login_button))
        ttk.Label(
            parent,
            text=(
                "Під час входу код і пароль 2FA вводяться у захищених діалогах "
                "та не записуються у файли."
            ),
            wraplength=760,
            style="Subtitle.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=(18, 0))
        parent.columnconfigure(1, weight=1)

    def _paste_api_hash(self) -> None:
        if not paste_clipboard_into_entry(
            self.api_hash_entry,
            clipboard_get=self.root.clipboard_get,
        ):
            self._set_status("Буфер обміну порожній")

    def _handle_api_hash_keypress(
        self,
        event: tk.Event[tk.Misc],
    ) -> str | None:
        if not is_ctrl_v_shortcut(
            state=event.state,
            keycode=event.keycode,
        ):
            return None
        self._paste_api_hash()
        return "break"

    def _handle_api_hash_virtual_paste(
        self,
        _event: tk.Event[tk.Misc],
    ) -> str:
        self._paste_api_hash()
        return "break"

    @staticmethod
    def _labeled_entry(
        parent: ttk.Frame | ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        *,
        width: int = 20,
        columnspan: int = 1,
        show: str | None = None,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(
            row=row, column=column, sticky=tk.W, padx=(0, 6), pady=5
        )
        entry = ttk.Entry(
            parent,
            textvariable=variable,
            width=width,
            show=show or "",
        )
        entry.grid(
            row=row,
            column=column + 1,
            columnspan=columnspan,
            sticky=tk.EW,
            padx=(0, 8),
            pady=5,
        )
        return entry

    def _load_settings(self) -> None:
        try:
            loaded = self.settings.load(self.profile_var.get())
        except Exception as error:
            self._show_error(error)
            return
        config = loaded.config
        self.api_id_var.set("" if config.api_id is None else str(config.api_id))
        self.phone_var.set(config.phone)
        self.session_var.set(config.session)
        self.google_credentials_var.set(config.google_client_secrets)
        self.google_calendar_var.set(config.google_calendar_id)
        self.google_timezone_var.set(config.google_timezone)
        self.zoom_recordings_dir_var.set(config.zoom_recordings_dir)
        self.automatic_tolerance_var.set(
            str(config.automatic_time_tolerance_minutes)
        )
        self.manual_window_var.set(
            str(config.manual_time_search_window_minutes)
        )
        self.next_overlap_var.set(
            str(config.next_lesson_overlap_tolerance_minutes)
        )
        self.minimum_video_size_var.set(str(config.minimum_video_size_mb))
        self.video_stability_var.set(
            str(config.video_stability_check_seconds)
        )
        self.secret_status_var.set(
            "API hash збережено"
            if loaded.api_hash_saved
            else "API hash ще не збережений"
        )

    def _pick_google_credentials(self) -> None:
        selected = filedialog.askopenfilename(
            title="Виберіть Google OAuth credentials.json",
            filetypes=[("JSON", "*.json")],
        )
        if selected:
            self.google_credentials_var.set(selected)

    def _pick_zoom_recordings_dir(self) -> None:
        selected = filedialog.askdirectory(
            title="Виберіть кореневу папку локальних записів Zoom",
            initialdir=self.zoom_recordings_dir_var.get() or None,
        )
        if selected and selected != self.zoom_recordings_dir_var.get():
            self.zoom_recordings_dir_var.set(selected)
            self._reset_zoom_workflow()

    def _reset_zoom_workflow(self) -> None:
        self.workflow = WorkflowStateMachine()
        self.preflight_result = None
        self.parsed_calendar_events = ()
        self.zoom_match_results.clear()
        self.unresolved_zoom_results.clear()
        self.missing_zoom_events.clear()
        self.zoom_catalog_issues.clear()
        self.manual_text_event_ids.clear()
        self.ignored_event_ids.clear()
        self.google_event_tree.delete(
            *self.google_event_tree.get_children()
        )
        self.google_tree_event_index.clear()
        self._apply_workflow_state()

    def _selected_period(self) -> tuple[date, date]:
        try:
            date_from = date.fromisoformat(self.google_from_var.get().strip())
            date_to = date.fromisoformat(self.google_to_var.get().strip())
        except ValueError as error:
            raise ValueError("Дати мають формат РРРР-ММ-ДД") from error
        if date_to < date_from:
            raise ValueError("Кінцева дата не може бути раніше початкової")
        return date_from, date_to

    def _current_preflight_settings(self) -> PreflightSettings:
        return PreflightSettings(
            minimum_video_size_mb=float(self.minimum_video_size_var.get()),
            video_stability_check_seconds=float(
                self.video_stability_var.get()
            ),
            timezone_name=self.google_timezone_var.get().strip(),
        )

    def _current_match_settings(self) -> ZoomMatchSettings:
        config = self.settings.load(self.profile_var.get()).config
        return ZoomMatchSettings(
            automatic_time_tolerance_minutes=(
                config.automatic_time_tolerance_minutes
            ),
            manual_time_search_window_minutes=(
                config.manual_time_search_window_minutes
            ),
            calendar_conflict_tolerance_minutes=(
                config.calendar_conflict_tolerance_minutes
            ),
            next_lesson_overlap_tolerance_minutes=(
                config.next_lesson_overlap_tolerance_minutes
            ),
        )

    def _preflight_matches_current_selection(self) -> bool:
        if self.preflight_result is None:
            return False
        try:
            date_from, date_to = self._selected_period()
        except ValueError:
            return False
        selected_root = (
            Path(self.zoom_recordings_dir_var.get())
            .expanduser()
            .resolve()
        )
        return (
            self.preflight_result.root.resolve() == selected_root
            and self.preflight_result.date_from == date_from
            and self.preflight_result.date_to == date_to
        )

    def _apply_workflow_state(self) -> None:
        buttons = self.workflow.buttons
        self.workflow_step_var.set(self.workflow.step_label)
        unresolved_count = 0
        if self.preflight_result is not None:
            unresolved_count = len(self.preflight_result.blocking_folders)
        if self.workflow.state in {
            WorkflowState.MATCHING_RUNNING,
            WorkflowState.RESOLUTION_REQUIRED,
            WorkflowState.BATCH_READY,
            WorkflowState.REVALIDATION_RUNNING,
            WorkflowState.BATCH_REVALIDATION_REQUIRED,
        }:
            unresolved_count = (
                len(self.unresolved_zoom_results)
                + len(self.missing_zoom_events)
                + len(self.zoom_catalog_issues)
            )
        self.unresolved_count_var.set(
            f"Невирішених питань: {unresolved_count}"
        )
        if self.busy:
            for button in (
                self.preflight_button,
                self.matching_button,
                self.next_problem_button,
                self.change_decision_button,
                self.open_problems_button,
                self.send_button,
            ):
                button.configure(state=tk.DISABLED)
            return
        self.preflight_button.configure(
            state=(
                tk.NORMAL
                if buttons.preflight_enabled
                else tk.DISABLED
            ),
            text=(
                "Повторити перевірку"
                if self.workflow.state is WorkflowState.PREFLIGHT_BLOCKED
                else "Перевірити конвертацію"
            ),
        )
        self.matching_button.configure(
            state=tk.NORMAL if buttons.matching_enabled else tk.DISABLED
        )
        self.next_problem_button.configure(
            state=tk.NORMAL if buttons.resolve_enabled else tk.DISABLED
        )
        self.change_decision_button.configure(
            state=tk.NORMAL if buttons.resolve_enabled else tk.DISABLED
        )
        self.open_problems_button.configure(
            state=(
                tk.NORMAL
                if buttons.open_problems_enabled
                else tk.DISABLED
            )
        )
        self.send_button.configure(
            state=tk.NORMAL if buttons.send_enabled else tk.DISABLED
        )

    def _start_zoom_preflight(self) -> None:
        try:
            if not self._save_google_settings():
                return
            date_from, date_to = self._selected_period()
            root = Path(self.zoom_recordings_dir_var.get()).expanduser()
            settings = self._current_preflight_settings()
            previous = self.preflight_result
            recheck_only = (
                self.workflow.state is WorkflowState.PREFLIGHT_BLOCKED
                and previous is not None
                and previous.root == root
                and previous.date_from == date_from
                and previous.date_to == date_to
            )
            self.workflow.start_preflight()
        except (ValueError, WorkflowTransitionError) as error:
            self._show_error(error)
            return
        self._apply_workflow_state()

        async def run_preflight() -> ZoomPreflightResult:
            if recheck_only and previous is not None:
                return await asyncio.to_thread(
                    self.preflight_service.recheck_blocked,
                    previous,
                    settings,
                )
            return await asyncio.to_thread(
                self.preflight_service.check,
                root,
                date_from,
                date_to,
                settings,
                manual_folder_starts=(
                    self.calendar_report_repository.get_zoom_folder_starts()
                ),
            )

        self._run_async(
            run_preflight(),
            self._preflight_finished,
            "Перевірка конвертації Zoom…",
        )

    def _preflight_finished(self, result: ZoomPreflightResult) -> None:
        self.preflight_result = result
        self.workflow.finish_preflight(
            has_blocking_problems=not result.is_passed
        )
        self._apply_workflow_state()
        if result.is_passed:
            self._log(
                "Preflight пройдено: усі "
                f"{len(result.ready_folders)} Zoom-папок готові."
            )
            messagebox.showinfo(
                APP_TITLE,
                "Усі записи технічно готові. "
                "Тепер натисніть «Перевірити відповідність».",
            )
            return
        self._log(
            "PREFLIGHT_BLOCKED: проблемних папок "
            f"{len(result.blocking_folders)} із {len(result.folders)}."
        )
        self._show_preflight_problems()

    def _show_preflight_problems(self) -> None:
        if (
            self.preflight_result is None
            or not self.preflight_result.blocking_folders
        ):
            messagebox.showinfo(APP_TITLE, "Проблемних Zoom-папок немає.")
            return
        problems = self.preflight_result.blocking_folders
        window = tk.Toplevel(self.root)
        window.title("Проблеми конвертації Zoom")
        window.geometry("1120x480")
        window.transient(self.root)
        columns = ("date", "time", "path", "status", "files", "reason", "action")
        tree = ttk.Treeview(
            window,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "date": "Дата",
            "time": "Час Zoom",
            "path": "Повний шлях",
            "status": "Статус",
            "files": "Знайдені файли",
            "reason": "Причина",
            "action": "Рекомендована дія",
        }
        for column, heading in headings.items():
            tree.heading(column, text=heading)
            tree.column(
                column,
                width=100 if column in {"date", "time"} else 190,
            )
        for index, problem in enumerate(problems):
            tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    problem.start.strftime("%d.%m.%Y"),
                    problem.start.strftime("%H:%M:%S"),
                    str(problem.folder),
                    problem.status.value,
                    ", ".join(path.name for path in problem.found_files),
                    problem.reason,
                    problem.recommended_action,
                ),
            )
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        buttons = ttk.Frame(window, padding=(12, 0, 12, 12))
        buttons.pack(fill=tk.X)

        def selected_problem() -> PreflightFolderResult | None:
            selection = tree.selection()
            return problems[int(selection[0])] if selection else None

        def open_selected_problem() -> None:
            problem = selected_problem()
            self._open_local_path(
                problem.folder if problem is not None else None
            )

        def open_all_problems() -> None:
            for problem in problems:
                self._open_local_path(problem.folder)

        def retry_preflight() -> None:
            window.destroy()
            self._start_zoom_preflight()

        ttk.Button(
            buttons,
            text="Відкрити папку",
            command=open_selected_problem,
        ).pack(side=tk.LEFT)
        ttk.Button(
            buttons,
            text="Відкрити всі проблемні папки",
            command=open_all_problems,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            buttons,
            text="Повторити перевірку",
            command=retry_preflight,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            buttons,
            text="Перейти до наступної проблеми",
            command=lambda: self._select_next_tree_item(tree),
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            buttons,
            text="Закрити",
            command=window.destroy,
        ).pack(side=tk.RIGHT)
        tree.selection_set("0")
        window.grab_set()

    @staticmethod
    def _select_next_tree_item(tree: ttk.Treeview) -> None:
        items = tree.get_children()
        if not items:
            return
        selection = tree.selection()
        index = (
            (items.index(selection[0]) + 1) % len(items)
            if selection
            else 0
        )
        tree.selection_set(items[index])
        tree.see(items[index])

    def _open_local_path(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as error:
            self._show_error(error)

    def _save_google_settings(self) -> bool:
        try:
            self.settings.save_google_calendar(
                client_secrets=self.google_credentials_var.get(),
                calendar_id=self._selected_google_calendar_id(),
                timezone_name=self.google_timezone_var.get(),
                zoom_recordings_dir=self.zoom_recordings_dir_var.get(),
                automatic_time_tolerance_minutes=int(
                    self.automatic_tolerance_var.get()
                ),
                manual_time_search_window_minutes=int(
                    self.manual_window_var.get()
                ),
                next_lesson_overlap_tolerance_minutes=int(
                    self.next_overlap_var.get()
                ),
                minimum_video_size_mb=float(
                    self.minimum_video_size_var.get()
                ),
                video_stability_check_seconds=float(
                    self.video_stability_var.get()
                ),
                profile_id=self.profile_var.get(),
            )
        except Exception as error:
            self._show_error(error)
            return False
        return True

    def _connect_google_calendar(self) -> None:
        if not self._save_google_settings():
            return
        credentials_path = Path(self.google_credentials_var.get()).expanduser()

        async def connect() -> tuple[
            GoogleCalendarService,
            tuple[GoogleCalendarInfo, ...],
        ]:
            manager = GoogleOAuthManager(self.google_token_store)
            credentials = await asyncio.to_thread(
                manager.authorize,
                credentials_path,
            )
            service = await asyncio.to_thread(
                GoogleCalendarService.from_credentials,
                credentials,
            )
            calendars = await asyncio.to_thread(service.list_calendars)
            return service, calendars

        self._run_async(
            connect(),
            self._google_connected,
            "Підключення Google Calendar…",
        )

    def _google_connected(
        self,
        result: tuple[
            GoogleCalendarService,
            tuple[GoogleCalendarInfo, ...],
        ],
    ) -> None:
        service, calendars = result
        self.google_service = service
        self.google_calendar_by_label.clear()
        labels: list[str] = []
        selected_label = ""
        configured_id = self.settings.load().config.google_calendar_id
        for calendar in calendars:
            label = (
                f"{calendar.summary} — {calendar.id}"
                + (" (основний)" if calendar.primary else "")
            )
            labels.append(label)
            self.google_calendar_by_label[label] = calendar
            if calendar.id == configured_id:
                selected_label = label
            elif not selected_label and configured_id == "primary" and calendar.primary:
                selected_label = label
        self.google_calendar_combo.configure(values=labels)
        if selected_label:
            self.google_calendar_var.set(selected_label)
        elif labels:
            self.google_calendar_var.set(labels[0])
        self.google_status_var.set(
            f"Підключено. Доступно календарів: {len(calendars)}"
        )
        self._log("Google Calendar підключено в режимі лише читання.")

    def _selected_google_calendar_id(self) -> str:
        selected = self.google_calendar_var.get().strip()
        calendar = self.google_calendar_by_label.get(selected)
        return calendar.id if calendar is not None else selected

    def _load_google_events(self) -> None:
        if self.workflow.state is not WorkflowState.PREFLIGHT_PASSED:
            self._show_error(
                WorkflowTransitionError(
                    "Спочатку успішно виконайте «Перевірити конвертацію»."
                )
            )
            return
        if not self._preflight_matches_current_selection():
            self._show_error(
                WorkflowTransitionError(
                    "Папка Zoom або діапазон дат змінилися після preflight. "
                    "Повторіть перевірку конвертації."
                )
            )
            return
        if self.google_service is None:
            self._show_error(
                ValueError("Спочатку натисніть «Підключити Google».")
            )
            return
        try:
            date_from, date_to = self._selected_period()
            if not self._save_google_settings():
                return
            self.workflow.start_matching()
        except (ValueError, WorkflowTransitionError) as error:
            self._show_error(error)
            return
        self._apply_workflow_state()
        calendar_id = self._selected_google_calendar_id()
        timezone_name = self.google_timezone_var.get().strip()

        async def load_events() -> tuple[GoogleCalendarEvent, ...]:
            assert self.google_service is not None
            return await asyncio.to_thread(
                self.google_service.list_events,
                calendar_id=calendar_id,
                date_from=date_from,
                date_to=date_to,
                timezone_name=timezone_name,
            )

        self._run_async(
            load_events(),
            self._google_events_loaded,
            "Завантаження подій Google Calendar…",
        )

    def _google_events_loaded(
        self,
        events: tuple[GoogleCalendarEvent, ...],
    ) -> None:
        self.google_events = list(events)
        self.google_event_tree.delete(*self.google_event_tree.get_children())
        self.google_tree_event_index.clear()
        timezone_name = self.google_timezone_var.get().strip()
        parsed_events = tuple(
            parse_calendar_event(event, timezone_name=timezone_name)
            for event in events
        )
        self.parsed_calendar_events = parsed_events
        for parsed in parsed_events:
            self.calendar_report_repository.save_calendar_event_report(parsed)
        try:
            if self.preflight_result is None or not self.preflight_result.is_passed:
                raise WorkflowTransitionError(
                    "Matching заборонено без успішного preflight."
                )
            metadata_by_path = {
                video: metadata
                for folder in self.preflight_result.ready_folders
                for video, metadata in zip(
                    folder.video_paths,
                    folder.metadata,
                    strict=True,
                )
            }
            catalog = ZoomRecordingCatalog.scan(
                self.preflight_result.root,
                timezone_name=timezone_name,
                metadata_probe=metadata_by_path.__getitem__,
                manual_folder_starts=(
                    self.calendar_report_repository.get_zoom_folder_starts()
                ),
                date_from=self.preflight_result.date_from,
                date_to=self.preflight_result.date_to,
            )
            ready_paths = {
                folder.folder
                for folder in self.preflight_result.ready_folders
            }
            segments = tuple(
                segment
                for folder in catalog.folders
                if folder.path in ready_paths
                for segment in folder.segments
            )
            results = list(
                match_zoom_segments(
                    segments,
                    parsed_events,
                    settings=self._current_match_settings(),
                )
            )
            for index, result in enumerate(results):
                if result.is_resolved or result.event is None:
                    continue
                decision = (
                    self.calendar_report_repository
                    .get_valid_zoom_decision(
                        result.segment,
                        result.event,
                    )
                )
                if decision is not None and decision.assignment_type not in {
                    AssignmentType.DEFERRED,
                }:
                    results[index] = replace(
                        result,
                        status=ZoomMatchStatus.MANUALLY_CONFIRMED,
                        reason=(
                            "Використано збережене ручне рішення: "
                            f"{decision.reason}"
                        ),
                    )
            assembly = assemble_zoom_batch(
                parsed_events,
                tuple(results),
                profile_id=self.profile_var.get().strip(),
                batch_id=self.batch_var.get().strip(),
                manual_text_event_ids=frozenset(
                    self.manual_text_event_ids
                ),
                ignored_event_ids=frozenset(self.ignored_event_ids),
            )
        except Exception:
            if self.workflow.state is WorkflowState.MATCHING_RUNNING:
                self.workflow.finish_matching(
                    has_unresolved_problems=True
                )
                self._apply_workflow_state()
            raise

        self.zoom_match_results = results
        self.unresolved_zoom_results = list(assembly.unresolved_results)
        self.missing_zoom_events = list(assembly.missing_events)
        self.zoom_catalog_issues = list(catalog.issues)
        self.lessons = list(assembly.lessons)
        unresolved_count = (
            len(self.unresolved_zoom_results)
            + len(self.missing_zoom_events)
            + len(self.zoom_catalog_issues)
        )
        self.workflow.finish_matching(
            has_unresolved_problems=unresolved_count > 0
        )
        self._render_zoom_matching_tree()
        self._refresh_lessons()
        self._apply_workflow_state()
        self.google_status_var.set(
            f"Calendar: {len(events)}; Zoom-сегментів: {len(results)}"
        )
        self._log(
            f"Matching завершено. Невирішених питань: {unresolved_count}."
        )
        if unresolved_count == 0:
            messagebox.showinfo(
                APP_TITLE,
                "Усі записи перевірено. Невирішених питань: 0.",
            )

    def _render_zoom_matching_tree(self) -> None:
        self.google_event_tree.delete(*self.google_event_tree.get_children())
        self.google_tree_event_index.clear()
        event_index = {
            event.id: index for index, event in enumerate(self.google_events)
        }
        for index, result in enumerate(self.zoom_match_results):
            event = result.event
            next_event = result.next_event
            iid = f"zoom-{index}"
            if event is not None and event.event_id in event_index:
                self.google_tree_event_index[iid] = event_index[event.event_id]
            self.google_event_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    result.segment.estimated_start.strftime(
                        "%d.%m.%Y %H:%M:%S"
                    ),
                    event.start.strftime("%H:%M") if event else "—",
                    event.end.strftime("%H:%M") if event else "—",
                    (
                        f"{result.start_difference_minutes:g} хв"
                        if result.start_difference_minutes is not None
                        else "—"
                    ),
                    result.segment.estimated_end.strftime("%H:%M:%S"),
                    (
                        f"{next_event.start:%H:%M} "
                        f"{next_event.student_name}"
                        if next_event
                        else "—"
                    ),
                    (
                        f"{result.next_overlap_seconds / 60:.1f} хв"
                        if result.next_overlap_seconds
                        else "0"
                    ),
                    result.segment.time_method.value,
                    result.status.value,
                    (
                        f"{event.student_id} {event.student_name} "
                        f"{event.student_age or ''}р"
                        if event
                        else "—"
                    ),
                    event.event_id if event else "—",
                ),
            )
        for index, event in enumerate(self.missing_zoom_events):
            iid = f"missing-{index}"
            if event.event_id in event_index:
                self.google_tree_event_index[iid] = event_index[event.event_id]
            self.google_event_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    "—",
                    event.start.strftime("%d.%m.%Y %H:%M"),
                    event.end.strftime("%H:%M"),
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "ZOOM_FOLDER_NOT_FOUND",
                    f"{event.student_id} {event.student_name}",
                    event.event_id,
                ),
            )
        for index, issue in enumerate(self.zoom_catalog_issues):
            self.google_event_tree.insert(
                "",
                tk.END,
                iid=f"parse-{index}",
                values=(
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    issue.status.value,
                    issue.path.name,
                    "—",
                ),
            )

    def _resolve_next_zoom_problem(self) -> None:
        if self.workflow.state is not WorkflowState.RESOLUTION_REQUIRED:
            messagebox.showinfo(APP_TITLE, "Невирішених питань немає.")
            return
        if self.zoom_catalog_issues:
            issue = self.zoom_catalog_issues[0]
            value = simpledialog.askstring(
                "Введіть дату й час Zoom",
                (
                    f"{issue.reason}\n{issue.path}\n\n"
                    "Введіть локальний час у форматі РРРР-ММ-ДД ГГ:ХХ:СС:"
                ),
                parent=self.root,
            )
            if not value:
                return
            try:
                parsed = datetime.fromisoformat(value.strip())
                timezone_name = self.google_timezone_var.get().strip()
                from zoneinfo import ZoneInfo

                aware = parsed.replace(tzinfo=ZoneInfo(timezone_name))
                self.calendar_report_repository.save_zoom_folder_start(
                    issue.path,
                    aware,
                )
            except Exception as error:
                self._show_error(error)
                return
            self._log(
                "Ручний час Zoom-папки збережено. "
                "Повторіть preflight для технічної перевірки."
            )
            self._restart_workflow_preflight()
            return
        if self.missing_zoom_events:
            self._resolve_missing_zoom_event(self.missing_zoom_events[0])
            return
        if self.unresolved_zoom_results:
            self._show_zoom_conflict_dialog(
                self.unresolved_zoom_results[0]
            )
            return
        self._rebuild_zoom_batch_after_resolution()

    def _change_selected_zoom_decision(self) -> None:
        selection = self.google_event_tree.selection()
        if not selection:
            self._resolve_next_zoom_problem()
            return
        item_id = selection[0]
        if item_id.startswith("zoom-"):
            self._show_zoom_conflict_dialog(
                self.zoom_match_results[int(item_id.removeprefix("zoom-"))]
            )
            return
        if item_id.startswith("missing-"):
            self._resolve_missing_zoom_event(
                self.missing_zoom_events[
                    int(item_id.removeprefix("missing-"))
                ]
            )
            return
        self._resolve_next_zoom_problem()

    def _resolve_missing_zoom_event(
        self,
        event: ParsedCalendarEvent,
    ) -> None:
        window = tk.Toplevel(self.root)
        window.title("Zoom-папку не знайдено")
        window.transient(self.root)
        ttk.Label(
            window,
            text=(
                f"{event.start:%d.%m.%Y %H:%M–}{event.end:%H:%M}\n"
                f"{event.student_id} {event.student_name} "
                f"{event.student_age or ''}р\n\n"
                "Оберіть явне рішення. Закриття вікна залишає "
                "питання невирішеним."
            ),
            justify=tk.LEFT,
            padding=16,
        ).pack(fill=tk.X)
        buttons = ttk.Frame(window, padding=16)
        buttons.pack(fill=tk.X)
        ttk.Button(
            buttons,
            text="Вказати MP4 вручну",
            command=lambda: self._attach_manual_mp4(event, window),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            buttons,
            text="Вказати Zoom-папку вручну",
            command=lambda: self._attach_manual_zoom_folder(
                event,
                window,
            ),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            buttons,
            text="Позначити урок як проведений без запису",
            command=lambda: self._resolve_missing_as_text(event, window),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            buttons,
            text="Позначити, що урок не проводився",
            command=lambda: self._resolve_missing_as_not_conducted(
                event,
                window,
            ),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            buttons,
            text="Відкласти рішення",
            command=window.destroy,
        ).pack(fill=tk.X, pady=3)
        window.grab_set()

    def _attach_manual_mp4(
        self,
        event: ParsedCalendarEvent,
        window: tk.Toplevel,
    ) -> None:
        selected = filedialog.askopenfilename(
            title=(
                f"Вкажіть MP4 для {event.start:%d.%m.%Y %H:%M} "
                f"{event.student_name}"
            ),
            filetypes=[("MP4 відео", "*.mp4")],
        )
        if not selected:
            return
        try:
            result = self._manual_zoom_result(
                event,
                Path(selected),
                estimated_start=event.start,
            )
        except Exception as error:
            self._show_error(error)
            return
        self.zoom_match_results.append(result)
        self._save_zoom_decision(
            result,
            AssignmentType.MANUALLY_SELECTED_EVENT,
            "MP4 вибрано вручну для події без знайденої Zoom-папки.",
        )
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _attach_manual_zoom_folder(
        self,
        event: ParsedCalendarEvent,
        window: tk.Toplevel,
    ) -> None:
        selected = filedialog.askdirectory(
            title=(
                f"Вкажіть Zoom-папку для "
                f"{event.start:%d.%m.%Y %H:%M}"
            )
        )
        if not selected:
            return
        folder = Path(selected)
        paths = tuple(
            sorted(
                (
                    path
                    for path in folder.iterdir()
                    if (
                        path.is_file()
                        and path.name.casefold().startswith("video")
                        and path.suffix.casefold() == ".mp4"
                    )
                ),
                key=lambda path: path.name.casefold(),
            )
        )
        if not paths:
            self._show_error(
                ValueError("У вибраній папці немає video*.mp4")
            )
            return
        results: list[ZoomMatchResult] = []
        estimated_start = event.start
        try:
            for path in paths:
                result = self._manual_zoom_result(
                    event,
                    path,
                    estimated_start=estimated_start,
                    sequence_number=len(results) + 1,
                )
                results.append(result)
                estimated_start = result.segment.estimated_end
        except Exception as error:
            self._show_error(error)
            return
        for result in results:
            self.zoom_match_results.append(result)
            self._save_zoom_decision(
                result,
                AssignmentType.MANUALLY_SELECTED_EVENT,
                "Zoom-папку вибрано користувачем вручну.",
            )
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    @staticmethod
    def _manual_zoom_result(
        event: ParsedCalendarEvent,
        path: Path,
        *,
        estimated_start: datetime,
        sequence_number: int = 1,
    ) -> ZoomMatchResult:
        metadata = probe_mp4(path)
        if not metadata.has_video_stream or metadata.duration_seconds <= 0:
            raise ValueError("Обраний MP4 не має валідного відеопотоку")
        segment = ZoomVideoSegment(
            source_folder=path.parent,
            path=path,
            sequence_number=sequence_number,
            duration_seconds=metadata.duration_seconds,
            estimated_start=estimated_start,
            estimated_end=(
                estimated_start
                + timedelta(seconds=metadata.duration_seconds)
            ),
            time_method=VideoTimeMethod.MANUALLY_CONFIRMED,
            confidence=VideoTimeConfidence.HIGH,
            file_size=path.stat().st_size,
        )
        return ZoomMatchResult(
            segment=segment,
            status=ZoomMatchStatus.MANUALLY_CONFIRMED,
            event=event,
            candidates=(),
            start_difference_minutes=abs(
                (estimated_start - event.start).total_seconds()
            )
            / 60,
            reason="MP4 вибрано користувачем вручну.",
        )

    def _resolve_missing_as_text(
        self,
        event: ParsedCalendarEvent,
        window: tk.Toplevel,
    ) -> None:
        self.manual_text_event_ids.add(event.event_id)
        self.ignored_event_ids.discard(event.event_id)
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _resolve_missing_as_not_conducted(
        self,
        event: ParsedCalendarEvent,
        window: tk.Toplevel,
    ) -> None:
        self.ignored_event_ids.add(event.event_id)
        self.manual_text_event_ids.discard(event.event_id)
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _show_zoom_conflict_dialog(
        self,
        result: ZoomMatchResult,
    ) -> None:
        window = tk.Toplevel(self.root)
        window.title("Підтвердьте відповідність часу")
        window.geometry("980x760")
        window.transient(self.root)
        window.protocol(
            "WM_DELETE_WINDOW",
            lambda: self._defer_zoom_result(result, window),
        )
        event = result.event
        details = [
            f"Zoom-папка: {result.segment.source_folder}",
            f"Файл: {result.segment.path.name}",
            f"Zoom start: {result.segment.estimated_start:%d.%m.%Y %H:%M:%S}",
            f"Video end: {result.segment.estimated_end:%H:%M:%S}",
            f"Статус: {result.status.value}",
            f"Причина: {result.reason}",
        ]
        if event is not None:
            details.extend([
                f"Calendar: {event.start:%H:%M}–{event.end:%H:%M}",
                (
                    f"Учень: {event.student_id} {event.student_name} "
                    f"{event.student_age or ''}р {event.lesson_type}"
                ),
                (
                    "Різниця: "
                    f"{result.start_difference_minutes:g} хв"
                    if result.start_difference_minutes is not None
                    else "Різниця: —"
                ),
            ])
        ttk.Label(
            window,
            text="\n".join(details),
            justify=tk.LEFT,
            wraplength=920,
        ).pack(fill=tk.X, padx=16, pady=12)
        preview = ttk.Frame(window)
        preview.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        images: list[tk.PhotoImage] = []
        try:
            offsets = (
                boundary_preview_offsets(
                    result.segment.duration_seconds,
                    boundary_seconds=result.split_offset_seconds,
                )
                if result.split_offset_seconds is not None
                else general_preview_offsets(
                    result.segment.duration_seconds
                )
            )
            frames = extract_preview_frames(
                result.segment.path,
                offsets,
                output_dir=(
                    self.workspace
                    / ".lesson-video-uploader"
                    / "previews"
                    / result.segment.path.stem
                ),
            )
            for index, (frame, offset) in enumerate(
                zip(frames, offsets, strict=True)
            ):
                cell = ttk.Frame(preview, padding=4)
                cell.grid(
                    row=index // 2,
                    column=index % 2,
                    sticky=tk.NSEW,
                )
                image = tk.PhotoImage(file=frame)
                images.append(image)
                ttk.Label(cell, image=image).pack()
                ttk.Label(
                    cell,
                    text=f"{_format_seconds(offset)}",
                ).pack()
                ttk.Button(
                    cell,
                    text="Переглянути біля цього моменту",
                    command=partial(
                        self._open_local_path,
                        result.segment.path,
                    ),
                ).pack()
            setattr(window, "_preview_images", images)
            preview.columnconfigure(0, weight=1)
            preview.columnconfigure(1, weight=1)
            preview.rowconfigure(0, weight=1)
            preview.rowconfigure(1, weight=1)
        except Exception as error:
            ttk.Label(
                preview,
                text=f"Preview-кадри недоступні: {error}",
                foreground="#a00000",
            ).pack()

        buttons = ttk.Frame(window, padding=12)
        buttons.pack(fill=tk.X)
        if event is not None:
            ttk.Button(
                buttons,
                text="Так, це цей урок",
                style="Primary.TButton",
                command=lambda: self._confirm_zoom_result(
                    result,
                    event,
                    AssignmentType.MANUALLY_CONFIRMED_TIME,
                    "Відповідність часу підтверджена користувачем.",
                    window,
                ),
            ).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Button(
            buttons,
            text="Обрати інший урок",
            command=lambda: self._choose_other_calendar_event(
                result,
                window,
            ),
        ).pack(side=tk.LEFT, padx=3, pady=3)
        if result.next_event is not None:
            ttk.Button(
                buttons,
                text="Усе відео поточному учню",
                command=lambda: self._confirm_zoom_result(
                    result,
                    event,
                    AssignmentType.CURRENT_EVENT_FULL_VIDEO,
                    "Користувач залишив усе відео поточному учню.",
                    window,
                ),
            ).pack(side=tk.LEFT, padx=3, pady=3)
            ttk.Button(
                buttons,
                text="Усе відео наступному учню",
                command=lambda: self._confirm_zoom_result(
                    result,
                    result.next_event,
                    AssignmentType.NEXT_EVENT,
                    "Користувач прив’язав усе відео до наступного уроку.",
                    window,
                ),
            ).pack(side=tk.LEFT, padx=3, pady=3)
            ttk.Button(
                buttons,
                text="Розрізати за календарем",
                command=lambda: self._split_zoom_conflict(
                    result,
                    result.split_offset_seconds,
                    window,
                ),
            ).pack(side=tk.LEFT, padx=3, pady=3)
            ttk.Button(
                buttons,
                text="Змінити точку розрізання",
                command=lambda: self._ask_custom_split(
                    result,
                    window,
                ),
            ).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Button(
            buttons,
            text="Це не урок",
            command=lambda: self._resolve_without_event(
                result,
                ZoomMatchStatus.NOT_A_LESSON,
                window,
            ),
        ).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Button(
            buttons,
            text="Пропустити",
            command=lambda: self._resolve_without_event(
                result,
                ZoomMatchStatus.SKIPPED_BY_USER,
                window,
            ),
        ).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Button(
            buttons,
            text="Відкласти рішення",
            command=lambda: self._defer_zoom_result(result, window),
        ).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Button(
            buttons,
            text="Відкрити відео",
            command=lambda: self._open_local_path(result.segment.path),
        ).pack(side=tk.RIGHT, padx=3)
        ttk.Button(
            buttons,
            text="Відкрити Zoom-папку",
            command=lambda: self._open_local_path(
                result.segment.source_folder
            ),
        ).pack(side=tk.RIGHT, padx=3)
        window.grab_set()

    def _choose_other_calendar_event(
        self,
        result: ZoomMatchResult,
        parent: tk.Toplevel,
    ) -> None:
        candidates = tuple(
            event
            for event in self.parsed_calendar_events
            if (
                event.requires_video
                and abs(
                    (
                        event.start - result.segment.estimated_start
                    ).total_seconds()
                )
                <= int(self.manual_window_var.get()) * 60
            )
        )
        if not candidates:
            messagebox.showinfo(
                APP_TITLE,
                "У межах ручного вікна немає проведених уроків.",
            )
            return
        chooser = tk.Toplevel(parent)
        chooser.title("Обрати інший урок")
        columns = ("start", "end", "difference", "student", "type", "title")
        tree = ttk.Treeview(
            chooser,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=150)
        for index, event in enumerate(candidates):
            tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    event.start.strftime("%d.%m.%Y %H:%M"),
                    event.end.strftime("%H:%M"),
                    (
                        f"{abs((event.start - result.segment.estimated_start).total_seconds()) / 60:.1f}"
                    ),
                    f"{event.student_id} {event.student_name} {event.student_age}р",
                    event.status.value,
                    event.original_summary,
                ),
            )
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        def choose() -> None:
            selection = tree.selection()
            if not selection:
                return
            event = candidates[int(selection[0])]
            chooser.destroy()
            self._confirm_zoom_result(
                result,
                event,
                AssignmentType.MANUALLY_SELECTED_EVENT,
                "Користувач обрав іншу Calendar event.",
                parent,
            )

        ttk.Button(
            chooser,
            text="Обрати",
            style="Primary.TButton",
            command=choose,
        ).pack(pady=(0, 10))
        tree.selection_set("0")
        chooser.grab_set()

    def _confirm_zoom_result(
        self,
        result: ZoomMatchResult,
        event: ParsedCalendarEvent | None,
        assignment_type: AssignmentType,
        reason: str,
        window: tk.Toplevel,
    ) -> None:
        if event is None:
            return
        index = self.zoom_match_results.index(result)
        resolved = replace(
            result,
            status=ZoomMatchStatus.MANUALLY_CONFIRMED,
            event=event,
            reason=reason,
        )
        self.zoom_match_results[index] = resolved
        self._save_zoom_decision(resolved, assignment_type, reason)
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _save_zoom_decision(
        self,
        result: ZoomMatchResult,
        assignment_type: AssignmentType,
        reason: str,
        *,
        split_point: float | None = None,
    ) -> None:
        if result.event is None:
            return
        self.calendar_report_repository.save_zoom_decision(
            ZoomAssignmentDecision(
                file_identity=FileIdentity.from_segment(result.segment),
                calendar_event_id=result.event.event_id,
                event_start_utc=result.event.start.astimezone(timezone.utc),
                assignment_type=assignment_type,
                confirmed_by=self.profile_var.get().strip(),
                confirmed_at=datetime.now(timezone.utc),
                reason=reason,
                manual_split_point_seconds=split_point,
            )
        )

    def _resolve_without_event(
        self,
        result: ZoomMatchResult,
        status: ZoomMatchStatus,
        window: tk.Toplevel,
    ) -> None:
        index = self.zoom_match_results.index(result)
        self.zoom_match_results[index] = replace(
            result,
            status=status,
            event=None,
            reason="Користувач виключив відео з batch.",
        )
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _defer_zoom_result(
        self,
        result: ZoomMatchResult,
        window: tk.Toplevel,
    ) -> None:
        index = self.zoom_match_results.index(result)
        self.zoom_match_results[index] = replace(
            result,
            status=ZoomMatchStatus.DEFERRED,
            reason="Рішення відкладено користувачем.",
        )
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _ask_custom_split(
        self,
        result: ZoomMatchResult,
        window: tk.Toplevel,
    ) -> None:
        raw = simpledialog.askstring(
            APP_TITLE,
            "Точка розрізання від початку відео (ГГ:ХХ:СС):",
            initialvalue=(
                _format_seconds(result.split_offset_seconds)
                if result.split_offset_seconds is not None
                else "00:30:00"
            ),
            parent=window,
        )
        if not raw:
            return
        try:
            offset = _parse_offset(raw)
        except ValueError as error:
            self._show_error(error)
            return
        self._split_zoom_conflict(result, offset, window)

    def _split_zoom_conflict(
        self,
        result: ZoomMatchResult,
        offset: float | None,
        window: tk.Toplevel,
    ) -> None:
        if (
            offset is None
            or result.event is None
            or result.next_event is None
        ):
            self._show_error(ValueError("Немає коректної точки розрізання"))
            return
        try:
            before_path, after_path = split_mp4(
                result.segment.path,
                split_offset_seconds=offset,
                output_dir=(
                    self.workspace
                    / ".lesson-video-uploader"
                    / "splits"
                    / result.segment.path.stem
                ),
            )
            after_duration = result.segment.duration_seconds - offset
            if after_duration <= 0:
                raise ValueError("Точка розрізання поза межами відео")
            before_segment = replace(
                result.segment,
                path=before_path,
                duration_seconds=offset,
                estimated_end=(
                    result.segment.estimated_start
                    + timedelta(seconds=offset)
                ),
                file_size=before_path.stat().st_size,
                time_method=VideoTimeMethod.MANUALLY_CONFIRMED,
            )
            after_segment = replace(
                result.segment,
                path=after_path,
                sequence_number=result.segment.sequence_number + 1,
                duration_seconds=after_duration,
                estimated_start=before_segment.estimated_end,
                estimated_end=result.segment.estimated_end,
                file_size=after_path.stat().st_size,
                time_method=VideoTimeMethod.MANUALLY_CONFIRMED,
            )
            before_result = replace(
                result,
                segment=before_segment,
                status=ZoomMatchStatus.MANUALLY_CONFIRMED,
                next_event=None,
                next_overlap_seconds=0,
                split_offset_seconds=None,
                reason="Відео розрізано за Calendar boundary.",
            )
            after_result = replace(
                result,
                segment=after_segment,
                status=ZoomMatchStatus.MANUALLY_CONFIRMED,
                event=result.next_event,
                next_event=None,
                next_overlap_seconds=0,
                split_offset_seconds=None,
                reason="Друга частина прив’язана до наступного уроку.",
            )
        except Exception as error:
            index = self.zoom_match_results.index(result)
            self.zoom_match_results[index] = replace(
                result,
                status=ZoomMatchStatus.VIDEO_SPLIT_FAILED,
                reason=f"Не вдалося розрізати відео: {error}",
            )
            window.destroy()
            self._rebuild_zoom_batch_after_resolution()
            self._show_error(error)
            return
        index = self.zoom_match_results.index(result)
        self.zoom_match_results[index:index + 1] = [
            before_result,
            after_result,
        ]
        self._save_zoom_decision(
            before_result,
            AssignmentType.SPLIT_BETWEEN_EVENTS,
            "Відео розрізано за підтвердженою точкою.",
            split_point=offset,
        )
        self._save_zoom_decision(
            after_result,
            AssignmentType.SPLIT_BETWEEN_EVENTS,
            "Друга частина відео прив’язана до наступного уроку.",
            split_point=offset,
        )
        window.destroy()
        self._rebuild_zoom_batch_after_resolution()

    def _rebuild_zoom_batch_after_resolution(self) -> None:
        assembly = assemble_zoom_batch(
            self.parsed_calendar_events,
            tuple(self.zoom_match_results),
            profile_id=self.profile_var.get().strip(),
            batch_id=self.batch_var.get().strip(),
            manual_text_event_ids=frozenset(
                self.manual_text_event_ids
            ),
            ignored_event_ids=frozenset(self.ignored_event_ids),
        )
        self.lessons = list(assembly.lessons)
        self.unresolved_zoom_results = list(assembly.unresolved_results)
        self.missing_zoom_events = list(assembly.missing_events)
        unresolved_count = (
            len(self.unresolved_zoom_results)
            + len(self.missing_zoom_events)
            + len(self.zoom_catalog_issues)
        )
        if (
            unresolved_count == 0
            and self.workflow.state is WorkflowState.RESOLUTION_REQUIRED
        ):
            self.workflow.finish_resolution(unresolved_count=0)
            messagebox.showinfo(
                APP_TITLE,
                "Усі записи перевірено. Невирішених питань: 0.",
            )
        self._render_zoom_matching_tree()
        self._refresh_lessons()
        self._apply_workflow_state()

    def _restart_workflow_preflight(self) -> None:
        try:
            self.workflow.start_preflight()
        except WorkflowTransitionError as error:
            self._show_error(error)
            return
        self._apply_workflow_state()
        self.workflow.finish_preflight(has_blocking_problems=True)
        self._apply_workflow_state()
        self.preflight_result = None
        self._start_zoom_preflight()

    def _import_google_event(self) -> None:
        selection = self.google_event_tree.selection()
        if not selection:
            self._show_error(ValueError("Виберіть подію календаря."))
            return
        event_index = self.google_tree_event_index.get(selection[0])
        if event_index is None:
            self._show_error(
                ValueError(
                    "Цей рядок не має однозначної Calendar event. "
                    "Спочатку розв’яжіть проблему."
                )
            )
            return
        event = self.google_events[event_index]
        try:
            form = calendar_event_to_lesson_form(
                event,
                timezone_name=self.google_timezone_var.get().strip(),
            )
        except CalendarEventNotSendable as error:
            self._show_error(error)
            return
        self.pending_calendar_form = form
        self.event_id_var.set(form.calendar_event_id)
        self.start_var.set(form.event_start)
        self.student_id_var.set(form.student_id)
        self.student_name_var.set(form.student_name)
        self.lesson_label_var.set(form.lesson_label)
        self.duration_var.set(str(form.duration_hours))
        self.trial_var.set(form.is_trial)
        self.no_recording_var.set(form.is_no_recording)
        matched_paths = [
            result.segment.path
            for result in self.zoom_match_results
            if (
                result.event is not None
                and result.event.event_id == form.calendar_event_id
                and result.is_resolved
            )
        ]
        self.pending_video_paths = (
            [] if form.is_no_recording else matched_paths
        )
        self._refresh_pending_files()
        self.notebook.select(self.send_tab)
        self._log(
            f"Імпортовано {form.calendar_status}. "
            + (
                "Це text-only урок; MP4 не потрібні."
                if form.is_no_recording
                else (
                    f"Автоматично додано MP4: {len(matched_paths)}."
                    if matched_paths
                    else "MP4 не знайдено; виберіть файл вручну."
                )
            )
        )

    def _disconnect_google_calendar(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "Видалити збережений Google OAuth token із системного keyring?",
        ):
            return
        GoogleOAuthManager(self.google_token_store).disconnect()
        self.google_service = None
        self.google_events.clear()
        self.google_calendar_by_label.clear()
        self.google_calendar_combo.configure(values=())
        self.google_event_tree.delete(*self.google_event_tree.get_children())
        self.google_status_var.set("Google Calendar не підключений")
        self._log("Google Calendar відключено; OAuth token видалено.")

    def _save_settings(self, *, quiet: bool = False) -> bool:
        api_hash_was_entered = bool(self.api_hash_var.get().strip())
        try:
            loaded = self.settings.save(
                api_id_text=self.api_id_var.get(),
                api_hash=self.api_hash_var.get(),
                phone=self.phone_var.get(),
                session=self.session_var.get(),
                profile_id=self.profile_var.get(),
            )
        except Exception as error:
            self._show_error(error)
            return False
        self.api_hash_var.set("")
        self.secret_status_var.set(
            "API hash збережено"
            if loaded.api_hash_saved
            else "API hash ще не збережений"
        )
        if not quiet:
            self._log("Налаштування збережено.")
            messagebox.showinfo(
                APP_TITLE,
                (
                    "API hash збережено"
                    if api_hash_was_entered
                    else "Налаштування збережено безпечно."
                ),
            )
        return True

    def _pick_videos(self) -> None:
        if self.no_recording_var.get():
            self._show_error(
                ValueError("Для уроку «без запису» MP4 не додаються.")
            )
            return
        selected = filedialog.askopenfilenames(
            title="Виберіть MP4 одного уроку в правильному порядку",
            filetypes=[("MP4 відео", "*.mp4")],
        )
        for raw_path in selected:
            path = Path(raw_path)
            if path not in self.pending_video_paths:
                self.pending_video_paths.append(path)
        self._refresh_pending_files()

    def _refresh_pending_files(self) -> None:
        self.pending_files.delete(0, tk.END)
        for index, path in enumerate(self.pending_video_paths, start=1):
            self.pending_files.insert(tk.END, f"{index}. {path.name}")

    def _move_video(self, delta: int) -> None:
        selection = self.pending_files.curselection()
        if not selection:
            return
        old = selection[0]
        new = old + delta
        if not 0 <= new < len(self.pending_video_paths):
            return
        self.pending_video_paths[old], self.pending_video_paths[new] = (
            self.pending_video_paths[new],
            self.pending_video_paths[old],
        )
        self._refresh_pending_files()
        self.pending_files.selection_set(new)

    def _remove_video(self) -> None:
        selection = self.pending_files.curselection()
        if selection:
            self.pending_video_paths.pop(selection[0])
            self._refresh_pending_files()

    def _add_lesson(self) -> None:
        try:
            calendar_form = (
                self.pending_calendar_form
                if (
                    self.pending_calendar_form is not None
                    and self.pending_calendar_form.calendar_event_id
                    == self.event_id_var.get().strip()
                )
                else None
            )
            if calendar_form is not None:
                current_values = {
                    "start": datetime.fromisoformat(self.start_var.get().strip()),
                    "student_id": self.student_id_var.get().strip(),
                    "student_name": self.student_name_var.get().strip(),
                    "lesson_label": self.lesson_label_var.get().strip(),
                    "duration": int(self.duration_var.get()),
                    "trial": self.trial_var.get(),
                    "no_recording": self.no_recording_var.get(),
                }
                expected_values = {
                    "start": datetime.fromisoformat(calendar_form.event_start),
                    "student_id": calendar_form.student_id,
                    "student_name": calendar_form.student_name,
                    "lesson_label": calendar_form.lesson_label,
                    "duration": calendar_form.duration_hours,
                    "trial": calendar_form.is_trial,
                    "no_recording": calendar_form.is_no_recording,
                }
                changed = [
                    name
                    for name, value in current_values.items()
                    if value != expected_values[name]
                ]
                if changed:
                    raise ValueError(
                        "Дані Calendar змінено у формі: "
                        + ", ".join(changed)
                        + ". Виправте подію в Google Calendar та імпортуйте її знову."
                    )
                form = replace(
                    calendar_form,
                    video_paths=(
                        ()
                        if calendar_form.is_no_recording
                        else tuple(self.pending_video_paths)
                    ),
                )
            else:
                form = LessonForm(
                    calendar_event_id=self.event_id_var.get(),
                    event_start=self.start_var.get(),
                    student_id=self.student_id_var.get(),
                    student_name=self.student_name_var.get(),
                    lesson_label=self.lesson_label_var.get(),
                    duration_hours=int(self.duration_var.get()),
                    is_trial=self.trial_var.get(),
                    video_paths=tuple(self.pending_video_paths),
                    is_no_recording=self.no_recording_var.get(),
                )
            lesson = create_lesson_from_form(
                profile_id=self.profile_var.get(),
                batch_id=self.batch_var.get(),
                form=form,
            )
            if any(
                item.calendar_event_id == lesson.calendar_event_id
                for item in self.lessons
            ):
                raise ValueError("Урок із цим Calendar event ID уже доданий")
        except Exception as error:
            self._show_error(error)
            return
        self.lessons.append(lesson)
        self.lessons.sort(key=lambda item: (item.event_start, item.calendar_event_id))
        self._refresh_lessons()
        self.pending_video_paths.clear()
        self.pending_calendar_form = None
        self.no_recording_var.set(False)
        self._refresh_pending_files()
        self.event_id_var.set(f"event-{datetime.now():%Y%m%d-%H%M%S}")
        self._log(f"Додано урок: {lesson.caption}")

    def _refresh_lessons(self) -> None:
        self.lesson_tree.delete(*self.lesson_tree.get_children())
        for index, lesson in enumerate(self.lessons):
            if lesson.send_mode is LessonSendMode.TEXT_ONLY:
                telegram_label = "текст"
            else:
                plans = plan_albums(lesson)
                telegram_label = (
                    "1 повідомлення"
                    if lesson.video_count == 1
                    else (
                        "1 альбом"
                        if len(plans) == 1
                        else f"{len(plans)} альбоми"
                    )
                )
            self.lesson_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    f"{lesson.event_start:%d.%m.%Y}",
                    lesson.caption,
                    lesson.video_count,
                    telegram_label,
                ),
            )

    def _show_selected_files(self, _event: object = None) -> None:
        self.preview_files.delete(0, tk.END)
        selection = self.lesson_tree.selection()
        if not selection:
            return
        lesson = self.lessons[int(selection[0])]
        for index, path in enumerate(lesson.ordered_video_paths, start=1):
            self.preview_files.insert(tk.END, f"{index}. {path}")

    def _edit_lesson(self) -> None:
        """Move the selected lesson back into the editor.

        It leaves the batch while being edited, so re-adding it is not blocked
        by its own Calendar event ID.
        """
        selection = self.lesson_tree.selection()
        if not selection:
            return
        if self.pending_video_paths and not messagebox.askyesno(
            APP_TITLE,
            "У редакторі вже є вибрані відео. Замінити їх уроком із пакета?",
        ):
            return
        lesson = self.lessons.pop(int(selection[0]))
        form = form_from_lesson(lesson)
        self.event_id_var.set(form.calendar_event_id)
        self.start_var.set(form.event_start)
        self.student_id_var.set(form.student_id)
        self.student_name_var.set(form.student_name)
        self.lesson_label_var.set(form.lesson_label)
        self.duration_var.set(str(form.duration_hours))
        self.trial_var.set(form.is_trial)
        self.no_recording_var.set(form.is_no_recording)
        self.pending_calendar_form = (
            form if form.calendar_snapshot is not None else None
        )
        self.pending_video_paths = list(form.video_paths)
        self._refresh_pending_files()
        self._refresh_lessons()
        self.preview_files.delete(0, tk.END)
        if lesson.details is None:
            self._log(
                "Урок мав власний caption — заповніть поля учня заново "
                "перед додаванням."
            )
        else:
            self._log(f"Урок у редакторі: {lesson.caption}")

    def _remove_lesson(self) -> None:
        selection = self.lesson_tree.selection()
        if not selection:
            return
        self.lessons.pop(int(selection[0]))
        self._refresh_lessons()
        self.preview_files.delete(0, tk.END)

    def _current_manifest(self) -> UploadManifest:
        return build_gui_manifest(
            profile_id=self.profile_var.get(),
            batch_id=self.batch_var.get(),
            target_peer=self.target_var.get(),
            lessons=tuple(self.lessons),
        )

    def _load_batch(self) -> None:
        path = filedialog.askopenfilename(
            title="Відкрити пакет уроків",
            filetypes=[("JSON пакет", "*.json")],
        )
        if not path:
            return
        try:
            manifest = load_manifest(Path(path))
        except Exception as error:
            self._show_error(error)
            return
        self.profile_var.set(manifest.profile_id)
        self.batch_var.set(manifest.batch_id)
        self.target_var.set(str(manifest.target_peer))
        self.lessons = list(manifest.lessons)
        self._refresh_lessons()
        self._log(f"Відкрито пакет: {path}")

    def _save_batch(self) -> None:
        try:
            manifest = self._current_manifest()
        except Exception as error:
            self._show_error(error)
            return
        path = filedialog.asksaveasfilename(
            title="Зберегти пакет",
            defaultextension=".json",
            filetypes=[("JSON пакет", "*.json")],
            initialfile=f"batch-{manifest.batch_id}.json",
        )
        if not path:
            return
        try:
            save_manifest(manifest, Path(path))
        except Exception as error:
            self._show_error(error)
            return
        self._log(f"Пакет збережено: {path}")

    def _start_login(self) -> None:
        if not self._save_settings(quiet=True):
            return
        try:
            profile_id = self.profile_var.get()
            config = self.settings.load(profile_id).config
            api_hash = self.settings.require_api_hash(profile_id)
            factory, password_error = telethon_components()
            service = TelegramAuthService(
                factory,
                password_required_error=password_error,
            )
        except Exception as error:
            self._show_error(error)
            return
        self._run_async(
            service.request_code(
                config,
                api_hash,
                profile_id=profile_id,
            ),
            lambda phone_hash: self._ask_login_code(
                service, config, api_hash, phone_hash
            ),
            "Надсилання коду Telegram…",
        )

    def _ask_login_code(
        self,
        service: TelegramAuthService,
        config: Any,
        api_hash: str,
        phone_hash: str,
    ) -> None:
        code = simpledialog.askstring(
            APP_TITLE,
            "Введіть код, який надіслав Telegram:",
            parent=self.root,
        )
        if not code:
            self._set_status("Вхід скасовано")
            return
        self._run_async(
            service.verify_code(
                config,
                api_hash,
                code=code,
                phone_code_hash=phone_hash,
                profile_id=self.profile_var.get(),
            ),
            lambda result: self._finish_login(service, config, api_hash, result),
            "Перевірка коду…",
        )

    def _finish_login(
        self,
        service: TelegramAuthService,
        config: Any,
        api_hash: str,
        result: LoginResult,
    ) -> None:
        if result is LoginResult.AUTHORIZED:
            self._login_success()
            return
        password = simpledialog.askstring(
            APP_TITLE,
            "Введіть пароль двофакторної автентифікації:",
            show="•",
            parent=self.root,
        )
        if not password:
            self._set_status("Вхід скасовано")
            return
        self._run_async(
            service.verify_password(
                config,
                api_hash,
                password=password,
                profile_id=self.profile_var.get(),
            ),
            lambda _result: self._login_success(),
            "Перевірка 2FA…",
        )

    def _login_success(self) -> None:
        self._log("Telegram-авторизація успішна.")
        messagebox.showinfo(APP_TITLE, "Вхід у Telegram виконано.")

    def _runtime(self) -> tuple[UploadManifest, Any, str, TelegramDesktopService]:
        manifest = self._current_manifest()
        config = self.settings.load(manifest.profile_id).config
        api_hash = self.settings.require_api_hash(manifest.profile_id)
        factory, _ = telethon_components()
        service = TelegramDesktopService(factory, self.database_path)
        return manifest, config, api_hash, service

    def _send_package(self) -> None:
        if self.workflow.state is not WorkflowState.BATCH_READY:
            self._show_error(
                WorkflowTransitionError(
                    "Надсилання заборонено: спочатку пройдіть preflight, "
                    "matching і розв’яжіть усі питання."
                )
            )
            return
        try:
            manifest, config, api_hash, service = self._runtime()
            date_from, date_to = self._selected_period()
            calendar_id = self._selected_google_calendar_id()
        except Exception as error:
            self._show_error(error)
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Надіслати {len(manifest.lessons)} урок(и) у {manifest.target_peer}?",
        ):
            return
        try:
            self.workflow.start_revalidation()
        except WorkflowTransitionError as error:
            self._show_error(error)
            return
        self._apply_workflow_state()

        def progress(current: int, total: int) -> None:
            percent = int(current * 100 / total) if total else 0
            self.root.after(0, lambda: self.progress_var.set(percent))

        def status(text: str) -> None:
            self.root.after(0, partial(self._log, text))

        async def calendar_revalidator(
            snapshots: Mapping[str, CalendarEventSnapshot],
        ) -> None:
            if self.google_service is None:
                raise BatchRevalidationRequired({
                    event_id: {
                        "connection": (
                            "Google Calendar connected",
                            "Google Calendar disconnected",
                        )
                    }
                    for event_id in snapshots
                })
            raw_events = await asyncio.to_thread(
                self.google_service.list_events,
                calendar_id=calendar_id,
                date_from=date_from,
                date_to=date_to,
                timezone_name=config.google_timezone,
            )
            current = {
                raw_event.id: parse_calendar_event(
                    raw_event,
                    timezone_name=config.google_timezone,
                )
                for raw_event in raw_events
            }
            expected = {
                event.event_id: build_calendar_snapshot(event)
                for event in self.parsed_calendar_events
            }
            expected.update(snapshots)
            added_events = {
                event_id: {
                    "event": (
                        None,
                        event.original_summary,
                    )
                }
                for event_id, event in current.items()
                if event_id not in expected
            }
            if added_events:
                raise BatchRevalidationRequired(added_events)
            validate_calendar_snapshots(expected, current)
            for event in current.values():
                self.calendar_report_repository.save_calendar_event_report(
                    event
                )
            validate_zoom_sources(self.zoom_match_results)
            for result in self.zoom_match_results:
                if (
                    result.status is ZoomMatchStatus.MANUALLY_CONFIRMED
                    and result.event is not None
                    and self.calendar_report_repository
                    .get_valid_zoom_decision(
                        result.segment,
                        result.event,
                    )
                    is None
                ):
                    raise RuntimeError(
                        "Ручне рішення для відео більше неактуальне: "
                        f"{result.segment.path}"
                    )
            backup = self.calendar_report_repository.create_backup(
                self.database_path.parent / "backups"
            )
            self.root.after(
                0,
                partial(self._log, f"SQLite backup створено: {backup}"),
            )
            self.workflow.finish_revalidation(success=True)

        self._run_async(
            service.send_manifest(
                manifest,
                config,
                api_hash,
                target_peer=manifest.target_peer,
                status_callback=status,
                progress_callback=progress,
                calendar_revalidator=calendar_revalidator,
            ),
            self._send_success,
            "Надсилання уроків…",
        )

    def _send_success(self, results: tuple[Lesson, ...]) -> None:
        if self.workflow.state is WorkflowState.SENDING:
            self.workflow.finish_sending(success=True)
        self._apply_workflow_state()
        count = sum(len(result.telegram_message_ids) for result in results)
        self.progress_var.set(100)
        self._log(f"Готово. Підтверджено {count} Telegram message IDs.")
        messagebox.showinfo(
            APP_TITLE,
            f"Успішно надіслано {len(results)} урок(и), "
            f"{count} Telegram-повідомлень.",
        )

    def _reconcile_package(self) -> None:
        try:
            manifest, config, api_hash, service = self._runtime()
        except Exception as error:
            self._show_error(error)
            return

        def status(text: str) -> None:
            self.root.after(0, partial(self._log, text))

        self._run_async(
            service.reconcile_manifest(
                manifest,
                config,
                api_hash,
                target_peer=manifest.target_peer,
                status_callback=status,
            ),
            self._reconcile_success,
            "Перевірка повідомлень Telegram…",
        )

    def _reconcile_success(self, results: tuple[Lesson, ...]) -> None:
        sent = sum(result.status is SendStatus.SENT for result in results)
        uncertain = len(results) - sent
        messagebox.showinfo(
            APP_TITLE,
            f"Підтверджено SENT: {sent}\nПотребують ручної перевірки: {uncertain}",
        )

    def _run_async(
        self,
        coroutine: Coroutine[Any, Any, Any],
        on_success: Callable[[Any], object],
        busy_text: str,
    ) -> None:
        if self.busy:
            coroutine.close()
            self._log("Зачекайте: попередня операція ще виконується.")
            return
        self._set_busy(True, busy_text)

        def worker() -> None:
            try:
                result = asyncio.run(coroutine)
            except Exception as error:
                self.root.after(
                    0,
                    partial(self._background_error, error),
                )
            else:
                self.root.after(
                    0,
                    partial(self._background_success, on_success, result),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _background_success(
        self,
        callback: Callable[[Any], object],
        result: Any,
    ) -> None:
        self._set_busy(False, "Готово")
        callback(result)

    def _background_error(self, error: Exception) -> None:
        if self.workflow.state is WorkflowState.PREFLIGHT_RUNNING:
            self.workflow.finish_preflight(has_blocking_problems=True)
        elif self.workflow.state is WorkflowState.MATCHING_RUNNING:
            self.workflow.finish_matching(has_unresolved_problems=True)
        elif self.workflow.state is WorkflowState.REVALIDATION_RUNNING:
            self.workflow.finish_revalidation(success=False)
        elif self.workflow.state is WorkflowState.SENDING:
            self.workflow.finish_sending(success=False)
        self._set_busy(False, "Помилка")
        self._show_error(error)

    def _set_busy(self, busy: bool, text: str) -> None:
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for button in self.action_buttons:
            button.configure(state=state)
        self._set_status(text)
        if not busy:
            self.progress_var.set(0)
        self._apply_workflow_state()

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _show_error(self, error: Exception) -> None:
        secret = self.api_hash_var.get()
        write_debug_exception(error, secrets=(secret,))
        message = str(error).replace(secret, "[REDACTED]") if secret else str(error)
        self._log(f"ПОМИЛКА: {message}")
        messagebox.showerror(APP_TITLE, message)

    def _log(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{datetime.now():%H:%M:%S}] {text}\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)


def main() -> None:
    root = tk.Tk()
    DesktopApplication(root)
    root.mainloop()


if __name__ == "__main__":
    main()
