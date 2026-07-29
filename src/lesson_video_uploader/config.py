from __future__ import annotations

import json
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .planning import DEFAULT_ALBUM_BATCH_TEMPLATE
from .telegram_runtime import telegram_session_path
from .zoom_recordings import default_zoom_recordings_dir


@dataclass(frozen=True, slots=True)
class AppConfig:
    album_batch: str = DEFAULT_ALBUM_BATCH_TEMPLATE
    api_id: int | None = None
    session: str = str(telegram_session_path("main"))
    phone: str = ""
    google_client_secrets: str = ""
    google_calendar_id: str = "primary"
    google_timezone: str = "Europe/Kyiv"
    calendar_conflict_tolerance_minutes: int = 10
    zoom_recordings_dir: str = field(
        default_factory=lambda: str(default_zoom_recordings_dir())
    )
    automatic_time_tolerance_minutes: int = 30
    manual_time_search_window_minutes: int = 180
    next_lesson_overlap_tolerance_minutes: int = 10
    minimum_video_size_mb: float = 5
    video_stability_check_seconds: float = 5


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    with path.open("rb") as file:
        raw = tomllib.load(file)
    caption = raw.get("caption", {})
    if not isinstance(caption, dict):
        raise ValueError("[caption] must be a TOML table")
    template = caption.get("album_batch", DEFAULT_ALBUM_BATCH_TEMPLATE)
    if not isinstance(template, str):
        raise ValueError("caption.album_batch must be a string")
    fields = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }
    required = {"album_number", "album_count"}
    if not required.issubset(fields):
        missing = ", ".join(sorted(required - fields))
        raise ValueError(f"caption.album_batch is missing {missing}")
    telegram = raw.get("telegram", {})
    if not isinstance(telegram, dict):
        raise ValueError("[telegram] must be a TOML table")
    api_id = telegram.get("api_id")
    if api_id is not None and (
        not isinstance(api_id, int) or isinstance(api_id, bool) or api_id <= 0
    ):
        raise ValueError("telegram.api_id must be a positive integer")
    session = telegram.get("session", str(telegram_session_path("main")))
    phone = telegram.get("phone", "")
    google_calendar = raw.get("google_calendar", {})
    zoom = raw.get("zoom", {})
    if not isinstance(session, str) or not session.strip():
        raise ValueError("telegram.session must be a non-empty string")
    if not isinstance(phone, str):
        raise ValueError("telegram.phone must be a string")
    if not isinstance(google_calendar, dict):
        raise ValueError("[google_calendar] must be a TOML table")
    if not isinstance(zoom, dict):
        raise ValueError("[zoom] must be a TOML table")
    google_client_secrets = google_calendar.get("client_secrets", "")
    google_calendar_id = google_calendar.get("calendar_id", "primary")
    google_timezone = google_calendar.get("timezone", "Europe/Kyiv")
    calendar_conflict_tolerance_minutes = google_calendar.get(
        "calendar_conflict_tolerance_minutes",
        10,
    )
    defaults = AppConfig()
    zoom_recordings_dir = zoom.get(
        "recordings_dir",
        defaults.zoom_recordings_dir,
    )
    automatic_time_tolerance_minutes = zoom.get(
        "automatic_time_tolerance_minutes",
        zoom.get(
            "match_tolerance_minutes",
            defaults.automatic_time_tolerance_minutes,
        ),
    )
    manual_time_search_window_minutes = zoom.get(
        "manual_time_search_window_minutes",
        defaults.manual_time_search_window_minutes,
    )
    next_lesson_overlap_tolerance_minutes = zoom.get(
        "next_lesson_overlap_tolerance_minutes",
        defaults.next_lesson_overlap_tolerance_minutes,
    )
    minimum_video_size_mb = zoom.get(
        "minimum_video_size_mb",
        defaults.minimum_video_size_mb,
    )
    video_stability_check_seconds = zoom.get(
        "video_stability_check_seconds",
        defaults.video_stability_check_seconds,
    )
    for name, value in (
        ("google_calendar.client_secrets", google_client_secrets),
        ("google_calendar.calendar_id", google_calendar_id),
        ("google_calendar.timezone", google_timezone),
    ):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    if not google_calendar_id.strip():
        raise ValueError("google_calendar.calendar_id must be non-empty")
    if not google_timezone.strip():
        raise ValueError("google_calendar.timezone must be non-empty")
    if (
        not isinstance(calendar_conflict_tolerance_minutes, int)
        or isinstance(calendar_conflict_tolerance_minutes, bool)
        or calendar_conflict_tolerance_minutes < 0
    ):
        raise ValueError(
            "google_calendar.calendar_conflict_tolerance_minutes "
            "must be a non-negative integer"
        )
    if not isinstance(zoom_recordings_dir, str):
        raise ValueError("zoom.recordings_dir must be a string")
    if not zoom_recordings_dir.strip():
        raise ValueError("zoom.recordings_dir must be non-empty")
    for name, value in (
        (
            "zoom.automatic_time_tolerance_minutes",
            automatic_time_tolerance_minutes,
        ),
        (
            "zoom.manual_time_search_window_minutes",
            manual_time_search_window_minutes,
        ),
        (
            "zoom.next_lesson_overlap_tolerance_minutes",
            next_lesson_overlap_tolerance_minutes,
        ),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative integer")
    assert isinstance(automatic_time_tolerance_minutes, int)
    assert isinstance(manual_time_search_window_minutes, int)
    assert isinstance(next_lesson_overlap_tolerance_minutes, int)
    for name, value in (
        ("zoom.minimum_video_size_mb", minimum_video_size_mb),
        (
            "zoom.video_stability_check_seconds",
            video_stability_check_seconds,
        ),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative number")
    return AppConfig(
        album_batch=template,
        api_id=api_id,
        session=session.strip(),
        phone=phone.strip(),
        google_client_secrets=google_client_secrets.strip(),
        google_calendar_id=google_calendar_id.strip(),
        google_timezone=google_timezone.strip(),
        calendar_conflict_tolerance_minutes=(
            calendar_conflict_tolerance_minutes
        ),
        zoom_recordings_dir=zoom_recordings_dir.strip(),
        automatic_time_tolerance_minutes=int(
            automatic_time_tolerance_minutes
        ),
        manual_time_search_window_minutes=int(
            manual_time_search_window_minutes
        ),
        next_lesson_overlap_tolerance_minutes=(
            int(next_lesson_overlap_tolerance_minutes)
        ),
        minimum_video_size_mb=float(minimum_video_size_mb),
        video_stability_check_seconds=float(
            video_stability_check_seconds
        ),
    )


def save_config(path: Path, config: AppConfig) -> None:
    """Persist non-secret settings as UTF-8 TOML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[telegram]"]
    if config.api_id is not None:
        lines.append(f"api_id = {config.api_id}")
    lines.extend([
        f"session = {json.dumps(config.session, ensure_ascii=False)}",
        f"phone = {json.dumps(config.phone, ensure_ascii=False)}",
        "",
        "[google_calendar]",
        (
            "client_secrets = "
            f"{json.dumps(config.google_client_secrets, ensure_ascii=False)}"
        ),
        f"calendar_id = {json.dumps(config.google_calendar_id, ensure_ascii=False)}",
        f"timezone = {json.dumps(config.google_timezone, ensure_ascii=False)}",
        (
            "calendar_conflict_tolerance_minutes = "
            f"{config.calendar_conflict_tolerance_minutes}"
        ),
        "",
        "[zoom]",
        (
            "recordings_dir = "
            f"{json.dumps(config.zoom_recordings_dir, ensure_ascii=False)}"
        ),
        (
            "automatic_time_tolerance_minutes = "
            f"{config.automatic_time_tolerance_minutes}"
        ),
        (
            "manual_time_search_window_minutes = "
            f"{config.manual_time_search_window_minutes}"
        ),
        (
            "next_lesson_overlap_tolerance_minutes = "
            f"{config.next_lesson_overlap_tolerance_minutes}"
        ),
        f"minimum_video_size_mb = {config.minimum_video_size_mb:g}",
        (
            "video_stability_check_seconds = "
            f"{config.video_stability_check_seconds:g}"
        ),
        "",
        "[caption]",
        f"album_batch = {json.dumps(config.album_batch, ensure_ascii=False)}",
        "",
    ])
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)
