from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Coroutine, Mapping

from .calendar_rules import (
    BatchRevalidationRequired,
    CalendarEventSnapshot,
    parse_calendar_event,
    resolve_calendar_slots,
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
from .models import Lesson, LessonSendMode, SendStatus
from .persistence import SQLiteSendItemRepository
from .planning import plan_albums
from .telegram_desktop import (
    LoginResult,
    TelegramAuthService,
    TelegramDesktopService,
    telethon_components,
)


APP_TITLE = "Lesson Video Uploader"


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
        self.notebook.add(google_tab, text="  Google Calendar  ")
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
        send_button = ttk.Button(
            actions,
            text="Надіслати пакет",
            style="Primary.TButton",
            command=self._send_package,
        )
        send_button.pack(side=tk.LEFT)
        reconcile_button = ttk.Button(
            actions,
            text="Перевірити невідому доставку",
            command=self._reconcile_package,
        )
        reconcile_button.pack(side=tk.LEFT, padx=8)
        self.action_buttons.extend((send_button, reconcile_button))

        self.log = ScrolledText(
            parent,
            height=6,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        self.log.pack(fill=tk.X, pady=(10, 0))

    def _build_lesson_editor(self, parent: ttk.Frame) -> None:
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

    def _build_preview(self, parent: ttk.Frame) -> None:
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
        load_button = ttk.Button(
            filters,
            text="Завантажити події",
            style="Primary.TButton",
            command=self._load_google_events,
        )
        load_button.grid(row=2, column=3, sticky=tk.E, pady=5)
        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)

        columns = ("start", "duration", "status", "summary", "event_id")
        self.google_event_tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=11,
        )
        self.google_event_tree.heading("start", text="Початок")
        self.google_event_tree.heading("duration", text="Год.")
        self.google_event_tree.heading("status", text="Статус")
        self.google_event_tree.heading("summary", text="Назва події")
        self.google_event_tree.heading("event_id", text="Google event ID")
        self.google_event_tree.column("start", width=135, stretch=False)
        self.google_event_tree.column(
            "duration",
            width=55,
            anchor=tk.CENTER,
            stretch=False,
        )
        self.google_event_tree.column("status", width=190, stretch=False)
        self.google_event_tree.column("summary", width=360)
        self.google_event_tree.column("event_id", width=180)
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
                load_button,
                import_button,
            )
        )

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
        self._labeled_entry(
            parent,
            "Telegram API hash",
            self.api_hash_var,
            2,
            0,
            show="•",
        )
        ttk.Label(parent, textvariable=self.secret_status_var).grid(
            row=2, column=2, sticky=tk.W, padx=(12, 0)
        )
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

    @staticmethod
    def _labeled_entry(
        parent: ttk.Frame,
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
        entry = ttk.Entry(parent, textvariable=variable, width=width, show=show)
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

    def _save_google_settings(self) -> bool:
        try:
            self.settings.save_google_calendar(
                client_secrets=self.google_credentials_var.get(),
                calendar_id=self._selected_google_calendar_id(),
                timezone_name=self.google_timezone_var.get(),
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
        if self.google_service is None:
            self._show_error(
                ValueError("Спочатку натисніть «Підключити Google».")
            )
            return
        try:
            date_from = date.fromisoformat(self.google_from_var.get().strip())
            date_to = date.fromisoformat(self.google_to_var.get().strip())
        except ValueError as error:
            self._show_error(
                ValueError("Дати мають формат РРРР-ММ-ДД")
            )
            return
        if not self._save_google_settings():
            return
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
        timezone_name = self.google_timezone_var.get().strip()
        parsed_events = [
            parse_calendar_event(event, timezone_name=timezone_name)
            for event in events
        ]
        for parsed in parsed_events:
            self.calendar_report_repository.save_calendar_event_report(parsed)
        manual_event_ids = {
            candidate.event_id
            for slot in resolve_calendar_slots(
                parsed_events,
                tolerance_minutes=(
                    self.settings.load().config
                    .calendar_conflict_tolerance_minutes
                ),
            )
            if slot.status is not None
            and slot.status.value == "MANUAL_SELECTION_REQUIRED"
            for candidate in slot.candidates
        }
        for index, (event, parsed) in enumerate(
            zip(events, parsed_events, strict=True)
        ):
            display_status = (
                "MANUAL_SELECTION_REQUIRED"
                if event.id in manual_event_ids
                else (
                    f"{parsed.status.value} / "
                    f"{parsed.cancellation_source.value}"
                    if parsed.cancellation_source is not None
                    else parsed.status.value
                )
            )
            self.google_event_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    parsed.start.strftime("%d.%m.%Y %H:%M"),
                    parsed.duration_hours,
                    display_status,
                    event.summary,
                    event.id,
                ),
            )
        self.google_status_var.set(f"Завантажено подій: {len(events)}")
        self._log(f"Google Calendar: завантажено {len(events)} подій.")

    def _import_google_event(self) -> None:
        selection = self.google_event_tree.selection()
        if not selection:
            self._show_error(ValueError("Виберіть подію календаря."))
            return
        event = self.google_events[int(selection[0])]
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
        if form.is_no_recording:
            self.pending_video_paths.clear()
            self._refresh_pending_files()
        self.notebook.select(self.send_tab)
        self._log(
            f"Імпортовано {form.calendar_status}. "
            + (
                "Це text-only урок; MP4 не потрібні."
                if form.is_no_recording
                else "Додайте MP4."
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
        try:
            manifest, config, api_hash, service = self._runtime()
        except Exception as error:
            self._show_error(error)
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Надіслати {len(manifest.lessons)} урок(и) у {manifest.target_peer}?",
        ):
            return

        def progress(current: int, total: int) -> None:
            percent = int(current * 100 / total) if total else 0
            self.root.after(0, lambda: self.progress_var.set(percent))

        def status(text: str) -> None:
            self.root.after(0, lambda value=text: self._log(value))

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
            current = {}
            missing_changes = {}
            for event_id, snapshot in snapshots.items():
                try:
                    raw_event = await asyncio.to_thread(
                        self.google_service.get_event,
                        calendar_id=snapshot.calendar_id,
                        event_id=event_id,
                        timezone_name=config.google_timezone,
                    )
                except Exception as error:
                    missing_changes[event_id] = {
                        "event": (snapshot.summary, str(error))
                    }
                    continue
                current[event_id] = parse_calendar_event(
                    raw_event,
                    timezone_name=config.google_timezone,
                )
                self.calendar_report_repository.save_calendar_event_report(
                    current[event_id]
                )
            if missing_changes:
                raise BatchRevalidationRequired(missing_changes)
            validate_calendar_snapshots(snapshots, current)

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
            self.root.after(0, lambda value=text: self._log(value))

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
        on_success,
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
                    lambda captured=error: self._background_error(captured),
                )
            else:
                self.root.after(
                    0,
                    lambda captured=result: self._background_success(
                        on_success, captured
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _background_success(self, callback, result: Any) -> None:
        self._set_busy(False, "Готово")
        callback(result)

    def _background_error(self, error: Exception) -> None:
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
