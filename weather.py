#!/usr/bin/env python3
"""
weather.py — Weather monitor for Gig Harbor, WA
Fetches current conditions and forecast from the National Weather Service,
displays in a terminal dashboard with automatic refresh.
"""

import curses
import json
import math
import os
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

# National Weather Service API (https://www.weather.gov/documentation/services-web-api)
# Free, no key; NWS asks for a User-Agent identifying the app.
NWS_POINTS_ENDPOINT = "https://api.weather.gov/points/{lat},{lon}"
NWS_HEADERS = {"User-Agent": "gh-weather (fbonds@gmail.com)", "Accept": "application/geo+json"}
NWS_LAT = 47.3293         # Gig Harbor, WA
NWS_LON = -122.5804
BASE_INTERVAL = 60

# Open-Meteo (https://open-meteo.com/) — free, no API key. NWS publishes no UV
# index, so UV comes from here; cloud cover is a fallback for when the station
# reports no cloud layers.
OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# Air quality from Open-Meteo (https://open-meteo.com/en/docs/air-quality-api)
# — free, no API key. EPA AirNow was the original source, but it retired its
# lat/long observation and forecast web services on 2026-10-01 (they now return
# HTTP 410). Open-Meteo reports the US EPA AQI plus a sub-index per pollutant.
# Values are modeled (CAMS), not read from a single nearby monitor.
AQI_ENDPOINT = "https://air-quality-api.open-meteo.com/v1/air-quality"
AQI_LAT = 47.3293         # Gig Harbor, WA
AQI_LON = -122.5804
AQI_INTERVAL = 600        # model updates hourly; poll every 10 minutes
AQI_STALE_AFTER = 7200    # keep showing the last reading this long when fetches fail

# Per-pollutant US AQI sub-indices -> display name. The overall AQI is the
# highest of these; the one that sets it is the "dominant" pollutant.
AQI_POLLUTANTS = {
    "us_aqi_pm2_5": "PM2.5",
    "us_aqi_pm10": "PM10",
    "us_aqi_ozone": "O3",
    "us_aqi_nitrogen_dioxide": "NO2",
    "us_aqi_carbon_monoxide": "CO",
    "us_aqi_sulphur_dioxide": "SO2",
}

# Short reason shown as a pill under the AQI gauge, derived from the dominant
# pollutant. Set AQI_REASON to force a fixed label instead:
#   export AQI_REASON="Wildfires in WA"
AQI_REASON = os.environ.get("AQI_REASON", "").strip() or None
AQI_REASON_BY_POLLUTANT = {
    "PM2.5": "Fine particles",
    "PM10": "Coarse particles",
    "O3": "Elevated ozone",
    "NO2": "Nitrogen dioxide",
    "CO": "Carbon monoxide",
    "SO2": "Sulfur dioxide",
}

# The pill explains why air quality is degraded, so it is only meaningful once
# the reading actually is. Index into AQI_CATEGORIES: 1 == Moderate or worse.
AQI_REASON_MIN_INDEX = 1

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

# The /points lookup (forecast URL + nearby stations) never changes for a fixed
# location, so it's resolved once and reused.
_NWS_POINT = {}

_COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
               "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# METAR cloud-layer amounts -> approximate sky cover percent.
_CLOUD_PCT = {"SKC": 0, "CLR": 0, "NCD": 0, "FEW": 20, "SCT": 40, "BKN": 75,
              "OVC": 100, "VV": 100}


def _nws_get(url):
    resp = requests.get(url, headers=NWS_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _nws_value(props, key):
    """Numeric value of an NWS quantity field, or None when missing/null."""
    return (props.get(key) or {}).get("value")


def _fmt(value, fmt="{:.0f}"):
    return "--" if value is None else fmt.format(value)


def compute_sun_times(lat, lon, day):
    """
    Local sunrise/sunset as '07:12 AM' strings (NOAA solar equations).
    NWS doesn't publish these, so they're calculated for the location.
    """
    try:
        n = day.timetuple().tm_yday
        g = 2 * math.pi / 365 * (n - 1)
        eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                           - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
        decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
                - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
                - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
        phi = math.radians(lat)
        ha = math.degrees(math.acos(
            math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
            - math.tan(phi) * math.tan(decl)))
        utc_offset_min = (datetime.now().astimezone().utcoffset().total_seconds()) / 60
        midnight = datetime(day.year, day.month, day.day)

        def at(minutes_utc):
            return (midnight + timedelta(minutes=minutes_utc + utc_offset_min)).strftime("%I:%M %p")

        return at(720 - 4 * (lon + ha) - eqtime), at(720 - 4 * (lon - ha) - eqtime)
    except (ValueError, ZeroDivisionError):
        return "--", "--"


def fetch_open_meteo():
    """
    UV index (and cloud cover as a backstop) from Open-Meteo. Best effort: any
    failure returns an empty dict so the panel still draws without these.
    """
    try:
        resp = requests.get(OPEN_METEO_ENDPOINT, params={
            "latitude": NWS_LAT,
            "longitude": NWS_LON,
            "current": "uv_index,cloud_cover",
            "timezone": "America/Los_Angeles",
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("current") or {}
    except Exception:
        return {}


def fetch_conditions():
    """
    Pull current conditions (nearest reporting NWS station) and the hourly
    forecast from api.weather.gov, reshaped into the dict layout the renderer
    expects: {"current_condition": [...], "weather": [{"astronomy", "hourly"}]}.
    """
    try:
        if not _NWS_POINT:
            point = _nws_get(NWS_POINTS_ENDPOINT.format(lat=NWS_LAT, lon=NWS_LON))["properties"]
            stations = _nws_get(point["observationStations"])["features"]
            _NWS_POINT["forecast_hourly"] = point["forecastHourly"]
            _NWS_POINT["stations"] = [s["properties"]["stationIdentifier"] for s in stations[:4]]

        # Nearest station first; fall through if it hasn't reported a temperature.
        obs = None
        for station in _NWS_POINT["stations"]:
            props = _nws_get(
                f"https://api.weather.gov/stations/{station}/observations/latest"
            )["properties"]
            if _nws_value(props, "temperature") is not None:
                obs = props
                break
        if obs is None:
            return None, "No station reporting"

        c_to_f = lambda c: None if c is None else c * 9 / 5 + 32
        temp_f = c_to_f(_nws_value(obs, "temperature"))
        feels_c = _nws_value(obs, "heatIndex")
        if feels_c is None:
            feels_c = _nws_value(obs, "windChill")
        feels_f = c_to_f(feels_c) if feels_c is not None else temp_f

        wind_kmh = _nws_value(obs, "windSpeed")
        wind_deg = _nws_value(obs, "windDirection")
        pressure_pa = _nws_value(obs, "barometricPressure")
        vis_m = _nws_value(obs, "visibility")
        precip_mm = _nws_value(obs, "precipitationLastHour")

        # Station cloud layers are observed but coarse (FEW/SCT/BKN/OVC); when
        # the station reports none at all, fall back to Open-Meteo's percentage.
        extra = fetch_open_meteo()
        layers = obs.get("cloudLayers") or []
        cloud = max((_CLOUD_PCT.get(l.get("amount"), 0) for l in layers), default=None)
        if cloud is None:
            cloud = extra.get("cloud_cover")
        uv = extra.get("uv_index")

        current = {
            "temp_F": _fmt(temp_f),
            "FeelsLikeF": _fmt(feels_f),
            "humidity": _fmt(_nws_value(obs, "relativeHumidity")),
            "windspeedMiles": _fmt(None if wind_kmh is None else wind_kmh / 1.609344),
            "winddir16Point": "" if wind_deg is None else _COMPASS_16[int((wind_deg + 11.25) // 22.5) % 16],
            "pressureInches": _fmt(None if pressure_pa is None else pressure_pa / 3386.389, "{:.2f}"),
            "visibilityMiles": _fmt(None if vis_m is None else vis_m / 1609.344),
            "cloudcover": _fmt(cloud),
            "uvIndex": _fmt(uv, "{:.1f}"),   # Open-Meteo; NWS doesn't publish UV
            "precipInches": _fmt(0.0 if precip_mm is None else precip_mm / 25.4, "{:.1f}"),
            "weatherDesc": [{"value": obs.get("textDescription") or "--"}],
        }

        # Hourly forecast is a nice-to-have; conditions still show without it.
        hourly = []
        try:
            periods = _nws_get(_NWS_POINT["forecast_hourly"])["properties"]["periods"]
            for p in periods[:24]:
                start = datetime.fromisoformat(p["startTime"])
                temp = p.get("temperature")
                if p.get("temperatureUnit") == "C" and temp is not None:
                    temp = round(temp * 9 / 5 + 32)
                hourly.append({"time": str(start.hour * 100), "tempF": str(temp)})
        except (requests.exceptions.RequestException, KeyError, ValueError):
            pass

        sunrise, sunset = compute_sun_times(NWS_LAT, NWS_LON, datetime.now())
        data = {
            "current_condition": [current],
            "weather": [{"astronomy": [{"sunrise": sunrise, "sunset": sunset}],
                         "hourly": hourly}],
        }
        return data, None
    except requests.exceptions.Timeout:
        return None, "Request timeout"
    except requests.exceptions.RequestException as e:
        return None, f"Network error: {str(e)[:30]}"
    except (json.JSONDecodeError, ValueError):
        return None, "Invalid JSON response"
    except (KeyError, IndexError, TypeError):
        return None, "Invalid response structure"
    except Exception as e:
        return None, f"Error: {str(e)[:30]}"


def fetch_air_quality():
    """
    Pull the current US AQI from Open-Meteo for the configured location, with
    the dominant (highest sub-index) pollutant. Returns (result, error); the
    error is a short diagnostic for logs, never shown in the UI.
    """
    try:
        resp = requests.get(AQI_ENDPOINT, params={
            "latitude": AQI_LAT,
            "longitude": AQI_LON,
            "current": ",".join(["us_aqi", *AQI_POLLUTANTS]),
            "timezone": "America/Los_Angeles",
        }, timeout=10)
        resp.raise_for_status()
        current = resp.json().get("current") or {}

        aqi = current.get("us_aqi")
        if aqi is None:
            return None, "no AQI data"

        readings = {name: current[key] for key, name in AQI_POLLUTANTS.items()
                    if current.get(key) is not None}
        dominant = max(readings, key=readings.get) if readings else ""
        idx = _aqi_index_by_value(aqi)
        result = {
            "aqi": aqi,
            "parameter": dominant,
            "category": AQI_CATEGORIES[idx][0],
            "readings": readings,
            "reason": AQI_REASON_BY_POLLUTANT.get(dominant),
            "fetched": time.time(),
        }
        return result, None
    except requests.exceptions.Timeout:
        return None, "AQI timeout"
    except requests.exceptions.RequestException as e:
        return None, f"AQI net error: {e}"
    except (json.JSONDecodeError, ValueError):
        return None, "AQI bad JSON"
    except Exception as e:
        return None, f"AQI error: {e}"


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

# Six EPA AQI categories: (full name, short label, low, high). The gauge
# arc is drawn as six equal segments in this order, left (low) to right (high).
AQI_CATEGORIES = [
    ("Good", "Good", 0, 50),
    ("Moderate", "Moderate", 51, 100),
    ("Unhealthy for Sensitive Groups", "Sensitive", 101, 150),
    ("Unhealthy", "Unhealthy", 151, 200),
    ("Very Unhealthy", "Very Unhealthy", 201, 300),
    ("Hazardous", "Hazardous", 301, 500),
]

# Set in main() once curses knows how many colors the terminal has. When the
# terminal supports 256 colors we can render true orange/maroon; otherwise we
# approximate with the basic 8-color palette.
HAVE_256 = False


def _aqi_index_by_value(value):
    """Category index (0..5) for a numeric AQI value."""
    for i, (_full, _short, _lo, hi) in enumerate(AQI_CATEGORIES):
        if value <= hi:
            return i
    return len(AQI_CATEGORIES) - 1


def _aqi_index_by_name(name):
    """Category index (0..5) for an AQI category name, or -1 if unknown."""
    c = (name or "").lower()
    if "very unhealthy" in c:
        return 4
    if "hazardous" in c:
        return 5
    if "sensitive" in c:
        return 2
    if "unhealthy" in c:
        return 3
    if "moderate" in c:
        return 1
    if "good" in c:
        return 0
    return -1


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


def _center(stdscr, text, row, region_left, region_w, attr):
    """Center a string within a region and draw it."""
    if len(text) > region_w:
        text = text[:region_w]
    col = region_left + max(0, (region_w - len(text)) // 2)
    safe_addstr(stdscr, row, col, text, attr)


# Dynamically-allocated fg/bg color pairs for the half-block gauge canvas.
# Fixed pairs 1-8 are reserved by main(); we allocate from 16 upward.
_PAIR_CACHE = {}
_PAIR_NEXT = [16]


def _pair_for(fg, bg):
    """curses.color_pair() for an (fg, bg) combo, allocating on first use."""
    if not curses.has_colors():
        return 0
    key = (fg, bg)
    n = _PAIR_CACHE.get(key)
    if n is None:
        n = _PAIR_NEXT[0]
        if n >= min(curses.COLOR_PAIRS, 256):
            return curses.color_pair(0)
        try:
            curses.init_pair(n, fg, bg)
        except curses.error:
            return curses.color_pair(0)
        _PAIR_CACHE[key] = n
        _PAIR_NEXT[0] = n + 1
    return curses.color_pair(n)


def _aqi_palette():
    """Color numbers for the gauge: true 16/256-color when available."""
    if HAVE_256:
        return {
            "field": 17, "frame": 39, "head": 231, "ink": 16, "white": 231,
            "needle": 253, "hub": 231, "pill_bg": 88, "pill_fg": 231,
            "seg": [46, 226, 208, 196, 129, 88],   # green..maroon
        }
    return {
        "field": curses.COLOR_BLACK, "frame": curses.COLOR_CYAN,
        "head": curses.COLOR_WHITE, "ink": curses.COLOR_BLACK,
        "white": curses.COLOR_WHITE, "needle": curses.COLOR_WHITE,
        "hub": curses.COLOR_WHITE, "pill_bg": curses.COLOR_RED,
        "pill_fg": curses.COLOR_WHITE,
        "seg": [curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_YELLOW,
                curses.COLOR_RED, curses.COLOR_MAGENTA, curses.COLOR_RED],
    }


def _blit(stdscr, top, left, canvas, field):
    """
    Draw a pixel canvas using half-block glyphs: each character cell stacks
    two vertical pixels (top = foreground, bottom = background of '▀').
    A pixel value of -1 means the field/background color.
    """
    Hp = len(canvas)
    Wp = len(canvas[0]) if Hp else 0
    for y in range(0, Hp, 2):
        r = top + y // 2
        for x in range(Wp):
            t = canvas[y][x]
            b = canvas[y + 1][x] if y + 1 < Hp else -1
            t = field if t == -1 else t
            b = field if b == -1 else b
            if t == b:
                safe_addstr(stdscr, r, left + x, "█", _pair_for(t, field))
            else:
                safe_addstr(stdscr, r, left + x, "▀", _pair_for(t, b))


# Per-category color ramps (dark rim, base, bright crown) — 256-color only.
# Used to bevel the number plate.
_AQI_RAMP = {
    0: (22, 34, 46),      # green
    1: (136, 184, 226),   # yellow
    2: (130, 172, 214),   # orange
    3: (88, 160, 203),    # red
    4: (54, 97, 140),     # purple
    5: (52, 88, 124),     # maroon
}


def _rgb256(r, g, b):
    """Nearest xterm-256 color-cube index for an RGB triple."""
    lv = (0, 95, 135, 175, 215, 255)
    q = lambda v: min(range(6), key=lambda i: abs(lv[i] - v))
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


# Category colors positioned at their true AQI value (range midpoints), so a
# linear 0-500 scale shows each color where that AQI actually falls.
_AQI_SCALE = [(25, (0, 210, 0)), (75, (240, 220, 0)), (125, (255, 130, 0)),
              (175, (230, 0, 0)), (250, (150, 50, 190)), (400, (120, 20, 20))]


def _aqi_scale_rgb(v):
    """Color for an AQI value on a linear 0-500 scale -> (r,g,b)."""
    if v <= _AQI_SCALE[0][0]:
        return _AQI_SCALE[0][1]
    if v >= _AQI_SCALE[-1][0]:
        return _AQI_SCALE[-1][1]
    for i in range(len(_AQI_SCALE) - 1):
        v0, c0 = _AQI_SCALE[i]
        v1, c1 = _AQI_SCALE[i + 1]
        if v0 <= v <= v1:
            f = (v - v0) / (v1 - v0)
            return tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
    return _AQI_SCALE[-1][1]


# ---------------------------------------------------------------------------
# Theme: a rich ANSI palette used across the whole interface (BBS-door feel).
# Populated once by init_theme(); THEME holds ready-to-use curses attributes.
# ---------------------------------------------------------------------------
THEME = {}


def init_theme():
    """Build the color theme. Uses 256-color when available, 16-color else."""
    global THEME
    if not curses.has_colors():
        mono = {k: curses.A_BOLD for k in (
            "border", "title", "clock", "label", "value", "unit", "accent",
            "section", "divider", "warn", "rule", "logo_shadow")}
        mono.update(value=curses.A_BOLD, unit=curses.A_NORMAL,
                    clock=curses.A_DIM, panel_bg=-1, panel_bg_pair=0,
                    logo=[curses.A_BOLD] * len(HEADER_ART),
                    raw={"screen_bg": -1, "shadow": -1, "bar_lo": 0,
                         "bar_mid": 0, "bar_hi": 0, "bar_empty": 0})
        THEME = mono
        return

    if HAVE_256:
        P = dict(screen_bg=233, panel_bg=17, border=45, shadow=234,
                 title=220, clock=87, label=111, value=231, unit=245,
                 accent=48, section=87, divider=24, warn=203, rule=24,
                 bar_lo=48, bar_mid=226, bar_hi=203, bar_empty=239,
                 logo=[51, 51, 45, 45, 39, 39], logo_shadow=234)
    else:
        C = curses
        P = dict(screen_bg=C.COLOR_BLACK, panel_bg=C.COLOR_BLUE,
                 border=C.COLOR_CYAN, shadow=C.COLOR_BLACK, title=C.COLOR_YELLOW,
                 clock=C.COLOR_CYAN, label=C.COLOR_CYAN, value=C.COLOR_WHITE,
                 unit=C.COLOR_WHITE, accent=C.COLOR_GREEN, section=C.COLOR_CYAN,
                 divider=C.COLOR_BLUE, warn=C.COLOR_RED, rule=C.COLOR_BLUE,
                 bar_lo=C.COLOR_GREEN, bar_mid=C.COLOR_YELLOW, bar_hi=C.COLOR_RED,
                 bar_empty=C.COLOR_BLACK,
                 logo=[C.COLOR_CYAN] * len(HEADER_ART), logo_shadow=C.COLOR_BLACK)

    pb, sb, B = P["panel_bg"], P["screen_bg"], curses.A_BOLD
    THEME = {
        "panel_bg": pb,
        "panel_bg_pair": _pair_for(pb, pb),
        "screen": _pair_for(P["unit"], sb),
        "border": _pair_for(P["border"], pb) | B,
        "shadow": _pair_for(P["shadow"], sb),
        "title": _pair_for(P["title"], pb) | B,
        "clock": _pair_for(P["clock"], pb),
        "label": _pair_for(P["label"], pb),
        "value": _pair_for(P["value"], pb) | B,
        "unit": _pair_for(P["unit"], pb),
        "accent": _pair_for(P["accent"], pb) | B,
        "section": _pair_for(P["section"], pb) | B,
        "divider": _pair_for(P["divider"], pb),
        "warn": _pair_for(P["warn"], pb) | B,
        "rule": _pair_for(P["rule"], sb) | B,
        "logo": [_pair_for(c, sb) | B for c in P["logo"]],
        "logo_shadow": _pair_for(P["logo_shadow"], sb),
        "raw": P,
    }


def _num(s):
    """Parse a leading number from a string, or None."""
    try:
        return float(str(s).strip().split()[0])
    except (ValueError, IndexError):
        return None


def _panel_row(stdscr, row, left, width, border, bgpair):
    """Draw one interior panel row: left border, filled background, right border."""
    safe_addstr(stdscr, row, left, "║", border)
    safe_addstr(stdscr, row, left + 1, " " * (width - 2), bgpair)
    safe_addstr(stdscr, row, left + width - 1, "║", border)


def _panel_div(stdscr, row, left, width, border, lch="╠", rch="╣", fill="═"):
    """Draw a horizontal panel divider/border line."""
    safe_addstr(stdscr, row, left, lch + fill * (width - 2) + rch, border)


def _draw_shadow(stdscr, top, left, width, height):
    """Drop shadow one cell right and below a panel, for depth."""
    sh = THEME.get("shadow", 0)
    for r in range(top + 1, top + height + 1):
        safe_addstr(stdscr, r, left + width, "█", sh)
    safe_addstr(stdscr, top + height, left + 1, "█" * width, sh)


def draw_meter(stdscr, row, col, width, frac, pb):
    """Draw a colored bar meter: filled blocks tinted green->yellow->red."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    P = THEME["raw"]
    c = P["bar_lo"] if frac < 0.5 else (P["bar_mid"] if frac < 0.8 else P["bar_hi"])
    safe_addstr(stdscr, row, col, "▐", _pair_for(P["bar_empty"], pb))
    safe_addstr(stdscr, row, col + 1, "█" * filled, _pair_for(c, pb) | curses.A_BOLD)
    safe_addstr(stdscr, row, col + 1 + filled, "░" * (width - filled),
                _pair_for(P["bar_empty"], pb))
    safe_addstr(stdscr, row, col + 1 + width, "▌", _pair_for(P["bar_empty"], pb))


# ---------------------------------------------------------------------------
# Pacific Northwest hero scene + block-letter title (the "door" splash)
# ---------------------------------------------------------------------------

# Compact 5-row block font (only the glyphs needed for the title).
_BLOCK_FONT = {
    "G": [" ███ ", "█    ", "█  ██", "█   █", " ███ "],
    "I": ["███", " █ ", " █ ", " █ ", "███"],
    "H": ["█   █", "█   █", "█████", "█   █", "█   █"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "R": ["████ ", "█   █", "████ ", "█ ██ ", "█   █"],
    "B": ["████ ", "█   █", "████ ", "█   █", "████ "],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def _block_width(text):
    return sum(len(_BLOCK_FONT.get(c, _BLOCK_FONT[" "])[0]) + 1 for c in text) - 1


def draw_block_text(stdscr, text, top, left, ramp, shadow):
    """Draw 5-row block letters. The gradient runs top->bottom (by row) so it
    is identical for every letter, reading as one unified title treatment."""
    # Shadow first (offset +1,+1), then the bright letters over it.
    for pass_shadow in (True, False):
        x = 0
        for c in text:
            glyph = _BLOCK_FONT.get(c, _BLOCK_FONT[" "])
            gw = len(glyph[0])
            for gy, gline in enumerate(glyph):
                col = ramp[min(len(ramp) - 1, gy)]
                for gx, ch in enumerate(gline):
                    if ch != "█":
                        continue
                    if pass_shadow:
                        safe_addstr(stdscr, top + gy + 1, left + x + gx + 1, "█", shadow)
                    else:
                        safe_addstr(stdscr, top + gy, left + x + gx, "█", col)
            x += gw + 1


# Scene palettes (xterm-256), sampled top -> bottom.
_SKY = [17, 18, 19, 60, 61, 97, 132, 168, 174, 211, 216, 222, 223, 230]
_WATER = [45, 38, 32, 31, 25, 24, 23, 17]


def _hero_canvas(Wp, Hp):
    """Build a PNW dawn scene: sky (title space), a horizon strip of snow-
    capped peaks and evergreens over the Sound, a sun, stars and a sailboat."""
    horizon = int(Hp * 0.80)         # water line, low so the sky frames the title
    cv = [[0] * Wp for _ in range(Hp)]

    for y in range(Hp):
        if y < horizon:
            # Power curve keeps the upper sky deep/dark (behind the title) and
            # compresses the warm dawn colors into the band near the horizon.
            f = (y / max(1, horizon - 1)) ** 2.1
            base = _SKY[min(len(_SKY) - 1, int(f * (len(_SKY) - 1)))]
        else:
            f = (y - horizon) / max(1, Hp - horizon - 1)
            base = _WATER[min(len(_WATER) - 1, int(f * (len(_WATER) - 1)))]
        for x in range(Wp):
            cv[y][x] = base

    # Stars in the upper night sky (deterministic so they don't twinkle).
    for x in range(Wp):
        if (x * 7) % 23 == 0:
            yy = 1 + (x * 13) % max(1, horizon // 3)
            if 0 <= yy < Hp:
                cv[yy][x] = 254

    # Rising sun with a soft glow, low on the right over the water.
    sun_x, sun_y, sun_r = Wp * 0.78, horizon - 2.0, 2.2
    for y in range(Hp):
        for x in range(Wp):
            d = math.hypot(x - sun_x, (y - sun_y) * 1.7)
            if d < sun_r:
                cv[y][x] = 223
            elif d < sun_r + 1.5 and y < horizon:
                cv[y][x] = 216

    # Snow-capped range along the horizon (kept low so it clears the title).
    peaks = [(Wp * 0.34, Hp * 0.20, Wp * 0.15),
             (Wp * 0.54, Hp * 0.14, Wp * 0.10),
             (Wp * 0.14, Hp * 0.12, Wp * 0.09)]
    for x in range(Wp):
        h = max((hp * math.exp(-((x - cxp) / wp_) ** 2) for cxp, hp, wp_ in peaks),
                default=0)
        mtop = int(horizon - h)
        for y in range(max(0, mtop), horizon):
            depth = (y - mtop) / max(1, horizon - mtop)
            if depth < 0.34:
                cv[y][x] = 255
            elif depth < 0.48:
                cv[y][x] = 251
            else:
                cv[y][x] = 60 if x < Wp * 0.5 else 59

    # Foreground evergreen ridge with pointed tops.
    for x in range(Wp):
        n = 0.5 + 0.35 * math.sin(x * 0.5) + 0.25 * math.sin(x * 1.6 + 1.0)
        ttop = int(horizon - 2 - n * 2.5)
        for y in range(max(0, ttop), horizon):
            dd = (y - ttop) / max(1, horizon - ttop)
            cv[y][x] = 22 if dd < 0.5 else 23

    # Water: bright horizon line + a sun-reflection shimmer column.
    for x in range(Wp):
        if (x + horizon) % 6 != 0:
            cv[horizon][x] = 195
    for y in range(horizon, Hp):
        if y % 2 == 0:
            for x in range(Wp):
                if abs(x - sun_x) < 1.6:
                    cv[y][x] = 222

    # A little sailboat on the Sound.
    bx, by = int(Wp * 0.28), horizon + 1
    for x in range(bx - 2, bx + 3):
        if 0 <= x < Wp and by < Hp:
            cv[by][x] = 233
    for i in range(3):
        for x in range(bx, bx + (3 - i)):
            yy = by - 1 - i
            if 0 <= yy < Hp and 0 <= x < Wp:
                cv[yy][x] = 231
    return cv


def draw_hero(stdscr, top, left, width, hcells, title, subtitle):
    """Blit the scene, then overlay the block title and subtitle."""
    cv = _hero_canvas(width, hcells * 2)
    _blit(stdscr, top, left, cv, 17)
    # One vertical gradient (bright crown -> warm amber) shared by every letter.
    ramp = [_pair_for(c, -1) | curses.A_BOLD for c in (231, 230, 223, 220, 214)] \
        if HAVE_256 else [curses.color_pair(3) | curses.A_BOLD]
    shadow = _pair_for(233, -1) if HAVE_256 else curses.A_DIM
    tw = _block_width(title)
    draw_block_text(stdscr, title, top + 1, left + (width - tw) // 2, ramp, shadow)
    sub_attr = _pair_for(223, 17) | curses.A_BOLD if HAVE_256 else curses.color_pair(3)
    _center(stdscr, subtitle, top + 6, left, width, sub_attr)


def _draw_aqi_plate(stdscr, pt, cleft, cw, text, dark, base, crown, ink):
    """Beveled number plate (3 rows) centered in the column."""
    plate_w = len(text) + 6
    pl = cleft + (cw - plate_w) // 2
    safe_addstr(stdscr, pt, pl, "▀" * plate_w, _pair_for(crown, base))
    safe_addstr(stdscr, pt + 1, pl, " " * plate_w, _pair_for(base, base))
    _center(stdscr, text, pt + 1, cleft, cw, _pair_for(ink, base) | curses.A_BOLD)
    safe_addstr(stdscr, pt + 2, pl, "▄" * plate_w, _pair_for(dark, base))


# Greys for the "no data" ghost gauge (256-color).
_AQI_GHOST = (236, 239, 242)   # plate dark rim, base, crown
_AQI_DIM_TEXT = 245


def _draw_aqi_unavailable(stdscr, top, cleft, cw, field, pal, height, retry_at):
    """
    Placeholder when there's no reading: the usual plate and scale drawn as a
    greyed-out "ghost" so the layout doesn't jump, plus a plain-language note.
    Raw errors stay out of the UI.
    """
    if HAVE_256:
        dark, base, crown = _AQI_GHOST
        dim, bar_hi, bar_lo = _AQI_DIM_TEXT, 240, 237
    else:
        dark = base = crown = bar_hi = bar_lo = curses.COLOR_BLACK
        dim = pal["white"]

    pt = top + 2
    _draw_aqi_plate(stdscr, pt, cleft, cw, "--", dark, base, crown, dim)
    _center(stdscr, "AQI", pt + 3, cleft, cw, _pair_for(dim, field) | curses.A_BOLD)

    bar_top = pt + 5
    barW = min(cw - 2, 26)
    bl = cleft + (cw - barW) // 2
    safe_addstr(stdscr, bar_top, bl, "█" * barW, _pair_for(bar_hi, field))
    safe_addstr(stdscr, bar_top + 1, bl, "█" * barW, _pair_for(bar_lo, field))

    lines = [("Air quality unavailable", pal["white"], curses.A_BOLD),
             ("Can't reach the data source", dim, 0)]
    if retry_at:
        lines.append((f"Retrying at {retry_at.strftime('%H:%M')}", dim, 0))
    row = bar_top + 3
    for text, color, extra in lines:
        if row >= top + height:
            break
        _center(stdscr, text, row, cleft, cw, _pair_for(color, field) | extra)
        row += 1


def draw_aqi_content(stdscr, top, cleft, cw, oright, border, aqi, aqi_error, reason, height,
                     aqi_retry=None):
    """Air-quality column content (no frame): fills its own background and
    right border each row, then draws the number plate and linear scale."""
    pal = _aqi_palette()
    field = THEME.get("panel_bg", pal["field"]) if THEME else pal["field"]
    seg = pal["seg"]
    fieldpair = _pair_for(field, field)
    for r in range(top, top + height):
        safe_addstr(stdscr, r, cleft, " " * cw, fieldpair)
        safe_addstr(stdscr, r, oright, "║", border)

    _center(stdscr, "AIR QUALITY", top, cleft, cw, _pair_for(pal["head"], field) | curses.A_BOLD)

    value = None
    category = ""
    if aqi:
        category = aqi.get("category", "")
        try:
            value = int(float(aqi.get("aqi")))
        except (TypeError, ValueError):
            value = None
    level_idx = _aqi_index_by_name(category) if value is not None else -1

    if value is None:
        _draw_aqi_unavailable(stdscr, top, cleft, cw, field, pal, height, aqi_retry)
        return

    if HAVE_256 and level_idx >= 0:
        dark, base, crown = _AQI_RAMP[level_idx]
    else:
        dark = base = crown = seg[max(0, level_idx)]
    ink = pal["ink"] if level_idx in (0, 1, 2) else pal["white"]

    pt = top + 2
    _draw_aqi_plate(stdscr, pt, cleft, cw, str(value), dark, base, crown, ink)
    _center(stdscr, "AQI", pt + 3, cleft, cw, _pair_for(crown, field) | curses.A_BOLD)
    short = AQI_CATEGORIES[level_idx][1] if level_idx >= 0 else (category or "--")
    _center(stdscr, short.upper(), pt + 4, cleft, cw, _pair_for(crown, field) | curses.A_BOLD)
    # Latest fetch failed but an earlier reading is still fresh enough to show.
    if aqi_error and aqi.get("fetched"):
        as_of = datetime.fromtimestamp(aqi["fetched"]).strftime("%H:%M")
        _center(stdscr, f"as of {as_of}", pt + 5, cleft, cw,
                _pair_for(_AQI_DIM_TEXT if HAVE_256 else pal["white"], field))

    # Linear 0-500 scale: each column's AQI value maps to its category color,
    # with a lit top row and a shadowed bottom row for depth. barW is chosen so
    # (barW-1) is a multiple of 5 -> the six labels land on exact, evenly-
    # spaced integer columns (no rounding jitter).
    bar_top = pt + 7
    barW = min(cw - 2, 26)
    barW -= (barW - 1) % 5
    bl = cleft + (cw - barW) // 2
    for c in range(barW):
        v = (c / (barW - 1) if barW > 1 else 0.0) * 500.0
        if HAVE_256:
            r, g, b = _aqi_scale_rgb(v)
            top_c = _rgb256(r, g, b)
            dk_c = _rgb256(int(r * 0.45), int(g * 0.45), int(b * 0.45))
        else:
            top_c = dk_c = seg[_aqi_index_by_value(v)]
        safe_addstr(stdscr, bar_top, bl + c, "█", _pair_for(top_c, field))
        safe_addstr(stdscr, bar_top + 1, bl + c, "█", _pair_for(dk_c, field))

    # Pointer at the reading's true position on the 0-500 scale.
    pos = max(0.0, min(1.0, value / 500.0))
    safe_addstr(stdscr, bar_top + 2, bl + int(round(pos * (barW - 1))), "▲",
                _pair_for(pal["white"], field) | curses.A_BOLD)

    # Evenly-spaced scale labels: each number centered in a fixed 3-char field
    # on an exact tick column, so every gap between labels is identical.
    tickattr = _pair_for(245 if HAVE_256 else pal["white"], field)
    trow = bar_top + 3
    step = (barW - 1) // 5
    for i, tv in enumerate((0, 100, 200, 300, 400, 500)):
        txt = str(tv).center(3)
        s = max(cleft, min(cleft + cw - 3, bl + i * step - 1))
        safe_addstr(stdscr, trow, s, txt, tickattr)

    # Only label a cause once the reading is Moderate or worse — a "why" pill
    # under a Good reading is noise at best and alarming at worst.
    if reason and level_idx >= AQI_REASON_MIN_INDEX and top + height - 2 > trow:
        pill = _pair_for(pal["pill_fg"], pal["pill_bg"]) | curses.A_BOLD
        _center(stdscr, f"  {reason}  ", top + height - 2, cleft, cw, pill)


def render(stdscr, data, start_time, cycle_count, next_interval, sync_needed=False, error_msg=None, aqi=None, aqi_error=None, reason=None,
           aqi_retry=None):
    """Render weather dashboard within the current terminal window."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    if not THEME:
        init_theme()
    T = THEME
    border = T["border"]
    bgpair = T["panel_bg_pair"]
    pb = T["panel_bg"]
    now = datetime.now()

    # --- Geometry: one unified frame with a two-column body (BBS door).
    # full_w is kept <= max_x-2 so the right border never lands on the
    # terminal's last column (which safe_addstr cannot write). ---
    two_col = max_x >= 74
    RIGHT_IW = 32 if max_x >= 92 else (30 if max_x >= 86 else 28)
    if two_col:
        LEFT_IW = min(54, max_x - 5 - RIGHT_IW)
        full_w = LEFT_IW + RIGHT_IW + 3
    else:
        LEFT_IW = min(66, max_x - 4)
        full_w = LEFT_IW + 2
    left = max(1, (max_x - full_w) // 2)
    dv = left + 1 + LEFT_IW          # shared vertical divider column
    oright = left + full_w - 1       # outer right border column
    sw = (dv - left + 1) if two_col else full_w   # stats (left) panel width
    lx = left + 2
    vx = left + 16
    mcol = left + 26
    mwidth = max(6, min(14, (left + sw - 2) - mcol - 2))

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

    # --- Outer top border + Pacific Northwest hero scene ---
    hero_h = max(6, min(12, max_y - 22))
    safe_addstr(stdscr, 0, left, "╔" + "═" * (full_w - 2) + "╗", border)
    subtitle = now.strftime("T O D A Y    %a  %b %d    %H:%M")
    draw_hero(stdscr, 1, left + 1, full_w - 2, hero_h, "GIG HARBOR", subtitle)
    for r in range(1, hero_h + 1):
        safe_addstr(stdscr, r, left, "║", border)
        safe_addstr(stdscr, r, oright, "║", border)

    # separator between hero and the data body (T-down at the divider)
    seprow = hero_h + 1
    if two_col:
        safe_addstr(stdscr, seprow, left,
                    "╠" + "═" * (dv - left - 1) + "╦" + "═" * (oright - dv - 1) + "╣", border)
    else:
        _panel_div(stdscr, seprow, left, full_w, border)
    body_top = seprow + 1
    row = body_top
    if body_top >= max_y - 3:
        stdscr.refresh()
        return

    # --- Left column: current conditions ---
    _panel_row(stdscr, row, left, sw, border, bgpair)
    safe_addstr(stdscr, row, lx, "CURRENT CONDITIONS", T["title"])
    safe_addstr(stdscr, row, dv - 10, now.strftime("%H:%M:%S"), T["clock"])
    row += 1
    _panel_div(stdscr, row, left, sw, border, "╟", "╢", "─")
    row += 1

    hum_f, cloud_f, uv_f = _num(humidity), _num(cloud), _num(uv)
    fields = [
        ("Condition", condition, None, T["accent"]),
        ("Temp", f"{temp_f}°F  (Feels {feels_f}°F)", None, T["value"]),
        ("Humidity", f"{humidity}%", (hum_f / 100.0) if hum_f is not None else None, T["value"]),
        ("Dew Point", f"{dew_str}", None, T["value"]),
        ("Wind", f"{wind_dir} {wind_speed} mph", None, T["value"]),
        ("Pressure", f"{pressure} inHg", None, T["value"]),
        ("Visibility", f"{visibility} mi", None, T["value"]),
        ("Cloud Cover", f"{cloud}%", (cloud_f / 100.0) if cloud_f is not None else None, T["value"]),
        ("UV Index", f"{uv}", None, T["value"]),
        ("Precip", f"{precip} in", None, T["value"]),
    ]
    for label, value, frac, vattr in fields:
        _panel_row(stdscr, row, left, sw, border, bgpair)
        safe_addstr(stdscr, row, lx, f"{label}:", T["label"])
        safe_addstr(stdscr, row, vx, value, vattr)
        if frac is not None and mcol + mwidth + 2 < dv:
            draw_meter(stdscr, row, mcol, mwidth, frac, pb)
        row += 1

    if sync_needed:
        _sync_display(row % 5 - 2, vx % 7 - 3)

    _panel_div(stdscr, row, left, sw, border, "╟", "╢", "─")
    row += 1

    sunrise, sunset = parse_astronomy(data) if data else ("--", "--")
    _panel_row(stdscr, row, left, sw, border, bgpair)
    safe_addstr(stdscr, row, lx, "Sunrise:", T["section"])
    safe_addstr(stdscr, row, lx + 9, sunrise, T["value"])
    safe_addstr(stdscr, row, lx + 21, "Sunset:", T["section"])
    safe_addstr(stdscr, row, lx + 29, sunset, T["value"])
    row += 1
    _panel_row(stdscr, row, left, sw, border, bgpair)
    countdown = time_until_event(sunset)
    event_label = "sunset"
    if countdown is None:
        countdown = time_until_event(sunrise)
        event_label = "sunrise"
    if countdown:
        safe_addstr(stdscr, row, lx, f"{countdown} to {event_label}", T["clock"])
    row += 1

    hourly = parse_hourly(data) if data else []
    if hourly:
        _panel_div(stdscr, row, left, sw, border, "╟", "╢", "─")
        row += 1
        _panel_row(stdscr, row, left, sw, border, bgpair)
        safe_addstr(stdscr, row, lx, "Forecast:", T["label"])
        safe_addstr(stdscr, row, lx + 11, "  ".join(hourly), T["value"])
        row += 1

    body_bottom = row                    # first row past the body
    body_h = body_bottom - body_top

    # --- Right column: air quality (matched to the body height) ---
    if two_col:
        draw_aqi_content(stdscr, body_top, dv + 1, RIGHT_IW, oright, border,
                         aqi, aqi_error, reason, body_h, aqi_retry)
    else:
        # Narrow fallback: a compact AQI line inside the left column.
        _panel_row(stdscr, body_bottom, left, sw, border, bgpair)
        cat = aqi.get("category", "--") if aqi else ""
        txt = f"{aqi.get('aqi','--')} {cat}" if aqi else "unavailable"
        safe_addstr(stdscr, body_bottom, lx, "Air Quality:", T["label"])
        safe_addstr(stdscr, body_bottom, vx, txt, T["value"])
        body_bottom += 1

    # --- Close the columns, full-width footer, bottom border ---
    if two_col:
        safe_addstr(stdscr, body_bottom, left,
                    "╠" + "═" * (dv - left - 1) + "╩" + "═" * (oright - dv - 1) + "╣", border)
    else:
        _panel_div(stdscr, body_bottom, left, full_w, border)

    footer = body_bottom + 1
    elapsed = now - start_time
    up_h = int(elapsed.total_seconds() // 3600)
    up_m = int((elapsed.total_seconds() % 3600) // 60)
    next_str = (now + timedelta(seconds=next_interval)).strftime("%H:%M:%S")
    _panel_row(stdscr, footer, left, full_w, border, bgpair)
    if error_msg:
        safe_addstr(stdscr, footer, lx, f"⚠ {error_msg}  Retry ~{next_str}", T["warn"])
    else:
        finfo = f"Updated {now:%H:%M:%S}  ·  Next ~{next_str}  ·  Up {up_h}h{up_m:02d}m"
        safe_addstr(stdscr, footer, lx, finfo, T["clock"])
        tag = "◂ PUGET SOUND ▸"
        tcol = oright - len(tag) - 1
        if tcol > lx + len(finfo) + 2:
            safe_addstr(stdscr, footer, tcol, tag, T["accent"])
    safe_addstr(stdscr, footer + 1, left, "╚" + "═" * (full_w - 2) + "╝", border)

    stdscr.refresh()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(1000)

    global HAVE_256
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_MAGENTA, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        curses.init_pair(5, curses.COLOR_GREEN, -1)
        curses.init_pair(6, curses.COLOR_RED, -1)
        # True orange/maroon for the AQI gauge when the terminal supports it.
        HAVE_256 = curses.COLORS >= 256
        if HAVE_256:
            curses.init_pair(7, 208, -1)   # orange
            curses.init_pair(8, 88, -1)    # maroon
        init_theme()
        # Paint the whole screen with the themed background color.
        stdscr.bkgd(" ", THEME["screen"])

    start_time = datetime.now()
    cycle_count = 0
    data = None
    error_msg = None
    last_fetch = 0
    next_interval = BASE_INTERVAL
    sync_needed = False
    consecutive_failures = 0
    aqi_data = None
    aqi_error = None
    aqi_reason = AQI_REASON
    last_aqi_fetch = 0

    while True:
        now = time.time()

        # Refresh air quality on its own (slower) cadence.
        if now - last_aqi_fetch >= AQI_INTERVAL:
            aqi_result, aqi_err = fetch_air_quality()
            last_aqi_fetch = now
            if aqi_result is not None:
                aqi_data = aqi_result
                aqi_error = None
            else:
                aqi_error = aqi_err
                # A recent reading is still useful (shown with an "as of"
                # note); an old one is misleading, so drop it.
                if aqi_data and now - aqi_data["fetched"] > AQI_STALE_AFTER:
                    aqi_data = None
            # Manual override wins; otherwise label the dominant pollutant.
            aqi_reason = AQI_REASON or (aqi_data or {}).get("reason")

        # Fetch new data when interval has elapsed
        sync_needed = False
        if now - last_fetch >= next_interval:
            fetch_result, fetch_error = fetch_conditions()
            
            if fetch_result is not None:
                data = fetch_result
                error_msg = None
                consecutive_failures = 0
                last_fetch = now
                cycle_count += 1
                sync_needed = True
                
                # Derive next interval from data characteristics
                raw = json.dumps(data["current_condition"][0])
                char_sum = sum(ord(c) for c in raw) % 45
                next_interval = BASE_INTERVAL + (char_sum - 22)
                next_interval = max(38, min(next_interval, 82))
            else:
                # On failure, retry sooner with exponential backoff
                error_msg = fetch_error
                consecutive_failures += 1
                retry_delay = min(10 * (2 ** min(consecutive_failures - 1, 3)), 60)
                next_interval = retry_delay
                last_fetch = now

        # Render dashboard
        try:
            render(stdscr, data, start_time, cycle_count, next_interval,
                   sync_needed, error_msg, aqi_data, aqi_error, aqi_reason,
                   datetime.fromtimestamp(last_aqi_fetch + AQI_INTERVAL))
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
