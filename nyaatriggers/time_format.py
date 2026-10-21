from datetime import datetime

_clock_format = "12"


def set_clock_format(value):
    global _clock_format
    _clock_format = "24" if value == "24" else "12"


def clock_format():
    return _clock_format


def format_time(timestamp, *, seconds=True):
    when = datetime.fromtimestamp(timestamp)
    if _clock_format == "24":
        return when.strftime("%H:%M:%S" if seconds else "%H:%M")
    clock = f"{when.hour % 12 or 12}:{when.minute:02d}"
    if seconds:
        clock += f":{when.second:02d}"
    return clock + (" AM" if when.hour < 12 else " PM")


def format_datetime(timestamp, *, seconds=True):
    when = datetime.fromtimestamp(timestamp)
    return f"{when.month}/{when.day}/{when.year} {format_time(timestamp, seconds=seconds)}"
