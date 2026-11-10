#!/usr/bin/env python3
"""
Glass Calendar: a translucent "next 7 days" desktop widget for Windows 10/11.

- Agenda for the coming week, grouped by day, with a day strip on top.
- Hover an event for a details flyout (time, duration, location, organiser,
  notes, meeting link). Click an event that has a link to open or join it.
- Fluent dark styling: translucent panel, your Windows accent colour,
  Segoe UI Variable, rounded corners and soft shadows.
- Reads any iCalendar (.ics) feed (Google Calendar, Outlook / Microsoft 365,
  iCloud, university timetables) or local .ics files. Shows demo events
  until you add one.

Run:  python calendar_widget.py    (shows errors in the console)
      pythonw calendar_widget.py   (no console window)
Right-click the widget or its tray icon for options. Settings: settings.json.
"""
from __future__ import annotations

import html
import json
import logging
import logging.handlers
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import (QDate, QEasingCurve, QFileSystemWatcher, QLocale, QLockFile, QObject,
                            QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer,
                            QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication,
                           QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap,
                           QPolygonF)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QMenu,
                               QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

try:
    from shiboken6 import isValid as qt_alive
except Exception:  # pragma: no cover
    def qt_alive(obj):
        return obj is not None

try:
    import icalendar
    import recurring_ical_events
except ImportError:  # demo mode still works without them
    icalendar = recurring_ical_events = None

APP_NAME = "Glass Calendar"
APP_ID = "GlassCalendarWidget"
APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
CACHE_PATH = APP_DIR / "calendar_cache.json"
IS_WINDOWS = sys.platform == "win32"
SHADOW = 16                     # transparent margin around the panel for its soft shadow


# ─────────────────────────────── logging ───────────────────────────────
def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("glass_calendar")
    logger.setLevel(logging.INFO)
    for target in (APP_DIR / "widget.log", Path(tempfile.gettempdir()) / "glass_calendar.log"):
        try:
            handler = logging.handlers.RotatingFileHandler(target, maxBytes=256_000, backupCount=1,
                                                           encoding="utf-8")
        except OSError:
            continue
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        logger.addHandler(handler)
        break
    return logger


log = _setup_logging()


def _log_unhandled(exc_type, exc, tb):
    log.error("Unhandled error", exc_info=(exc_type, exc, tb))
    if sys.stderr:
        sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _log_unhandled

# ─────────────────────────────── settings ───────────────────────────────
DEFAULT_SETTINGS: dict = {
    "calendars": [],
    "_calendar_example": {"name": "Personal", "color": "#60CDFF",
                          "url": "https://calendar.google.com/calendar/ical/.../basic.ics"},
    "days_ahead": 7,
    "refresh_minutes": 15,
    "time_format": "auto",
    "width": 340,
    "max_height": 620,
    "opacity": 0.80,
    "accent": "auto",
    "layer": "desktop",
    "locked": False,
    "blur": False,
    "show_empty_days": True,
    "position": None,
}
PALETTE = ["#60CDFF", "#B4A0FF", "#6CCB5F", "#FFB86B", "#FF8FB1", "#4FD1C5", "#F7D774", "#FF7B72"]


class Settings:
    """settings.json next to this script. Unknown keys are kept as-is."""

    def __init__(self, path: Path):
        self.path = path
        self.data = dict(DEFAULT_SETTINGS)
        self.error = ""
        self.last_written = ""
        self.load()

    def load(self):
        self.error = ""
        if not self.path.exists():
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("the file must contain a JSON object")
            self.data = {**DEFAULT_SETTINGS, **raw}
        except Exception as exc:
            self.error = f"settings.json could not be read ({exc}). Using the last good settings."
            log.warning(self.error)

    def save(self):
        try:
            text = json.dumps(self.data, indent=2, ensure_ascii=False)
            self.path.write_text(text, encoding="utf-8")
            self.last_written = text
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    def __getitem__(self, key):
        return self.data.get(key, DEFAULT_SETTINGS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def sources(self) -> list[dict]:
        cals = self["calendars"]
        out = []
        for item in cals if isinstance(cals, list) else []:
            if isinstance(item, str):
                item = {"url": item}
            if isinstance(item, dict) and item.get("enabled", True) and (item.get("url") or item.get("path")):
                out.append(item)
        return out


def clamp_num(value, lo, hi, default):
    try:
        return max(lo, min(hi, type(default)(value)))
    except (TypeError, ValueError):
        return default


# ─────────────────────────────── time helpers ───────────────────────────────
def now_local() -> datetime:
    return datetime.now().astimezone()


def today_local() -> date:
    return now_local().date()


def local_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min).astimezone()


def fmt_minutes(mins: int) -> str:
    if mins < 60:
        return f"{mins} min"
    days, rem = divmod(mins, 1440)
    hours, minutes = divmod(rem, 60)
    parts = [f"{days} day{'s' if days != 1 else ''}"] if days else []
    if hours:
        parts.append(f"{hours} h")
    if minutes and not days:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def fmt_duration(td: timedelta) -> str:
    mins = round(td.total_seconds() / 60)
    return fmt_minutes(mins) if mins > 0 else ""


def fmt_until(td: timedelta) -> str:
    return fmt_minutes(max(1, math.ceil(td.total_seconds() / 60)))


class Fmt:
    """Locale-aware date and time text that follows your Windows region settings."""

    def __init__(self, time_format: str = "auto"):
        self.loc = QLocale.system()
        choice = str(time_format).lower()
        if choice in ("24h", "24"):
            self.h24 = True
        elif choice in ("12h", "12"):
            self.h24 = False
        else:
            self.h24 = "H" in self.loc.timeFormat(QLocale.ShortFormat)
        long_fmt = self.loc.dateFormat(QLocale.LongFormat)
        day = re.search(r"(?<!d)d{1,2}(?!d)", long_fmt)
        month = re.search(r"M{3,4}", long_fmt)
        self.month_first = bool(day and month and month.start() < day.start())

    def time(self, dt: datetime) -> str:
        return dt.strftime("%H:%M") if self.h24 else dt.strftime("%I:%M %p").lstrip("0")

    def weekday(self, d: date, short: bool = False) -> str:
        return self.loc.dayName(QDate(d.year, d.month, d.day).dayOfWeek(),
                                QLocale.ShortFormat if short else QLocale.LongFormat)

    def day_month(self, d: date, short: bool = False) -> str:
        m = "MMM" if short else "MMMM"
        return self.loc.toString(QDate(d.year, d.month, d.day), f"{m} d" if self.month_first else f"d {m}")

    def long_date(self, d: date) -> str:
        return f"{self.weekday(d)}{', ' if self.month_first else ' '}{self.day_month(d)}"

    def short_date(self, d: date) -> str:
        return f"{self.weekday(d, True)}{', ' if self.month_first else ' '}{self.day_month(d, True)}"


# ─────────────────────────────── events ───────────────────────────────
@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    description: str = ""
    calendar: str = ""
    color: str = PALETTE[0]
    organizer: str = ""
    link: str = ""
    recurring: bool = False
    uid: str = ""

    def key(self):
        return (self.uid or self.title, self.start.isoformat(), self.calendar)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        names = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in names}
        d["start"], d["end"] = datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"])
        return cls(**d)


MEETING_HOSTS = ("teams.microsoft.com", "teams.live.com", "zoom.us", "meet.google.com", "webex.com",
                 "whereby.com", "gotomeeting.com", "chime.aws")
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+", re.I)


def clean_inline(value) -> str:
    if value is None:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(value)))
    return re.sub(r"\s+", " ", text).strip()


def clean_block(value, limit: int = 600) -> str:
    """Tidy an event description: strip HTML, invite boilerplate rules and very long URLs."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    text = re.sub(r"<?(https?://([^/\s>]+)[^\s>]*)>?",
                  lambda m: m.group(1) if len(m.group(1)) <= 48 else f"({m.group(2)})", text)
    lines: list[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
        if line and re.fullmatch(r"[_\-–—=~*.·•]{4,}", line):
            continue                       # separator rows in Teams / Outlook invites
        if line or (lines and lines[-1]):
            lines.append(line)
    text = "\n".join(lines).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def find_link(*candidates) -> str:
    found: list[str] = []
    for c in candidates:
        if c:
            found += [u.rstrip(".,;") for u in _URL_RE.findall(str(c))]
    for url in found:
        if any(h in url.lower() for h in MEETING_HOSTS):
            return url
    return found[0] if found else ""


def soft_wrap(text: str) -> str:
    """Let long tokens (URLs, e-mail addresses) wrap inside narrow labels."""
    return re.sub(r"\S{28,}", lambda m: re.sub(r"([/?&=._@-])", "\\1\u200b", m.group(0)), text)


def _to_local(value) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return value.astimezone(), False          # floating (naive) times are taken as local
    if isinstance(value, date):
        return local_midnight(value), True
    raise TypeError(f"unsupported date value {value!r}")


def parse_ics(data: bytes, cal_name: str, color: str, start: datetime, end: datetime) -> list[Event]:
    cal = icalendar.Calendar.from_ical(data)
    try:
        query = recurring_ical_events.of(cal, skip_bad_series=True)
    except TypeError:
        query = recurring_ical_events.of(cal)
    events: list[Event] = []
    series = {str(c.get("UID")) for c in cal.walk("VEVENT") if c.get("RRULE") or c.get("RDATE")}
    # query a slightly wider window, then filter precisely in local time
    for comp in query.between((start - timedelta(days=1)).date(), (end + timedelta(days=1)).date()):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED" or comp.get("DTSTART") is None:
            continue
        raw_start = comp.get("DTSTART").dt
        ev_start, all_day = _to_local(raw_start)
        raw_end = comp.get("DTEND").dt if comp.get("DTEND") is not None else None
        duration = comp.get("DURATION").dt if comp.get("DURATION") is not None else None
        if all_day:
            if isinstance(raw_end, date) and not isinstance(raw_end, datetime):
                end_day = raw_end
            elif duration is not None:
                end_day = raw_start + timedelta(days=max(1, duration.days))
            else:
                end_day = raw_start + timedelta(days=1)
            ev_end = local_midnight(max(end_day, raw_start + timedelta(days=1)))
        elif raw_end is not None:
            ev_end = _to_local(raw_end)[0]
        else:
            ev_end = ev_start + (duration or timedelta(0))
        ev_end = max(ev_end, ev_start)
        if ev_end == ev_start:
            if not start <= ev_start < end:
                continue
        elif not (ev_start < end and ev_end > start):
            continue
        org = comp.get("ORGANIZER")
        if isinstance(org, list):
            org = org[0] if org else None
        organizer = ""
        if org is not None:
            cn = getattr(org, "params", {}).get("CN")
            organizer = str(cn).strip('"') if cn else re.sub(r"(?i)^mailto:", "", str(org))
        location = clean_inline(comp.get("LOCATION"))
        description = comp.get("DESCRIPTION")
        events.append(Event(
            title=clean_inline(comp.get("SUMMARY")) or "(No title)",
            start=ev_start, end=ev_end, all_day=all_day,
            location=location, description=clean_block(description),
            calendar=cal_name, color=color, organizer=organizer,
            link=find_link(comp.get("URL"), comp.get("X-MICROSOFT-SKYPETEAMSMEETINGURL"),
                           comp.get("X-GOOGLE-CONFERENCE"), location, description),
            recurring=str(comp.get("UID", "")) in series,
            uid=str(comp.get("UID", "")),
        ))
    return events


def fetch_ics(source: dict) -> bytes:
    url = str(source.get("url") or "").strip()
    if url:
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 GlassCalendar/1.0",
                                    "Accept": "text/calendar, */*"})
        with urlopen(req, timeout=25) as resp:
            return resp.read()
    path = Path(os.path.expandvars(os.path.expanduser(str(source.get("path") or "").strip())))
    return path.read_bytes()


def short_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"the server answered HTTP {exc.code}. Check the link."
    if isinstance(exc, URLError):
        return f"no connection ({exc.reason})."
    if isinstance(exc, FileNotFoundError):
        return "the file was not found."
    if isinstance(exc, (TimeoutError, OSError)) and "timed out" in str(exc):
        return "the request timed out."
    return (str(exc).strip() or exc.__class__.__name__)[:140]


class CalendarLoader(QObject):
    """Fetches and parses every calendar on a background thread."""
    loaded = Signal(object, object)          # events, [(calendar name, message)]

    def __init__(self):
        super().__init__()
        self.busy = False

    def start(self, sources: list[dict], days: int) -> bool:
        if self.busy:
            return False
        self.busy = True
        threading.Thread(target=self._work, args=(sources, days), daemon=True).start()
        return True

    def _work(self, sources, days):
        events, errors = [], []
        start = local_midnight(today_local())
        end = local_midnight(today_local() + timedelta(days=days))
        if icalendar is None:
            errors.append(("Setup", "run: pip install icalendar recurring-ical-events tzdata"))
        else:
            for i, src in enumerate(sources):
                name = str(src.get("name") or f"Calendar {i + 1}")
                color = str(src.get("color") or PALETTE[i % len(PALETTE)])
                try:
                    events += parse_ics(fetch_ics(src), name, color, start, end)
                except Exception as exc:
                    log.warning("Loading %s failed", name, exc_info=True)
                    errors.append((name, short_error(exc)))
        self.busy = False
        self.loaded.emit(events, errors)


def dedupe(events: list[Event]) -> list[Event]:
    seen, out = set(), []
    for ev in events:
        if ev.key() not in seen:
            seen.add(ev.key())
            out.append(ev)
    return out


def sources_key(sources: list[dict]) -> str:
    return json.dumps([[s.get("name"), s.get("url"), s.get("path"), s.get("color")] for s in sources])


def load_cache(key: str):
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("sources") != key:
            return [], None
        updated = datetime.fromisoformat(data["updated"]) if data.get("updated") else None
        return [Event.from_dict(d) for d in data.get("events", [])], updated
    except Exception:
        return [], None


def save_cache(key: str, events: list[Event], updated: datetime | None):
    try:
        CACHE_PATH.write_text(json.dumps({"sources": key, "updated": updated.isoformat() if updated else None,
                                          "events": [e.to_dict() for e in events]}), encoding="utf-8")
    except OSError:
        pass


def demo_events(days: int) -> list[Event]:
    today = today_local()
    work, uni, me, social = (("Work", "#60CDFF"), ("University", "#B4A0FF"),
                             ("Personal", "#6CCB5F"), ("Social", "#FFB86B"))

    def at(offset, hh, mm, minutes, title, cal, **kw):
        s = datetime.combine(today + timedelta(days=offset), time(hh, mm)).astimezone()
        return Event(title=title, start=s, end=s + timedelta(minutes=minutes),
                     calendar=cal[0], color=cal[1], **kw)

    def allday(offset, n, title, cal, **kw):
        return Event(title=title, start=local_midnight(today + timedelta(days=offset)),
                     end=local_midnight(today + timedelta(days=offset + n)), all_day=True,
                     calendar=cal[0], color=cal[1], **kw)

    evs = [
        at(0, 8, 0, 45, "Morning run", me, location="Riverside path",
           description="Easy 5 km. Keep the pace conversational."),
        at(0, 9, 30, 15, "Team stand-up", work, organizer="Priya Shah", recurring=True,
           link="https://teams.microsoft.com/l/meetup-join/demo", location="Microsoft Teams",
           description="Daily check-in: what you did yesterday, what's next, anything blocking you."),
        at(0, 11, 0, 120, "Algorithms lecture", uni, location="Lecture Theatre 2", organizer="Dr Reid",
           recurring=True, description="Week 7: graph traversal (BFS, DFS) and topological sort. "
                                       "Bring last week's problem sheet."),
        at(0, 17, 30, 60, "Gym: upper body", me, location="City Fitness"),
        allday(1, 1, "Coursework deadline", uni,
               description="Submit the report and source code on the module page before 23:59."),
        at(1, 14, 0, 120, "Lab session", uni, location="Lab 3, Computing Building", recurring=True),
        at(1, 19, 0, 120, "Dinner with Sam", social, location="Bella Napoli, Grey Street"),
        at(2, 10, 0, 30, "1:1 with Jordan", work, organizer="Jordan Lee",
           link="https://meet.google.com/abc-defg-hij", description="Monthly catch-up on goals and feedback."),
        at(2, 15, 0, 45, "Coffee with Alex", social, location="Grind Café"),
        at(4, 13, 0, 60, "Project sync", work, recurring=True, link="https://zoom.us/j/1234567890",
           description="Demo the new search page, then agree on next sprint's scope."),
        at(4, 18, 30, 60, "Five-a-side football", social, location="Leisure Centre, pitch 2"),
        allday(5, 2, "Weekend away", me, location="Seaside cottage",
               description="Train leaves at 08:15. Pack walking boots."),
        at(6, 12, 0, 30, "Call home", me, link="https://meet.google.com/xyz-abcd-efg"),
    ]
    return [e for e in evs if (e.start.date() - today).days < days]


# ─────────────────────────────── Windows integration ───────────────────────────────
if IS_WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes

    class _AccentPolicy(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class _CompositionData(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


class Win:
    """Small, failure-tolerant wrappers around Windows APIs. All are no-ops elsewhere."""
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @staticmethod
    def build() -> int:
        try:
            return sys.getwindowsversion().build
        except AttributeError:
            return 0

    @staticmethod
    def accent() -> QColor | None:
        if not IS_WINDOWS:
            return None
        try:   # AccentPalette holds 8 RGBA swatches; #1 is the light tint Windows uses in dark mode
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as k:
                pal, _ = winreg.QueryValueEx(k, "AccentPalette")
                if pal and len(pal) >= 8:
                    return QColor(pal[4], pal[5], pal[6])
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as k:
                v, _ = winreg.QueryValueEx(k, "AccentColor")       # 0xAABBGGRR
                return QColor(v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF).lighter(150)
        except OSError:
            return None

    @staticmethod
    def set_blur(hwnd: int, enabled: bool):
        if not IS_WINDOWS:
            return
        try:
            accent = _AccentPolicy(3 if enabled else 0, 0, 0, 0)   # ACCENT_ENABLE_BLURBEHIND / DISABLED
            data = _CompositionData(19, ctypes.addressof(accent), ctypes.sizeof(accent))  # WCA_ACCENT_POLICY
            ctypes.windll.user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))
            if enabled and Win.build() >= 22000:                    # Windows 11: round the blurred region
                pref = ctypes.c_int(2)                              # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(pref),
                                                           ctypes.sizeof(pref))
        except Exception:
            log.warning("Blur not available", exc_info=True)

    @staticmethod
    def pin_to_desktop(hwnd: int, pinned: bool):
        """Make the desktop own the window, so it stays on the wallpaper and survives Win+D."""
        if not IS_WINDOWS:
            return
        try:
            user32 = ctypes.windll.user32
            user32.FindWindowW.restype = wintypes.HWND
            owner = user32.FindWindowW("Progman", None) if pinned else None
            setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            setter(wintypes.HWND(hwnd), -8, owner or 0)            # GWLP_HWNDPARENT = owner
        except Exception:
            log.warning("Could not pin to desktop", exc_info=True)

    @staticmethod
    def autostart_enabled() -> bool:
        if not IS_WINDOWS:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, Win.RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_ID)
                return True
        except OSError:
            return False

    @staticmethod
    def set_autostart(enabled: bool):
        exe = Path(sys.executable)
        if exe.with_name("pythonw.exe").exists():
            exe = exe.with_name("pythonw.exe")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, Win.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, f'"{exe}" "{Path(__file__).resolve()}"')
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass

    @staticmethod
    def set_app_id():
        if IS_WINDOWS:
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
            except Exception:
                pass


# ─────────────────────────────── theme & painting ───────────────────────────────
class Theme:
    """Windows 11 dark-mode colour roles."""

    def __init__(self):
        self.update({})

    def update(self, s: dict):
        value = str(s.get("accent", "auto"))
        accent = QColor(value) if value.lower() != "auto" else (Win.accent() or QColor())
        self.accent = accent if accent.isValid() else QColor("#60CDFF")
        self.on_accent = QColor(0, 0, 0, 228) if self.accent.lightnessF() > 0.55 else QColor(255, 255, 255)
        self.opacity = clamp_num(s.get("opacity", 0.8), 0.35, 1.0, 0.8)
        self.panel = QColor(32, 32, 32)
        self.panel.setAlphaF(self.opacity)
        self.flyout = QColor(40, 40, 40)
        self.flyout.setAlphaF(min(0.97, self.opacity + 0.14))
        self.text = QColor(255, 255, 255)
        self.text2 = QColor(255, 255, 255, 200)
        self.text3 = QColor(255, 255, 255, 139)
        self.stroke_top = QColor(255, 255, 255, 36)
        self.stroke_bottom = QColor(255, 255, 255, 14)
        self.divider = QColor(255, 255, 255, 21)
        self.warning = QColor("#FCE100")


def css(c: QColor) -> str:
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"


def qcolor(value, fallback: str = PALETTE[0]) -> QColor:
    c = QColor(str(value)) if value else QColor()
    return c if c.isValid() else QColor(fallback)


@lru_cache(maxsize=None)
def make_font(px: int, weight: QFont.Weight = QFont.Weight.Normal, display: bool = False) -> QFont:
    f = QFont()
    first = "Segoe UI Variable Display" if display else "Segoe UI Variable Text"
    f.setFamilies([first, "Segoe UI Variable", "Segoe UI", "Selawik", "Noto Sans", "DejaVu Sans"])
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def make_label(font: QFont, color: QColor, wrap: bool = False) -> QLabel:
    lbl = QLabel()
    lbl.setTextFormat(Qt.PlainText)            # event text is never rendered as HTML
    lbl.setFont(font)
    lbl.setWordWrap(wrap)
    lbl.setStyleSheet(f"color: {css(color)}; background: transparent;")
    return lbl


def tween(owner: QWidget, attr: str, end: float, ms: int = 140):
    """Animate a float attribute on a widget, repainting on every step."""
    key = f"_tween_{attr}"
    anim = getattr(owner, key, None)
    if anim is None:
        anim = QVariantAnimation(owner)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(lambda v, o=owner, a=attr: (setattr(o, a, float(v)), o.update()))
        setattr(owner, key, anim)
    anim.stop()
    anim.setDuration(ms)
    anim.setStartValue(float(getattr(owner, attr)))
    anim.setEndValue(float(end))
    anim.start()


def paint_glass(p: QPainter, rect: QRectF, radius: float, fill: QColor, theme: Theme,
                shadow: int = 0, strength: float = 0.30):
    """Rounded translucent panel with a soft shadow, a faint top sheen and a hairline border."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    panel = QPainterPath()
    panel.addRoundedRect(rect, radius, radius)
    if shadow:
        outside = QPainterPath()
        outside.addRect(rect.adjusted(-shadow, -shadow, shadow, shadow + 4))
        p.setClipPath(outside.subtracted(panel))
        p.setBrush(Qt.NoBrush)
        for i in range(1, shadow + 1):
            t = 1.0 - i / (shadow + 1)
            p.setPen(QPen(QColor(0, 0, 0, int(255 * strength * t ** 2.2)), 1.0))
            r = rect.adjusted(-i + 0.5, -i + 2.5, i - 0.5, i + 1.5)      # light falls from above
            p.drawRoundedRect(r, radius + i * 0.9, radius + i * 0.9)
        p.setClipping(False)
    p.fillPath(panel, fill)
    sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    sheen.setColorAt(0.0, QColor(255, 255, 255, 13))
    sheen.setColorAt(0.3, QColor(255, 255, 255, 0))
    p.fillPath(panel, sheen)
    border = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    border.setColorAt(0.0, theme.stroke_top)
    border.setColorAt(1.0, theme.stroke_bottom)
    p.setPen(QPen(QBrush(border), 1.0))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)
    p.restore()


def draw_icon(p: QPainter, name: str, r: QRectF, color: QColor, width: float = 1.3):
    """Tiny line icons in the spirit of Segoe Fluent Icons, drawn as vectors."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()

    def P(fx, fy):
        return QPointF(x + fx * w, y + fy * h)

    def line(*pts):
        p.drawPolyline(QPolygonF([P(*pt) for pt in pts]))

    if name == "pin":
        rad, cx, cy = w * 0.28, x + w / 2, y + h * 0.38
        circle = QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)
        path = QPainterPath()
        path.arcMoveTo(circle, -30)
        path.arcTo(circle, -30, 240)
        path.lineTo(cx, cy + rad * 2.1)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(QPointF(cx, cy), rad * 0.36, rad * 0.36)
    elif name == "calendar":
        p.drawRoundedRect(QRectF(P(0.12, 0.2), P(0.88, 0.88)), w * 0.12, w * 0.12)
        line((0.12, 0.42), (0.88, 0.42))
        line((0.34, 0.1), (0.34, 0.28))
        line((0.66, 0.1), (0.66, 0.28))
    elif name == "person":
        p.drawEllipse(P(0.5, 0.32), w * 0.17, h * 0.17)
        p.drawArc(QRectF(P(0.18, 0.62), P(0.82, 1.18)), 0, 180 * 16)
    elif name == "repeat":
        line((0.18, 0.56), (0.18, 0.44), (0.3, 0.32), (0.82, 0.32))
        line((0.7, 0.2), (0.82, 0.32), (0.7, 0.44))
        line((0.82, 0.44), (0.82, 0.56), (0.7, 0.68), (0.18, 0.68))
        line((0.3, 0.56), (0.18, 0.68), (0.3, 0.8))
    elif name == "link":
        p.translate(r.center())
        p.rotate(-45)
        lw, lh = w * 0.48, h * 0.28
        p.drawRoundedRect(QRectF(-lw + w * 0.07, -lh / 2, lw, lh), lh / 2, lh / 2)
        p.drawRoundedRect(QRectF(-w * 0.07, -lh / 2, lw, lh), lh / 2, lh / 2)
    elif name == "video":
        p.drawRoundedRect(QRectF(P(0.08, 0.28), P(0.64, 0.72)), w * 0.08, w * 0.08)
        p.drawPolygon(QPolygonF([P(0.64, 0.45), P(0.92, 0.3), P(0.92, 0.7), P(0.64, 0.55)]))
    elif name == "refresh":
        rad, c = w * 0.34, r.center()
        p.drawArc(QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad), 90 * 16, -300 * 16)
        a = math.radians(150)
        tip = QPointF(c.x() + rad * math.cos(a), c.y() - rad * math.sin(a))
        back = (-math.sin(a), -math.cos(a))
        wings = []
        for turn in (0.75, -0.75):
            bx = back[0] * math.cos(turn) - back[1] * math.sin(turn)
            by = back[0] * math.sin(turn) + back[1] * math.cos(turn)
            wings.append(QPointF(tip.x() + bx * w * 0.24, tip.y() + by * w * 0.24))
        p.drawPolyline(QPolygonF([wings[0], tip, wings[1]]))
    elif name == "more":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        for fx in (0.22, 0.5, 0.78):
            p.drawEllipse(P(fx, 0.5), w * 0.075, w * 0.075)
    elif name == "warning":
        p.drawPolygon(QPolygonF([P(0.5, 0.1), P(0.94, 0.88), P(0.06, 0.88)]))
        line((0.5, 0.4), (0.5, 0.6))
        p.drawPoint(P(0.5, 0.75))
    p.restore()


# ─────────────────────────────── event text helpers ───────────────────────────────
def group_by_day(events: list[Event], days: list[date]) -> dict[date, list[Event]]:
    out: dict[date, list[Event]] = {d: [] for d in days}
    for d in days:
        d0, d1 = local_midnight(d), local_midnight(d + timedelta(days=1))
        for ev in events:
            hit = (d0 <= ev.start < d1) if ev.end == ev.start else (ev.start < d1 and ev.end > d0)
            if hit:
                out[d].append(ev)
        out[d].sort(key=lambda e: (0 if e.all_day else 1, e.start, e.title.lower()))
    return out


def time_labels(ev: Event, day: date, fmt: Fmt) -> tuple[str, str]:
    if ev.all_day:
        return "All day", ""
    d0, d1 = local_midnight(day), local_midnight(day + timedelta(days=1))
    starts, ends = ev.start >= d0, ev.end <= d1
    if starts and ends:
        return fmt.time(ev.start), fmt.time(ev.end) if ev.end > ev.start else ""
    if starts:
        return fmt.time(ev.start), "→"
    if ends:
        return "Until", fmt.time(ev.end)
    return "All day", ""


def secondary_text(ev: Event) -> str:
    if ev.location:
        return ev.location
    if not ev.all_day and ev.end > ev.start:
        return f"{fmt_duration(ev.end - ev.start)}, {ev.calendar}" if ev.calendar else fmt_duration(ev.end - ev.start)
    return ev.calendar


def relative_status(ev: Event, now: datetime) -> tuple[str, bool]:
    """Short status for the flyout, plus whether it deserves the accent colour."""
    if ev.all_day:
        first, last, today = ev.start.date(), (ev.end - timedelta(seconds=1)).date(), now.date()
        if first <= today <= last:
            n = (last - first).days + 1
            return ("Today, all day" if n == 1 else f"Day {(today - first).days + 1} of {n}"), True
        if today < first:
            n = (first - today).days
            return ("Tomorrow" if n == 1 else f"In {n} days"), False
        return "Ended", False
    if now < ev.start:
        delta = ev.start - now
        if delta < timedelta(minutes=1):
            return "Starting now", True
        if delta < timedelta(hours=18):
            return f"Starts in {fmt_until(delta)}", delta <= timedelta(hours=1)
        n = (ev.start.date() - now.date()).days
        return ("Tomorrow" if n == 1 else f"In {n} days"), False
    if now < ev.end:
        return f"Happening now, {fmt_until(ev.end - now)} left", True
    ago = now - ev.end
    return (f"Ended {fmt_until(ago)} ago" if ago < timedelta(hours=12) else "Ended"), False


def when_text(ev: Event, fmt: Fmt) -> str:
    if ev.all_day:
        first, last = ev.start.date(), (ev.end - timedelta(seconds=1)).date()
        if first == last:
            return f"{fmt.long_date(first)}\nAll day"
        return f"{fmt.short_date(first)} – {fmt.short_date(last)}\nAll day, {(last - first).days + 1} days"
    s, e = ev.start, ev.end
    if e <= s:
        return f"{fmt.long_date(s.date())}\n{fmt.time(s)}"
    if s.date() == e.date() or e == local_midnight(s.date() + timedelta(days=1)):
        return f"{fmt.long_date(s.date())}\n{fmt.time(s)} – {fmt.time(e)}  ({fmt_duration(e - s)})"
    return (f"{fmt.short_date(s.date())} {fmt.time(s)} – {fmt.short_date(e.date())} {fmt.time(e)}\n"
            f"{fmt_duration(e - s)}")


# ─────────────────────────────── small widgets ───────────────────────────────
class Separator(QWidget):
    def __init__(self, theme: Theme):
        super().__init__()
        self.theme = theme
        self.setFixedHeight(1)

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), self.theme.divider)


class IconButton(QAbstractButton):
    def __init__(self, icon: str, tip: str, theme: Theme):
        super().__init__()
        self.icon_name, self.theme = icon, theme
        self.hover, self.angle = 0.0, 0.0
        self.setFixedSize(30, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(900)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(lambda v: (setattr(self, "angle", float(v)), self.update()))

    def set_spinning(self, on: bool):
        if on:
            self._spin.start()
        else:
            self._spin.stop()
            self.angle = 0.0
            self.update()

    def enterEvent(self, e):
        tween(self, "hover", 1.0, 110)

    def leaveEvent(self, e):
        tween(self, "hover", 0.0, 180)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        a = 0.06 * self.hover + (0.05 if self.isDown() else 0)
        if a > 0:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(255 * a)))
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        c = QRectF(self.rect()).center()
        if self.angle:
            p.translate(c)
            p.rotate(self.angle)
            p.translate(-c)
        r = QRectF(0, 0, 16, 16)
        r.moveCenter(c)
        draw_icon(p, self.icon_name, r, self.theme.text2)


class DayStrip(QWidget):
    """Seven day cells; today sits in an accent circle, dots show that day's calendars."""
    daySelected = Signal(object)

    def __init__(self, theme: Theme, fmt: Fmt):
        super().__init__()
        self.theme, self.fmt = theme, fmt
        self.days: list[date] = []
        self.dots: dict[date, list[QColor]] = {}
        self.hover_i = -1
        self.setMouseTracking(True)
        self.setFixedHeight(62)

    def set_days(self, days, dots):
        self.days, self.dots = days[:7], dots
        self.update()

    def _index_at(self, x: float) -> int:
        if not self.days:
            return -1
        i = int(x // (self.width() / len(self.days)))
        return i if 0 <= i < len(self.days) else -1

    def mouseMoveEvent(self, e):
        i = self._index_at(e.position().x())
        if i != self.hover_i:
            self.hover_i = i
            self.update()
        e.ignore()                              # let the panel handle dragging

    def leaveEvent(self, e):
        self.hover_i = -1
        self.update()

    def on_click(self, pos: QPoint):
        i = self._index_at(pos.x())
        if i >= 0:
            self.daySelected.emit(self.days[i])

    def paintEvent(self, _):
        if not self.days:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, today = self.theme, today_local()
        cw = self.width() / len(self.days)
        for i, d in enumerate(self.days):
            cx, is_today = cw * i + cw / 2, d == today
            p.setFont(make_font(11, QFont.Weight.DemiBold if is_today else QFont.Weight.Normal))
            p.setPen(t.accent if is_today else t.text3)
            p.drawText(QRectF(cw * i, 0, cw, 16), Qt.AlignCenter, self.fmt.weekday(d, short=True))
            circle = QRectF(cx - 15, 19, 30, 30)
            if is_today or i == self.hover_i:
                p.setPen(Qt.NoPen)
                p.setBrush(t.accent if is_today else QColor(255, 255, 255, 22))
                p.drawEllipse(circle)
            p.setFont(make_font(14, QFont.Weight.DemiBold if is_today else QFont.Weight.Normal))
            p.setPen(t.on_accent if is_today else (t.text if d.weekday() < 5 else t.text2))
            p.drawText(circle, Qt.AlignCenter, str(d.day))
            cols = self.dots.get(d, [])[:3]
            x0 = cx - (len(cols) * 4 + (len(cols) - 1) * 3) / 2
            p.setPen(Qt.NoPen)
            for j, col in enumerate(cols):
                p.setBrush(col)
                p.drawEllipse(QRectF(x0 + j * 7, 55, 4, 4))


class DayHeader(QWidget):
    def __init__(self, day: date, theme: Theme, fmt: Fmt, first: bool):
        super().__init__()
        self.day, self.theme, self.fmt = day, theme, fmt
        self.setFixedHeight(26 if first else 38)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, today = self.theme, today_local()
        if self.day == today:
            title, color = "Today", t.accent
        elif self.day == today + timedelta(days=1):
            title, color = "Tomorrow", t.text
        else:
            title, color = self.fmt.weekday(self.day), t.text
        rect = QRectF(4, self.height() - 24, self.width() - 8, 20)
        f = make_font(12, QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(color)
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setFont(make_font(12))
        p.setPen(t.text3)
        p.drawText(rect.adjusted(QFontMetrics(f).horizontalAdvance(title) + 8, 0, 0, 0),
                   Qt.AlignLeft | Qt.AlignVCenter, self.fmt.day_month(self.day, short=True))


class EmptyDay(QWidget):
    def __init__(self, theme: Theme):
        super().__init__()
        self.theme = theme
        self.setFixedHeight(30)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setFont(make_font(12))
        p.setPen(self.theme.text3)
        p.drawText(QRectF(18, 0, self.width() - 18, self.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   "Nothing scheduled")


class EmptyWeek(QWidget):
    def __init__(self, theme: Theme, days: int):
        super().__init__()
        self.theme, self.days = theme, days
        self.setFixedHeight(130)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.width() / 2
        draw_icon(p, "calendar", QRectF(c - 15, 18, 30, 30), self.theme.text3, 1.6)
        p.setFont(make_font(14, QFont.Weight.DemiBold))
        p.setPen(self.theme.text)
        p.drawText(QRectF(0, 58, self.width(), 22), Qt.AlignCenter, "Your week is clear")
        p.setFont(make_font(12))
        p.setPen(self.theme.text3)
        p.drawText(QRectF(0, 82, self.width(), 20), Qt.AlignCenter, f"No events in the next {self.days} days")


class EventRow(QWidget):
    entered = Signal(object)
    left = Signal(object)

    def __init__(self, ev: Event, day: date, theme: Theme, fmt: Fmt, time_w: int):
        super().__init__()
        self.ev, self.day, self.theme, self.time_w = ev, day, theme, time_w
        self.hover = 0.0
        self.top, self.bottom = time_labels(ev, day, fmt)
        self.sub = secondary_text(ev)
        self.setFixedHeight(48)
        if ev.link:
            self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        tween(self, "hover", 1.0, 110)
        self.entered.emit(self)

    def leaveEvent(self, e):
        tween(self, "hover", 0.0, 200)
        self.left.emit(self)

    def on_click(self, _pos):
        if self.ev.link:
            QDesktopServices.openUrl(QUrl(self.ev.link))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        ev, t, now = self.ev, self.theme, now_local()
        w, h = self.width(), self.height()
        live = not ev.all_day and ev.start <= now < ev.end
        if self.hover > 0.01:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(17 * self.hover)))
            p.drawRoundedRect(QRectF(0, 1, w, h - 2), 6, 6)
        if ev.end <= now:
            p.setOpacity(0.42)                  # already over
        color = qcolor(ev.color)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(6, 9, 3, h - 18), 1.5, 1.5)
        x, tx = 18, 18 + self.time_w + 12
        p.setFont(make_font(12, QFont.Weight.DemiBold if live else QFont.Weight.Normal))
        p.setPen(t.text if live else t.text2)
        p.drawText(QRectF(x, 6, self.time_w, 20), Qt.AlignLeft | Qt.AlignVCenter, self.top)
        p.setFont(make_font(11))
        p.setPen(t.text3)
        p.drawText(QRectF(x, 24, self.time_w, 18), Qt.AlignLeft | Qt.AlignVCenter, self.bottom)
        right = w - 10
        if live:                                # "Now" pill
            pf = make_font(11, QFont.Weight.DemiBold)
            pw = QFontMetrics(pf).horizontalAdvance("Now") + 14
            pill = QRectF(right - pw, 8, pw, 18)
            bg = QColor(t.accent)
            bg.setAlpha(46)
            p.setBrush(bg)
            p.drawRoundedRect(pill, 9, 9)
            p.setFont(pf)
            p.setPen(t.accent)
            p.drawText(pill, Qt.AlignCenter, "Now")
            right = pill.left() - 8
        title_f, sub_f = make_font(13), make_font(12)
        p.setFont(title_f)
        p.setPen(t.text)
        avail = max(10, int(right - tx))
        p.drawText(QRectF(tx, 6, avail, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(title_f).elidedText(ev.title, Qt.ElideRight, avail))
        sub_w = max(10, int(w - 10 - tx))
        p.setFont(sub_f)
        p.setPen(t.text3)
        p.drawText(QRectF(tx, 24, sub_w, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(sub_f).elidedText(self.sub, Qt.ElideRight, sub_w))
        if live:                                # progress through the event
            frac = max(0.0, min(1.0, (now - ev.start) / (ev.end - ev.start)))
            track = QRectF(tx, h - 4, w - 10 - tx, 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawRoundedRect(track, 1, 1)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(track.x(), track.y(), track.width() * frac, 2), 1, 1)


SCROLL_QSS = """
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0 2px 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,46); border-radius: 3px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,96); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""


class AgendaView(QScrollArea):
    def __init__(self, theme: Theme):
        super().__init__()
        self.theme = theme
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setStyleSheet(SCROLL_QSS)
        self.viewport().setAutoFillBackground(False)
        self.body = QWidget()
        self.vbox = QVBoxLayout(self.body)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)
        self.setWidget(self.body)
        self.body.setAutoFillBackground(False)   # setWidget() switches it on
        self.headers: dict[date, QWidget] = {}
        self.rows: list[EventRow] = []
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def populate(self, days, by_day, show_empty, make_row, fmt):
        while self.vbox.count():
            w = self.vbox.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        self.headers, self.rows = {}, []

        def add(w):
            self.vbox.addWidget(w)
            w.show()          # widgets added to a visible layout otherwise appear one event-loop pass later
            return w

        if not any(by_day.values()):
            add(EmptyWeek(self.theme, len(days)))
        else:
            for d in days:
                evs = by_day.get(d, [])
                if not evs and not show_empty:
                    continue
                header = DayHeader(d, self.theme, fmt, first=not self.headers)
                add(header)
                self.headers[d] = header
                for ev in evs:
                    row = make_row(ev, d)
                    self.rows.append(row)
                    add(row)
                if not evs:
                    add(EmptyDay(self.theme))
        self.vbox.addStretch(1)
        self.verticalScrollBar().setValue(0)

    def content_height(self) -> int:
        return self.vbox.sizeHint().height()

    def scroll_to(self, d: date):
        w = self.headers.get(d)
        if w is not None:
            self._anim.stop()
            self._anim.setStartValue(self.verticalScrollBar().value())
            self._anim.setEndValue(min(w.y(), self.verticalScrollBar().maximum()))
            self._anim.start()


class Footer(QWidget):
    def __init__(self, theme: Theme):
        super().__init__()
        self.theme = theme
        self.left_text, self.right_text, self.warn = "", "", False
        self.setFixedHeight(18)

    def set(self, left: str, right: str = "", warn: bool = False, tip: str = ""):
        self.left_text, self.right_text, self.warn = left, right, warn
        self.setToolTip(tip)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = make_font(11)
        fm = QFontMetrics(f)
        p.setFont(f)
        x, w, h = 0, self.width(), self.height()
        if self.warn:
            draw_icon(p, "warning", QRectF(0, 2, 13, 13), self.theme.warning)
            x = 19
        rw = fm.horizontalAdvance(self.right_text)
        p.setPen(self.theme.text3)
        p.drawText(QRectF(w - rw, 0, rw, h), Qt.AlignRight | Qt.AlignVCenter, self.right_text)
        avail = max(10, w - x - rw - 12)
        p.setPen(self.theme.warning if self.warn else self.theme.text3)
        p.drawText(QRectF(x, 0, avail, h), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self.left_text, Qt.ElideRight, avail))


# ─────────────────────────────── details flyout ───────────────────────────────
class IconWidget(QWidget):
    def __init__(self, name: str, color: QColor, size: int = 15):
        super().__init__()
        self.name, self.color, self.size_px = name, color, size
        self.setFixedSize(size, size + 2)

    def paintEvent(self, _):
        draw_icon(QPainter(self), self.name, QRectF(0, 2, self.size_px, self.size_px), self.color)


class DetailRow(QWidget):
    def __init__(self, icon: str, text: str, theme: Theme, icon_color: QColor | None = None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(IconWidget(icon, icon_color or theme.text3), 0, Qt.AlignTop)
        label = make_label(make_font(12), theme.text2, wrap=True)
        label.setText(soft_wrap(text))
        lay.addWidget(label, 1)


class FlyoutHeader(QWidget):
    def __init__(self, theme: Theme):
        super().__init__()
        self.color = QColor(theme.accent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 0, 0)
        lay.setSpacing(3)
        self.title = make_label(make_font(15, QFont.Weight.DemiBold, display=True), theme.text, wrap=True)
        self.when = make_label(make_font(12), theme.text2, wrap=True)
        lay.addWidget(self.title)
        lay.addWidget(self.when)

    def set(self, color: QColor, title: str, when: str):
        self.color = color
        self.title.setText(soft_wrap(title))
        self.when.setText(when)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawRoundedRect(QRectF(0, 2, 4, self.height() - 4), 2, 2)


class Chip(QWidget):
    def __init__(self, theme: Theme):
        super().__init__()
        self.theme, self.text, self.hot = theme, "", False
        self.font_ = make_font(11, QFont.Weight.DemiBold)
        self.setFixedHeight(22)

    def set(self, text: str, hot: bool):
        self.text, self.hot = text, hot
        self.setFixedWidth(QFontMetrics(self.font_).horizontalAdvance(text) + 20)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(self.theme.accent) if self.hot else QColor(255, 255, 255)
        bg.setAlpha(46 if self.hot else 20)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()), 11, 11)
        p.setFont(self.font_)
        p.setPen(self.theme.accent if self.hot else self.theme.text2)
        p.drawText(QRectF(self.rect()), Qt.AlignCenter, self.text)


class EventFlyout(QWidget):
    """Click-through pop-up shown beside the widget while an event is hovered."""
    WIDTH = 300
    M = 22                                      # transparent room for the shadow

    def __init__(self, theme: Theme):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
                         | Qt.NoDropShadowWindowHint)
        self.theme = theme
        self.ev: Event | None = None
        self.hiding = False
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedWidth(self.WIDTH + 2 * self.M)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(self.M + 16, self.M + 15, self.M + 16, self.M + 16)
        lay.setSpacing(11)
        self.header = FlyoutHeader(theme)
        lay.addWidget(self.header)
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(14, 0, 0, 0)
        self.chip = Chip(theme)
        chip_row.addWidget(self.chip)
        chip_row.addStretch(1)
        lay.addLayout(chip_row)
        lay.addWidget(Separator(theme))
        self.details = QVBoxLayout()
        self.details.setSpacing(8)
        lay.addLayout(self.details)
        self.desc_sep = Separator(theme)
        lay.addWidget(self.desc_sep)
        self.desc = make_label(make_font(12), theme.text3, wrap=True)
        lay.addWidget(self.desc)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.finished.connect(self._fade_done)
        self.slide = QPropertyAnimation(self, b"pos", self)
        self.slide.setEasingCurve(QEasingCurve.OutCubic)

    def fill(self, ev: Event, fmt: Fmt):
        t = self.theme
        self.ev = ev
        self.header.set(qcolor(ev.color), ev.title, when_text(ev, fmt))
        self.chip.set(*relative_status(ev, now_local()))
        while self.details.count():
            w = self.details.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        if ev.location:
            self.details.addWidget(DetailRow("pin", ev.location, t))
        self.details.addWidget(DetailRow("calendar", ev.calendar or "Calendar", t, qcolor(ev.color)))
        if ev.organizer:
            self.details.addWidget(DetailRow("person", f"Organised by {ev.organizer}", t))
        if ev.recurring:
            self.details.addWidget(DetailRow("repeat", "Repeating event", t))
        if ev.link:
            host = urlparse(ev.link).netloc.removeprefix("www.") or ev.link
            meeting = any(h in host for h in MEETING_HOSTS)
            verb = "join the meeting" if meeting else "open the link"
            self.details.addWidget(DetailRow("video" if meeting else "link",
                                             f"{host}\nClick the event to {verb}", t))
        for i in range(self.details.count()):
            self.details.itemAt(i).widget().show()
        self.desc.setText(soft_wrap(ev.description))
        self.desc.setVisible(bool(ev.description))
        self.desc_sep.setVisible(bool(ev.description))
        self.adjustSize()

    def refresh_status(self):
        if self.ev is not None and self.isVisible():
            self.chip.set(*relative_status(self.ev, now_local()))

    def show_beside(self, row_rect: QRect, panel_rect: QRect):
        screen = QGuiApplication.screenAt(panel_rect.center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        m, gap, h = self.M, 8, self.height()
        room_right = avail.right() - panel_rect.right()
        room_left = panel_rect.left() - avail.left()
        side = 1 if room_right >= self.WIDTH + gap or room_right >= room_left else -1
        x = panel_rect.right() + 1 + gap - m if side > 0 else panel_rect.left() - gap - self.WIDTH - m
        y = row_rect.top() - m - 4
        y = max(avail.top() - m + 6, min(y, avail.bottom() - h + m - 6))
        target = QPoint(x, y)
        self.hiding = False
        self.fade.stop()
        self.slide.stop()
        if not self.isVisible():
            start = target - QPoint(10 * side, 0)
            self.move(start)
            self.setWindowOpacity(0.0)
            self.show()
            self._run(self.slide, start, target, 180)
        else:
            self._run(self.slide, self.pos(), target, 130)
        self._run(self.fade, self.windowOpacity(), 1.0, 140)

    def hide_animated(self):
        if self.isVisible() and not self.hiding:
            self.hiding = True
            self.fade.stop()
            self._run(self.fade, self.windowOpacity(), 0.0, 120)

    def hide_now(self):
        self.fade.stop()
        self.slide.stop()
        self.hiding = False
        self.hide()

    @staticmethod
    def _run(anim, start, end, ms):
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(ms)
        anim.start()

    def _fade_done(self):
        if self.hiding:
            self.hiding = False
            self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(self.M, self.M, -self.M, -self.M)
        paint_glass(p, r, 10, self.theme.flyout, self.theme, shadow=self.M - 2, strength=0.42)


# ─────────────────────────────── main panel ───────────────────────────────
class CalendarWidget(QWidget):
    def __init__(self, ctrl: "Controller"):
        super().__init__(None)
        self.ctrl, self.settings, self.theme = ctrl, ctrl.settings, ctrl.theme
        self.fmt = Fmt(self.settings["time_format"])
        self.events: list[Event] = []
        self.errors: list[tuple[str, str]] = []
        self.updated: datetime | None = None
        self.demo = self.loading = False
        self.margin, self.radius, self.blur, self.layer = SHADOW, 12, False, None
        self._press = None
        self._dragging = False
        self._pending_row: EventRow | None = None
        self._day = today_local()
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        root = QVBoxLayout(self)
        root.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(2)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.lbl_weekday = make_label(make_font(13, QFont.Weight.DemiBold), self.theme.accent)
        self.lbl_date = make_label(make_font(26, QFont.Weight.DemiBold, display=True), self.theme.text)
        titles.addWidget(self.lbl_weekday)
        titles.addWidget(self.lbl_date)
        head.addLayout(titles, 1)
        self.btn_refresh = IconButton("refresh", "Refresh now", self.theme)
        self.btn_refresh.clicked.connect(ctrl.refresh)
        self.btn_more = IconButton("more", "Options", self.theme)
        self.btn_more.clicked.connect(
            lambda: ctrl.menu.popup(self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height() + 4))))
        head.addWidget(self.btn_refresh, 0, Qt.AlignTop)
        head.addWidget(self.btn_more, 0, Qt.AlignTop)
        root.addLayout(head)
        self.strip = DayStrip(self.theme, self.fmt)
        self.strip.daySelected.connect(lambda d: self.agenda.scroll_to(d))
        root.addWidget(self.strip)
        root.addWidget(Separator(self.theme))
        self.agenda = AgendaView(self.theme)
        self.agenda.verticalScrollBar().valueChanged.connect(lambda _v: self._reset_hover())
        root.addWidget(self.agenda)
        self.footer = Footer(self.theme)
        root.addWidget(self.footer)

        self.flyout = EventFlyout(self.theme)
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(260)
        self._show_timer.timeout.connect(self._show_flyout)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(140)
        self._hide_timer.timeout.connect(self.flyout.hide_animated)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(30_000)
        self.apply_settings(first=True)

    # settings & layering ------------------------------------------------
    def apply_settings(self, first: bool = False):
        s = self.settings
        old_margin = self.margin
        self.fmt = self.strip.fmt = Fmt(s["time_format"])
        self.blur = IS_WINDOWS and bool(s["blur"])
        self.margin = 0 if self.blur else SHADOW
        self.radius = (8 if Win.build() >= 22000 else 0) if self.blur else 12
        m = self.margin
        self.layout().setContentsMargins(m + 16, m + 14, m + 16, m + 12)
        self.setFixedWidth(clamp_num(s["width"], 280, 640, 340) + 2 * m)
        self.lbl_weekday.setStyleSheet(f"color: {css(self.theme.accent)}; background: transparent;")
        if s["layer"] != self.layer:
            self._apply_layer()
        if not first:
            if old_margin != m:
                self.move(self.pos() + QPoint(old_margin - m, old_margin - m))
            self._apply_native()
            self.rebuild()

    def _apply_layer(self):
        self.layer = self.settings["layer"]
        flags = (Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                 | Qt.WindowDoesNotAcceptFocus)
        if self.layer == "top":
            flags |= Qt.WindowStaysOnTopHint
        elif self.layer == "desktop":
            flags |= Qt.WindowStaysOnBottomHint
        visible, pos = self.isVisible(), self.pos()
        self.setWindowFlags(flags)
        if visible:
            self.move(pos)
            self.show()
            QTimer.singleShot(0, self._apply_native)

    def _apply_native(self):
        if IS_WINDOWS and self.isVisible():
            hwnd = int(self.winId())
            Win.set_blur(hwnd, self.blur)
            Win.pin_to_desktop(hwnd, self.layer == "desktop")

    def reveal(self):
        self._place()
        self.setWindowOpacity(0.0)
        self.show()
        QTimer.singleShot(0, self._apply_native)
        self._reveal = QPropertyAnimation(self, b"windowOpacity", self)
        self._reveal.setDuration(320)
        self._reveal.setStartValue(0.0)
        self._reveal.setEndValue(1.0)
        self._reveal.setEasingCurve(QEasingCurve.OutCubic)
        self._reveal.start()

    def _place(self):
        m, pos = self.margin, self.settings["position"]
        if isinstance(pos, list) and len(pos) == 2:
            pt = QPoint(int(pos[0]) - m, int(pos[1]) - m)
            if any(s.availableGeometry().intersects(QRect(pt, self.size())) for s in QGuiApplication.screens()):
                self.move(pt)
                return
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.move(avail.right() - self.width() - 24 + m, avail.top() + 24 - m)

    def _keep_on_screen(self):
        m = self.margin
        panel = QRect(self.pos() + QPoint(m, m), self.size() - QSize(2 * m, 2 * m))
        screen = QGuiApplication.screenAt(panel.center()) or self.screen() or QGuiApplication.primaryScreen()
        a = screen.availableGeometry()
        x = max(a.left(), min(panel.left(), a.right() - panel.width() + 1))
        y = max(a.top(), min(panel.top(), a.bottom() - panel.height() + 1))
        self.move(x - m, y - m)

    # data ---------------------------------------------------------------
    def set_data(self, events, errors, updated, demo):
        self.events, self.errors, self.updated, self.demo = events, errors, updated, demo
        self.rebuild()

    def set_loading(self, on: bool):
        self.loading = on
        self.btn_refresh.set_spinning(on)
        self._update_footer()

    def rebuild(self):
        self._reset_hover()
        n = clamp_num(self.settings["days_ahead"], 1, 14, 7)
        days = [today_local() + timedelta(days=i) for i in range(n)]
        by_day = group_by_day(self.events, days)
        self.strip.set_days(days, {d: [qcolor(e.color) for e in evs] for d, evs in by_day.items()})
        fm = QFontMetrics(make_font(12, QFont.Weight.DemiBold))
        samples = ["00:00", "23:59"] if self.fmt.h24 else ["12:00 AM", "10:58 PM"]
        time_w = max(fm.horizontalAdvance(s) for s in samples + ["All day", "Until"]) + 2
        self.agenda.populate(days, by_day, bool(self.settings["show_empty_days"]),
                             lambda ev, d: self._make_row(ev, d, time_w), self.fmt)
        self.count = len({e.key() for evs in by_day.values() for e in evs})
        today = today_local()
        self.lbl_weekday.setText(self.fmt.weekday(today))
        self.lbl_date.setText(self.fmt.day_month(today))
        self._update_footer()
        self._fit_height()

    def _make_row(self, ev, d, time_w):
        row = EventRow(ev, d, self.theme, self.fmt, time_w)
        row.entered.connect(self._row_entered)
        row.left.connect(self._row_left)
        return row

    def _update_footer(self):
        count = getattr(self, "count", 0)
        right = f"{count} event{'s' if count != 1 else ''}"
        if self.loading:
            self.footer.set("Syncing calendars…", right)
        elif self.errors:
            tip = "\n".join(f"{name}: {msg}" for name, msg in self.errors)
            left = (f"{self.errors[0][0]}: {self.errors[0][1]}" if len(self.errors) == 1
                    else f"{len(self.errors)} calendars didn't load. Hover for details.")
            self.footer.set(left, right, warn=True, tip=tip)
        elif self.demo:
            self.footer.set("Sample events. Right-click to add yours.", right,
                            tip="Add an .ics link to \"calendars\" in settings.json")
        elif self.updated:
            when = self.fmt.time(self.updated) if self.updated.date() == today_local() \
                else self.fmt.short_date(self.updated.date())
            self.footer.set(f"Updated {when}", right)
        else:
            self.footer.set("", right)

    def _fit_height(self):
        m = self.margin
        self.agenda.setFixedHeight(0)
        self.layout().activate()
        base = self.layout().sizeHint().height()
        limit = clamp_num(self.settings["max_height"], 320, 1600, 620)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen:
            limit = min(limit, screen.availableGeometry().height() - 48)
        agenda_h = max(70, min(self.agenda.content_height(), limit + 2 * m - base))
        self.agenda.setFixedHeight(agenda_h)
        self.setFixedHeight(base + agenda_h)
        if self.isVisible():
            self._keep_on_screen()

    def _on_tick(self):
        if today_local() != self._day:
            self._day = today_local()
            self.ctrl.new_day()
            return
        for row in self.agenda.rows:
            row.update()
        self.flyout.refresh_status()

    # hover flyout ---------------------------------------------------------
    def _row_entered(self, row):
        if self._dragging:
            return
        self._hide_timer.stop()
        self._pending_row = row
        if self.flyout.isVisible() and not self.flyout.hiding:
            self._show_flyout()
        else:
            self._show_timer.start()

    def _row_left(self, row):
        if self._pending_row is row:
            self._show_timer.stop()
        self._hide_timer.start()

    def _show_flyout(self):
        row = self._pending_row
        if row is None or not qt_alive(row) or not row.isVisible():
            return
        if self.flyout.ev is not row.ev or not self.flyout.isVisible():
            self.flyout.fill(row.ev, self.fmt)
        m = self.margin
        panel = QRect(self.mapToGlobal(QPoint(m, m)), self.size() - QSize(2 * m, 2 * m))
        self.flyout.show_beside(QRect(row.mapToGlobal(QPoint(0, 0)), row.size()), panel)

    def _reset_hover(self):
        self._pending_row = None
        self._show_timer.stop()
        self._hide_timer.stop()
        self.flyout.hide_now()

    # mouse: drag anywhere, click rows and day cells -------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = (e.globalPosition().toPoint(), self.pos(), e.position().toPoint())
            self._dragging = False

    def mouseMoveEvent(self, e):
        if not self._press or not (e.buttons() & Qt.LeftButton):
            return
        delta = e.globalPosition().toPoint() - self._press[0]
        if not self._dragging and delta.manhattanLength() >= 5 and not self.settings["locked"]:
            self._dragging = True
            self._reset_hover()
        if self._dragging:
            self.move(self._press[1] + delta)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self._press:
            return
        local = self._press[2]
        if self._dragging:
            self._dragging = False
            self._keep_on_screen()
            self.ctrl.remember_position(self.pos() + QPoint(self.margin, self.margin))
        else:
            target = self.childAt(local)
            while target is not None and target is not self:
                if hasattr(target, "on_click"):
                    target.on_click(target.mapFrom(self, local))
                    break
                target = target.parentWidget()
        self._press = None

    def contextMenuEvent(self, e):
        self._reset_hover()
        self.ctrl.menu.popup(e.globalPos())

    def paintEvent(self, _):
        p = QPainter(self)
        m = self.margin
        paint_glass(p, QRectF(self.rect()).adjusted(m, m, -m, -m), self.radius, self.theme.panel,
                    self.theme, shadow=m)


# ─────────────────────────────── controller ───────────────────────────────
MENU_QSS = """
QMenu { background-color: rgba(44,44,44,250); border: 1px solid rgba(255,255,255,26);
        border-radius: 8px; padding: 4px; color: white; }
QMenu::item { padding: 7px 28px 7px 30px; border-radius: 4px; margin: 1px 0; background: transparent; }
QMenu::item:selected { background-color: rgba(255,255,255,20); }
QMenu::separator { height: 1px; background: rgba(255,255,255,22); margin: 4px 8px; }
QMenu::indicator { width: 16px; height: 16px; left: 8px; }
"""


def apply_dark_palette(app: QApplication, theme: Theme):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, QColor(32, 32, 32)), (QPalette.WindowText, theme.text),
                        (QPalette.Base, QColor(28, 28, 28)), (QPalette.Text, theme.text),
                        (QPalette.Button, QColor(45, 45, 45)), (QPalette.ButtonText, theme.text),
                        (QPalette.ToolTipBase, QColor(43, 43, 43)), (QPalette.ToolTipText, theme.text),
                        (QPalette.Highlight, theme.accent), (QPalette.HighlightedText, theme.on_accent)):
        pal.setColor(role, color)
    app.setPalette(pal)
    app.setFont(make_font(13))
    app.setStyleSheet("QToolTip { background-color: #2b2b2b; color: white; padding: 6px 8px;"
                      " border: 1px solid rgba(255,255,255,30); }")


def make_tray_icon(theme: Theme, day: int) -> QIcon:
    icon = QIcon()
    for s in (16, 20, 24, 32, 48, 64):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(s * 0.06, s * 0.1, s * 0.88, s * 0.84)
        path = QPainterPath()
        path.addRoundedRect(body, s * 0.18, s * 0.18)
        p.fillPath(path, QColor(36, 36, 36))
        p.setClipPath(path)
        p.fillRect(QRectF(body.left(), body.top(), body.width(), s * 0.22), theme.accent)
        p.setClipping(False)
        p.setFont(make_font(max(7, int(s * 0.46)), QFont.Weight.Bold))
        p.setPen(QColor(255, 255, 255))
        p.drawText(body.adjusted(0, s * 0.2, 0, 0), Qt.AlignCenter, str(day))
        p.end()
        icon.addPixmap(pm)
    return icon


class Controller(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.settings = Settings(SETTINGS_PATH)
        self.theme = Theme()
        self.theme.update(self.settings.data)
        apply_dark_palette(app, self.theme)
        self.events: list[Event] = []
        self.updated: datetime | None = None
        self.loader = CalendarLoader()
        self.loader.loaded.connect(self._on_loaded)
        self.menu = self._build_menu()
        self.widget = CalendarWidget(self)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_tray_icon(self.theme, today_local().day), self)
            self.tray.setToolTip(APP_NAME)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self._tray_clicked)
            self.tray.show()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self._restart_refresh_timer()
        self.watcher = QFileSystemWatcher([str(SETTINGS_PATH)], self)
        self.watcher.fileChanged.connect(self._settings_file_changed)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(400)
        self._reload_timer.timeout.connect(self._reload_settings)
        self._show_initial_data()
        self.widget.reveal()
        QTimer.singleShot(400, self.refresh)

    def _show_initial_data(self):
        sources = self.settings.sources()
        if sources:
            self.events, self.updated = load_cache(sources_key(sources))
            self.widget.set_data(self.events, self._settings_errors(), self.updated, demo=False)
        else:
            self.refresh()

    def _settings_errors(self):
        return [("Settings", self.settings.error)] if self.settings.error else []

    # menu & tray --------------------------------------------------------
    def _build_menu(self) -> QMenu:
        menu = QMenu()
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setStyleSheet(MENU_QSS)
        menu.setFont(make_font(13))
        menu.addAction("Refresh now").triggered.connect(self.refresh)
        menu.addSeparator()
        self.act_top = menu.addAction("Always on top")
        self.act_lock = menu.addAction("Lock position")
        self.act_blur = menu.addAction("Blur background") if IS_WINDOWS else None
        self.act_auto = menu.addAction("Start with Windows") if IS_WINDOWS else None
        for act in (self.act_top, self.act_lock, self.act_blur, self.act_auto):
            if act:
                act.setCheckable(True)
        self._sync_menu()
        self.act_top.toggled.connect(lambda on: self._set("layer", "top" if on else "desktop"))
        self.act_lock.toggled.connect(lambda on: self._set("locked", on))
        if IS_WINDOWS:
            self.act_blur.toggled.connect(lambda on: self._set("blur", on))
            self.act_auto.toggled.connect(self._set_autostart)
        menu.addSeparator()
        menu.addAction("Edit settings…").triggered.connect(self.open_settings)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self.quit)
        return menu

    def _sync_menu(self):
        pairs = [(self.act_top, self.settings["layer"] == "top"), (self.act_lock, bool(self.settings["locked"]))]
        if IS_WINDOWS:
            pairs += [(self.act_blur, bool(self.settings["blur"])), (self.act_auto, Win.autostart_enabled())]
        for act, value in pairs:
            act.blockSignals(True)
            act.setChecked(value)
            act.blockSignals(False)

    def _set(self, key, value):
        self.settings[key] = value
        self.settings.save()
        self.theme.update(self.settings.data)
        self.widget.apply_settings()

    def _set_autostart(self, on):
        try:
            Win.set_autostart(on)
        except OSError:
            log.warning("Could not change autostart", exc_info=True)
        self._sync_menu()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.widget.show()
            self.widget.raise_()              # peek above other windows until you click elsewhere

    def open_settings(self):
        if not SETTINGS_PATH.exists():
            self.settings.save()
        if IS_WINDOWS:
            try:
                subprocess.Popen(["notepad.exe", str(SETTINGS_PATH)])
                return
            except OSError:
                pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(SETTINGS_PATH)))

    def remember_position(self, panel_pos: QPoint):
        self.settings["position"] = [panel_pos.x(), panel_pos.y()]
        self.settings.save()

    def quit(self):
        self.widget.flyout.hide_now()
        if self.tray:
            self.tray.hide()
        self.app.quit()

    # data ---------------------------------------------------------------
    def refresh(self):
        days = clamp_num(self.settings["days_ahead"], 1, 14, 7)
        sources = self.settings.sources()
        if not sources:
            self.widget.set_data(demo_events(days), self._settings_errors(), now_local(), demo=True)
            return
        if self.loader.start(sources, days):
            self.widget.set_loading(True)

    def _on_loaded(self, events, errors):
        sources = self.settings.sources()
        failed = {name for name, _ in errors}
        if failed:                             # keep the last good copy of calendars that failed
            events = events + [e for e in self.events if e.calendar in failed]
        if len(failed) < max(1, len(sources)):
            self.updated = now_local()
        self.events = dedupe(events)
        save_cache(sources_key(sources), self.events, self.updated)
        self.widget.loading = False
        self.widget.btn_refresh.set_spinning(False)
        self.widget.set_data(self.events, self._settings_errors() + list(errors), self.updated, demo=False)

    def new_day(self):
        if self.tray:
            self.tray.setIcon(make_tray_icon(self.theme, today_local().day))
        self.widget.rebuild()
        self.refresh()

    def _restart_refresh_timer(self):
        self.refresh_timer.start(clamp_num(self.settings["refresh_minutes"], 1, 1440, 15) * 60_000)

    # settings.json hot reload ---------------------------------------------
    def _settings_file_changed(self, path):
        QTimer.singleShot(250, lambda: self.watcher.addPath(path) if os.path.exists(path) else None)
        self._reload_timer.start()

    def _reload_settings(self):
        try:
            if SETTINGS_PATH.read_text(encoding="utf-8-sig") == self.settings.last_written:
                return                         # our own save
        except OSError:
            return
        before = json.dumps([self.settings["calendars"], self.settings["days_ahead"]])
        self.settings.load()
        self.theme.update(self.settings.data)
        apply_dark_palette(self.app, self.theme)
        self._sync_menu()
        self._restart_refresh_timer()
        if self.tray:
            self.tray.setIcon(make_tray_icon(self.theme, today_local().day))
        self.widget.apply_settings()
        if json.dumps([self.settings["calendars"], self.settings["days_ahead"]]) != before or self.settings.error:
            self.events = []
            self.refresh()


def main() -> int:
    Win.set_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(os.path.join(tempfile.gettempdir(), "glass-calendar-widget.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        log.info("Already running, exiting.")
        return 0
    controller = Controller(app)  # noqa: F841  (keeps everything alive)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
