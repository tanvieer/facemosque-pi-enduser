"""Alexa skill: answers questions about the mosque's prayer times.

Runs on Alexa-hosted Lambda. No API call and no network access at all -- the
whole year is in prayer-times.json next to this file, the same file
`adhanctl bundle` writes. That keeps the API key out of the cloud, and keeps
every answer inside Alexa's 8 second response budget with room to spare.

Standard library only, exactly like the Pi service, so there is nothing to
install and nothing to keep up to date.
"""

import json
import os
from datetime import date, datetime, time, timedelta

# ---------------------------------------------------------------- the data

DATA_PATH = os.path.join(os.path.dirname(__file__), "prayer-times.json")

PRAYER_LABELS = {
    "fajr": "Fajr",
    "dhuhr": "Dhuhr",
    "asr": "Asr",
    "maghrib": "Maghrib",
    "isha": "Isha",
}
# The order the day runs in, which is not alphabetical and not the order the
# JSON happens to store them in.
PRAYER_ORDER = ("fajr", "dhuhr", "asr", "maghrib", "isha")

FRIDAY = 4  # date.weekday()


def _load():
    with open(DATA_PATH, encoding="utf-8") as handle:
        blob = json.load(handle)
    days = {}
    for entry in blob.get("days", []):
        days[entry["date"]] = entry
    # Same date in whatever year the bundle holds, newest year winning. This
    # is what lets the skill keep answering after the bundled year runs out.
    by_month_day = {}
    for iso in sorted(days):
        _, month, day = iso.split("-")
        by_month_day[(month, day)] = days[iso]
    return days, by_month_day, blob.get("jummah", [])


# Loaded once per container, not once per request.
DAYS, BY_MONTH_DAY, JUMMAH = _load()


def day_entry(day):
    """Times for `day` as (entry, is_from_another_year).

    Prayer times barely move from one year to the next -- a minute here or
    there as the mosque adjusts. So rather than going silent on the 1st of
    January, fall back to the same calendar date in the bundled year. Wrong
    by a minute beats knowing nothing, and the file can be refreshed whenever
    the mosque publishes the new year.
    """
    exact = DAYS.get(day.isoformat())
    if exact is not None:
        return exact, False

    entry = BY_MONTH_DAY.get((f"{day.month:02d}", f"{day.day:02d}"))
    if entry is None and (day.month, day.day) == (2, 29):
        # A leap day, and the bundled year had none.
        entry = BY_MONTH_DAY.get(("02", "28"))
    return entry, entry is not None


# ------------------------------------------------------------ time helpers


def _last_sunday(year, month):
    """The last Sunday of a month -- when EU clocks change."""
    day = date(year, month, 31) if month in (3, 10) else None
    while day.weekday() != 6:  # Sunday
        day -= timedelta(days=1)
    return day


def berlin_now():
    """Now, as a wall clock in Europe/Berlin.

    Hand-rolled rather than zoneinfo: AWS Lambda images ship without the tz
    database, so ZoneInfo("Europe/Berlin") raises unless you add the tzdata
    package. This keeps the skill dependency-free. EU rule: summer time runs
    from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last Sunday
    of October.

    Only correct for the EU. A mosque elsewhere should replace this function
    with zoneinfo and add `tzdata` to requirements.txt.
    """
    utc = datetime.utcnow()
    start = datetime.combine(_last_sunday(utc.year, 3), time(1, 0))
    end = datetime.combine(_last_sunday(utc.year, 10), time(1, 0))
    offset = 2 if start <= utc < end else 1
    return utc + timedelta(hours=offset)


def _parse(value):
    if not value:
        return None
    parts = value.split(":")
    try:
        return time(int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return None


def spoken_time(when):
    """21:01 -> "9:01 PM". Alexa reads this shape correctly on its own."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def spoken_gap(delta):
    total = int(delta.total_seconds() // 60)
    if total < 1:
        return "right now"
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"in {hours} hour{'s' if hours > 1 else ''} and {minutes} minute{'s' if minutes > 1 else ''}"
    if hours:
        return f"in {hours} hour{'s' if hours > 1 else ''}"
    return f"in {minutes} minute{'s' if minutes > 1 else ''}"


# --------------------------------------------------------------- schedule


def jummah_slots():
    """Friday prayers, numbered, with their language label."""
    out = []
    for index, slot in enumerate(JUMMAH, 1):
        at = _parse(slot.get("at"))
        if at is None:
            continue
        name = "Jummah" if len(JUMMAH) == 1 else f"Jummah {index}"
        if slot.get("label"):
            name += f", the {slot['label']} one,"
        out.append((name, at))
    return out


def slots_for(day):
    """Everything on `day`, earliest first.

    On Friday the jummah replaces Dhuhr, matching what the Pi actually plays.
    The two must agree: being told one time and hearing the adhan at another
    would be worse than not asking.
    """
    entry, _ = day_entry(day)
    if entry is None:
        return []

    # Friday comes from the real date, never from the year the times were
    # borrowed from -- 15 March is a Sunday one year and a Tuesday the next.
    jummah = jummah_slots() if day.weekday() == FRIDAY else []
    slots = []
    for prayer in PRAYER_ORDER:
        if prayer == "dhuhr" and jummah:
            slots.extend(jummah)
            continue
        at = _parse(entry.get("adhan", {}).get(prayer))
        if at is not None:
            slots.append((PRAYER_LABELS[prayer], at))
    slots.sort(key=lambda pair: pair[1])
    return slots


def next_slot(now):
    """(name, datetime) of the next prayer at or after `now`."""
    day = now.date()
    for _ in range(400):  # the bundle is a year; stop rather than loop forever
        for name, at in slots_for(day):
            when = datetime.combine(day, at)
            if when >= now:
                return name, when
        day += timedelta(days=1)
    return None


# --------------------------------------------------------------- responses


def say(text, end=True):
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end,
        },
    }


def ask(text):
    response = say(text, end=False)
    response["response"]["reprompt"] = {
        "outputSpeech": {"type": "PlainText", "text": "What would you like to know?"}
    }
    return response


NO_DATA = "I do not have any prayer times loaded."

# Said only when the answer came from a different year's table, so it is a
# real caveat rather than a disclaimer nobody reads.
STALE = " These are last year's times, so they may be a minute or two out."


def stale_note(day):
    _, borrowed = day_entry(day)
    return STALE if borrowed else ""

HELP = (
    "You can ask me when the next prayer is, what time a prayer is, what "
    "today's prayer times are, when jummah is, or when iqamah is for a "
    "prayer. What would you like to know?"
)


# ------------------------------------------------------------------ intents


def handle_next(now):
    found = next_slot(now)
    if found is None:
        return say(NO_DATA)
    name, when = found
    note = stale_note(when.date())
    if when.date() != now.date():
        return say(
            f"The next prayer is {name} tomorrow at {spoken_time(when.time())}.{note}"
        )
    gap = spoken_gap(when - now)
    return say(f"The next prayer is {name} at {spoken_time(when.time())}, {gap}.{note}")


def _slot_value(intent, name):
    slot = (intent.get("slots") or {}).get(name) or {}
    resolutions = (slot.get("resolutions") or {}).get("resolutionsPerAuthority") or []
    for authority in resolutions:
        values = authority.get("values") or []
        if values:
            return values[0]["value"]["name"].lower()
    return (slot.get("value") or "").lower()


def handle_prayer_time(now, intent, which="adhan"):
    prayer = _slot_value(intent, "prayer")
    if prayer not in PRAYER_LABELS:
        return ask("Which prayer? You can say Fajr, Dhuhr, Asr, Maghrib or Isha.")

    entry, _ = day_entry(now.date())
    if entry is None:
        return say(NO_DATA)

    label = PRAYER_LABELS[prayer]
    at = _parse(entry.get(which, {}).get(prayer))
    word = "iqamah" if which == "iqamah" else "adhan"
    note = stale_note(now.date())
    if at is None:
        return say(f"The mosque has not published the {label} {word} time for today.")

    when = datetime.combine(now.date(), at)
    if when >= now:
        return say(
            f"{label} {word} is at {spoken_time(at)} today, "
            f"{spoken_gap(when - now)}.{note}"
        )

    tomorrow, _ = day_entry(now.date() + timedelta(days=1))
    later = _parse((tomorrow or {}).get(which, {}).get(prayer))
    if later is None:
        return say(f"{label} {word} was at {spoken_time(at)} today.{note}")
    return say(
        f"{label} {word} was at {spoken_time(at)} today. Tomorrow it is at "
        f"{spoken_time(later)}.{note}"
    )


def handle_today(now):
    slots = slots_for(now.date())
    if not slots:
        return say(NO_DATA)
    parts = [f"{name} at {spoken_time(at)}" for name, at in slots]
    listed = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    day = "Today" if now.date().weekday() != FRIDAY else "Today, Friday,"
    return say(f"{day} the prayer times are {listed}.{stale_note(now.date())}")


def handle_jummah(now):
    slots = jummah_slots()
    if not slots:
        return say("The mosque has not published any jummah times.")

    days_ahead = (FRIDAY - now.date().weekday()) % 7
    when = "today" if days_ahead == 0 else ("tomorrow" if days_ahead == 1 else "on Friday")
    parts = [f"{name} at {spoken_time(at)}" for name, at in slots]
    if len(parts) == 1:
        return say(f"Jummah is {when} at {spoken_time(slots[0][1])}.")
    listed = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return say(f"The mosque holds {len(parts)} jummah prayers {when}: {listed}.")


# ------------------------------------------------------------------- entry


def lambda_handler(event, context):
    request = event.get("request", {})
    kind = request.get("type")
    now = berlin_now()

    if kind == "LaunchRequest":
        found = next_slot(now)
        if found is None:
            return ask(NO_DATA)
        name, when = found
        return ask(
            f"The next prayer is {name} at {spoken_time(when.time())}, "
            f"{spoken_gap(when - now)}. You can also ask about today's times, "
            "jummah, or iqamah."
        )

    if kind == "SessionEndedRequest":
        return say("")

    if kind != "IntentRequest":
        return say(HELP)

    intent = request.get("intent", {})
    name = intent.get("name", "")

    if name == "NextPrayerIntent":
        return handle_next(now)
    if name == "PrayerTimeIntent":
        return handle_prayer_time(now, intent, "adhan")
    if name == "IqamahIntent":
        return handle_prayer_time(now, intent, "iqamah")
    if name == "TodayTimesIntent":
        return handle_today(now)
    if name == "JummahIntent":
        return handle_jummah(now)
    if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NavigateHomeIntent"):
        return say("")
    if name == "AMAZON.HelpIntent":
        return ask(HELP)

    return ask("Sorry, I did not catch that. " + HELP)
