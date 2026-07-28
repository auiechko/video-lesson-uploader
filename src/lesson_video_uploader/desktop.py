from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Coroutine

from .credentials import WindowsCredentialStore
from .desktop_controller import (
    DesktopSettingsController,
    LessonForm,
    build_gui_manifest,
    create_lesson_from_form,
)
from .manifest import UploadManifest, load_manifest, save_manifest
from .models import Lesson, SendStatus
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
            WindowsCredentialStore(),
        )
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
        self.api_id_var = tk.StringVar()
        self.api_hash_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        self.session_var = tk.StringVar(value=".lesson-video-uploader/telegram")
        self.secret_status_var = tk.StringVar(value="API hash ще не збережений")
        self.status_var = tk.StringVar(value="Готово")
        self.progress_var = tk.DoubleVar(value=0)

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

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)
        send_tab = ttk.Frame(notebook, padding=14)
        settings_tab = ttk.Frame(notebook, padding=18)
        notebook.add(send_tab, text="  Уроки та надсилання  ")
        notebook.add(settings_tab, text="  Telegram і безпека  ")
        self._build_send_tab(send_tab)
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
        ttk.Button(
            parent,
            text="Видалити вибраний урок",
            command=self._remove_lesson,
        ).pack(anchor=tk.E, pady=(8, 0))

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        intro = ttk.Label(
            parent,
            text=(
                "API ID зберігається у config.toml. API hash зберігається "
                "окремо у Windows Credential Manager і не потрапляє в Git."
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
            "Telethon session",
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
            loaded = self.settings.load()
        except Exception as error:
            self._show_error(error)
            return
        config = loaded.config
        self.api_id_var.set("" if config.api_id is None else str(config.api_id))
        self.phone_var.set(config.phone)
        self.session_var.set(config.session)
        self.secret_status_var.set(
            "API hash збережений у Credential Manager"
            if loaded.api_hash_saved
            else "API hash ще не збережений"
        )

    def _save_settings(self, *, quiet: bool = False) -> bool:
        try:
            loaded = self.settings.save(
                api_id_text=self.api_id_var.get(),
                api_hash=self.api_hash_var.get(),
                phone=self.phone_var.get(),
                session=self.session_var.get(),
            )
        except Exception as error:
            self._show_error(error)
            return False
        self.api_hash_var.set("")
        self.secret_status_var.set(
            "API hash збережений у Credential Manager"
            if loaded.api_hash_saved
            else "API hash ще не збережений"
        )
        if not quiet:
            self._log("Налаштування збережено.")
            messagebox.showinfo(APP_TITLE, "Налаштування збережено безпечно.")
        return True

    def _pick_videos(self) -> None:
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
            lesson = create_lesson_from_form(
                profile_id=self.profile_var.get(),
                batch_id=self.batch_var.get(),
                form=LessonForm(
                    calendar_event_id=self.event_id_var.get(),
                    event_start=self.start_var.get(),
                    student_id=self.student_id_var.get(),
                    student_name=self.student_name_var.get(),
                    lesson_label=self.lesson_label_var.get(),
                    duration_hours=int(self.duration_var.get()),
                    is_trial=self.trial_var.get(),
                    video_paths=tuple(self.pending_video_paths),
                ),
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
        self._refresh_pending_files()
        self.event_id_var.set(f"event-{datetime.now():%Y%m%d-%H%M%S}")
        self._log(f"Додано урок: {lesson.caption}")

    def _refresh_lessons(self) -> None:
        self.lesson_tree.delete(*self.lesson_tree.get_children())
        for index, lesson in enumerate(self.lessons):
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
            config = self.settings.load().config
            api_hash = self.settings.require_api_hash()
            factory, password_error = telethon_components()
            service = TelegramAuthService(
                factory,
                password_required_error=password_error,
            )
        except Exception as error:
            self._show_error(error)
            return
        self._run_async(
            service.request_code(config, api_hash),
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
            ),
            lambda _result: self._login_success(),
            "Перевірка 2FA…",
        )

    def _login_success(self) -> None:
        self._log("Telegram-авторизація успішна.")
        messagebox.showinfo(APP_TITLE, "Вхід у Telegram виконано.")

    def _runtime(self) -> tuple[UploadManifest, Any, str, TelegramDesktopService]:
        manifest = self._current_manifest()
        config = self.settings.load().config
        api_hash = self.settings.require_api_hash()
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

        self._run_async(
            service.send_manifest(
                manifest,
                config,
                api_hash,
                target_peer=manifest.target_peer,
                status_callback=status,
                progress_callback=progress,
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
            f"Успішно надіслано {len(results)} урок(и), {count} відео.",
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
        self._log(f"ПОМИЛКА: {error}")
        messagebox.showerror(APP_TITLE, str(error))

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
