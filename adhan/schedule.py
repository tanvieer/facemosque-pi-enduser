"""The cached prayer-time window and the policy for refreshing it."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .api import (
    ApiError,
    ApiUnavailable,
    DayTimes,
    JummahSlot,
    fetch_jummah,
    fetch_range,
)
from .config import PRAYER_LABELS, Config

log = logging.getLogger(__name__)

STATE_FILENAME = "schedule.json"
FALLBACK_FILENAME = "fallback.json"

# Bumped when the shape of schedule.json changes, so an older cache is refetched
# instead of quietly missing whatever the new version added.
STATE_VERSION = 2

FRIDAY = 4  # date.weekday()


@dataclass(frozen=True)
class Slot:
    """Something to play, and when.

    `key` is the identity used to remember what has already fired today, so
    every jummah needs its own -- "jummah1", "jummah2" -- or the second one
    would be treated as already played.
    """

    key: str
    at: time
    label: str


def select_jummah(slots: list[Slot], choice: int | None) -> list[Slot]:
    """Which of the mosque's Friday prayers to play.

    `None` (JUMMAH left blank) means every one of them. A number picks that
    one, counting from 1. A number past the end picks the last rather than
    nothing: a mosque that drops from three jummahs to two should still get an
    adhan, and a silent Friday is the one failure nobody would notice in time.
    """
    if not slots or choice is None:
        return list(slots)
    return [slots[min(choice, len(slots)) - 1]]


def write_bundle(
    path: Path,
    mosque_id: int,
    days: list[DayTimes],
    jummah: list[JummahSlot],
    generated: date,
) -> Path:
    """Write a year of prayer times to disk as an offline fallback.

    Two callers: `adhanctl bundle`, which writes the copy committed to the
    repo, and the service, which keeps its own copy fresh in the state
    directory. Same format either way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "version": STATE_VERSION,
        "mosque_id": mosque_id,
        "generated": generated.isoformat(),
        "days": [d.to_json() for d in days],
        "jummah": [j.to_json() for j in jummah],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def fetch_years(config: Config, today: date) -> tuple[list[DayTimes], list[JummahSlot]]:
    """A year of days either side of the turn, plus the jummah times.

    The API refuses a range over 366 days, so this year and next are two
    separate calls. Next year is usually empty -- the admin has not filled it
    in yet -- and that is fine; the point is that the moment they do, the very
    next rebuild picks it up and December stops being a cliff edge.
    """
    days: list[DayTimes] = []
    for year in (today.year, today.year + 1):
        days.extend(fetch_range(config, date(year, 1, 1), date(year, 12, 31)))
    jummah = fetch_jummah(config)
    return days, jummah


class Schedule:
    """A rolling window of days, persisted so a reboot or a dead uplink never
    leaves the device without prayer times."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._days: dict[date, DayTimes] = {}
        self._jummah: list[JummahSlot] = []
        self._fallback: dict[date, DayTimes] = {}
        self._fallback_jummah: list[JummahSlot] = []
        self.fallback_generated: date | None = None
        self._version = STATE_VERSION
        self.fetched_on: date | None = None

    # ---------------------------------------------------------------- state

    @property
    def path(self):
        return self._config.state_dir / STATE_FILENAME

    @property
    def fallback_path(self) -> Path:
        """The service's own copy of the offline fallback.

        Kept out of the repo deliberately: the committed file is the baseline
        a fresh clone starts from, and rewriting it at runtime would turn
        every `git pull` into a conflict over a file nobody edited by hand.
        """
        return self._config.state_dir / FALLBACK_FILENAME

    def _read_bundle(
        self, path: Path
    ) -> tuple[dict[date, DayTimes], list[JummahSlot], date | None]:
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, [], None
        except (OSError, ValueError) as exc:
            log.warning("%s unreadable (%s); ignoring it", path.name, exc)
            return {}, [], None

        found = blob.get("mosque_id")
        if found != self._config.mosque_id:
            log.warning(
                "%s holds mosque %s but MOSQUE_ID is %s; ignoring it",
                path.name,
                found,
                self._config.mosque_id,
            )
            return {}, [], None

        days: dict[date, DayTimes] = {}
        for entry in blob.get("days", []):
            try:
                day = DayTimes.from_json(entry)
            except (KeyError, TypeError, ValueError):
                continue
            days[day.day] = day
        jummah = [
            slot
            for slot in (JummahSlot.from_json(e) for e in blob.get("jummah", []))
            if slot is not None
        ]
        generated = None
        try:
            generated = date.fromisoformat(blob["generated"])
        except (KeyError, TypeError, ValueError):
            pass
        return days, jummah, generated

    def _load_fallback(self) -> None:
        """Read the prayer times to fall back on when the API is unreachable.

        Two sources, in order: the year committed to the repo, then whatever
        the service has since rebuilt for itself. This is what makes a Pi with
        no internet still call the adhan.
        """
        self._fallback = {}
        self._fallback_jummah = []
        self.fallback_generated = None
        for path in (self._config.fallback_path, self.fallback_path):
            days, jummah, generated = self._read_bundle(path)
            if not days:
                continue
            self._fallback.update(days)
            if jummah:
                self._fallback_jummah = jummah
            if generated and (
                self.fallback_generated is None or generated > self.fallback_generated
            ):
                self.fallback_generated = generated
            log.info(
                "fallback: %d day(s), %d jummah from %s (built %s)",
                len(days),
                len(jummah),
                path.name,
                generated or "unknown",
            )

    def load(self) -> None:
        self._load_fallback()
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("no cached schedule at %s", self.path)
            return
        except (OSError, ValueError) as exc:
            log.warning("cached schedule unreadable (%s); ignoring it", exc)
            return

        self._days = {}
        for entry in blob.get("days", []):
            try:
                day = DayTimes.from_json(entry)
            except (KeyError, TypeError, ValueError):
                continue
            self._days[day.day] = day

        self._jummah = [
            slot
            for slot in (JummahSlot.from_json(e) for e in blob.get("jummah", []))
            if slot is not None
        ]
        self._version = int(blob.get("version", 1))

        fetched = blob.get("fetched_on")
        self.fetched_on = date.fromisoformat(fetched) if fetched else None
        log.info(
            "loaded %d cached day(s) and %d jummah, last fetched %s",
            len(self._days),
            len(self._jummah),
            self.fetched_on or "never",
        )

    def save(self) -> None:
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        # Only what was fetched. The bundled fallback is part of the repo and
        # copying it into the state file would just make it go stale twice.
        blob = {
            "version": STATE_VERSION,
            "fetched_on": self.fetched_on.isoformat() if self.fetched_on else None,
            "days": [self._days[k].to_json() for k in sorted(self._days)],
            "jummah": [j.to_json() for j in self._jummah],
        }
        # Write-then-rename, so a power cut cannot leave a half-written file.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        log.info("saved %d day(s) to %s", len(self._days), self.path)

    # ----------------------------------------------------------- inspection

    @property
    def _merged(self) -> dict[date, DayTimes]:
        """Bundled times with anything freshly fetched laid over the top."""
        return {**self._fallback, **self._days}

    @property
    def sorted_days(self) -> list[DayTimes]:
        merged = self._merged
        return [merged[k] for k in sorted(merged)]

    def __len__(self) -> int:
        return len(self._merged)

    def find(self, day: date) -> DayTimes | None:
        return self._days.get(day) or self._fallback.get(day)

    @property
    def last_day(self) -> date | None:
        merged = self._merged
        return max(merged) if merged else None

    # -------------------------------------------------------------- refresh

    def needs_refresh(self, today: date) -> bool:
        if not self._days or self.fetched_on is None:
            return True
        if self._version != STATE_VERSION:
            log.info("cached schedule is version %d; refetching", self._version)
            return True
        if today >= self.fetched_on + timedelta(days=self._config.refresh_interval_days):
            return True
        # The server omits days nobody filled in, so the window can be shorter
        # than requested. Top it up before it runs out underneath us. This
        # asks the fetched window, not `last_day`: the bundled year would
        # otherwise keep the answer "no" until 2027.
        last = max(self._days) if self._days else None
        return last is None or today >= last - timedelta(days=2)

    def refresh(self, today: date) -> bool:
        """Fetch the next `schedule_days` days. Returns True on success.

        Whatever comes back replaces the window wholesale; on failure the
        existing cache is left untouched.
        """
        end = today + timedelta(days=self._config.schedule_days - 1)
        days = fetch_range(self._config, today, end)
        if not days:
            log.warning("server returned no days; keeping the cached schedule")
            return False

        self._days = {d.day: d for d in days}

        # A jummah failure must not throw away a good 30-day window, so this is
        # caught rather than raised: keep whatever jummah we already had.
        try:
            self._jummah = fetch_jummah(self._config)
        except (ApiError, ApiUnavailable) as exc:
            log.warning(
                "could not refresh jummah (%s); keeping %d cached",
                exc,
                len(self._jummah),
            )

        self._version = STATE_VERSION
        self.fetched_on = today
        self.save()
        log.info(
            "window %s .. %s (%d day(s)), %d jummah",
            days[0].day.isoformat(),
            days[-1].day.isoformat(),
            len(days),
            len(self._jummah),
        )
        return True

    # ------------------------------------------------- offline fallback

    def needs_bundle_refresh(self, today: date) -> bool:
        """Is the offline fallback due for a rebuild?

        Once a month, on the 1st. Phrased as "was it built in an earlier
        month" rather than "is today the 1st" on purpose: a device that was
        offline on the 1st stays due, and rebuilds the moment it next reaches
        the API. Missing the window entirely is the failure mode that matters
        here -- this is the copy that has to be right when nothing else works.
        """
        if self.fallback_generated is None:
            return True
        built = self.fallback_generated
        return (built.year, built.month) < (today.year, today.month)

    def refresh_bundle(self, today: date) -> bool:
        """Rebuild the offline fallback from the API. Raises on network error.

        Written to the state directory rather than over the committed file --
        see `fallback_path`. On failure the existing fallback is untouched.
        """
        days, jummah = fetch_years(self._config, today)
        if not days:
            log.warning("bundle rebuild returned no days; keeping the old fallback")
            return False

        write_bundle(
            self.fallback_path,
            self._config.mosque_id,
            days,
            jummah or self._fallback_jummah,
            today,
        )
        self._load_fallback()
        log.info(
            "offline fallback rebuilt: %d day(s) through %s",
            len(days),
            days[-1].day.isoformat(),
        )
        return True

    # ------------------------------------------------------------ scheduling

    @property
    def jummah(self) -> list[Slot]:
        """Every Friday prayer the mosque holds, numbered in order."""
        entries = self._jummah or self._fallback_jummah
        slots = []
        for index, entry in enumerate(entries, 1):
            label = "Jummah" if len(entries) == 1 else f"Jummah {index}"
            if entry.label:
                label += f" ({entry.label})"
            slots.append(Slot(key=f"jummah{index}", at=entry.at, label=label))
        return slots

    def slots_for(self, day: date) -> list[Slot]:
        """Everything to play on `day`, earliest first.

        On Friday the jummah adhan replaces dhuhr rather than joining it --
        there is one midday prayer, and at a mosque with several jummahs it is
        called once per jummah. If the mosque has no jummah configured, dhuhr
        stays: better the ordinary adhan than a silent Friday.
        """
        entry = self.find(day)
        if entry is None:
            return []

        jummah = (
            select_jummah(self.jummah, self._config.jummah_choice)
            if day.weekday() == FRIDAY
            else []
        )

        slots: list[Slot] = []
        for prayer in self._config.prayers:
            if prayer == "dhuhr" and jummah:
                slots.extend(jummah)
                continue
            at = entry.adhan.get(prayer)
            if at is not None:
                slots.append(Slot(prayer, at, PRAYER_LABELS[prayer]))
        slots.sort(key=lambda slot: slot.at)
        return slots

    def upcoming(self, now: datetime) -> tuple[Slot, datetime] | None:
        """Next (slot, local datetime) at or after `now`, searching forward
        through the cached window. None if nothing is left in it."""
        day = now.date()
        horizon = self.last_day
        while horizon is not None and day <= horizon:
            for slot in self.slots_for(day):
                when = datetime.combine(day, slot.at, tzinfo=now.tzinfo)
                if when >= now:
                    return slot, when
            day += timedelta(days=1)
        return None
