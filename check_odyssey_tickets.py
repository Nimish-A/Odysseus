#!/usr/bin/env python3
"""
Odyssey IMAX Sydney ticket checker (seats-available alert, all dates).

Watches this cinema page for "The Odyssey":
  https://www.eventcinemas.com.au/cinema/imax-sydney

Important: this site's date tabs (e.g. "Fri 21/08") are plain JS click
handlers with a data-date attribute -- they are NOT reflected in the URL,
so loading the page with a #date=... fragment does nothing. To see other
dates, this script drives a real (headless) Chrome browser via Selenium,
loads the page once, then actually clicks through every date tab that's
currently present in the page (the site only shows tabs for dates that
have sessions -- there can be gaps of a week or more), reading each
session's data straight from the site's own markup after each click:

    <a class="session-btn" data-sessionid="15510350"
       data-time="2026-08-21T13:50" data-seatsavailable="52" ...>

Alert condition: a session's seats-available count is greater than
SEATS_AVAILABLE_THRESHOLD. This naturally catches brand-new sessions too,
since a session that just goes on sale typically opens with most seats
free. Each session only triggers an alert ONCE (the first time it's seen
above the threshold) -- it won't re-alert every check after that.

SETUP (run once):
    pip install selenium webdriver-manager beautifulsoup4

You also need Google Chrome installed on your machine. Selenium's
webdriver-manager will download the matching chromedriver automatically.

USAGE:
    python check_odyssey_tickets.py

Runs continuously, checking every CHECK_INTERVAL_SECONDS. Press Ctrl+C to stop.

NOTE: Because this clicks through every date tab present on the page, each
full check takes a while (roughly PAGE_LOAD_WAIT_SECONDS x number of date
tabs currently shown, commonly 20-30). The default CHECK_INTERVAL_SECONDS
accounts for that.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.eventcinemas.com.au/cinema/imax-sydney"
MOVIE_NAME_MATCH = "odyssey"          # case-insensitive substring match on movie name
SEATS_AVAILABLE_THRESHOLD = 65        # alert when a session has MORE than this many seats free
CHECK_INTERVAL_SECONDS = 1800         # 30 minutes (clicking through many dates takes a while)
STATE_FILE = Path(__file__).parent / "odyssey_ticket_state.json"
PAGE_LOAD_WAIT_SECONDS = 3            # time to let each date's sessions render after clicking


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def get_driver():
    """Create a headless Chrome webdriver."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    except ImportError:
        # Fall back to whatever chromedriver is on PATH
        service = Service()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,2000")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    return webdriver.Chrome(service=service, options=options)


def parse_sessions_from_html(html: str, date_str: str):
    """
    Given the page's current rendered HTML (after clicking a date tab),
    return a dict mapping session_id -> {"date", "time", "seats_available",
    "url"} for sessions belonging to the target movie, read straight from
    each session button's data attributes.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Find the movie's list item by its data-name attribute (exact structure
    # seen on the real page: <li class="movie-list-item ..." data-name="The Odyssey">)
    movie_li = None
    for li in soup.find_all("li", class_="movie-list-item"):
        name = (li.get("data-name") or "").lower()
        if MOVIE_NAME_MATCH in name:
            movie_li = li
            break

    if movie_li is None:
        return {}

    sessions = {}
    for a in movie_li.find_all("a", class_="session-btn"):
        session_id = a.get("data-sessionid")
        if not session_id:
            continue

        raw_seats = a.get("data-seatsavailable", "")
        try:
            seats_available = int(raw_seats)
        except ValueError:
            seats_available = None

        data_time = a.get("data-time", "")  # e.g. "2026-08-21T13:50"
        display_time_tag = a.find("span", class_="time")
        display_time = display_time_tag.get_text(strip=True) if display_time_tag else data_time

        href = a.get("href", "")
        sessions[session_id] = {
            "date": date_str,
            "time": display_time,
            "data_time": data_time,
            "seats_available": seats_available,
            "url": href if href.startswith("http") else f"https://www.eventcinemas.com.au{href}",
        }

    return sessions


def fetch_sessions():
    """
    Load the cinema page once, then click through each available date tab
    (the site only exposes tabs for dates that actually have sessions --
    there can be gaps), scraping session data after each click, and return
    a combined dict of all sessions found across every date.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    all_sessions = {}
    driver = get_driver()
    try:
        driver.get(BASE_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a.date[data-date]"))
        )

        # Collect the list of available dates from the tabs currently in the DOM.
        date_elements = driver.find_elements(By.CSS_SELECTOR, "a.date[data-date]")
        available_dates = [el.get_attribute("data-date") for el in date_elements if el.get_attribute("data-date")]
        available_dates = [d for d in dict.fromkeys(available_dates)]  # de-dupe, keep order

        if not available_dates:
            log("WARNING: no date tabs found on the page at all.")
            return {}

        for date_str in available_dates:
            try:
                el = driver.find_element(By.CSS_SELECTOR, f'a.date[data-date="{date_str}"]')
                driver.execute_script("arguments[0].click();", el)
                time.sleep(PAGE_LOAD_WAIT_SECONDS)
                html = driver.page_source
            except Exception as e:
                log(f"  WARNING: failed to load {date_str}: {e}")
                continue

            day_sessions = parse_sessions_from_html(html, date_str)
            if day_sessions:
                all_sessions.update(day_sessions)
    finally:
        driver.quit()

    return all_sessions


def load_previous_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(sessions):
    STATE_FILE.write_text(json.dumps(sessions, indent=2))


def check_once():
    log("Checking sessions across the date range...")
    try:
        current = fetch_sessions()
    except Exception as e:
        log(f"ERROR while checking: {e}")
        return

    if not current:
        log("No sessions found at all (page structure may have changed, or nothing on sale yet).")
        return

    previous = load_previous_state()  # {session_id: {"seats_available": int, "alerted": bool, ...}}

    alerts = []
    updated_state = {}

    for session_id, info in current.items():
        seats = info["seats_available"]
        prev_info = previous.get(session_id)
        already_alerted = bool(prev_info and prev_info.get("alerted"))

        will_alert = (seats is not None) and (seats > SEATS_AVAILABLE_THRESHOLD) and not already_alerted

        if will_alert:
            alerts.append(info)

        updated_state[session_id] = {
            "date": info["date"],
            "time": info["time"],
            "seats_available": seats,
            "alerted": already_alerted or will_alert,
        }

    if alerts:
        print("\n" + "=" * 60)
        print(f"🎬  SESSION(S) WITH >{SEATS_AVAILABLE_THRESHOLD} SEATS AVAILABLE  🎬")
        for info in sorted(alerts, key=lambda i: (i["date"], i["data_time"])):
            print(f"  Date: {info['date']}")
            print(f"  Time: {info['time']}")
            print(f"  Seats available: {info['seats_available']}")
            print(f"  Book: {info['url']}")
            print("-" * 40)
        print("=" * 60 + "\n")
    else:
        log(f"No sessions currently over the {SEATS_AVAILABLE_THRESHOLD}-seat threshold "
            f"that haven't already been flagged. ({len(current)} session(s) checked.)")

    save_state(updated_state)


def main():
    log(f"Starting Odyssey IMAX Sydney ticket checker.")
    log(f"Watching: {BASE_URL} (clicking through every date tab present on the page)")
    log(f"Checking every {CHECK_INTERVAL_SECONDS // 60} minute(s). Press Ctrl+C to stop.\n")

    try:
        while True:
            check_once()
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
