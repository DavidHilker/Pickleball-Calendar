import asyncio
import re
import json
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
    captured_events = []

    # Intercept the XHR that FullCalendar fires to populate events
    async def handle_response(response):
        url = response.url
        if "courtreserve.com" not in url:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            data = await response.json()
            print(f"  JSON response from: {url}")
            print(f"  Type: {type(data)}, preview: {str(data)[:300]}")
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

    # Load the calendar page and wait generously for JS to fire
    url = f"https://app.courtreserve.com/Online/Calendar/Events/{org_id}/Month"
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(8000)

    # Click next month twice to load more events
    for i in range(2):
        try:
            btn = page.locator("button.fc-next-button").first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
            await page.wait_for_timeout(3000)
            print(f"  Clicked next month ({i+1})")
        except Exception:
            print(f"  Could not click next month ({i+1})")
            break

    page.remove_listener("response", handle_response)
    print(f"  Total events from XHR: {len(captured_events)}")

    # If XHR gave nothing, try reading rendered event elements directly from the DOM
    if not captured_events:
        print("  Trying DOM event elements...")
        dom_events = await page.evaluate("""
            () => {
                const results = [];
                // FullCalendar renders events as <a class="fc-event"> elements
                document.querySelectorAll('a.fc-event, .fc-event').forEach(el => {
                    const titleEl = el.querySelector('.fc-event-title, .fc-title, .fc-event-main');
                    const title = titleEl ? titleEl.innerText.trim() : el.innerText.trim();
                    // The event time is stored in the parent fc-timegrid-event or as data attributes
                    const start = el.getAttribute('data-start') ||
                                  el.closest('[data-start]')?.getAttribute('data-start') || '';
                    const end   = el.getAttribute('data-end') ||
                                  el.closest('[data-end]')?.getAttribute('data-end') || '';
                    if (title) results.push({ title, start, end });
                });
                return results;
            }
        """)
        print(f"  DOM events found: {len(dom_events)}")
        if dom_events:
            print(f"  Sample: {dom_events[0]}")
        captured_events = dom_events

    for ev in captured_events:
        if not isinstance(ev, dict):
            continue
        title = (ev.get("title") or ev.get("Title") or ev.get("name") or "").strip()
        start_raw = ev.get("start") or ev.get("Start") or ev.get("startTime") or ""
        end_raw   = ev.get("end")   or ev.get("End")   or ev.get("endTime")   or ""

        if not title or not start_raw:
            continue

        try:
            start = _parse_courtreserve_dt(str(start_raw))
            end   = _parse_courtreserve_dt(str(end_raw)) if end_raw else start + timedelta(hours=1)
        except (ValueError, TypeError):
            continue

        if not (today <= start.date() <= end_date):
            continue

        add_event(title, start, end, f"CR {org_id}")
async def debug_mvp_events_page(page):
    print("\n-- DEBUG: MVP Event Info Page --")
    url = "https://mvp.clubautomation.com/calendar/event-info?id=1070097&style=1&isFrame=2"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)

    # Print full HTML
    html = await page.content()
    print(f"  FULL HTML:\n{html[:5000]}")

    # Print all unique class names
    all_classes = await page.evaluate("""
        () => {
            const classes = new Set();
            document.querySelectorAll('*').forEach(el => {
                el.classList.forEach(c => classes.add(c));
            });
            return Array.from(classes);
        }
    """)
    print(f"  All classes: {all_classes}")

    # Print text of every element that might contain a date or time
    date_time_texts = await page.evaluate("""
        () => {
            const results = [];
            document.querySelectorAll('*').forEach(el => {
                const text = el.innerText?.trim();
                if (text && (
                    text.match(/\d{1,2}:\d{2}/) ||
                    text.match(/AM|PM/i) ||
                    text.match(/Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec/i) ||
                    text.match(/Mon|Tue|Wed|Thu|Fri|Sat|Sun/i)
                ) && text.length < 200) {
                    results.push({ tag: el.tagName, class: el.className, text });
                }
            });
            return results.slice(0, 50);
        }
    """)
    print(f"  Date/time elements found:")
    for el in date_time_texts:
        print(f"    <{el['tag']} class='{el['class']}'> {el['text']}")



async def scrape_mvp(page):
    """
    MVP ClubAutomation page structure (confirmed from debug output):

    The entire schedule lives in ONE element matching [class*='schedule'].
    Inside it, each class is a .block div containing:
      - .row_link  -> class name
      - .row_text divs -> facility, department, days of week

    This page shows recurring classes (not dated events), so we generate
    one event per upcoming matching day-of-week within DAYS_FORWARD.
    """
    print("\n-- MVP Gym (ClubAutomation) --")

    await page.goto(MVP_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)

    blocks = await page.query_selector_all(".block")
    print(f"  Total .block elements: {len(blocks)}")

    today = datetime.now(tz=LOCAL_TZ).date()
    end_date = today + timedelta(days=DAYS_FORWARD)

    # Map day abbreviations to Python weekday numbers (Mon=0 ... Sun=6)
    DAY_MAP = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3,
        "fri": 4, "sat": 5, "sun": 6
    }

    for block in blocks:
        # Skip header row (it has no .row_link with actual text)
        link_el = await block.query_selector(".row_link")
        if not link_el:
            continue
        title = (await link_el.inner_text()).strip()

        if "open play" not in title.lower():
            continue

        # .row_text divs: [0]=facility, [1]=department, [2]=days of week
        row_texts = await block.query_selector_all(".row_text")
        texts = [(await el.inner_text()).strip() for el in row_texts]
        print(f"  Matched: '{title}' | row_texts: {texts}")

        if len(texts) < 1:
            continue

        facility = texts[0] if len(texts) > 0 else ""
        days_str = texts[2] if len(texts) > 2 else ""

        if facility and "sportsplex" not in facility.lower():
            continue

        if not days_str:
            print(f"  WARNING: no days found for '{title}'")
            continue

        # Parse day abbreviations e.g. "Fri, Mon, Tue"
        day_nums = []
        for part in re.split(r"[,\s]+", days_str):
            key = part.strip().lower()[:3]
            if key in DAY_MAP:
                day_nums.append(DAY_MAP[key])

        if not day_nums:
            print(f"  WARNING: could not parse days '{days_str}'")
            continue

        # We don't have a time on this view — default to 8:00-9:00 AM
        # as a placeholder (update if you find a time source)
        DEFAULT_START_HOUR = 8
        DEFAULT_DURATION = timedelta(hours=1)

        # Generate one event for each matching weekday in the next 30 days
        current = today
        while current <= end_date:
            if current.weekday() in day_nums:
                start = datetime(
                    current.year, current.month, current.day,
                    DEFAULT_START_HOUR, 0, tzinfo=LOCAL_TZ
                )
                end = start + DEFAULT_DURATION
                add_event(title, start, end, "MVP Sportsplex")
            current += timedelta(days=1)


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

        await debug_mvp_events_page(page) 
        await browser.close()

    total = len(calendar.events)
    print(f"\n-- Writing calendar.ics ({total} events) --")
    with open("calendar.ics", "w", encoding="utf-8") as f:
        f.writelines(calendar)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())



