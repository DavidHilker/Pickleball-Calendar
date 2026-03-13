import asyncio
import re
from playwright.async_api import async_playwright
from ics import Calendar, Event
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Configuration ──────────────────────────────────────────────────────────────
COURTRESERVE_ORGS = ["9314", "16119"]
MVP_URL = (
    "https://mvp.clubautomation.com/calendar/classes/programs"
    "?isFrame=2&style=1&calendars=3&facilities=1&tab=by-class"
)
DAYS_FORWARD = 90
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
    print(f"  + {e.name}  [{start.strftime('%Y-%m-%d %H:%M')}]")


def _parse_courtreserve_dt(iso):
    iso = iso.rstrip("Z").split(".")[0]
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


async def scrape_courtreserve(page, org_id):
    print(f"\n-- CourtReserve org {org_id} --")

    captured_events = []

    async def handle_response(response):
        url = response.url
        if f"/{org_id}/" in url and response.status == 200:
            ct = response.headers.get("content-type", "")
            if "json" in ct or "javascript" in ct:
                try:
                    data = await response.json()
                    if isinstance(data, list):
                        captured_events.extend(data)
                    elif isinstance(data, dict):
                        for key in ("data", "events", "Events", "items"):
                            if key in data and isinstance(data[key], list):
                                captured_events.extend(data[key])
                                break
                except Exception:
                    pass

    page.on("response", handle_response)

    today = datetime.now(tz=LOCAL_TZ).date()
    end_date = today + timedelta(days=DAYS_FORWARD)

    base_url = f"https://app.courtreserve.com/Online/Calendar/Events/{org_id}/Month"
    await page.goto(base_url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)

    for _ in range(2):
        try:
            next_btn = page.locator(
                "button.fc-next-button, "
                "a[aria-label='next'], "
                ".fc-button-next, "
                "[data-action='next']"
            ).first
            await next_btn.click(timeout=5000)
            await page.wait_for_timeout(2500)
        except Exception:
            break

    page.remove_listener("response", handle_response)

    if not captured_events:
        print(f"  XHR capture empty - trying JS object fallback for {org_id}")
        captured_events = await page.evaluate("""
            () => {
                try {
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

        if not (today <= start.date() <= end_date):
            continue

        add_event(title, start, end, f"CR {org_id}")


async def scrape_mvp(page):
    print("\n-- MVP Gym (ClubAutomation) --")

    await page.goto(MVP_URL, wait_until="domcontentloaded", timeout=30000)

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
            await page.wait_for_selector(sel, timeout=7000)
            loaded_selector = sel
            print(f"  Found items with selector: {sel}")
            break
        except Exception:
            continue

    if loaded_selector is None:
        print("  WARNING: Could not find schedule items on MVP page.")
        return

    items = await page.query_selector_all(loaded_selector)
    print(f"  Raw items found: {len(items)}")

    today = datetime.now(tz=LOCAL_TZ)

    for ev in items:
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

        facility = ""
        for fac_sel in [".location", ".facility", ".gym", ".club-name", ".venue"]:
            el = await ev.query_selector(fac_sel)
            if el:
                facility = (await el.inner_text()).strip()
                break

        if facility and "sportsplex" not in facility.lower():
            continue

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

        time_text = ""
        for time_sel in [".time", ".event-time", ".hours", ".schedule-time"]:
            el = await ev.query_selector(time_sel)
            if el:
                time_text = (await el.inner_text()).strip()
                break

        if not time_text:
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
    print("Done​​​​​​​​​​​​​​​​


