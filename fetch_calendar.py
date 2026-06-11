#!/usr/bin/env python3
"""Fetch upcoming economic event dates (CPI, NFP, FOMC) from public sources.

Scrapes:
  - BLS schedule pages for CPI and Employment Situation (NFP) release dates
  - Federal Reserve FOMC calendar page for meeting dates

If BLS blocks the request (Akamai bot detection), falls back to a hardcoded
schedule derived from published BLS dates. The static data covers through
end of 2026 and should be updated every 6 months.

Outputs:
    data/calendar.json  -  list of upcoming events with dates and types

Usage:
    python3 fetch_calendar.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / "data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── Static fallback: actual BLS published release dates ──────────────
# Source: https://www.bls.gov/schedule/news_release/cpi.htm
# Source: https://www.bls.gov/schedule/news_release/empsit.htm
# Update every 6 months.
STATIC_CPI = [
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]
STATIC_NFP = [
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03",
    "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]


def fetch_url(url: str) -> str:
    """Fetch a URL and return the response text."""
    print(f"  Fetching {url}")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


# ── BLS HTML scraping ────────────────────────────────────────────────

def parse_bls_schedule(html: str, event_type: str) -> list[dict]:
    """Parse BLS schedule HTML page to extract release dates."""
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                date_text = cells[1].get_text(strip=True)
                parsed = _parse_bls_date(date_text)
                if parsed:
                    ref = cells[0].get_text(strip=True)
                    events.append({
                        "date": parsed.strftime("%Y-%m-%d"),
                        "type": event_type,
                        "label": f"{event_type} ({ref})",
                    })
    return events


def _parse_bls_date(text: str) -> datetime | None:
    """Parse BLS date formats like 'Jun. 10, 2026'."""
    text = text.strip().rstrip(".")
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fetch_bls_events() -> list[dict]:
    """Try to scrape BLS. Returns empty list if blocked."""
    events = []
    urls = [
        ("https://www.bls.gov/schedule/news_release/cpi.htm", "CPI"),
        ("https://www.bls.gov/schedule/news_release/empsit.htm", "NFP"),
    ]
    for url, etype in urls:
        try:
            html = fetch_url(url)
            found = parse_bls_schedule(html, etype)
            print(f"  Found {len(found)} {etype} dates from BLS")
            events.extend(found)
        except Exception as e:
            print(f"  BLS scrape failed for {etype}: {e}")
    return events


# ── Static fallback ──────────────────────────────────────────────────

def static_events() -> list[dict]:
    """Return events from hardcoded BLS dates."""
    events = []
    for d in STATIC_CPI:
        dt = datetime.strptime(d, "%Y-%m-%d")
        ref = dt.replace(day=1).strftime("%b %Y")
        events.append({"date": d, "type": "CPI", "label": f"CPI ({ref})"})
    for d in STATIC_NFP:
        dt = datetime.strptime(d, "%Y-%m-%d")
        ref = dt.replace(day=1).strftime("%b %Y")
        events.append({"date": d, "type": "NFP", "label": f"NFP ({ref})"})
    return events


# ── FOMC scraping ────────────────────────────────────────────────────

def parse_fomc_calendar(html: str) -> list[dict]:
    """Parse the FOMC calendar page for meeting dates."""
    soup = BeautifulSoup(html, "html.parser")
    events = []
    current_year = None

    for panel in soup.find_all("div", class_="panel"):
        heading = panel.find("h5") or panel.find("div", class_="panel-heading")
        if heading:
            m = re.search(r"\d{4}", heading.get_text())
            if m:
                current_year = int(m.group())
        if not current_year:
            continue

        body = panel.find("div", class_="panel-body")
        if not body:
            continue

        for item in body.find_all("div", class_="row"):
            text = item.get_text(strip=True)
            match = re.search(
                r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{1,2})"
                r"(?:\s*[-/]\s*\d{1,2})?",
                text,
            )
            if match and current_year:
                month_str, day = match.group(1), int(match.group(2))
                try:
                    dt = datetime.strptime(f"{month_str} {day} {current_year}", "%B %d %Y")
                    events.append({
                        "date": dt.strftime("%Y-%m-%d"),
                        "type": "FOMC",
                        "label": f"FOMC Meeting ({month_str})",
                    })
                except ValueError:
                    pass

    # Fallback: regex on full text if structured parse finds nothing
    if not events:
        full_text = soup.get_text()
        for ym in re.finditer(r"(\d{4})\s+FOMC\s+Meetings?", full_text):
            yr = int(ym.group(1))
            block = full_text[ym.end():ym.end() + 2000]
            for mm in re.finditer(
                r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{1,2})",
                block,
            ):
                try:
                    dt = datetime.strptime(f"{mm.group(1)} {int(mm.group(2))} {yr}", "%B %d %Y")
                    events.append({
                        "date": dt.strftime("%Y-%m-%d"),
                        "type": "FOMC",
                        "label": f"FOMC Meeting ({mm.group(1)})",
                    })
                except ValueError:
                    pass
    return events


def fetch_fomc_events() -> list[dict]:
    """Scrape FOMC meeting dates from the Fed website."""
    try:
        html = fetch_url("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
        events = parse_fomc_calendar(html)
        print(f"  Found {len(events)} FOMC dates")
        return events
    except Exception as e:
        print(f"  FOMC scrape failed: {e}")
        return []


# ── Main ─────────────────────────────────────────────────────────────

def filter_upcoming(events: list[dict], days_ahead: int = 120, days_back: int = 7) -> list[dict]:
    """Keep events from N days back to N days ahead."""
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    return [e for e in events if start <= e["date"] <= cutoff]


def main():
    DATA_DIR.mkdir(exist_ok=True)
    all_events: list[dict] = []

    # 1. Try BLS scraping, fall back to static
    bls = fetch_bls_events()
    if bls:
        all_events.extend(bls)
    else:
        print("  Using static BLS fallback dates")
        all_events.extend(static_events())

    # 2. FOMC from the Fed
    all_events.extend(fetch_fomc_events())

    # Filter, sort, deduplicate
    upcoming = filter_upcoming(all_events, days_ahead=120)
    upcoming.sort(key=lambda e: e["date"])
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for e in upcoming:
        key = (e["date"], e["type"])
        if key not in seen:
            seen.add(key)
            unique.append(e)

    calendar_data = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "events": unique,
    }

    out_path = DATA_DIR / "calendar.json"
    with open(out_path, "w") as f:
        json.dump(calendar_data, f, indent=2)
    print(f"Wrote {out_path} ({len(unique)} upcoming events)")
    for e in unique:
        print(f"  {e['date']}  {e['type']:4s}  {e['label']}")


if __name__ == "__main__":
    main()
