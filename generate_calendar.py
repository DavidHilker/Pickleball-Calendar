import asyncio
import re
from playwright.async_api import async_playwright
from ics import Calendar, Event
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

COURTRESERVE_ORGS = ["9314", "16119"]
MVP_URL = "https://mvp.clubautomation.com/calendar/event-info?id=1070097&style=1&isFrame=2"
DAYS_FORWARD = 30
LOCAL_TZ = ZoneInfo("America/New_York")

calendar = Calendar()


def add_event(title, start, end, source):
    if "open play" not in title.lower():
        return
    e = Event()
    e.name = f"{title} ({source})"
    e.begin = start
    e.end = end if end > start else start + timedelta(hours=1)
    calendar.events.add(e)
    print(f"  ADDED: {e.name}  [{start.strftime('%Y-%m-%d %H:%M')}]")


def _parse_courtreserve_dt(iso):
    iso = str(iso).rstrip("Z").split(".")[0]
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


async def scrape_courtreserve(page, org_id):
    print(f"\n-- CourtReserve org {org_id} --")

    today = datetime.now(tz=LOCAL_TZ).date()
    end_date = today + timedelta(days=DAYS_FORWARD)
    captured_events = []

    async def handle_response(response):
        url = response.url
        if "courtreserve.com" not in url:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            data = await response.json()
            print(f"  JSON from: {url}")
            print(f"  Preview: {str(data)[:300]}")
            if isinstance(data, list):
                captured_events.extend(data)
            elif isinstance(data, dict):
                for key in ("data", "events", "Events", "items", "Data"):
                    if key in data and isinstance(data[key], list):
                        captured_events.extend(data[key])
                        break
        except Exception as exc:
            print(f"  JSON parse error: {exc}")

    page.on("response", handle_response)

    url = f"https://app.courtreserve.com/Online/Calendar/Events/{org_id}/Month"
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(8000)

    # Click next month to get more events
    for i in range(1):
        try:
            btn = page.locator("button.fc-next-button").first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
            await page.wait_for_timeout(3000)
        except Exception:
            break

    page.remove_listener("response", handle_response)
    print(f"  Events from XHR: {len(captured_events)}")

    # Print first event to see its actual field names
    if captured_events:
        print(f"  First event keys: {list(captured_events[0].keys()) if isinstance(captured_events[0], dict) else captured_events[0]}")

    for ev in captured_events:
        if not isinstance(ev, dict):
            continue

        # Try every common field name variation CourtReserve uses
        title = (
            ev.get("title") or ev.get("Title") or
            ev.get("name") or ev.get("Name") or
            ev.get("EventName") or ev.get("eventName") or ""
        ).strip()

        start_raw = (
            ev.get("start") or ev.get("Start") or
            ev.get("startTime") or ev.get("StartTime") or
            ev.get("StartDate") or ev.get("startDate") or ""
        )
        end_raw = (
            ev.get("end") or ev.get("End") or
            ev.get("endTime") or ev.get("EndTime") or
            ev.get("EndDate") or ev.get("endDate") or ""
        )

        if not title or not start_raw:
            continue

        try:
            start = _parse_courtreserve_dt(start_raw)
            end   = _parse_courtreserve_dt(end_raw) if end_raw else start + timedelta(hours=1)
        except (ValueError, TypeError):
            continue

        if not (today <= start.date() <= end_date):
            continue

        add_event(title, start, end, f"CR {org_id}")


async def scrape_mvp(page):
    """
    MVP event-info page structure (confirmed from debug):
      Each event is an <li class='row_1'> or <li class='event row_2'>
      Inside each li:
        - First  .col-md-1.row_text > strong  = date  e.g. "Saturday, April 11"
        - Second .col-md-1.row_text           = time  e.g. "04:00pm - 06:00pm"
      The page only contains Open Play events so no title filtering needed,
      but we still label them correctly.
    """
    print("\n-- MVP Gym (ClubAutomation) --")

    await page.goto(MVP_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)

    rows = await page.query_selector_all("li.row_1, li.event.row_2, li.row_2")
    print(f"  Event rows found: {len(rows)}")

    today = datetime.now(tz=LOCAL_TZ).date()
    end_date = today + timedelta(days=DAYS_FORWARD)

    for row in rows:
        row_text_els = await row.query_selector_all(".col-md-1.row_text")
        if len(row_text_els) < 2:
            continue

        # Date is in first col-md-1 row_text (inside a <strong>)
        date_el = await row_text_els[0].query_selector("strong")
        date_str = (await date_el.inner_text()).strip() if date_el else (await row_text_els[0].inner_text()).strip()

        # Time is in second col-md-1 row_text
        time_str = (await row_text_els[1].inner_text()).strip()

        print(f"  Row: date='{date_str}'  time='{time_str}'")

        # Parse date e.g. "Saturday, April 11" — no year, so use current/next year
        try:
            parsed = datetime.strptime(date_str, "%A, %B %d")
            year = today.year
            event_date = parsed.replace(year=year).date()
            # If the date already passed this year, try next year
            if event_date < today:
                event_date = parsed.replace(year=year + 1).date()
        except ValueError:
            print(f"  WARNING: could not parse date '{date_str}'")
            continue

        if event_date > end_date:
            continue

        # Parse time e.g. "04:00pm - 06:00pm"
        time_str = time_str.replace("\u2013", "-").replace("\u2014", "-")
        parts = [p.strip() for p in time_str.split("-")]
        if len(parts) != 2:
            continue

        try:
            start = datetime.strptime(
                f"{event_date} {parts[0].upper()}", "%Y-%m-%d %I:%M%p"
            ).replace(tzinfo=LOCAL_TZ)
            end = datetime.strptime(
                f"{event_date} {parts[1].upper()}", "%Y-%m-%d %I:%M%p"
            ).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            print(f"  WARNING: could not parse time '{time_str}'")
            continue

        add_event("Open Play", start, end, "MVP Sportsplex")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for org in COURTRESERVE_ORGS:
            try:
                await scrape_courtreserve(page, org)
            except Exception as exc:
                print(f"  FAILED CourtReserve {org}: {exc}")

        try:
            await scrape_mvp(page)
        except Exception as exc:
            print(f"  FAILED MVP: {exc}")

        await browser.close()

    total = len(calendar.events)
    print(f"\n-- Writing calendar.ics ({total} events) --")
    with open("calendar.ics", "w", encoding="utf-8") as f:
        f.writelines(calendar)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())


