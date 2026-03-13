import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event
from datetime import datetime, timedelta

calendar = Calendar()

COURTRESERVE_ORGS = ["9314", "16119"]

DAYS_FORWARD = 90


def add_event(title, start, end, source):

    if "open play" not in title.lower():
        return

    e = Event()
    e.name = f"{title} ({source})"
    e.begin = start
    e.end = end

    calendar.events.add(e)


def scrape_courtreserve(org_id):

    start_date = datetime.today()
    end_date = start_date + timedelta(days=DAYS_FORWARD)

    url = (
        f"https://app.courtreserve.com/api/events"
        f"?organizationId={org_id}"
        f"&startDate={start_date.strftime('%Y-%m-%d')}"
        f"&endDate={end_date.strftime('%Y-%m-%d')}"
    )

    r = requests.get(url)
    events = r.json()

    for e in events:

        title = e.get("title", "")

        start = datetime.fromisoformat(e["start"])
        end = datetime.fromisoformat(e["end"])

        add_event(title, start, end, f"CR {org_id}")


def scrape_mvp():

    url = "https://mvp.clubautomation.com/calendar/classes/programs?isFrame=2&style=1&calendars=3&facilities=1&tab=by-class"

    r = requests.get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    events = soup.select(".schedule-item")

    for ev in events:

        title_el = ev.select_one(".title")
        facility_el = ev.select_one(".location")
        time_el = ev.select_one(".time")

        if not title_el or not facility_el or not time_el:
            continue

        title = title_el.text.strip()
        facility = facility_el.text.strip()

        if "sportsplex" not in facility.lower():
            continue

        times = time_el.text.split("-")

        if len(times) != 2:
            continue

        today = datetime.today().date()

        start = datetime.strptime(str(today) + " " + times[0].strip(), "%Y-%m-%d %I:%M %p")
        end = datetime.strptime(str(today) + " " + times[1].strip(), "%Y-%m-%d %I:%M %p")

        add_event(title, start, end, "MVP Sportsplex")


for org in COURTRESERVE_ORGS:
    scrape_courtreserve(org)

scrape_mvp()

with open("calendar.ics", "w") as f:
    f.writelines(calendar)
