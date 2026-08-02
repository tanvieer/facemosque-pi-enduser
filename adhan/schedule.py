"""The cached prayer-time window and the policy for refreshing it."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .api import ApiError, ApiUnavailable, DayTimes, JummahSlot, fetch_jummah, fetch_range
from .config import PRAYER_LABELS, Config

log = logging.getLogger(__name__)

STATE_FILENAME = "schedule.json"

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
    config: Config,
    days: list[DayTimes],
    jummah: list[JummahSlot],
    generated: date,
) -> "Path":
    """Write the offline fallback that ships in the repo. See `adhanctl bundle`."""
    path = config.fallback_path
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "version": STATE_VERSION,
        "mosque_id": config.mosque_id,
        "generated": generated.isoformat(),
        "days": [d.to_json() for d in days],
        "jummah": [j.to_json() for j in jummah],
    }
    path.write_text(json.dumps(blob, indent=1) + "\n", encoding="utf-8")
    return path


class Schedule:
    """A rolling window of days, persisted so a reboot or a dead uplink never
    leaves the device without prayer times."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._days: dict[date, DayTimes] = {}
        self._jummah: list[JummahSlot] = []
        self._fallback: dict[date, DayTimes] = {}
        self._fallback_jummah: list[JummahSlot] = []
        self._version = STATE_VERSION
        self.fetched_on: date | None = None

    # ---------------------------------------------------------------- state

    @property
    def path(self):
        return self._config.state_dir / STATE_FILENAME

    def _load_fallback(self) -> None:
        """Read the prayer times shipped in the repo.

        This is what makes a Pi with no internet still call the adhan: a whole
        year of times committed alongside the code, consulted whenever the live
        cache has nothing for the day in question. It is never written to.
        """
        path = self._config.fallback_path
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("no bundled prayer times at %s", path)
            return
        except (OSError, ValueError) as exc:
            log.warning("bundled prayer times unreadable (%s); ignoring", exc)
            return

        found = blob.get("mosque_id")
        if found != self._config.mosque_id:
            log.warning(
                "%s holds mosque %s but MOSQUE_ID is %s; ignoring it",
                path.name,
                found,
                self._config.mosque_id,
            )
            return

        for entry in blob.get("days", []):
            try:
                day = DayTimes.from_json(entry)
            except (KeyError, TypeError, ValueError):
                continue
            self._fallback[day.day] = day
        self._fallback_jummah = [
            slot
            for slot in (JummahSlot.from_json(e) for e in blob.get("jummah", []))
            if slot is not None
        ]
        log.info(
            "bundled fallback: %d day(s) and %d jummah from %s",
            len(self._fallback),
            len(self._fallback_jummah),
            path.name,
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
