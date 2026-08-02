"""The long-running service: keeps the schedule fresh, the speaker connected,
and fires the adhan at each waqt."""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import date, datetime

from .alexa import AlexaDevice
from .api import ApiError, ApiUnavailable
from .bluetooth import BluetoothLink
from .config import Config
from .player import Player
from .schedule import Schedule

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
STATUS_INTERVAL = 900  # 15 min
FETCH_RETRY_SECONDS = 300


class AdhanService:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._schedule = Schedule(config)
        self._player = Player(config)
        self._bluetooth = BluetoothLink(config)
        self._alexa: AlexaDevice | None = None

        self._fired_date: date | None = None
        self._fired: set[str] = set()
        self._next_fetch = 0.0
        self._next_status = 0.0
        self._running = True

    # ------------------------------------------------------- fired state

    @property
    def _fired_path(self):
        return self._config.state_dir / "fired.json"

    def _load_fired(self) -> None:
        try:
            blob = json.loads(self._fired_path.read_text(encoding="utf-8"))
            self._fired_date = date.fromisoformat(blob["date"])
            self._fired = set(blob.get("fired", []))
        except (OSError, ValueError, KeyError):
            self._fired_date = None
            self._fired = set()

    def _save_fired(self) -> None:
        if self._fired_date is None:
            return
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._fired_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"date": self._fired_date.isoformat(), "fired": sorted(self._fired)}
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._fired_path)

    def _roll_day(self, now: datetime) -> None:
        """Start a new day, retiring anything already past. Without this a Pi
        that boots at noon would fire Fajr and Dhuhr back to back."""
        self._fired_date = now.date()
        self._fired = set()
        slots = self._schedule.slots_for(now.date())
        for slot in slots:
            when = datetime.combine(now.date(), slot.at, tzinfo=now.tzinfo)
            if (now - when).total_seconds() > self._config.fire_tolerance_seconds:
                self._fired.add(slot.key)
        self._save_fired()
        log.info(
            "new day %s: %s%s",
            now.date(),
            ", ".join(f"{s.label} {s.at:%H:%M}" for s in slots) or "nothing scheduled",
            f" ({len(self._fired)} already past)" if self._fired else "",
        )

    # ------------------------------------------------------------- alexa

    def _on_alexa(self, turn_on: bool) -> None:
        if turn_on:
            # Handy for testing from the couch: "Alexa, turn on adhan".
            self._player.play("manual")
        else:
            self._player.stop()

    # ------------------------------------------------------------ pieces

    def _maybe_refresh(self, now: datetime) -> None:
        if self._player.is_playing():
            return  # never contend with playback
        if time.monotonic() < self._next_fetch:
            return
        if not self._schedule.needs_refresh(now.date()):
            return
        try:
            self._schedule.refresh(now.date())
            self._next_fetch = time.monotonic() + 3600
        except ApiUnavailable as exc:
            log.warning("api unreachable (%s); keeping cached schedule", exc)
            self._next_fetch = time.monotonic() + FETCH_RETRY_SECONDS
        except ApiError as exc:
            log.error("api error: %s", exc)
            self._next_fetch = time.monotonic() + FETCH_RETRY_SECONDS

    def _maybe_fire(self, now: datetime) -> None:
        if self._fired_date != now.date():
            self._roll_day(now)

        for slot in self._schedule.slots_for(now.date()):
            if slot.key in self._fired:
                continue
            when = datetime.combine(now.date(), slot.at, tzinfo=now.tzinfo)
            elapsed = (now - when).total_seconds()
            if elapsed < 0:
                continue
            if elapsed > self._config.fire_tolerance_seconds:
                log.info("%s missed by %.0fs; skipping", slot.label, elapsed)
                self._fired.add(slot.key)
                self._save_fired()
                continue

            log.info("%s adhan due", slot.label)
            self._fired.add(slot.key)
            self._save_fired()
            if not self._bluetooth.is_connected():
                log.warning("speaker not connected; attempting to connect first")
                self._bluetooth.connect()
            self._player.play(slot.key)
            return

    def _log_status(self, now: datetime) -> None:
        if time.monotonic() < self._next_status:
            return
        self._next_status = time.monotonic() + STATUS_INTERVAL
        upcoming = self._schedule.upcoming(now)
        if upcoming is None:
            nxt = "nothing in the cached window"
        else:
            slot, when = upcoming
            delta = when - now
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            nxt = f"{slot.label} in {hours}h {rem // 60}m"
        log.info(
            "status: %d day(s) known | speaker %s | next %s",
            len(self._schedule),
            "up" if self._bluetooth.is_connected() else "down",
            nxt,
        )

    # -------------------------------------------------------------- main

    def stop(self, *_args) -> None:
        self._running = False

    def run(self) -> int:
        for warning in self._config.warnings:
            log.warning("%s", warning)

        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        self._schedule.load()
        self._load_fired()

        if self._config.alexa_enabled:
            self._alexa = AlexaDevice(
                name=self._config.alexa_device_name,
                port=self._config.alexa_port,
                on_change=self._on_alexa,
                get_state=self._player.is_playing,
            )
            self._alexa.start()

        log.info(
            "started: mosque %d, tz %s, prayers %s",
            self._config.mosque_id,
            self._config.timezone.key,
            ",".join(self._config.prayers),
        )

        last_bt_check = 0.0
        while self._running:
            now = datetime.now(self._config.timezone)

            # Checking the link costs a subprocess, so do it every 15s rather
            # than every tick.
            if time.monotonic() - last_bt_check > 15:
                last_bt_check = time.monotonic()
                self._bluetooth.tick()

            self._player.reap()
            self._maybe_refresh(now)
            self._maybe_fire(now)
            self._log_status(now)

            time.sleep(TICK_SECONDS)

        log.info("shutting down")
        self._player.stop()
        if self._alexa is not None:
            self._alexa.stop()
        return 0
