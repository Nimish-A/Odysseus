#!/usr/bin/env python3
"""
Odyssey IMAX Sydney ticket checker (seats-available + seat-increase alerts, all dates).

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

Three independent alert conditions, any of which plays a sound and prints
a banner:

  1. NEW SESSION: a session ID that has never been seen before. This fires
     regardless of how many seats are free -- a new screening that opens
     with only a handful of seats left still counts as a drop. Note this
     is the only condition that catches such a session at all: it can't
     cross the threshold below, and it has no previous seat count to be
     compared against.

  2. THRESHOLD: a session's seats-available count is greater than
     SEATS_AVAILABLE_THRESHOLD. Each session only triggers this alert ONCE
     (the first time it's seen above the threshold). A session caught by
     condition 1 is reported there rather than here, so a roomy new
     session doesn't produce two banners.

  3. SEAT INCREASE: a session's seats-available count has gone UP by at
     least SEATS_INCREASE_THRESHOLD since the last check (e.g. someone
     cancelled/released seats). This can re-trigger on later checks if
     seats keep climbing further.

On the very first run (no state file yet) every session looks new, so
condition 1 is suppressed and that run just records a baseline.

STATE: sessions are remembered in STATE_FILE even after they stop appearing
on the site, so that a temporarily failed date-tab load doesn't make known
sessions look brand new on the next check. Entries not seen for
STATE_RETENTION_DAYS are dropped to keep the file from growing forever.

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

SOUND: uses the built-in winsound module, so alerts beep automatically on
Windows with no extra install. On macOS/Linux this is skipped silently
(the printed banner still appears).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# The alert banners below contain emoji, but a Windows console is often set to
# a legacy code page (cp932/cp1252/...) that can't encode them -- printing one
# then raises UnicodeEncodeError. That would kill the script at the worst
# possible moment: mid-alert, before the state file is saved. Degrade
# unencodable characters to "?" instead of crashing. Consoles that do support
# UTF-8 still render the emoji normally.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.eventcinemas.com.au/cinema/imax-sydney"
MOVIE_NAME_MATCH = "odyssey"          # case-insensitive substring match on movie name
SEATS_AVAILABLE_THRESHOLD = 5        # alert when a session has MORE than this many seats free
SEATS_INCREASE_THRESHOLD = 5          # alert when seats free jump up by at least this much since last check
CHECK_INTERVAL_SECONDS = 1500         # 25 minutes (clicking through many dates takes a while)
STATE_FILE = Path(__file__).parent / "odyssey_ticket_state.json"
PAGE_LOAD_WAIT_SECONDS = 3            # time to let each date's sessions render after clicking
STATE_RETENTION_DAYS = 30             # forget sessions not seen on the site for this long

# Push notifications via ntfy.sh. Set NTFY_TOPIC to the topic name you
# subscribed to in the ntfy app; leave it unset to disable pushes entirely
# (useful when running locally, where the beeps are enough). The topic name is
# the only secret -- anyone who knows it can read your alerts -- so keep it
# long and random, and never commit it.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_ATTEMPTS = 3                     # retries before giving up on a push
MAX_SESSIONS_PER_PUSH = 8             # keep the notification body readable


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
    each session button's data attributes. ds
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


def play_alert_sound(state: bool):
    """Play a short attention-grabbing sound. Windows only (winsound is
    stdlib); silently does nothing elsewhere."""
    if not HAS_WINSOUND:
        return
    try:
        # Three quick beeps, rising in pitch
        if state:
            for freq in (800, 1000, 1300):
                winsound.Beep(freq, 200)
        else:
             for freq in (1300,1000,800):
                winsound.Beep(freq, 200)
                
    except RuntimeError:
        # Beep() can fail on some systems/VMs; fall back to the default
        # system alert sound instead.
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def send_push(title, body, click_url=None):
    """
    POST a notification to ntfy.sh. Returns True on success, False on failure.

    Returns False rather than raising: a push failure must never take down the
    watcher, and the caller decides what to do about it (see check_once, which
    deliberately leaves state unsaved so the alert is retried).

    No-ops and reports success when NTFY_TOPIC isn't set, so running locally
    without push configured isn't treated as an error.
    """
    if not NTFY_TOPIC:
        return True

    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "clapper",
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click_url:
        headers["Click"] = click_url

    # ntfy sends headers as latin-1; strip anything it can't carry so a stray
    # non-ASCII character in a title can't turn into an exception here.
    headers = {k: v.encode("ascii", "replace").decode("ascii") for k, v in headers.items()}

    request = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    for attempt in range(1, NTFY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if 200 <= response.status < 300:
                    return True
                log(f"  push attempt {attempt}: server returned HTTP {response.status}")
        except (urllib.error.URLError, OSError) as e:
            log(f"  push attempt {attempt} failed: {e}")
        if attempt < NTFY_ATTEMPTS:
            time.sleep(2 * attempt)

    return False


def format_push_body(new_alerts, threshold_alerts, increase_alerts):
    """Build a compact multi-line notification body from the alert lists."""
    lines = []

    def add(heading, alerts, describe):
        if not alerts:
            return
        lines.append(heading)
        ordered = sorted(alerts, key=lambda i: (i["date"], i["data_time"]))
        for info in ordered[:MAX_SESSIONS_PER_PUSH]:
            lines.append(f"  {info['date']} {info['time']} - {describe(info)}")
        remaining = len(ordered) - MAX_SESSIONS_PER_PUSH
        if remaining > 0:
            lines.append(f"  ...and {remaining} more")

    add("NEW SESSIONS:", new_alerts, lambda i: f"{i['seats_available']} seats")
    add(f"OVER {SEATS_AVAILABLE_THRESHOLD} SEATS:", threshold_alerts,
        lambda i: f"{i['seats_available']} seats")
    add("SEATS OPENED UP:", increase_alerts,
        lambda i: f"{i['prev_seats']} -> {i['seats_available']} (+{i['increase']})")

    return "\n".join(lines)


def load_previous_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(sessions):
    STATE_FILE.write_text(json.dumps(sessions, indent=2))


def prune_state(state, today=None):
    """
    Drop entries not seen on the site for STATE_RETENTION_DAYS, so the file
    doesn't accumulate every screening that ever existed. Entries written by
    an older version of this script have no "last_seen" -- treat those as
    seen today so the first run after upgrading doesn't discard them (and so
    they don't come back as "new sessions" on the run after that).
    """
    today = today or date.today()
    cutoff = today - timedelta(days=STATE_RETENTION_DAYS)

    kept = {}
    for session_id, info in state.items():
        raw_last_seen = info.get("last_seen")
        if not raw_last_seen:
            kept[session_id] = {**info, "last_seen": today.isoformat()}
            continue
        try:
            last_seen = date.fromisoformat(raw_last_seen)
        except ValueError:
            kept[session_id] = {**info, "last_seen": today.isoformat()}
            continue
        if last_seen >= cutoff:
            kept[session_id] = info

    return kept


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

    # With no state file there's nothing to compare against, so every session
    # would look brand new. Record a baseline instead of alerting 90+ times.
    is_baseline = not previous

    today = date.today().isoformat()
    new_alerts = []
    threshold_alerts = []
    increase_alerts = []

    # Carry the previous state forward rather than rebuilding from scratch:
    # sessions that aren't in this sweep (a date tab that failed to load, a
    # screening that's sold out and delisted) must stay remembered, or they'd
    # be reported as new drops the next time they appear.
    updated_state = dict(previous)

    for session_id, info in current.items():
        seats = info["seats_available"]
        prev_info = previous.get(session_id)
        prev_seats = prev_info.get("seats_available") if prev_info else None
        already_alerted = bool(prev_info and prev_info.get("alerted"))

        # Condition 1: a session ID we've never seen before, at any seat count.
        is_new = (prev_info is None) and not is_baseline
        if is_new:
            new_alerts.append(info)

        # Condition 2: crossed the seats-available threshold (fires once per
        # session). Skipped for brand-new sessions -- condition 1 already
        # reports those, and firing both would print the same session twice.
        will_threshold_alert = (
            (seats is not None)
            and (seats > SEATS_AVAILABLE_THRESHOLD)
            and not already_alerted
            and not is_new
        )
        if will_threshold_alert:
            threshold_alerts.append(info)

        # Condition 3: seats jumped up by SEATS_INCREASE_THRESHOLD+ since last check
        # (can re-trigger on subsequent checks if seats keep climbing)
        if seats is not None and prev_seats is not None:
            increase = seats - prev_seats
            if increase >= SEATS_INCREASE_THRESHOLD:
                increase_alerts.append({**info, "increase": increase, "prev_seats": prev_seats})

        updated_state[session_id] = {
            "date": info["date"],
            "time": info["time"],
            "seats_available": seats,
            # A new session has been announced once already, so don't let the
            # threshold condition announce it a second time later on.
            "alerted": already_alerted or will_threshold_alert or is_new,
            "last_seen": today,
        }

    if new_alerts:
        print("\n" + "=" * 60)
        print("🚨  NEW SESSION(S) JUST DROPPED  🚨")
        for info in sorted(new_alerts, key=lambda i: (i["date"], i["data_time"])):
            print(f"  Date: {info['date']}")
            print(f"  Time: {info['time']}")
            print(f"  Seats available: {info['seats_available']}")
            print(f"  Book: {info['url']}")
            print("-" * 40)
        print("=" * 60 + "\n")

    if threshold_alerts:
        print("\n" + "=" * 60)
        print(f"🎬  SESSION(S) WITH >{SEATS_AVAILABLE_THRESHOLD} SEATS AVAILABLE  🎬")
        for info in sorted(threshold_alerts, key=lambda i: (i["date"], i["data_time"])):
            print(f"  Date: {info['date']}")
            print(f"  Time: {info['time']}")
            print(f"  Seats available: {info['seats_available']}")
            print(f"  Book: {info['url']}")
            print("-" * 40)
        print("=" * 60 + "\n")

    if increase_alerts:
        print("\n" + "=" * 60)
        print(f"🔔  SEATS OPENED UP (+{SEATS_INCREASE_THRESHOLD} OR MORE) — POSSIBLE CANCELLATIONS  🔔")
        for info in sorted(increase_alerts, key=lambda i: (i["date"], i["data_time"])):
            print(f"  Date: {info['date']}")
            print(f"  Time: {info['time']}")
            print(f"  Seats available: {info['prev_seats']} -> {info['seats_available']} (+{info['increase']})")
            print(f"  Book: {info['url']}")
            print("-" * 40)
        print("=" * 60 + "\n")

    any_alerts = bool(new_alerts or threshold_alerts or increase_alerts)

    if is_baseline:
        log(f"Baseline saved: {len(current)} session(s) recorded. New-session "
            f"alerts start from the next check.")
    elif not any_alerts:
        log(f"No new alerts. ({len(current)} session(s) on site, "
            f"{len(updated_state)} known, threshold={SEATS_AVAILABLE_THRESHOLD}, "
            f"increase>={SEATS_INCREASE_THRESHOLD}.)")

    play_alert_sound(any_alerts)

    if any_alerts:
        counts = []
        if new_alerts:
            counts.append(f"{len(new_alerts)} new")
        if threshold_alerts:
            counts.append(f"{len(threshold_alerts)} roomy")
        if increase_alerts:
            counts.append(f"{len(increase_alerts)} opened up")
        title = "Odyssey IMAX: " + ", ".join(counts)
        body = format_push_body(new_alerts, threshold_alerts, increase_alerts)
        first = sorted(new_alerts or threshold_alerts or increase_alerts,
                       key=lambda i: (i["date"], i["data_time"]))[0]

        if not send_push(title, body, click_url=first["url"]):
            # State is deliberately NOT saved. Saving it would mark these
            # sessions as alerted and the alert would be lost for good; leaving
            # it means the next check re-detects them and tries again. A
            # duplicate alert is a far better failure than a missed ticket.
            log("ERROR: could not send push notification -- leaving state "
                "unsaved so this alert is retried on the next check.")
            return

    save_state(prune_state(updated_state))


def main():
    if "--test-push" in sys.argv:
        if not NTFY_TOPIC:
            log("NTFY_TOPIC is not set, so there's nothing to test. Set it and re-run.")
            sys.exit(1)
        log(f"Sending a test push to {NTFY_SERVER}/<topic>...")
        ok = send_push("Odyssey IMAX: test", "If you can read this, push alerts work.",
                       click_url=BASE_URL)
        log("Test push sent." if ok else "Test push FAILED -- see the attempts above.")
        sys.exit(0 if ok else 1)

    log("Starting Odyssey IMAX Sydney ticket checker.")
    log(f"Watching: {BASE_URL} (clicking through every date tab present on the page)")
    log(f"Push notifications: {'on' if NTFY_TOPIC else 'off (NTFY_TOPIC not set)'}")

    # --once runs a single check and exits, for schedulers that invoke the
    # script themselves (GitHub Actions cron, systemd timers, Task Scheduler)
    # rather than leaving it running.
    if "--once" in sys.argv:
        log("Single check (--once).\n")
        check_once()
        return

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
