from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import AppConfig, load_config, save_config
from .credentials import CredentialStore, resolve_api_hash
from .manifest import UploadManifest
from .models import Lesson, LessonDetails
from .planning import build_caption


@dataclass(frozen=True, slots=True)
class LoadedDesktopSettings:
    config: AppConfig
    api_hash_saved: bool


@dataclass(frozen=True, slots=True)
class LessonForm:
    calendar_event_id: str
    event_start: str
    student_id: str
    student_name: str
    lesson_label: str
    duration_hours: int
    is_trial: bool
    video_paths: tuple[Path, ...]


class DesktopSettingsController:
    def __init__(
        self,
        config_path: Path,
        credential_store: CredentialStore,
    ) -> None:
        self.config_path = config_path
        self.credential_store = credential_store

    def load(self) -> LoadedDesktopSettings:
        config = (
            load_config(self.config_path)
            if self.config_path.is_file()
            else load_config()
        )
        return LoadedDesktopSettings(
            config=config,
            api_hash_saved=self.credential_store.get_secret() is not None,
        )

    def save(
        self,
        *,
        api_id_text: str,
        api_hash: str,
        phone: str,
        session: str,
        album_batch: str | None = None,
    ) -> LoadedDesktopSettings:
        try:
            api_id = int(api_id_text.strip())
        except ValueError as error:
            raise ValueError("Telegram API ID має бути числом") from error
        if api_id <= 0:
            raise ValueError("Telegram API ID має бути додатним числом")
        if not phone.strip():
            raise ValueError("Укажіть номер телефону Telegram")
        if not session.strip():
            raise ValueError("Укажіть шлях Telethon session")
        current = self.load().config
        config = AppConfig(
            api_id=api_id,
            api_hash_env=current.api_hash_env,
            session=session.strip(),
            phone=phone.strip(),
            album_batch=album_batch or current.album_batch,
            google_client_secrets=current.google_client_secrets,
            google_calendar_id=current.google_calendar_id,
            google_timezone=current.google_timezone,
        )
        save_config(self.config_path, config)
        if api_hash.strip():
            self.credential_store.set_secret(api_hash.strip())
        return LoadedDesktopSettings(
            config=config,
            api_hash_saved=self.credential_store.get_secret() is not None,
        )

    def require_api_hash(self) -> str:
        return resolve_api_hash(
            self.load().config.api_hash_env,
            self.credential_store,
        )

    def save_google_calendar(
        self,
        *,
        client_secrets: str,
        calendar_id: str,
        timezone_name: str,
    ) -> LoadedDesktopSettings:
        if not client_secrets.strip():
            raise ValueError("Виберіть Google OAuth credentials.json")
        if not calendar_id.strip():
            raise ValueError("Укажіть Google Calendar ID")
        if not timezone_name.strip():
            raise ValueError("Укажіть часовий пояс календаря")
        current = self.load()
        config = replace(
            current.config,
            google_client_secrets=client_secrets.strip(),
            google_calendar_id=calendar_id.strip(),
            google_timezone=timezone_name.strip(),
        )
        save_config(self.config_path, config)
        return LoadedDesktopSettings(
            config=config,
            api_hash_saved=current.api_hash_saved,
        )


def parse_target_peer(value: str) -> int | str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Укажіть Telegram-чат")
    try:
        return int(normalized)
    except ValueError:
        return normalized


def create_lesson_from_form(
    *,
    profile_id: str,
    batch_id: str,
    form: LessonForm,
) -> Lesson:
    if not profile_id.strip() or not batch_id.strip():
        raise ValueError("Укажіть profile ID та batch ID")
    if not form.calendar_event_id.strip():
        raise ValueError("Укажіть Calendar event ID")
    if not (
        form.student_id.strip()
        and form.student_name.strip()
        and form.lesson_label.strip()
    ):
        raise ValueError("Укажіть ID учня, ім’я та опис уроку")
    try:
        event_start = datetime.fromisoformat(form.event_start.strip())
    except ValueError as error:
        raise ValueError(
            "Дата уроку має формат РРРР-ММ-ДД ГГ:ХХ"
        ) from error
    for path in form.video_paths:
        if path.suffix.lower() != ".mp4":
            raise ValueError(f"Підтримуються лише MP4: {path.name}")
        if not path.is_file():
            raise ValueError(f"Відео не знайдено: {path}")
    return Lesson(
        profile_id=profile_id.strip(),
        batch_id=batch_id.strip(),
        calendar_event_id=form.calendar_event_id.strip(),
        event_start=event_start,
        caption=build_caption(
            date=event_start.date(),
            student_id=form.student_id,
            student_name=form.student_name,
            lesson_label=form.lesson_label,
            duration_hours=form.duration_hours,
            is_trial=form.is_trial,
        ),
        ordered_video_paths=form.video_paths,
        details=LessonDetails(
            student_id=form.student_id.strip(),
            student_name=form.student_name.strip(),
            lesson_label=form.lesson_label.strip(),
            duration_hours=form.duration_hours,
            is_trial=form.is_trial,
        ),
    )


def form_from_lesson(lesson: Lesson) -> LessonForm:
    """Turn a lesson back into editor fields.

    A batch written by hand may carry only a caption, with nothing to fill the
    student fields from. Those come back empty rather than guessed, so the
    caption is retyped deliberately instead of silently rebuilt from parsed
    fragments.
    """
    details = lesson.details
    return LessonForm(
        calendar_event_id=lesson.calendar_event_id,
        event_start=lesson.event_start.strftime("%Y-%m-%d %H:%M"),
        student_id=details.student_id if details else "",
        student_name=details.student_name if details else "",
        lesson_label=details.lesson_label if details else "",
        duration_hours=details.duration_hours if details else 1,
        is_trial=details.is_trial if details else False,
        video_paths=lesson.ordered_video_paths,
    )


def build_gui_manifest(
    *,
    profile_id: str,
    batch_id: str,
    target_peer: str,
    lessons: tuple[Lesson, ...],
) -> UploadManifest:
    profile = profile_id.strip()
    batch = batch_id.strip()
    if not profile or not batch:
        raise ValueError("Укажіть profile ID та batch ID")
    if not lessons:
        raise ValueError("Додайте хоча б один урок")
    normalized_lessons = tuple(
        replace(lesson, profile_id=profile, batch_id=batch)
        for lesson in lessons
    )
    return UploadManifest(
        profile_id=profile,
        batch_id=batch,
        target_peer=parse_target_peer(target_peer),
        lessons=normalized_lessons,
    )
