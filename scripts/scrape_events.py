"""Fetch, normalize, and save upcoming TOR13 events from QRTicket."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = ROOT / "_data" / "events.json"
POSTER_DIRECTORY = ROOT / "assets" / "events"
SOURCE_URL = (
    "https://qrticket.de/alleveranstaltungen.php"
    "?idv=440&tsva=a3294c739e0c71fba813ae4c1cf78cc5"
)
BERLIN = ZoneInfo("Europe/Berlin")
MAX_POSTER_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30

DATE_PATTERN = re.compile(
    r"(?:Mo|Di|Mi|Do|Fr|Sa|So)?\s*"
    r"(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})\s*"
    r"-\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?:\s*-\s*(?P<location>.*))?"
)
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONTHS = (
    "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
    "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
)


class QRTicketEventParser(HTMLParser):
    """Extract event cards from QRTicket's documented page markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_events_heading = False
        self.cards: list[dict[str, str | None]] = []
        self.card_count = 0
        self._heading_depth = 0
        self._card: dict[str, Any] | None = None
        self._card_depth = 0
        self._capture_field: str | None = None
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attributes_by_name = dict(attributes)
        classes = set((attributes_by_name.get("class") or "").split())

        if tag == "h2":
            self._heading_depth += 1

        if tag == "div":
            if self._card is None and "va_cont" in classes:
                self._card = {"date_text": "", "title": "", "poster_url": None, "ticket_url": None}
                self._card_depth = 1
                self.card_count += 1
            elif self._card is not None:
                self._card_depth += 1

        if self._card is None:
            return

        if tag == "p" and "va_termin" in classes:
            self._capture_field = "date_text"
            self._capture_text = []
        elif tag == "p" and "va_titel" in classes:
            self._capture_field = "title"
            self._capture_text = []
        elif tag == "img" and "va_bild" in classes:
            source = attributes_by_name.get("src")
            if source:
                self._card["poster_url"] = urljoin(SOURCE_URL, source)
        elif tag == "a":
            href = attributes_by_name.get("href")
            if href and "ticketbestellung.php" in href:
                self._card["ticket_url"] = urljoin(SOURCE_URL, href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._heading_depth:
            self._heading_depth -= 1

        if tag == "p" and self._capture_field is not None and self._card is not None:
            self._card[self._capture_field] = " ".join("".join(self._capture_text).split())
            self._capture_field = None
            self._capture_text = []

        if tag == "div" and self._card is not None:
            self._card_depth -= 1
            if self._card_depth == 0:
                self.cards.append(self._card)
                self._card = None
                self._capture_field = None
                self._capture_text = []

    def handle_data(self, data: str) -> None:
        if self._heading_depth and "Aktuelle Veranstaltungen" in data:
            self.has_events_heading = True
        if self._capture_field is not None:
            self._capture_text.append(data)

    def close(self) -> None:
        super().close()
        if self._card is not None:
            self.cards.append(self._card)
            self._card = None


def _normalize_card(card: dict[str, str | None]) -> dict[str, str | None]:
    date_text = card["date_text"] or ""
    title = (card["title"] or "").strip()
    ticket_url = card["ticket_url"]
    match = DATE_PATTERN.fullmatch(date_text)

    if not match or not title or not ticket_url:
        raise ValueError(f"Incomplete or unrecognized QRTicket event card: {card!r}")

    ticket_parts = urlsplit(ticket_url)
    if ticket_parts.hostname not in {"qrticket.de", "www.qrticket.de"}:
        raise ValueError(f"Unexpected ticket host in event card: {ticket_url}")

    event_id = parse_qs(ticket_parts.query).get("idveranstaltung", [None])[0]
    if not event_id or not event_id.isdigit():
        raise ValueError(f"Missing event ID in ticket URL: {ticket_url}")

    try:
        starts_at = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            tzinfo=BERLIN,
        )
    except ValueError as exception:
        raise ValueError(f"Invalid event date: {date_text}") from exception

    location = (match.group("location") or "").strip(" -")
    if not location:
        location = "Basdorfer Weg 1a · Wandlitz"

    return {
        "id": event_id,
        "title": title,
        "starts_at": starts_at.isoformat(),
        "date_iso": starts_at.date().isoformat(),
        "weekday": WEEKDAYS[starts_at.weekday()],
        "day": f"{starts_at.day:02d}",
        "month": MONTHS[starts_at.month - 1],
        "time": starts_at.strftime("%H:%M"),
        "location": location,
        "ticket_url": ticket_url,
        "poster_url": card["poster_url"],
        "poster": None,
    }


def parse_events(
    html: str,
    now: datetime | None = None,
) -> list[dict[str, str | None]]:
    """Parse the event page, discard started events, and sort chronologically."""
    parser = QRTicketEventParser()
    parser.feed(html)
    parser.close()

    if not parser.has_events_heading:
        raise ValueError("QRTicket event heading was not found; refusing to replace saved events")
    if parser.card_count != len(parser.cards):
        raise ValueError("One or more QRTicket event cards were malformed")

    current_time = now or datetime.now(BERLIN)
    if current_time.tzinfo is None:
        raise ValueError("The comparison time must be timezone-aware")
    current_time = current_time.astimezone(BERLIN)

    upcoming_events: list[dict[str, str | None]] = []
    for card in parser.cards:
        event = _normalize_card(card)
        event_start = datetime.fromisoformat(event["starts_at"] or "")
        if event_start > current_time:
            upcoming_events.append(event)

    return sorted(upcoming_events, key=lambda event: event["starts_at"] or "")


def fetch_page() -> str:
    request = Request(
        SOURCE_URL,
        headers={"User-Agent": "TOR13-event-updater/1.0 (+https://github.com/)"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def _poster_filename(event: dict[str, str | None], image_url: str) -> str:
    parts = urlsplit(image_url)
    if parts.hostname not in {"qrticket.de", "www.qrticket.de"}:
        raise ValueError(f"Unexpected poster host: {image_url}")

    extension = Path(parts.path).suffix.lower()
    if extension == ".jpeg":
        extension = ".jpg"
    if extension not in {".jpg", ".png", ".webp"}:
        raise ValueError(f"Unsupported poster image type: {image_url}")

    return f"{event['id']}{extension}"


def mirror_posters(events: list[dict[str, str | None]]) -> None:
    """Download posters to a temporary directory, then copy them into the site."""
    POSTER_DIRECTORY.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tor13-posters-") as temporary_directory:
        staged_directory = Path(temporary_directory)
        staged_filenames: set[str] = set()

        for event in events:
            image_url = event.pop("poster_url", None)
            if not image_url:
                event["poster"] = None
                continue

            filename = _poster_filename(event, image_url)
            request = Request(
                image_url,
                headers={"User-Agent": "TOR13-event-updater/1.0 (+https://github.com/)"},
            )
            try:
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    if response.headers.get_content_type() not in {
                        "image/jpeg", "image/png", "image/webp"
                    }:
                        raise ValueError(f"QRTicket returned a non-image poster: {image_url}")
                    contents = response.read(MAX_POSTER_BYTES + 1)
            except URLError as exception:
                raise RuntimeError(f"Could not download QRTicket poster: {image_url}") from exception

            if len(contents) > MAX_POSTER_BYTES:
                raise ValueError(f"QRTicket poster exceeds 10 MiB: {image_url}")

            (staged_directory / filename).write_bytes(contents)
            staged_filenames.add(filename)
            event["poster"] = f"/assets/events/{filename}"

        for filename in staged_filenames:
            shutil.copyfile(staged_directory / filename, POSTER_DIRECTORY / filename)


def save_events(events: list[dict[str, str | None]]) -> None:
    """Replace the Jekyll data file atomically after a successful scrape."""
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = EVENTS_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(events, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_file, EVENTS_FILE)


def remove_stale_posters(events: list[dict[str, str | None]]) -> None:
    """Delete posters no longer referenced by the upcoming-event data."""
    retained_posters = {
        Path(event["poster"]).name
        for event in events
        if event.get("poster")
    }
    for poster_file in POSTER_DIRECTORY.glob("*"):
        if poster_file.is_file() and poster_file.name not in retained_posters:
            poster_file.unlink()


def main() -> None:
    events = parse_events(fetch_page())
    mirror_posters(events)
    save_events(events)
    remove_stale_posters(events)
    print(f"Saved {len(events)} upcoming TOR13 event(s).")


if __name__ == "__main__":
    main()
