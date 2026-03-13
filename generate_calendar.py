import asyncio

import re

from playwright.async_api import async_playwright

from ics import Calendar, Event

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

# ── Configuration ──────────────────────────────────────────────────────────────

COURTRESERVE_ORGS = [“9314”, “16119”]

MVP_URL = (

“https://mvp.clubautomation.com/calendar/classes/programs”

“?isFrame=2&style=1&calendars=3&facilities=1&tab=by-class”

)

DAYS_FORWARD = 90          # scrape ~3 months ahead

LOCAL_TZ = ZoneInfo(“America/New_York”)   # ← adjust to your local timezone

calendar = Calendar()

# ── Helpers ────────────────────────────────────────────────────────────────────

def add_event(title: str, start: datetime, end: datetime, source: str) -> None:

“”“Add event to calendar only if title contains ‘open play’ (case-insensitive).”””

if “open play” not in title.lower():

return

e = Event()

e.name = f”{title} ({source})”

e.begin = start

e.end = end if end > start else start + timedelta(hours=1)   # guard zero-duration

calendar.events.add(e)

print(f”  ✓ {e.name}  [{start.strftime(’%Y-%m-%d %H:%M’)}]”)

def _parse_courtreserve_dt(iso: str) -> datetime:

“””

CourtReserve sends ISO-8601 strings that may or may not carry a timezone.

Normalise to aware datetimes in LOCAL_TZ.

“””

# Remove trailing ‘Z’ and microseconds for clean parsing

iso = iso.rstrip(“Z”).split(”.”)[0]

dt = datetime.fromisoformat(iso)

if dt.tzinfo is None:

dt = dt.replace(tzinfo=LOCAL_TZ)

return dt

# ── CourtReserve ───────────────────────────────────────────────────────────────

async def scrape_courtreserve(page, org_id: str) -> None:

“””

Scrape one CourtReserve org for the next DAYS_FORWARD days.

```

Strategy: intercept the XHR that FullCalendar fires when it requests

events.  The endpoint is  /Online/Events/GetEventsList/<orgId>

and returns JSON.  This is far more reliable than poking at the JS

calendar object (which changed between FC versions).

"""

print(f"\n── CourtReserve org {org_id} ──")

captured_events: list[dict] = []

async def handle_response(response):

    """Intercept JSON event feed responses."""

    url = response.url

    if f"/{org_id}/" in url and response.status == 200:

        ct = response.headers.get("content-type", "")

        if "json" in ct or "javascript" in ct:

            try:

                data = await response.json()

                # FullCalendar event feeds are typically a bare list

                if isinstance(data, list):

                    captured_events.extend(data)

                elif isinstance(data, dict):

                    # Some CR versions wrap in {"success":true,"data":[...]}

                    for key in ("data", "events", "Events", "items"):

                        if key in data and isinstance(data[key], list):

                            captured_events.extend(data[key])

                            break

            except Exception:

                pass  # non-JSON body – ignore

page.on("response", handle_response)

today = datetime.now(tz=LOCAL_TZ).date()

end_date = today + timedelta(days=DAYS_FORWARD)

# Load the first month; CourtReserve auto-fetches via XHR

base_url = f"https://app.courtreserve.com/Online/Calendar/Events/{org_id}/Month"

await page.goto(base_url, wait_until="networkidle", timeout=30_000)

await page.wait_for_timeout(3_000)

# Navigate forward month-by-month to trigger additional XHR calls

# (FullCalendar only requests visible month; we need 3 months)

for _ in range(2):

    try:

        # The "next" button in FullCalendar month view

        next_btn = page.locator(

            "button.fc-next-button, "

            "a[aria-label='next'], "

            ".fc-button-next, "

            "[data-action='next']"

        ).first

        await next_btn.click(timeout=5_000)

        await page.wait_for_timeout(2_500)

    except Exception:

        break   # couldn't find the next button – that's OK

page.remove_listener("response", handle_response)

# ── Fallback: read from FullCalendar JS object if XHR gave nothing ──────

if not captured_events:

    print(f"  XHR capture empty – trying JS object fallback for {org_id}")

    captured_events = await page.evaluate("""

        () => {

            try {

                // FullCalendar v5/v6 stores the calendar on the element

                const el = document.querySelector('[data-fc-instance], .fc, #calendar');

                if (!el) return [];

                const cal = el._calendar ||

                            window.fullCalendarInstances?.[0] ||

                            window.__fc_calendar;

                if (!cal) return [];

                return cal.getEvents().map(ev => ({

                    title: ev.title,

                    start: ev.start ? ev.start.toISOString() : null,

                    end:   ev.end   ? ev.end.toISOString()   : null,

                }));

            } catch(e) { return []; }

        }

    """)

print(f"  Raw events captured: {len(captured_events)}")

for ev in captured_events:

    title = (

        ev.get("title") or ev.get("Title") or ev.get("name") or ""

    ).strip()

    start_raw = ev.get("start") or ev.get("Start") or ev.get("startTime") or ""

    end_raw   = ev.get("end")   or ev.get("End")   or ev.get("endTime")   or ""

    if not title or not start_raw:

        continue

    try:

        start = _parse_courtreserve_dt(start_raw)

        end   = _parse_courtreserve_dt(end_raw) if end_raw else start + timedelta(hours=1)

    except (ValueError, TypeError):

        continue

    # Only include events within our window

    if not (today <= start.date() <= end_date):

        continue

    add_event(title, start, end, f"CR {org_id}")

```

# ── MVP / ClubAutomation ───────────────────────────────────────────────────────

async def scrape_mvp(page) -> None:

“””

Scrape MVP Gym (ClubAutomation) calendar.

```

ClubAutomation renders class cards inside an iframe-style page.

The selector structure differs from the original script; this version

tries multiple fallback selectors and also handles dates properly

(the original used today's date for every event – this version reads

the date from the card when available).

"""

print("\n── MVP Gym (ClubAutomation) ──")

await page.goto(MVP_URL, wait_until="domcontentloaded", timeout=30_000)

# Wait for content; try several known selectors

selectors_to_try = [

    ".schedule-item",

    ".class-item",

    ".group-class",

    "li.event",

    ".fc-event",

    "[class*='schedule']",

    "[class*='class-row']",

]

loaded_selector = None

for sel in selectors_to_try:

    try:

        await page.wait_for_selector(sel, timeout=7_000)

        loaded_selector = sel

        print(f"  Found items with selector: {sel}")

        break

    except Exception:

        continue

if loaded_selector is None:

    print("  ⚠️  Could not find schedule items on MVP page – page may require login.")

    return

items = await page.query_selector_all(loaded_selector)

print(f"  Raw items found: {len(items)}")

today = datetime.now(tz=LOCAL_TZ)

for ev in items:

    # ── Title ──────────────────────────────────────────────────────────

    title = ""

    for title_sel in [".title", ".class-name", ".event-name", "h3", "h4", ".name"]:

        el = await ev.query_selector(title_sel)

        if el:

            title = (await el.inner_text()).strip()

            break

    if not title:

        continue

    if "open play" not in title.lower():

        continue

    # ── Facility ───────────────────────────────────────────────────────

    facility = ""

    for fac_sel in [".location", ".facility", ".gym", ".club-name", ".venue"]:

        el = await ev.query_selector(fac_sel)

        if el:

            facility = (await el.inner_text()).strip()

            break

    if facility and "sportsplex" not in facility.lower():

        continue

    # If facility text is missing we still include the event (benefit of doubt)

    # ── Date ───────────────────────────────────────────────────────────

    event_date = today.date()

    for date_sel in [".date", ".event-date", ".day", "time", "[datetime]"]:

        el = await ev.query_selector(date_sel)

        if el:

            raw_date = (

                await el.get_attribute("datetime")

                or await el.inner_text()

            )

            if raw_date:

                for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"]:

                    try:

                        event_date = datetime.strptime(raw_date.strip(), fmt).date()

                        break

                    except ValueError:

                        continue

            break

    # ── Time ───────────────────────────────────────────────────────────

    time_text = ""

    for time_sel in [".time", ".event-time", ".hours", ".schedule-time"]:

        el = await ev.query_selector(time_sel)

        if el:

            time_text = (await el.inner_text()).strip()

            break

    if not time_text:

        continue

    # Normalise separators: "10:00AM - 11:00AM" or "10:00 am – 11:00 am"

    time_text = time_text.replace("–", "-").replace("—", "-")

    # Remove internal spaces around AM/PM so strptime is happy

    time_text = re.sub(r"\s*(AM|PM|am|pm)\s*", lambda m: m.group(1).upper(), time_text)

    parts = [p.strip() for p in time_text.split("-")]

    if len(parts) != 2:

        continue

    # Inherit AM/PM from end time if start is missing it

    start_str, end_str = parts

    if not re.search(r"[AaPp][Mm]", start_str):

        am_pm = re.search(r"(AM|PM)", end_str)

        if am_pm:

            start_str += am_pm.group(1)

    try:

        start = datetime.strptime(

            f"{event_date} {start_str}", "%Y-%m-%d %I:%M%p"

        ).replace(tzinfo=LOCAL_TZ)

        end = datetime.strptime(

            f"{event_date} {end_str}", "%Y-%m-%d %I:%M%p"

        ).replace(tzinfo=LOCAL_TZ)

    except ValueError:

        # Try without leading zero: "9:00AM"

        try:

            start = datetime.strptime(

                f"{event_date} {start_str}", "%Y-%m-%d %I:%M%p"

            ).replace(tzinfo=LOCAL_TZ)

            end = datetime.strptime(

                f"{event_date} {end_str}", "%Y-%m-%d %I:%M%p"

            ).replace(tzinfo=LOCAL_TZ)

        except ValueError:

            print(f"  ⚠️  Could not parse time '{time_text}' for '{title}'")

            continue

    add_event(title, start, end, "MVP Sportsplex")

```

# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:

async with async_playwright() as p:

browser = await p.chromium.launch(headless=True)

context = await browser.new_context(

user_agent=(

“Mozilla/5.0 (Windows NT 10.0; Win64; x64) “

“AppleWebKit/537.36 (KHTML, like Gecko) “

“Chrome/124.0.0.0 Safari/537.36”

),

viewport={“width”: 1280, “height”: 900},

)

page = await context.new_page()

```

    for org in COURTRESERVE_ORGS:

        try:

            await scrape_courtreserve(page, org)

        except Exception as exc:

            print(f"  ✗ CourtReserve {org} failed: {exc}")

    try:

        await scrape_mvp(page)

    except Exception as exc:

        print(f"  ✗ MVP scrape failed: {exc}")

    await browser.close()

total = len(calendar.events)

print(f"\n── Writing calendar.ics  ({total} events) ──")

with open("calendar.ics", "w", encoding="utf-8") as f:

    f.writelines(calendar)

print("Done ✓")

```

if **name** == “**main**”:

asyncio.run(main())
CA | MVP
 
