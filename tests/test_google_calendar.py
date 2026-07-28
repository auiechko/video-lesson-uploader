from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lesson_video_uploader.google_calendar import (
    CALENDAR_READONLY_SCOPE,
    GoogleCalendarService,
    GoogleOAuthManager,
    calendar_event_to_lesson_form,
)


class FakeCredentialStore:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def get_secret(self) -> str | None:
        return self.value

    def set_secret(self, secret: str) -> None:
        self.value = secret

    def delete_secret(self) -> None:
        self.value = None


class FakeCredentials:
    def __init__(
        self,
        *,
        valid: bool,
        expired: bool = False,
        refresh_token: str | None = None,
        marker: str = "stored",
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.marker = marker
        self.refresh_calls = 0

    def refresh(self, _request: object) -> None:
        self.refresh_calls += 1
        self.valid = True
        self.expired = False
        self.marker = "refreshed"

    def to_json(self) -> str:
        return json.dumps({"marker": self.marker})


class FakeFlow:
    def __init__(self, credentials: FakeCredentials) -> None:
        self.credentials = credentials
        self.ports: list[int] = []

    def run_local_server(self, *, port: int) -> FakeCredentials:
        self.ports.append(port)
        return self.credentials


class GoogleOAuthManagerTests(unittest.TestCase):
    def test_valid_credential_is_loaded_without_browser_flow(self) -> None:
        store = FakeCredentialStore('{"marker": "stored"}')
        loaded = FakeCredentials(valid=True)
        flow_calls: list[tuple[Path, tuple[str, ...]]] = []
        manager = GoogleOAuthManager(
            store,
            credentials_loader=lambda info, scopes: loaded,
            request_factory=object,
            flow_factory=lambda path, scopes: flow_calls.append(
                (path, tuple(scopes))
            ),
        )

        result = manager.authorize(Path("unused.json"))

        self.assertIs(result, loaded)
        self.assertEqual(flow_calls, [])

    def test_expired_credential_is_refreshed_and_saved(self) -> None:
        store = FakeCredentialStore('{"marker": "expired"}')
        loaded = FakeCredentials(
            valid=False,
            expired=True,
            refresh_token="refresh-token",
        )
        manager = GoogleOAuthManager(
            store,
            credentials_loader=lambda info, scopes: loaded,
            request_factory=object,
            flow_factory=lambda path, scopes: self.fail("browser flow was used"),
        )

        result = manager.authorize(Path("unused.json"))

        self.assertIs(result, loaded)
        self.assertEqual(loaded.refresh_calls, 1)
        self.assertEqual(store.value, '{"marker": "refreshed"}')

    def test_missing_credential_runs_desktop_loopback_flow_and_saves_token(self) -> None:
        store = FakeCredentialStore()
        issued = FakeCredentials(valid=True, marker="new")
        flow = FakeFlow(issued)
        with tempfile.TemporaryDirectory() as directory:
            client_file = Path(directory) / "credentials.json"
            client_file.write_text("{}", encoding="utf-8")
            manager = GoogleOAuthManager(
                store,
                credentials_loader=lambda info, scopes: self.fail(
                    "stored credential loader was used"
                ),
                request_factory=object,
                flow_factory=lambda path, scopes: flow,
            )

            result = manager.authorize(client_file)

        self.assertIs(result, issued)
        self.assertEqual(flow.ports, [0])
        self.assertEqual(store.value, '{"marker": "new"}')

    def test_force_authorize_ignores_existing_token(self) -> None:
        store = FakeCredentialStore('{"marker": "old"}')
        issued = FakeCredentials(valid=True, marker="new")
        flow = FakeFlow(issued)
        with tempfile.TemporaryDirectory() as directory:
            client_file = Path(directory) / "credentials.json"
            client_file.write_text("{}", encoding="utf-8")
            manager = GoogleOAuthManager(
                store,
                credentials_loader=lambda info, scopes: self.fail(
                    "stored credential loader was used"
                ),
                request_factory=object,
                flow_factory=lambda path, scopes: flow,
            )

            manager.authorize(client_file, force=True)

        self.assertEqual(store.value, '{"marker": "new"}')

    def test_missing_client_secrets_file_is_rejected_before_browser_flow(self) -> None:
        manager = GoogleOAuthManager(
            FakeCredentialStore(),
            credentials_loader=lambda info, scopes: None,
            request_factory=object,
            flow_factory=lambda path, scopes: self.fail("browser flow was used"),
        )

        with self.assertRaisesRegex(ValueError, "credentials.json"):
            manager.authorize(Path("missing-credentials.json"))


class FakeExecutable:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def execute(self) -> dict:
        return self.payload


class FakeResource:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.calls: list[dict] = []

    def list(self, **kwargs) -> FakeExecutable:
        self.calls.append(kwargs)
        return FakeExecutable(self.pages.pop(0))


class FakeCalendarApi:
    def __init__(self, *, calendars: list[dict], events: list[dict]) -> None:
        self.calendar_resource = FakeResource(calendars)
        self.event_resource = FakeResource(events)

    def calendarList(self) -> FakeResource:
        return self.calendar_resource

    def events(self) -> FakeResource:
        return self.event_resource


class GoogleCalendarServiceTests(unittest.TestCase):
    def test_lists_all_calendar_pages_and_marks_primary(self) -> None:
        api = FakeCalendarApi(
            calendars=[
                {
                    "items": [{"id": "primary-id", "summary": "Основний", "primary": True}],
                    "nextPageToken": "next",
                },
                {"items": [{"id": "lessons", "summary": "Уроки"}]},
            ],
            events=[],
        )

        calendars = GoogleCalendarService(api).list_calendars()

        self.assertEqual([item.id for item in calendars], ["primary-id", "lessons"])
        self.assertTrue(calendars[0].primary)
        self.assertEqual(
            api.calendar_resource.calls,
            [{}, {"pageToken": "next"}],
        )

    def test_lists_timed_events_in_range_with_pagination_and_ordering(self) -> None:
        api = FakeCalendarApi(
            calendars=[],
            events=[
                {
                    "items": [
                        {
                            "id": "event-2",
                            "status": "confirmed",
                            "summary": "2 Марія урок",
                            "description": "",
                            "start": {"dateTime": "2026-06-12T12:00:00+03:00"},
                            "end": {"dateTime": "2026-06-12T13:00:00+03:00"},
                        },
                        {
                            "id": "all-day",
                            "status": "confirmed",
                            "summary": "Не урок",
                            "start": {"date": "2026-06-12"},
                            "end": {"date": "2026-06-13"},
                        },
                    ],
                    "nextPageToken": "next",
                },
                {
                    "items": [
                        {
                            "id": "event-1",
                            "status": "confirmed",
                            "summary": "1 Ільяс урок",
                            "description": "пробне",
                            "htmlLink": "https://calendar.google.com/event?eid=1",
                            "start": {"dateTime": "2026-06-12T10:00:00+03:00"},
                            "end": {"dateTime": "2026-06-12T12:00:00+03:00"},
                        },
                        {
                            "id": "cancelled",
                            "status": "cancelled",
                            "summary": "Скасовано",
                            "start": {"dateTime": "2026-06-12T09:00:00+03:00"},
                            "end": {"dateTime": "2026-06-12T10:00:00+03:00"},
                        },
                    ]
                },
            ],
        )
        service = GoogleCalendarService(api)

        events = service.list_events(
            calendar_id="lessons",
            date_from=date(2026, 6, 12),
            date_to=date(2026, 6, 12),
            timezone_name="Europe/Kyiv",
        )

        self.assertEqual([event.id for event in events], ["event-1", "event-2"])
        self.assertEqual(events[0].duration_hours, 2)
        self.assertEqual(events[0].html_link, "https://calendar.google.com/event?eid=1")
        first_call = api.event_resource.calls[0]
        self.assertEqual(first_call["calendarId"], "lessons")
        self.assertEqual(first_call["timeMin"], "2026-06-12T00:00:00+03:00")
        self.assertEqual(first_call["timeMax"], "2026-06-13T00:00:00+03:00")
        self.assertTrue(first_call["singleEvents"])
        self.assertEqual(first_call["orderBy"], "startTime")
        self.assertFalse(first_call["showDeleted"])
        self.assertEqual(
            api.event_resource.calls[1]["pageToken"],
            "next",
        )

    def test_invalid_date_range_is_rejected_without_api_call(self) -> None:
        api = FakeCalendarApi(calendars=[], events=[])
        with self.assertRaisesRegex(ValueError, "діапазон"):
            GoogleCalendarService(api).list_events(
                calendar_id="primary",
                date_from=date(2026, 6, 13),
                date_to=date(2026, 6, 12),
                timezone_name="Europe/Kyiv",
            )
        self.assertEqual(api.event_resource.calls, [])


class CalendarEventToLessonFormTests(unittest.TestCase):
    @staticmethod
    def _event_from_summary(summary: str):
        api = FakeCalendarApi(
            calendars=[],
            events=[{
                "items": [{
                    "id": "google-event-id",
                    "status": "confirmed",
                    "summary": summary,
                    "description": "",
                    "start": {"dateTime": "2026-07-29T19:00:00+03:00"},
                    "end": {"dateTime": "2026-07-29T20:00:00+03:00"},
                }],
            }],
        )
        return GoogleCalendarService(api).list_events(
            calendar_id="primary",
            date_from=date(2026, 7, 29),
            date_to=date(2026, 7, 29),
            timezone_name="Europe/Kyiv",
        )[0]

    def test_prefills_editable_lesson_fields_from_calendar_event(self) -> None:
        api = FakeCalendarApi(
            calendars=[],
            events=[{
                "items": [{
                    "id": "google-event-id",
                    "status": "confirmed",
                    "summary": "105813989 Ільяс 10р індив ДВІ ГОДИНИ (пробне)",
                    "description": "",
                    "start": {"dateTime": "2026-06-12T10:00:00+03:00"},
                    "end": {"dateTime": "2026-06-12T12:00:00+03:00"},
                }],
            }],
        )
        event = GoogleCalendarService(api).list_events(
            calendar_id="primary",
            date_from=date(2026, 6, 12),
            date_to=date(2026, 6, 12),
            timezone_name="Europe/Kyiv",
        )[0]

        form = calendar_event_to_lesson_form(event)

        self.assertEqual(form.calendar_event_id, "google-event-id")
        self.assertEqual(form.event_start, "2026-06-12 10:00")
        self.assertEqual(form.student_id, "105813989")
        self.assertEqual(form.student_name, "Ільяс")
        self.assertEqual(form.lesson_label, "10р індив")
        self.assertEqual(form.duration_hours, 2)
        self.assertTrue(form.is_trial)
        self.assertEqual(form.video_paths, ())

    def test_scope_is_read_only(self) -> None:
        self.assertEqual(
            CALENDAR_READONLY_SCOPE,
            "https://www.googleapis.com/auth/calendar.readonly",
        )

    def test_parses_student_from_parentheses_and_ignores_service_markers(self) -> None:
        event = self._event_from_summary(
            "105813989 Сервер Османов (Ільяс 10) Учечко ТГ"
        )

        form = calendar_event_to_lesson_form(event)

        self.assertEqual(form.student_id, "105813989")
        self.assertEqual(form.student_name, "Ільяс")
        self.assertEqual(form.lesson_label, "10р індив")

    def test_keeps_subject_after_parentheses_and_normalizes_age(self) -> None:
        event = self._event_from_summary(
            "106248208 Valentyna Stoieva ТГ (Поліна 15р) JAVA | Учечко"
        )

        form = calendar_event_to_lesson_form(event)

        self.assertEqual(form.student_id, "106248208")
        self.assertEqual(form.student_name, "Поліна")
        self.assertEqual(form.lesson_label, "15р JAVA")


if __name__ == "__main__":
    unittest.main()
