"""Regression tests for event expiry, order, and scraper safety."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.scrape_events import parse_events

BERLIN = ZoneInfo("Europe/Berlin")


def event_card(date_text: str, title: str, event_id: int) -> str:
    return f"""
    <div class="va_cont" id="vacont_{event_id}">
      <p class="va_termin">{date_text}</p>
      <p class="va_titel">{title}</p>
      <div class="va_beschreibung">
        <img class="va_bild" src="/ticketbilder/ticketbild{event_id}.jpg?version=1">
        <p class="va_button"><a href="https://qrticket.de/ticketbestellung.php?idveranstaltung={event_id}">Tickets kaufen!</a></p>
      </div>
    </div>
    """


class ParseEventsTests(unittest.TestCase):
    def test_removes_started_events_and_sorts_upcoming_events(self) -> None:
        page = "<h2>Aktuelle Veranstaltungen</h2>" + "".join(
            (
                event_card("Sa 21.11.2026 - 19:00 - TOR13", "Later event", 102),
                event_card("Do 12.11.2026 - 18:30 - Wandlitz", "Next event", 101),
                event_card("Di 10.11.2026 - 22:00 - Wandlitz", "Expired event", 100),
            )
        )
        now = datetime(2026, 11, 11, 12, 0, tzinfo=BERLIN)

        events = parse_events(page, now)

        self.assertEqual([event["title"] for event in events], ["Next event", "Later event"])
        self.assertEqual(events[0]["starts_at"], "2026-11-12T18:30:00+01:00")
        self.assertEqual(events[0]["date_iso"], "2026-11-12")
        self.assertEqual(events[0]["weekday"], "Do")
        self.assertEqual(events[0]["month"], "Nov")
        self.assertEqual(events[0]["location"], "Wandlitz")
        self.assertEqual(
            events[0]["poster_url"],
            "https://qrticket.de/ticketbilder/ticketbild101.jpg?version=1",
        )

    def test_event_at_current_start_time_is_not_upcoming(self) -> None:
        page = "<h2>Aktuelle Veranstaltungen</h2>" + event_card(
            "Mi 11.11.2026 - 12:00 - Wandlitz", "Starting now", 103
        )
        now = datetime(2026, 11, 11, 12, 0, tzinfo=BERLIN)

        self.assertEqual(parse_events(page, now), [])

    def test_valid_empty_listing_clears_old_events(self) -> None:
        page = "<h2>Aktuelle Veranstaltungen</h2><p>Keine Termine</p>"

        self.assertEqual(parse_events(page, datetime(2026, 11, 11, tzinfo=BERLIN)), [])

    def test_unrecognized_page_is_rejected_instead_of_clearing_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "heading was not found"):
            parse_events("<h1>Unexpected provider error</h1>")

    def test_incomplete_card_is_rejected_instead_of_silently_dropped(self) -> None:
        page = """
        <h2>Aktuelle Veranstaltungen</h2>
        <div class="va_cont">
          <p class="va_termin">Do 12.11.2026 - 18:30 - Wandlitz</p>
        </div>
        """

        with self.assertRaisesRegex(ValueError, "Incomplete or unrecognized"):
            parse_events(page, datetime(2026, 11, 11, tzinfo=BERLIN))

    def test_naive_comparison_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            parse_events("<h2>Aktuelle Veranstaltungen</h2>", datetime(2026, 11, 11))


if __name__ == "__main__":
    unittest.main()
