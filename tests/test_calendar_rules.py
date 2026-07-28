from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from lesson_video_uploader.calendar_rules import (
    BatchRevalidationRequired,
    CalendarEventStatus,
    CancellationSource,
    build_calendar_snapshot,
    parse_calendar_event,
    resolve_calendar_slots,
    validate_calendar_snapshots,
)
from lesson_video_uploader.google_calendar import GoogleCalendarEvent

KYIV = ZoneInfo("Europe/Kyiv")


def event(
    summary: str,
    *,
    event_id: str = "event-1",
    hour: int = 10,
    end_hour: int | None = None,
) -> GoogleCalendarEvent:
    start = datetime(2026, 6, 12, hour, tzinfo=KYIV)
    end = datetime(2026, 6, 12, end_hour or hour + 1, tzinfo=KYIV)
    return GoogleCalendarEvent(
        id=event_id,
        calendar_id="lessons",
        summary=summary,
        description="",
        start=start,
        end=end,
    )


class CalendarClassificationTests(unittest.TestCase):
    def test_student_cancellation_is_ignored_and_never_needs_video(self) -> None:
        parsed = parse_calendar_event(
            event("ВП учень 105853087 Наталія (Святослав 14) Учко ТГ")
        )

        self.assertEqual(parsed.status, CalendarEventStatus.IGNORED_CANCELLED)
        self.assertEqual(parsed.cancellation_source, CancellationSource.STUDENT)
        self.assertFalse(parsed.is_conducted)
        self.assertFalse(parsed.requires_video)
        self.assertFalse(parsed.is_split_boundary)

    def test_teacher_cancellation_is_case_insensitive(self) -> None:
        parsed = parse_calendar_event(
            event("вп ВИКЛАДАЧ 105853087 Наталія (Святослав 14р) Учко ТГ")
        )

        self.assertEqual(parsed.status, CalendarEventStatus.IGNORED_CANCELLED)
        self.assertEqual(parsed.cancellation_source, CancellationSource.TEACHER)

    def test_other_configured_cancellation_source_is_kept_as_other(self) -> None:
        parsed = parse_calendar_event(
            event(
                "ВП адміністратор 105853087 Наталія "
                "(Святослав 14) Учко ТГ"
            )
        )

        self.assertEqual(parsed.status, CalendarEventStatus.IGNORED_CANCELLED)
        self.assertEqual(parsed.cancellation_source, CancellationSource.OTHER)

    def test_letters_inside_another_word_do_not_mean_cancellation(self) -> None:
        parsed = parse_calendar_event(
            event("105853087 Вправи (Святослав 14 років) Учко ТГ")
        )

        self.assertEqual(parsed.status, CalendarEventStatus.NORMAL)
        self.assertTrue(parsed.requires_video)

    def test_transfer_uses_current_event_time_and_is_absent_from_caption(self) -> None:
        parsed = parse_calendar_event(
            event(
                "Перенос 105853087 Наталія (Святослав 14) Учко ТГ",
                hour=18,
            )
        )

        self.assertEqual(parsed.status, CalendarEventStatus.TRANSFERRED)
        self.assertEqual(parsed.start.hour, 18)
        self.assertNotIn("Перенос", parsed.caption)
        self.assertEqual(
            parsed.caption,
            "12.06.2026 105853087 Святослав 14р індив",
        )

    def test_transferred_trial_is_conducted_and_needs_video(self) -> None:
        parsed = parse_calendar_event(
            event(
                "Перенос 105853087 Наталія "
                "(Святослав 14 р) Учко ТГ (ПРОБНЕ)"
            )
        )

        self.assertEqual(
            parsed.status,
            CalendarEventStatus.TRANSFERRED_TRIAL,
        )
        self.assertTrue(parsed.is_trial)
        self.assertTrue(parsed.requires_video)
        self.assertTrue(parsed.caption.endswith("(пробне)"))

    def test_transferred_no_recording_is_text_only(self) -> None:
        parsed = parse_calendar_event(
            event(
                "Перенос 105853087 Наталія "
                "(Святослав 14 років) Учко ТГ (без запису)"
            )
        )

        self.assertEqual(
            parsed.status,
            CalendarEventStatus.TRANSFERRED_NO_RECORDING,
        )
        self.assertTrue(parsed.is_conducted)
        self.assertTrue(parsed.is_text_only)
        self.assertFalse(parsed.requires_video)
        self.assertTrue(parsed.caption.endswith("(без запису)"))

    def test_pause_has_priority_and_is_not_a_split_boundary(self) -> None:
        parsed = parse_calendar_event(
            event(
                "(ПАУЗА ДО ВЕРЕСНЯ) "
                "105977805 Maryna Kvasenko ТГ (Аліна 14р)"
            )
        )

        self.assertEqual(parsed.status, CalendarEventStatus.IGNORED_PAUSE)
        self.assertFalse(parsed.is_conducted)
        self.assertFalse(parsed.requires_video)
        self.assertFalse(parsed.is_split_boundary)

    def test_age_formats_and_subject_after_parentheses_are_supported(self) -> None:
        examples = {
            "10": "10р",
            "10р": "10р",
            "10 р": "10р",
            "10 років": "10р",
        }
        for raw_age, expected in examples.items():
            with self.subTest(raw_age=raw_age):
                parsed = parse_calendar_event(
                    event(
                        "106248208 Valentyna Stoieva ТГ "
                        f"(Поліна {raw_age}) JAVAI Учко"
                    )
                )
                self.assertEqual(parsed.student_name, "Поліна")
                self.assertEqual(parsed.student_age, 10)
                self.assertEqual(parsed.lesson_type, "JAVAI")
                self.assertIn(f"Поліна {expected} JAVAI", parsed.caption)

    def test_trial_and_no_recording_combination_is_text_only_with_both_marks(self) -> None:
        parsed = parse_calendar_event(
            event(
                "105853087 Наталія (Святослав 14) Учко ТГ "
                "(пробне) (без запису)"
            )
        )

        self.assertEqual(parsed.status, CalendarEventStatus.NO_RECORDING)
        self.assertTrue(parsed.is_trial)
        self.assertTrue(parsed.is_text_only)
        self.assertTrue(parsed.caption.endswith("(пробне) (без запису)"))

    def test_unparseable_conducted_event_is_parse_error(self) -> None:
        parsed = parse_calendar_event(event("звичайна подія без даних учня"))

        self.assertEqual(parsed.status, CalendarEventStatus.PARSE_ERROR)
        self.assertFalse(parsed.requires_video)


class CalendarSlotResolutionTests(unittest.TestCase):
    def test_cancelled_old_slot_and_transferred_new_slot_are_separate(self) -> None:
        parsed = (
            parse_calendar_event(
                event(
                    "ВП учень 105853087 Наталія (Святослав 14) Учко ТГ",
                    event_id="old",
                    hour=14,
                )
            ),
            parse_calendar_event(
                event(
                    "Перенос 105853087 Наталія (Святослав 14) Учко ТГ",
                    event_id="new",
                    hour=18,
                )
            ),
        )

        slots = resolve_calendar_slots(parsed, tolerance_minutes=10)

        self.assertEqual(len(slots), 2)
        self.assertIsNone(slots[0].selected)
        self.assertEqual(slots[0].ignored[0].status, CalendarEventStatus.IGNORED_CANCELLED)
        self.assertEqual(slots[1].selected.event_id, "new")

    def test_normal_and_transfer_in_one_slot_require_manual_selection(self) -> None:
        parsed = (
            parse_calendar_event(
                event(
                    "Перенос 105853087 Наталія (Святослав 14) Учко ТГ",
                    event_id="transfer",
                    hour=18,
                )
            ),
            parse_calendar_event(
                event(
                    "105813989 Сервер Османов (Ільяс 10) Учко ТГ",
                    event_id="normal",
                    hour=18,
                )
            ),
        )

        slot = resolve_calendar_slots(parsed, tolerance_minutes=10)[0]

        self.assertEqual(
            slot.status,
            CalendarEventStatus.MANUAL_SELECTION_REQUIRED,
        )
        self.assertIsNone(slot.selected)
        self.assertEqual(
            {candidate.event_id for candidate in slot.candidates},
            {"transfer", "normal"},
        )

    def test_cancelled_candidate_is_filtered_before_auto_selection(self) -> None:
        parsed = (
            parse_calendar_event(
                event(
                    "ВП учень 105853087 Наталія (Святослав 14) Учко ТГ",
                    event_id="cancelled",
                    hour=18,
                )
            ),
            parse_calendar_event(
                event(
                    "105813989 Сервер Османов (Ільяс 10) Учко ТГ",
                    event_id="normal",
                    hour=18,
                )
            ),
        )

        slot = resolve_calendar_slots(parsed, tolerance_minutes=10)[0]

        self.assertEqual(slot.selected.event_id, "normal")
        self.assertEqual([item.event_id for item in slot.ignored], ["cancelled"])

    def test_events_more_than_tolerance_apart_are_different_slots(self) -> None:
        parsed = (
            parse_calendar_event(
                event(
                    "105813989 Сервер Османов (Ільяс 10) Учко ТГ",
                    event_id="first",
                    hour=10,
                )
            ),
            parse_calendar_event(
                GoogleCalendarEvent(
                    id="second",
                    calendar_id="lessons",
                    summary="105853087 Наталія (Святослав 14) Учко ТГ",
                    description="",
                    start=datetime(2026, 6, 12, 10, 11, tzinfo=KYIV),
                    end=datetime(2026, 6, 12, 11, 11, tzinfo=KYIV),
                )
            ),
        )

        self.assertEqual(
            len(resolve_calendar_slots(parsed, tolerance_minutes=10)),
            2,
        )


class CalendarSnapshotValidationTests(unittest.TestCase):
    def test_change_to_cancellation_requires_batch_revalidation(self) -> None:
        original = parse_calendar_event(
            event("105853087 Наталія (Святослав 14) Учко ТГ")
        )
        current = parse_calendar_event(
            event("ВП учень 105853087 Наталія (Святослав 14) Учко ТГ")
        )

        with self.assertRaises(BatchRevalidationRequired) as captured:
            validate_calendar_snapshots(
                {original.event_id: build_calendar_snapshot(original)},
                {current.event_id: current},
            )

        self.assertIn("status", captured.exception.changes["event-1"])

    def test_time_change_requires_batch_revalidation(self) -> None:
        original = parse_calendar_event(
            event("105853087 Наталія (Святослав 14) Учко ТГ", hour=10)
        )
        current = parse_calendar_event(
            event("105853087 Наталія (Святослав 14) Учко ТГ", hour=11)
        )

        with self.assertRaises(BatchRevalidationRequired) as captured:
            validate_calendar_snapshots(
                {original.event_id: build_calendar_snapshot(original)},
                {current.event_id: current},
            )

        self.assertIn("start", captured.exception.changes["event-1"])

    def test_age_change_requires_revalidation_and_new_snapshot_uses_new_age(self) -> None:
        original = parse_calendar_event(
            event("105853087 Наталія (Святослав 14) Учко ТГ")
        )
        current = parse_calendar_event(
            event("105853087 Наталія (Святослав 15) Учко ТГ")
        )

        with self.assertRaises(BatchRevalidationRequired) as captured:
            validate_calendar_snapshots(
                {original.event_id: build_calendar_snapshot(original)},
                {current.event_id: current},
            )

        self.assertIn("student_age", captured.exception.changes["event-1"])
        self.assertIn(
            "student_age: 14 → 15",
            str(captured.exception),
        )
        self.assertEqual(build_calendar_snapshot(current).student_age, 15)

    def test_unchanged_current_calendar_event_passes_validation(self) -> None:
        parsed = parse_calendar_event(
            event("105853087 Наталія (Святослав 14) Учко ТГ")
        )
        snapshot = build_calendar_snapshot(parsed)

        validate_calendar_snapshots(
            {parsed.event_id: snapshot},
            {parsed.event_id: parsed},
        )


if __name__ == "__main__":
    unittest.main()
