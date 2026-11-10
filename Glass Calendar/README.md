# Glass Calendar

A translucent, Windows 11–style desktop widget that shows your next 7 days of events. Hover an event to see its details in a flyout; click an event with a meeting link to join it.

## Set up

1. Install Python 3.10+ from python.org (tick "Add python.exe to PATH").
2. Put `calendar_widget.py` and `requirements.txt` in a folder, open a terminal there and run:

   ```
   pip install -r requirements.txt
   python calendar_widget.py
   ```

   The widget appears in the top-right corner with sample events, and creates `settings.json` next to the script.
3. Once it works, right-click the widget and tick **Start with Windows**. From then on it starts silently with `pythonw`.

## Connect your calendar

The widget reads iCalendar (.ics) links. Get yours:

- **Google Calendar:** Settings → pick your calendar under "Settings for my calendars" → Integrate calendar → copy **Secret address in iCal format**.
- **Outlook.com / Microsoft 365:** Outlook on the web → Settings → Calendar → Shared calendars → Publish a calendar → choose the calendar and "Can view all details" → Publish → copy the **ICS** link. (Some work and school accounts have publishing turned off.)
- **iCloud:** Calendar app → share icon next to the calendar → Public Calendar → copy the link (`webcal://` links work).
- **University timetable:** look for "Subscribe" or "iCal" in your timetable system, or download the `.ics` file and use `"path"` instead of `"url"`.

Then right-click the widget → **Edit settings…** and add it:

```json
"calendars": [
  { "name": "Personal", "url": "https://calendar.google.com/calendar/ical/…/basic.ics", "color": "#60CDFF" },
  { "name": "Timetable", "path": "C:/Users/you/Downloads/timetable.ics", "color": "#B4A0FF" }
]
```

Save the file and the widget reloads on its own. Treat these links like passwords: anyone with one can read that calendar.

## Using it

- **Move:** drag anywhere on the panel. The position is remembered.
- **Details:** hover an event. The flyout opens on whichever side has more room.
- **Join a meeting:** click an event that has a Teams, Zoom, Meet or other link.
- **Jump to a day:** click it in the day strip.
- **Options:** right-click the widget, click the ⋯ button, or use the tray icon (it shows today's date). Clicking the tray icon brings the widget above your windows.

## Settings reference (`settings.json`)

| Key | Default | What it does |
|---|---|---|
| `calendars` | `[]` | Your calendars. Each has `url` or `path`, plus optional `name`, `color`, `enabled`. |
| `days_ahead` | `7` | How many days to show (1–14). |
| `refresh_minutes` | `15` | How often to re-download calendars. |
| `time_format` | `"auto"` | `"auto"` follows Windows, or `"24h"` / `"12h"`. |
| `opacity` | `0.8` | Panel background opacity, 0.35–1. Text always stays solid. |
| `accent` | `"auto"` | Uses your Windows accent colour, or set a hex like `"#B4A0FF"`. |
| `layer` | `"desktop"` | `"desktop"` sits on the wallpaper, `"top"` stays above windows, `"normal"` behaves like a window. |
| `blur` | `false` | Frosted-glass blur behind the panel (Windows 10/11). |
| `width`, `max_height` | `340`, `620` | Panel size in pixels. The list scrolls past the max height. |
| `show_empty_days` | `true` | Show "Nothing scheduled" for free days. |
| `locked` | `false` | Stop the widget from being dragged. |

## Troubleshooting

- **Nothing happens with `pythonw`:** run `python calendar_widget.py` to see errors, and check `widget.log` next to the script.
- **A calendar shows a warning:** hover the footer for the reason. An "HTTP 404" usually means the link was reset or copied incompletely.
- **Widget vanishes with Win+D or after Explorer restarts:** set `"layer": "normal"` or `"top"`, or simply launch the script again.
