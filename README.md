# Glass Calendar

A translucent "next 7 days" calendar widget for Windows 10/11, styled to match the Windows dark theme.

## Features

- Agenda for the coming week, grouped by day, with a day strip along the top
- Hover over an event to see a details flyout (time, duration, location, organiser, notes, meeting link)
- Click an event with a link to open or join it
- Fluent dark styling: translucent panel, your Windows accent colour, rounded corners, soft shadows
- Reads any iCalendar (`.ics`) feed or local file: Google Calendar, Outlook / Microsoft 365, iCloud, university timetables
- Right-click the widget or its tray icon for options
- Shows demo events until you add a calendar

## Requirements

- Windows 10 or 11
- Python 3.10+

## Installation

```bash
git clone https://github.com/<your-username>/GlassCalendar.git
cd GlassCalendar
pip install PySide6 icalendar recurring-ical-events tzdata
```

## Usage

```bash
python calendar_widget.py     # with console (shows errors)
pythonw calendar_widget.py    # no console window
```

To add your calendar, right-click the widget and choose **Edit settings**, then add your `.ics` URL or file path.

## Built with

[PySide6](https://doc.qt.io/qtforpython-6/), [icalendar](https://github.com/collective/icalendar), [recurring-ical-events](https://github.com/niccokunzmann/python-recurring-ical-events)

## License

MIT. See [LICENSE](LICENSE).
