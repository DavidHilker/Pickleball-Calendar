import asyncio
import re
from playwright.async_api import async_playwright
from ics import Calendar, Event
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

COURTRESERVE_ORGS = ["9314", "16119"]
MVP_URL = (
    "https://mvp.clubautomation.com/calendar/classes/programs"
    "?isFrame=2&style=1&calendars=3&facilities=1&tab=by-class"
)
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
    iso = iso.rstrip("Z").split(".")[0]
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


async def scrape_courtreserve(page, org_id):
    print(f"\n-- CourtReserve org {org_id} --")

    today = datetime.now(tz=LOCAL_TZ).date()
    end_date = today + timedelta(days=DAYS_FORWARD)

    # Try hitting the JSON feed endpoint directly
    json_url = (
        f"https://app.courtreserve.com/Online/Events/GetEventsList/{org_id}"
        f"?startDate={today.strftime('%Y-%m-%d')}"
        f"&endDate={end_date.strftime('%Y-%m-%d')}"
    )
    print(f"  Trying direct JSON URL: {json_url}")

    captured_events = []

    try:
        response = await page.goto(json_url, wait_until="networkidle", timeout=15000)
        content_type = response.headers.get("content-type", "")
        body = await page.content()
        print(f"  Content-Type: {content_type}")
        print(f"  Body preview: {body[:500]}")

        if "json" in content_type:
            import json
            data = json.loads(await page.evaluate("() => document.body.innerText"))
            if isinstance(data, list):
                captured_events = data
            elif isinstance(data, dict):
                for key in ("data", "events", "Events", "items"):
                    if key in data and isinstance(data[key], list):
                        captured_events = data[key]
                        break
    except Exception as exc:
        print(f"  Direct JSON failed: {exc}")

    # Fall back to loading the calendar page and dumping its HTML for debugging
    if not captured_events:
        print(f"  Falling back to calendar page for org {org_id}")
        base_url = f"https://app.courtreserve.com/Online/Calendar/Events/{org_id}/Month"
        await page.goto(base_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

        # Dump page HTML so we can inspect the structure
        html = await page.content()
        print(f"  PAGE HTML PREVIEW (first 2000 chars):\n{html[:2000]}")

        # Try to read events from every possible FullCalendar pattern
        captured_events = await page.evaluate("""
            () => {
                try {
                    // Pattern 1: FC v5/v6 on element
                    const fcEl = document.querySelector('.fc');
                    if (fcEl && fcEl._calendar) {
                        return fcEl._calendar.getEvents().map(ev => ({
                            title: ev.title,
                            start: ev.start ? ev.start.toISOString() : null,
                            end: ev.end ? ev.end.toISOString() : null,
                        }));
                    }
                    // Pattern 2: global calendar instance
                    if (window.calendar && window.calendar.getEvents) {
                        return window.calendar.getEvents().map(ev => ({
                            title: ev.title,
                            start: ev.start ? ev.start.toISOString() : null,
                            end: ev.end ? ev.end.toISOString() : null,
                        }));
                    }
                    // Pattern 3: scan all jQuery data for FC instance
                    const allEls = document.querySelectorAll('[id],[class]');
                    for (const el of allEls) {
                        const keys = Object.keys(el);
                        for (const k of keys) {
                            if (k.startsWith('jQuery') && el[k].fullCalendar) {
                                const evs = el[k].fullCalendar.clientEvents();
                                return evs.map(ev => ({
                                    title: ev.title,
                                    start: ev.start ? ev.start.toISOString() : null,
                                    end: ev.end ? ev.end.toISOString() : null,
                                }));
                            }
                        }
                    }
                    return [];
                } catch(e) { return ["ERROR: " + e.toString()]; }
            }
        """)

    print(f"  Events captured: {len(captured_events)}")
    if captured_events:
        print(f"  First event sample: {captured_events[0]}")

    for ev in captured_events:
        if not isinstance(ev, dict):
            continue
        title = (ev.get("title") or ev.get("Title") or ev.get("name") or "").strip()
        start_raw = ev.get("start") or ev.get("Start") or ev.get("startTime") or ""
        end_raw = ev.get("end") or ev.get("End") or ev.get("endTime") or ""

        if not title or not start_raw:
            continue

        try:
            start = _parse_courtreserve_dt(start_raw)
            end = _parse_courtreserve_dt(end_raw) if end_raw else start + timedelta(hours=1)
        except (ValueError, TypeError):
            continue

        if not (today <= start.date() <= end_date):
            continue

        add_event(title, start, end, f"CR {org_id}")


async def scrape_mvp(page):
    print("\n-- MVP Gym (ClubAutomation) --")

    await page.goto(MVP_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)

    # Dump full HTML so we can see the real structure
    html = await page.content()
    print(f"  MVP PAGE HTML PREVIEW (first 3000 chars):\n{html[:3000]}")

    # Also print ALL unique class names found on the page to identify selectors
    all_classes = await page.evaluate("""
        () => {
            const classes = new Set();
            document.querySelectorAll('*').forEach(el => {
                el.classList.forEach(c => classes.add(c));
            });
            return Array.from(classes).slice(0, 100);
        }
    """)
    print(f"  Classes found on page: {all_classes}")

    selectors_to_try = [
        ".schedule-item",
        ".class-item",
        ".group-class",
        "li.event",
        ".fc-event",
        ".program-item",
        ".class-block",
        "[class*='schedule']",
        "[class*='class-row']",
        "[class*='event-item']",
    ]

    loaded_selector = None
    for sel in selectors_to_try:
        items = await page.query_selector_all(sel)
        if items:
            loaded_selector = sel
            print(f"  Found {len(items)} items with selector: {sel}")
            # Print the HTML of the first item so we know its structure
            first_html = await items[0].inner_html()
            print(f"  First item HTML: {first_html[:500]}")
            break

    if loaded_selector is None:
        print("  WARNING: No items matched any selector.")
        return

    items = await page.query_selector_all(loaded_selector)
    today = datetime.now(tz=LOCAL_TZ)

    for ev in items:
        # Print the full text of each item for debugging
        full_text = (await ev.inner_text()).strip()
        print(f"  Item text: {full_text[:200]}")

        title = ""
        for title_sel in [".title", ".class-name", ".event-name", "h3", "h4", ".name", "strong", "b"]:
            el = await ev.query_selector(title_sel)
            if el:
                title = (await el.inner_text()).strip()
                break

        if not title:
            # Use the full text as title if no specific element found
            title = full_text.split("\n")[0].strip()

        if not title or "open play" not in title.lower():
            continue

        facility = ""
        for fac_sel in [".location", ".facility", ".gym", ".club-name", ".venue", ".studio"]:
            el = await ev.query_selector(fac_sel)
            if el:
                facility = (await el.inner_text()).strip()
                break

        print(f"  Matched Open Play - facility: '{facility}'")

        if facility and "sportsplex" not in facility.lower():
            continue

        event_date = today.date()
        for date_sel in [".date", ".event-date", ".day", "time", "[datetime]", ".when"]:
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

        time_text = ""
        for time_sel in [".time", ".event-time", ".hours", ".schedule-time", ".when", ".start-time"]:
            el = await ev.query_selector(time_sel)
            if el:
                time_text = (await el.inner_text()).strip()
                break

        if not time_text:
            print(f"  WARNING: no time found for '{title}'")
            continue

        time_text = time_text.replace("\u2013", "-").replace("\u2014", "-")
        time_text = re.sub(r"\s*(AM|PM|am|pm)\s*", lambda m: m.group(1).upper(), time_text)
        parts = [p.strip() for p in time_text.split("-")]
        if len(parts) != 2:
            continue

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
            print(f"  WARNING: Could not parse time '{time_text}' for '{title}'")
            continue

        add_event(title, start, end, "MVP Sportsplex")


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


