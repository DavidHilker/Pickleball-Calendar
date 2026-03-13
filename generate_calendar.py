import asyncio

from playwright.async_api import async_playwright

from ics import Calendar, Event

from datetime import datetime, timedelta

calendar = Calendar()

COURTRESERVE_ORGS = ["9314", "16119"]

MVP_URL = "https://mvp.clubautomation.com/calendar/classes/programs?isFrame=2&style=1&calendars=3&facilities=1&tab=by-class"

DAYS_FORWARD = 90  # look ahead 3 months

def add_event(title, start, end, source):

    if "open play" not in title.lower():

        return

    e = Event()

    e.name = f"{title} ({source})"

    e.begin = start

    e.end = end

    calendar.events.add(e)

async def scrape_courtreserve(page, org_id):
   url = f"https://app.courtreserve.com/Online/Calendar/Events/{org_id}/Month"
   await page.goto(url, wait_until="networkidle")  # wait until network requests are done
   # Give extra time for JS calendar to populate
   await page.wait_for_timeout(5000)  # wait 5 seconds
   # Extract events directly from JavaScript calendar object
   events_data = await page.evaluate("""
       () => {
           const events = window.FullCalendar?.getCalendar?.()?.getEvents?.() || [];
           return events.map(e => ({
               title: e.title,
               start: e.start?.toISOString(),
               end: e.end?.toISOString()
           }));
       }
   """)
   for e in events_data:
       if not e["start"]:
           continue
       start = datetime.fromisoformat(e["start"])
       end = datetime.fromisoformat(e["end"]) if e["end"] else start
       add_event(e["title"], start, end, f"CR {org_id}")

async def scrape_mvp(page):

    await page.goto(MVP_URL)
    await page.wait_for_timeout(5000)  # wait 5 seconds
    await page.wait_for_selector(".schedule-item")

    items = await page.query_selector_all(".schedule-item")

    for ev in items:

        title_el = await ev.query_selector(".title")

        facility_el = await ev.query_selector(".location")

        time_el = await ev.query_selector(".time")

        if not title_el or not facility_el or not time_el:

            continue

        title = (await title_el.inner_text()).strip()

        facility = (await facility_el.inner_text()).strip()

        if "sportsplex" not in facility.lower():

            continue

        times = (await time_el.inner_text()).split("-")

        if len(times) != 2:

            continue

        today = datetime.today().date()

        start = datetime.strptime(str(today) + " " + times[0].strip(), "%Y-%m-%d %I:%M %p")

        end = datetime.strptime(str(today) + " " + times[1].strip(), "%Y-%m-%d %I:%M %p")

        add_event(title, start, end, "MVP Sportsplex")

async def main():

    async with async_playwright() as p:

        browser = await p.chromium.launch()

        page = await browser.new_page()

        for org in COURTRESERVE_ORGS:

            await scrape_courtreserve(page, org)

        await scrape_mvp(page)

        await browser.close()

    with open("calendar.ics", "w") as f:

        f.writelines(calendar)

asyncio.run(main())
CA | MVP
 
