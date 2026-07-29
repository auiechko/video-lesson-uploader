from __future__ import annotations

import unittest

from lesson_video_uploader.desktop import (
    sorted_table_item_ids,
    table_sort_key,
)


class DesktopTableSortingTests(unittest.TestCase):
    def test_dates_and_times_are_sorted_chronologically(self) -> None:
        rows = (
            ("late", "29.07.2026 19:02:12"),
            ("early", "28.07.2026 20:15:00"),
            ("middle", "29.07.2026 08:00:00"),
        )

        ordered = sorted_table_item_ids(rows)

        self.assertEqual(ordered, ("early", "middle", "late"))

    def test_minute_values_are_sorted_numerically(self) -> None:
        rows = (
            ("ten", "10 хв"),
            ("two", "2 хв"),
            ("half", "0.5 хв"),
        )

        ordered = sorted_table_item_ids(rows)

        self.assertEqual(ordered, ("half", "two", "ten"))

    def test_second_click_can_reverse_sort_while_blanks_stay_last(self) -> None:
        rows = (
            ("blank", "—"),
            ("alpha", "Аліна 14р"),
            ("beta", "Святослав 15р"),
        )

        ordered = sorted_table_item_ids(rows, descending=True)

        self.assertEqual(ordered, ("beta", "alpha", "blank"))

    def test_sort_key_is_case_insensitive(self) -> None:
        self.assertEqual(table_sort_key("zoom"), table_sort_key("ZOOM"))


if __name__ == "__main__":
    unittest.main()
