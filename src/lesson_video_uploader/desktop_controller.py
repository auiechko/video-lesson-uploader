from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .config import AppConfig, load_config, save_config
from .credentials import CredentialStore, resolve_api_hash
from .manifest import UploadManifest
from .models import Lesson, LessonDetails, LessonSendMode
from .planning import build_caption
from .telegram_runtime import telegram_session_path

if TYPE_CHECKING:
    from .calendar_rules import CalendarEventSnapshot


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
    student_age: int | None = None
    calendar_status: str = "NORMAL"
    is_no_recording: bool = False
    is_transferred: bool = False
    calendar_snapshot: CalendarEventSnapshot | None = None


class DesktopSettingsController:
    def __init__(
        self,
        config_path: Path,
        credential_store: CredentialStore,
    ) -> None:
        self.config_path = config_path
        self.credential_store = credential_store

    def load(self, profile_id: str = "main") -> LoadedDesktopSettings:
        config = (
            load_config(self.config_path)
            if self.config_path.is_file()
            else load_config()
        )
        config = replace(
            config,
            session=str(telegram_session_path(profile_id)),
        )
        profile_getter = getattr(
            self.credential_store,
            "get_telegram_api_hash",
            None,
        )
        secret = (
            profile_getter(profile_id)
            if callable(profile_getter)
            else self.credential_store.get_secret()
        )
        return LoadedDesktopSettings(
            config=config,
            api_hash_saved=bool(secret),
        )

    def save(
        self,
        *,
        api_id_text: str,
        api_hash: str,
        phone: str,
        session: str,
        album_batch: str | None = None,
        profile_id: str = "main",
    ) -> LoadedDesktopSettings:
        try:
            api_id = int(api_id_text.strip())
        except ValueError as error:
            raise ValueError("Telegram API ID має бути числом") from error
        if api_id <= 0:
            raise ValueError("Telegram API ID має бути додатним числом")
        if not phone.strip():
            raise ValueError("Укажіть номер телефону Telegram")
        current = self.load(profile_id).config
        config = AppConfig(
            api_id=api_id,
            session=str(telegram_session_path(profile_id)),
            phone=phone.strip(),
            album_batch=album_batch or current.album_batch,
            google_client_secrets=current.google_client_secrets,
            google_calendar_id=current.google_calendar_id,
            google_timezone=current.google_timezone,
            calendar_conflict_tolerance_minutes=(
                current.calendar_conflict_tolerance_minutes
            ),
            zoom_recordings_dir=current.zoom_recordings_dir,
            automatic_time_tolerance_minutes=(
                current.automatic_time_tolerance_minutes
            ),
            manual_time_search_window_minutes=(
                current.manual_time_search_window_minutes
            ),
            next_lesson_overlap_tolerance_minutes=(
                current.next_lesson_overlap_tolerance_minutes
            ),
            minimum_video_size_mb=current.minimum_video_size_mb,
            video_stability_check_seconds=(
                current.video_stability_check_seconds
            ),
        )
        save_config(self.config_path, config)
        if api_hash.strip():
            profile_setter = getattr(
                self.credential_store,
                "set_telegram_api_hash",
                None,
            )
            if callable(profile_setter):
                profile_setter(profile_id, api_hash)
            else:
                self.credential_store.set_secret(api_hash.strip())
        profile_getter = getattr(
            self.credential_store,
            "get_telegram_api_hash",
            None,
        )
        secret = (
            profile_getter(profile_id)
            if callable(profile_getter)
            else self.credential_store.get_secret()
        )
        return LoadedDesktopSettings(
            config=config,
            api_hash_saved=bool(secret),
        )

    def require_api_hash(self, profile_id: str = "main") -> str:
        return resolve_api_hash(profile_id, self.credential_store)

    def save_google_calendar(
        self,
        *,
        client_secrets: str,
        calendar_id: str,
        timezone_name: str,
        zoom_recordings_dir: str | None = None,
        automatic_time_tolerance_minutes: int | None = None,
        manual_time_search_window_minutes: int | None = None,
        next_lesson_overlap_tolerance_minutes: int | None = None,
        minimum_video_size_mb: float | None = None,
        video_stability_check_seconds: float | None = None,
        profile_id: str = "main",
    ) -> LoadedDesktopSettings:
        if not client_secrets.strip():
            raise ValueError("Виберіть Google OAuth credentials.json")
        if not calendar_id.strip():
            raise ValueError("Укажіть Google Calendar ID")
        if not timezone_name.strip():
            raise ValueError("Укажіть часовий пояс календаря")
        current = self.load(profile_id)
        zoom_directory = (
            current.config.zoom_recordings_dir
            if zoom_recordings_dir is None
            else zoom_recordings_dir.strip()
        )
        if not zoom_directory:
            raise ValueError("Виберіть папку локальних записів Zoom")
        zoom_tolerances = {
            "automatic_time_tolerance_minutes": (
                current.config.automatic_time_tolerance_minutes
                if automatic_time_tolerance_minutes is None
                else automatic_time_tolerance_minutes
            ),
            "manual_time_search_window_minutes": (
                current.config.manual_time_search_window_minutes
                if manual_time_search_window_minutes is None
                else manual_time_search_window_minutes
            ),
            "next_lesson_overlap_tolerance_minutes": (
                current.config.next_lesson_overlap_tolerance_minutes
                if next_lesson_overlap_tolerance_minutes is None
                else next_lesson_overlap_tolerance_minutes
            ),
        }
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in zoom_tolerances.values()
        ):
            raise ValueError(
                "Допуски часу Zoom мають бути невід’ємними цілими числами"
            )
        media_settings = {
            "minimum_video_size_mb": (
                current.config.minimum_video_size_mb
                if minimum_video_size_mb is None
                else minimum_video_size_mb
            ),
            "video_stability_check_seconds": (
                current.config.video_stability_check_seconds
                if video_stability_check_seconds is None
                else video_stability_check_seconds
            ),
        }
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
            for value in media_settings.values()
        ):
            raise ValueError(
                "Розмір MP4 і час стабільності мають бути невід’ємними числами"
            )
        config = replace(
            current.config,
            google_client_secrets=client_secrets.strip(),
            google_calendar_id=calendar_id.strip(),
            google_timezone=timezone_name.strip(),
            zoom_recordings_dir=zoom_directory,
            automatic_time_tolerance_minutes=int(
                zoom_tolerances["automatic_time_tolerance_minutes"]
            ),
            manual_time_search_window_minutes=int(
                zoom_tolerances["manual_time_search_window_minutes"]
            ),
            next_lesson_overlap_tolerance_minutes=int(
                zoom_tolerances[
                    "next_lesson_overlap_tolerance_minutes"
                ]
            ),
            minimum_video_size_mb=float(
                media_settings["minimum_video_size_mb"]
            ),
            video_stability_check_seconds=float(
                media_settings["video_stability_check_seconds"]
            ),
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
    caption = build_caption(
        date=event_start.date(),
        student_id=form.student_id,
        student_name=form.student_name,
        lesson_label=form.lesson_label,
        duration_hours=form.duration_hours,
        is_trial=form.is_trial,
    )
    if form.is_no_recording:
        caption += " (без запису)"
    return Lesson(
        profile_id=profile_id.strip(),
        batch_id=batch_id.strip(),
        calendar_event_id=form.calendar_event_id.strip(),
        event_start=event_start,
        caption=caption,
        ordered_video_paths=form.video_paths,
        send_mode=(
            LessonSendMode.TEXT_ONLY
            if form.is_no_recording
            else LessonSendMode.MEDIA
        ),
        calendar_snapshot=form.calendar_snapshot,
        details=LessonDetails(
            student_id=form.student_id.strip(),
            student_name=form.student_name.strip(),
            lesson_label=form.lesson_label.strip(),
            duration_hours=form.duration_hours,
            is_trial=form.is_trial,
            student_age=form.student_age,
            calendar_status=form.calendar_status,
            is_no_recording=form.is_no_recording,
            is_transferred=form.is_transferred,
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
        event_start=(
            lesson.event_start.isoformat(timespec="minutes")
            if lesson.calendar_snapshot is not None
            else lesson.event_start.strftime("%Y-%m-%d %H:%M")
        ),
        student_id=details.student_id if details else "",
        student_name=details.student_name if details else "",
        lesson_label=details.lesson_label if details else "",
        duration_hours=details.duration_hours if details else 1,
        is_trial=details.is_trial if details else False,
        video_paths=lesson.ordered_video_paths,
        student_age=details.student_age if details else None,
        calendar_status=details.calendar_status if details else "NORMAL",
        is_no_recording=details.is_no_recording if details else False,
        is_transferred=details.is_transferred if details else False,
        calendar_snapshot=lesson.calendar_snapshot,
    )


def save_lesson_to_batch(
    *,
    lessons: tuple[Lesson, ...],
    lesson: Lesson,
    editing_calendar_event_id: str | None = None,
) -> tuple[Lesson, ...]:
    """Add a lesson or replace the lesson currently open in the editor."""
    editing_index: int | None = None
    if editing_calendar_event_id is not None:
        editing_index = next(
            (
                index
                for index, item in enumerate(lessons)
                if item.calendar_event_id == editing_calendar_event_id
            ),
            None,
        )
        if editing_index is None:
            raise ValueError(
                "Урок, який редагується, більше не існує в пакеті"
            )

    if any(
        item.calendar_event_id == lesson.calendar_event_id
        and index != editing_index
        for index, item in enumerate(lessons)
    ):
        raise ValueError("Урок із цим Calendar event ID уже доданий")

    updated = list(lessons)
    if editing_index is None:
        updated.append(lesson)
    else:
        updated[editing_index] = lesson
    updated.sort(key=lambda item: (item.event_start, item.calendar_event_id))
    return tuple(updated)


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
