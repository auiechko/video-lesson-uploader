from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .credentials import CredentialStore


CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_SCOPES = (CALENDAR_READONLY_SCOPE,)


class GoogleCredentials(Protocol):
    valid: bool
    expired: bool
    refresh_token: str | None

    def refresh(self, request: object) -> None: ...
    def to_json(self) -> str: ...


CredentialsLoader = Callable[[dict[str, Any], Sequence[str]], GoogleCredentials]
FlowFactory = Callable[[Path, Sequence[str]], Any]
RequestFactory = Callable[[], object]


class GoogleOAuthManager:
    """Authorize a desktop app while keeping OAuth tokens out of the filesystem."""

    def __init__(
        self,
        token_store: CredentialStore,
        *,
        credentials_loader: CredentialsLoader | None = None,
        request_factory: RequestFactory | None = None,
        flow_factory: FlowFactory | None = None,
    ) -> None:
        self.token_store = token_store
        self.credentials_loader = credentials_loader or self._credentials_from_info
        self.request_factory = request_factory or self._request
        self.flow_factory = flow_factory or self._flow_from_file

    def authorize(
        self,
        client_secrets_path: Path,
        *,
        force: bool = False,
    ) -> GoogleCredentials:
        credentials: GoogleCredentials | None = None
        raw_token = None if force else self.token_store.get_secret()
        if raw_token:
            try:
                info = json.loads(raw_token)
                if not isinstance(info, dict):
                    raise ValueError("OAuth token must be a JSON object")
                credentials = self.credentials_loader(info, CALENDAR_SCOPES)
            except (ValueError, TypeError, json.JSONDecodeError):
                self.token_store.delete_secret()
                credentials = None

        if credentials and credentials.valid:
            return credentials
        if (
            credentials
            and credentials.expired
            and credentials.refresh_token
        ):
            credentials.refresh(self.request_factory())
            self.token_store.set_secret(credentials.to_json())
            return credentials

        if not client_secrets_path.is_file():
            raise ValueError(
                "Не знайдено Google credentials.json. "
                "Виберіть OAuth Client файл типу Desktop app."
            )
        flow = self.flow_factory(client_secrets_path, CALENDAR_SCOPES)
        credentials = flow.run_local_server(port=0)
        self.token_store.set_secret(credentials.to_json())
        return credentials

    def disconnect(self) -> None:
        self.token_store.delete_secret()

    @staticmethod
    def _credentials_from_info(
        info: dict[str, Any],
        scopes: Sequence[str],
    ) -> GoogleCredentials:
        try:
            from google.oauth2.credentials import Credentials
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Google Calendar libraries are not installed. "
                "Run .venv\\Scripts\\python.exe -m pip install -e ."
            ) from error
        return Credentials.from_authorized_user_info(info, scopes)

    @staticmethod
    def _request() -> object:
        from google.auth.transport.requests import Request

        return Request()

    @staticmethod
    def _flow_from_file(path: Path, scopes: Sequence[str]):
        from google_auth_oauthlib.flow import InstalledAppFlow

        return InstalledAppFlow.from_client_secrets_file(str(path), scopes)


@dataclass(frozen=True, slots=True)
class GoogleCalendarInfo:
    id: str
    summary: str
    primary: bool = False


@dataclass(frozen=True, slots=True)
class GoogleCalendarEvent:
    id: str
    calendar_id: str
    summary: str
    description: str
    start: datetime
    end: datetime
    html_link: str = ""

    @property
    def duration_hours(self) -> int:
        seconds = max(0, int((self.end - self.start).total_seconds()))
        rounded = int((seconds + 1800) // 3600)
        return min(3, max(1, rounded))


class GoogleCalendarService:
    def __init__(self, service: Any) -> None:
        self.service = service

    @classmethod
    def from_credentials(cls, credentials: GoogleCredentials):
        try:
            from googleapiclient.discovery import build
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Google Calendar libraries are not installed. "
                "Run .venv\\Scripts\\python.exe -m pip install -e ."
            ) from error
        return cls(
            build(
                "calendar",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )
        )

    def list_calendars(self) -> tuple[GoogleCalendarInfo, ...]:
        calendars: list[GoogleCalendarInfo] = []
        page_token: str | None = None
        while True:
            kwargs = {"pageToken": page_token} if page_token else {}
            response = self.service.calendarList().list(**kwargs).execute()
            for item in response.get("items", []):
                calendar_id = item.get("id")
                summary = item.get("summary")
                if isinstance(calendar_id, str) and isinstance(summary, str):
                    calendars.append(GoogleCalendarInfo(
                        id=calendar_id,
                        summary=summary,
                        primary=bool(item.get("primary", False)),
                    ))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return tuple(calendars)

    def list_events(
        self,
        *,
        calendar_id: str,
        date_from: date,
        date_to: date,
        timezone_name: str,
    ) -> tuple[GoogleCalendarEvent, ...]:
        if date_from > date_to:
            raise ValueError("Некоректний діапазон дат календаря")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Невідомий часовий пояс: {timezone_name}") from error
        time_min = datetime.combine(date_from, time.min, timezone)
        time_max = datetime.combine(
            date_to + timedelta(days=1),
            time.min,
            timezone,
        )
        events: list[GoogleCalendarEvent] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "calendarId": calendar_id,
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
                "showDeleted": False,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = self.service.events().list(**kwargs).execute()
            for item in response.get("items", []):
                event = self._event_from_api(item, calendar_id, timezone)
                if event is not None:
                    events.append(event)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return tuple(sorted(events, key=lambda event: (event.start, event.id)))

    @staticmethod
    def _event_from_api(
        item: dict[str, Any],
        calendar_id: str,
        timezone: ZoneInfo,
    ) -> GoogleCalendarEvent | None:
        if item.get("status") == "cancelled":
            return None
        start_data = item.get("start", {})
        end_data = item.get("end", {})
        start_raw = start_data.get("dateTime")
        end_raw = end_data.get("dateTime")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            return None
        event_id = item.get("id")
        if not isinstance(event_id, str) or not event_id:
            return None
        start = _parse_rfc3339(start_raw, timezone)
        end = _parse_rfc3339(end_raw, timezone)
        if end <= start:
            return None
        return GoogleCalendarEvent(
            id=event_id,
            calendar_id=calendar_id,
            summary=str(item.get("summary", "")).strip(),
            description=str(item.get("description", "")).strip(),
            start=start,
            end=end,
            html_link=str(item.get("htmlLink", "")).strip(),
        )


def _parse_rfc3339(value: str, fallback_timezone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=fallback_timezone)
        if parsed.tzinfo is None
        else parsed
    )


_AGE_PATTERN = re.compile(r"^(?P<age>\d{1,2})\s*р?$", re.IGNORECASE)
_DURATION_PATTERN = re.compile(
    r"\b(?:ДВІ|ТРИ)\s+ГОДИНИ\b",
    re.IGNORECASE,
)
_TRIAL_PATTERN = re.compile(r"\(?\bпробн\w*\b\)?", re.IGNORECASE)
_SERVICE_MARKERS = {"тг", "учечко"}


def calendar_event_to_lesson_form(event: GoogleCalendarEvent):
    from .desktop_controller import LessonForm

    summary = " ".join(event.summary.split())
    student_id_match = re.match(r"^(?P<id>\d+)\b", summary)
    student_id = student_id_match.group("id") if student_id_match else ""
    student_name = ""
    lesson_label = ""

    student_group: re.Match[str] | None = None
    age = ""
    for match in re.finditer(r"\(([^()]*)\)", summary):
        inner_tokens = match.group(1).split()
        if inner_tokens and (age_match := _AGE_PATTERN.match(inner_tokens[-1])):
            student_group = match
            age = age_match.group("age")
            student_name = " ".join(inner_tokens[:-1]).strip()
            break

    if student_group is not None:
        subject = _clean_lesson_text(summary[student_group.end():])
        lesson_label = f"{age}р {subject or 'індив'}".strip()
    else:
        remainder = summary[student_id_match.end():].strip() if student_id_match else summary
        tokens = remainder.split()
        if tokens:
            student_name = tokens[0]
            lesson_label = _clean_lesson_text(" ".join(tokens[1:]))

    is_trial = bool(
        _TRIAL_PATTERN.search(f"{event.summary} {event.description}")
    )
    return LessonForm(
        calendar_event_id=event.id,
        event_start=event.start.strftime("%Y-%m-%d %H:%M"),
        student_id=student_id,
        student_name=student_name,
        lesson_label=lesson_label,
        duration_hours=event.duration_hours,
        is_trial=is_trial,
        video_paths=(),
    )


def _clean_lesson_text(value: str) -> str:
    cleaned = _DURATION_PATTERN.sub(" ", value)
    cleaned = _TRIAL_PATTERN.sub(" ", cleaned)
    cleaned = cleaned.replace("|", " ")
    tokens = [
        token
        for token in cleaned.split()
        if token.casefold().strip("(),") not in _SERVICE_MARKERS
    ]
    return " ".join(tokens).strip()
