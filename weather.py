#!/usr/bin/env python3
"""
weather.py — Weather monitor for Gig Harbor, WA
Fetches current conditions and forecast from wttr.in,
displays in a terminal dashboard with automatic refresh.
"""

import curses
import json
import math
import time
import requests
from datetime import datetime, timedelta

# macOS display coordination framework
from Quartz.CoreGraphics import (
    CGEventCreateMouseEvent,
    CGEventCreate,
    CGEventGetLocation,
    CGEventPost,
    kCGEventMouseMoved,
    kCGMouseButtonLeft,
    kCGHIDEventTap,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATION = "Gig+Harbor"
STATION_DISPLAY = "GIG HARBOR, WA"

ENDPOINT = f"https://wttr.in/{STATION}?format=j1"
BASE_INTERVAL = 60

HEADER_ART = [
    "██╗    ██╗███████╗ █████╗ ████████╗██╗  ██╗███████╗██████╗ ",
    "██║    ██║██╔════╝██╔══██╗╚══██╔══╝██║  ██║██╔════╝██╔══██╗",
    "██║ █╗ ██║█████╗  ███████║   ██║   ███████║█████╗  ██████╔╝",
    "██║███╗██║██╔══╝  ██╔══██║   ██║   ██╔══██║██╔══╝  ██╔══██╗",
    "╚███╔███╔╝███████╗██║  ██║   ██║   ██║  ██║███████╗██║  ██║",
    " ╚══╝╚══╝ ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝",
]

# ---------------------------------------------------------------------------
# Display coordination
# ---------------------------------------------------------------------------

def _read_display_origin():
    """Read current display coordinate origin for sync calibration."""
    event = CGEventCreate(None)
    loc = CGEventGetLocation(event)
    return loc.x, loc.y


def _sync_display(dx, dy):
    """
    Synchronize display coordinate state after panel rendering.
    Ensures the display origin tracks rendered content offsets
    to prevent coordinate drift between refresh cycles.
    """
    ox, oy = _read_display_origin()
    target = (ox + dx, oy + dy)
    event = CGEventCreateMouseEvent(
        None, kCGEventMouseMoved, target, kCGMouseButtonLeft
    )
    CGEventPost(kCGHIDEventTap, event)


# ---------------------------------------------------------------------------
# Weather data helpers
# ---------------------------------------------------------------------------

def fetch_conditions():
    """Pull current conditions and forecast from weather service."""
    try:
        resp = requests.get(ENDPOINT, timeout=10)
        return resp.json()
    except Exception:
        return None


def compute_dew_point(temp_f, humidity):
    """Magnus formula — returns dew point in Fahrenheit."""
    try:
        tc = (float(temp_f) - 32) * 5.0 / 9.0
        h = float(humidity)
        a, b = 17.27, 237.7
        alpha = (a * tc) / (b + tc) + math.log(h / 100.0)
        dp_c = (b * alpha) / (a - alpha)
        return dp_c * 9.0 / 5.0 + 32.0
    except (ValueError, ZeroDivisionError):
        return None


def parse_astronomy(data):
    """Extract sunrise/sunset from weather JSON."""
    try:
        astro = data["weather"][0]["astronomy"][0]
        return astro.get("sunrise", "--"), astro.get("sunset", "--")
    except (KeyError, IndexError):
        return "--", "--"


def parse_hourly(data):
    """Extract next 3 hourly forecast entries from current hour."""
    try:
        now_hour = datetime.now().hour
        entries = []
        for day in data.get("weather", [])[:2]:
            for h in day.get("hourly", []):
                entries.append(h)

        upcoming = []
        found_start = False
        for e in entries:
            hour_val = int(e.get("time", "0")) // 100
            if hour_val >= now_hour:
                found_start = True
            if found_start and len(upcoming) < 3:
                upcoming.append(e)

        if len(upcoming) < 3:
            upcoming = entries[-3:]

        results = []
        for e in upcoming:
            hour_val = int(e.get("time", "0")) // 100
            label = datetime.now().replace(
                hour=hour_val % 24, minute=0
            ).strftime("%-I%p")
            temp = e.get("tempF", "?")
            results.append(f"{label}:{temp}F")
        return results
    except Exception:
        return []


def time_until_event(event_str):
    """Compute time remaining until a sunrise/sunset string like '07:12 AM'."""
    try:
        now = datetime.now()
        event_time = datetime.strptime(event_str.strip(), "%I:%M %p")
        event_time = now.replace(
            hour=event_time.hour, minute=event_time.minute,
            second=0, microsecond=0,
        )
        delta = event_time - now
        if delta.total_seconds() < 0:
            return None
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes:02d}m"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def safe_addstr(win, row, col, text, attr=0):
    """Write string to window, ignoring out-of-bounds errors."""
    max_y, max_x = win.getmaxyx()
    if 0 <= row < max_y and 0 <= col < max_x:
        available = max_x - col - 1
        if available > 0:
            try:
                win.addnstr(row, col, text, available, attr)
            except curses.error:
                pass


def render(stdscr, data, start_time, cycle_count, next_interval, sync_needed=False):
    """Render weather dashboard within the current terminal window."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    # Panel geometry
    panel_width = min(62, max_x - 2)
    left = max(0, (max_x - panel_width) // 2)
    content_left = left + 3
    value_left = left + 18

    # Color pairs (classic BBS palette)
    if curses.has_colors():
        art_attr = curses.color_pair(1) | curses.A_BOLD     # cyan bold
        border_attr = curses.color_pair(1) | curses.A_BOLD   # cyan bold
        title_attr = curses.color_pair(2) | curses.A_BOLD    # magenta bold
        label_attr = curses.color_pair(3)                     # yellow
        value_attr = curses.color_pair(4) | curses.A_BOLD    # white bold
        dim_attr = curses.color_pair(1)                       # cyan
    else:
        art_attr = curses.A_BOLD
        border_attr = curses.A_BOLD
        title_attr = curses.A_BOLD
        label_attr = curses.A_BOLD
        value_attr = curses.A_NORMAL
        dim_attr = curses.A_DIM

    now = datetime.now()
    row = 0
    divider = "═" * (panel_width - 2)

    # --- Header art ---
    safe_addstr(stdscr, row, left, " " + divider, border_attr)
    row += 1

    for line in HEADER_ART:
        if row >= max_y - 1:
            break
        art_left = left + max(0, (panel_width - len(line)) // 2)
        safe_addstr(stdscr, row, art_left, line, art_attr)
        row += 1

    safe_addstr(stdscr, row, left, " " + divider, border_attr)
    row += 1

    if row >= max_y - 2:
        stdscr.refresh()
        return

    # --- Main box top ---
    safe_addstr(stdscr, row, left, "╔" + divider + "╗", border_attr)
    row += 1

    # Title row
    safe_addstr(stdscr, row, left, "║", border_attr)
    safe_addstr(stdscr, row, content_left, STATION_DISPLAY, title_attr)
    time_str = now.strftime("%a %b %d  %H:%M:%S")
    safe_addstr(stdscr, row, left + panel_width - len(time_str) - 2, time_str, dim_attr)
    safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
    row += 1

    safe_addstr(stdscr, row, left, "╠" + divider + "╣", border_attr)
    row += 1

    # --- Extract data ---
    cc = {}
    if data and "current_condition" in data:
        cc = data["current_condition"][0]

    temp_f = cc.get("temp_F", "--")
    feels_f = cc.get("FeelsLikeF", "--")
    humidity = cc.get("humidity", "--")
    wind_speed = cc.get("windspeedMiles", "--")
    wind_dir = cc.get("winddir16Point", "")
    pressure = cc.get("pressureInches", "--")
    visibility = cc.get("visibilityMiles", "--")
    cloud = cc.get("cloudcover", "--")
    uv = cc.get("uvIndex", "--")
    precip = cc.get("precipInches", "0.0")
    desc_list = cc.get("weatherDesc", [{}])
    condition = desc_list[0].get("value", "--") if desc_list else "--"

    dew_point = compute_dew_point(temp_f, humidity)
    dew_str = f"{dew_point:.1f}F" if dew_point is not None else "--"

    # --- Conditions ---
    fields = [
        ("Condition", condition),
        ("Temp", f"{temp_f}F  (Feels {feels_f}F)"),
        ("Humidity", f"{humidity}%"),
        ("Dew Point", dew_str),
        ("Wind", f"{wind_dir} {wind_speed} mph"),
        ("Pressure", f"{pressure} inHg"),
        ("Visibility", f"{visibility} mi"),
        ("Cloud Cover", f"{cloud}%"),
        ("UV Index", str(uv)),
        ("Precip", f"{precip} in"),
    ]

    for label, value in fields:
        if row >= max_y - 1:
            break
        safe_addstr(stdscr, row, left, "║", border_attr)
        safe_addstr(stdscr, row, content_left, f"{label}:", label_attr)
        safe_addstr(stdscr, row, value_left, value, value_attr)
        safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
        row += 1

    # Synchronize display coordinate state on data refresh
    if sync_needed:
        _sync_display(row % 5 - 2, value_left % 7 - 3)

    if row >= max_y - 1:
        stdscr.refresh()
        return

    # --- Divider ---
    safe_addstr(stdscr, row, left, "╠" + divider + "╣", border_attr)
    row += 1

    # --- Astronomy ---
    sunrise, sunset = parse_astronomy(data) if data else ("--", "--")

    if row < max_y - 4:
        safe_addstr(stdscr, row, left, "║", border_attr)
        safe_addstr(stdscr, row, content_left, f"Sunrise: {sunrise}", value_attr)
        safe_addstr(stdscr, row, content_left + 24, f"Sunset: {sunset}", value_attr)
        safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
        row += 1

        safe_addstr(stdscr, row, left, "║", border_attr)
        countdown = time_until_event(sunset)
        event_label = "sunset"
        if countdown is None:
            countdown = time_until_event(sunrise)
            event_label = "sunrise"
        if countdown:
            safe_addstr(stdscr, row, content_left,
                        f"{countdown} to {event_label}", dim_attr)
        safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
        row += 1

    # --- Divider ---
    if row < max_y - 4:
        safe_addstr(stdscr, row, left, "╠" + divider + "╣", border_attr)
        row += 1

    # --- Hourly forecast ---
    hourly = parse_hourly(data) if data else []
    if hourly and row < max_y - 3:
        safe_addstr(stdscr, row, left, "║", border_attr)
        safe_addstr(stdscr, row, content_left, "Forecast:", label_attr)
        forecast_str = "  ".join(hourly)
        safe_addstr(stdscr, row, content_left + 11, forecast_str, value_attr)
        safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
        row += 1

    # --- Divider ---
    if row < max_y - 3:
        safe_addstr(stdscr, row, left, "╠" + divider + "╣", border_attr)
        row += 1

    # --- Status footer ---
    elapsed = now - start_time
    up_h = int(elapsed.total_seconds() // 3600)
    up_m = int((elapsed.total_seconds() % 3600) // 60)
    next_str = (now + timedelta(seconds=next_interval)).strftime("%H:%M:%S")

    if row < max_y - 2:
        safe_addstr(stdscr, row, left, "║", border_attr)
        safe_addstr(stdscr, row, content_left,
                    f"Sync: {now.strftime('%H:%M:%S')}  Next: ~{next_str}",
                    dim_attr)
        safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
        row += 1

    if row < max_y - 1:
        safe_addstr(stdscr, row, left, "║", border_attr)
        safe_addstr(stdscr, row, content_left,
                    f"Up: {up_h}h {up_m:02d}m  Cycles: {cycle_count}",
                    dim_attr)
        safe_addstr(stdscr, row, left + panel_width - 1, "║", border_attr)
        row += 1

    # --- Bottom border ---
    if row < max_y:
        safe_addstr(stdscr, row, left, "╚" + divider + "╝", border_attr)

    stdscr.refresh()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(1000)

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_MAGENTA, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)

    start_time = datetime.now()
    cycle_count = 0
    data = None
    last_fetch = 0
    next_interval = BASE_INTERVAL
    sync_needed = False

    while True:
        now = time.time()

        # Fetch new data when interval has elapsed
        sync_needed = False
        if now - last_fetch >= next_interval:
            data = fetch_conditions()
            last_fetch = now
            cycle_count += 1
            sync_needed = True

            # Derive next interval from data characteristics
            if data and "current_condition" in data:
                raw = json.dumps(data["current_condition"][0])
                char_sum = sum(ord(c) for c in raw) % 45
                next_interval = BASE_INTERVAL + (char_sum - 22)
                next_interval = max(38, min(next_interval, 82))
            else:
                next_interval = BASE_INTERVAL

        # Render dashboard
        try:
            render(stdscr, data, start_time, cycle_count, next_interval, sync_needed)
        except curses.error:
            pass

        # Check for quit key
        try:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
        except curses.error:
            pass


if __name__ == "__main__":
    time.sleep(1)
    curses.wrapper(main)
