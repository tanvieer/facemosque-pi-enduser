"""The cached prayer-time window and the policy for refreshing it."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta

from .api import DayTimes, fetch_range
from .config import Config

log = logging.getLogger(__name__)

STATE_FILENAME = "schedule.json"


class Schedule:
    """A rolling window of days, persisted so a reboot or a dead uplink never
    leaves the device without prayer times."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._days: dict[date, DayTimes] = {}
        self.fetched_on: date | None = None

    # ---------------------------------------------------------------- state

    @property
    def path(self):
        return self._config.state_dir / STATE_FILENAME

    def load(self) -> None:
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

        fetched = blob.get("fetched_on")
        self.fetched_on = date.fromisoformat(fetched) if fetched else None
        log.info(
            "loaded %d cached day(s), last fetched %s",
            len(self._days),
            self.fetched_on or "never",
        )

    def save(self) -> None:
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        blob = {
            "fetched_on": self.fetched_on.isoformat() if self.fetched_on else None,
            "days": [d.to_json() for d in self.sorted_days],
        }
        # Write-then-rename, so a power cut cannot leave a half-written file.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        log.info("saved %d day(s) to %s", len(self._days), self.path)

    # ----------------------------------------------------------- inspection

    @property
    def sorted_days(self) -> list[DayTimes]:
        return [self._days[k] for k in sorted(self._days)]

    def __len__(self) -> int:
        return len(self._days)

    def find(self, day: date) -> DayTimes | None:
        return self._days.get(day)

    @property
    def last_day(self) -> date | None:
        return max(self._days) if self._days else None

    # -------------------------------------------------------------- refresh

    def needs_refresh(self, today: date) -> bool:
        if not self._days or self.fetched_on is None:
            return True
        if today >= self.fetched_on + timedelta(days=self._config.refresh_interval_days):
            return True
        # The server omits days nobody filled in, so the window can be shorter
        # than requested. Top it up before it runs out underneath us.
        last = self.last_day
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
        self.fetched_on = today
        self.save()
        log.info(
            "window %s .. %s (%d day(s))",
            days[0].day.isoformat(),
            days[-1].day.isoformat(),
            len(days),
        )
        return True

    # ------------------------------------------------------------ scheduling

    def upcoming(self, now: datetime) -> tuple[str, datetime] | None:
        """Next (prayer, local datetime) at or after `now`, searching forward
        through the cached window. None if nothing is left in it."""
        day = now.date()
        horizon = self.last_day
        while horizon is not None and day <= horizon:
            entry = self._days.get(day)
            if entry is not None:
                candidates = sorted(
                    (
                        (prayer, datetime.combine(day, at, tzinfo=now.tzinfo))
                        for prayer, at in entry.adhan.items()
                    ),
                    key=lambda pair: pair[1],
                )
                for prayer, when in candidates:
                    if when >= now:
                        return prayer, when
            day += timedelta(days=1)
        return None
