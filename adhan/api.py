"""Facemosque prayer-times API client.

Standard library only — no requests, no venv, nothing for Bookworm's PEP 668
to object to.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from .config import ALL_PRAYERS, Config

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20


class ApiError(Exception):
    """A request reached the server and came back as an error."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class ApiUnavailable(Exception):
    """DNS, TLS or socket failure — the cached schedule stays in charge."""


@dataclass(frozen=True)
class DayTimes:
    day: date
    adhan: dict[str, time] = field(default_factory=dict)
    iqamah: dict[str, time] = field(default_factory=dict)
    sunrise: time | None = None

    def to_json(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "adhan": {k: v.strftime("%H:%M:%S") for k, v in self.adhan.items()},
            "iqamah": {k: v.strftime("%H:%M:%S") for k, v in self.iqamah.items()},
            "sunrise": self.sunrise.strftime("%H:%M:%S") if self.sunrise else None,
        }

    @classmethod
    def from_json(cls, blob: dict) -> "DayTimes":
        return cls(
            day=date.fromisoformat(blob["date"]),
            adhan={k: _parse_time(v) for k, v in blob.get("adhan", {}).items()
                   if _parse_time(v) is not None},
            iqamah={k: _parse_time(v) for k, v in blob.get("iqamah", {}).items()
                    if _parse_time(v) is not None},
            sunrise=_parse_time(blob.get("sunrise")),
        )


@dataclass(frozen=True)
class JummahSlot:
    """One Friday prayer. A mosque can hold several, in different languages.

    The API gives jummah a khutbah and an iqamah time but no adhan time, since
    there is nothing to compute -- the adhan is called as the khatib takes the
    minbar. So khutbah_time is when this plays, with iqamah_time as the
    fallback for a mosque that only filled that one in.
    """

    order: int
    at: time
    label: str = ""

    def to_json(self) -> dict:
        return {
            "order": self.order,
            "at": self.at.strftime("%H:%M:%S"),
            "label": self.label,
        }

    @classmethod
    def from_json(cls, blob: dict) -> "JummahSlot | None":
        at = _parse_time(blob.get("at"))
        if at is None:
            return None
        return cls(order=int(blob.get("order", 0)), at=at, label=blob.get("label", ""))


def _parse_time(value: object) -> time | None:
    """'HH:MM:SS' -> time. None for null/blank/malformed.

    iqamah is nullable, and sunrise/suhur/iftar arrive as bare strings rather
    than {adhan, iqamah} objects, so this has to tolerate both shapes' misses.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    return time(hour, minute, second)


def _get(config: Config, path: str) -> dict:
    url = f"{config.base_url}/api/v1{path}"
    request = urllib.request.Request(
        url,
        headers={
            "X-API-Key": config.api_key,
            "Accept": "application/json",
            "User-Agent": "facemosque-adhan/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = {}
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - error bodies are best-effort
            pass
        error = body.get("error", {}) if isinstance(body, dict) else {}
        raise ApiError(
            exc.code,
            error.get("code", "unknown"),
            error.get("message", exc.reason or ""),
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiUnavailable(str(exc)) from exc


def _parse_day(blob: dict) -> DayTimes | None:
    try:
        day = date.fromisoformat(blob["date"])
    except (KeyError, TypeError, ValueError):
        log.warning("skipping day with unparseable date: %r", blob.get("date"))
        return None

    times = blob.get("times") or {}
    adhan: dict[str, time] = {}
    iqamah: dict[str, time] = {}
    for prayer in ALL_PRAYERS:
        slot = times.get(prayer) or {}
        if not isinstance(slot, dict):
            continue
        value = _parse_time(slot.get("adhan"))
        if value is not None:
            adhan[prayer] = value
        value = _parse_time(slot.get("iqamah"))
        if value is not None:
            iqamah[prayer] = value

    # iftar is deliberately dropped: it *is* the Maghrib adhan, which already
    # has its own entry. suhur is only sent during Ramadan and is not played.
    return DayTimes(
        day=day,
        adhan=adhan,
        iqamah=iqamah,
        sunrise=_parse_time(times.get("sunrise")),
    )


def fetch_range(config: Config, start: date, end: date) -> list[DayTimes]:
    """GET /prayer-times/range.

    Days the mosque admin has not filled in are omitted by the server rather
    than returned as nulls, so the result can be shorter than the requested
    window. Callers must match on `.day`, never on position.
    """
    payload = _get(
        config,
        f"/mosques/{config.mosque_id}/prayer-times/range"
        f"?from={start.isoformat()}&to={end.isoformat()}",
    )
    days_blob = (payload.get("data") or {}).get("days") or []
    days = [d for d in (_parse_day(b) for b in days_blob) if d is not None]
    days.sort(key=lambda d: d.day)

    _warn_about_gaps(days, start, end)
    return days


def fetch_jummah(config: Config) -> list[JummahSlot]:
    """GET /jummah — the mosque's Friday prayers, in the order they are held.

    Deliberately a separate call: the /range response carries no jummah at all,
    only the single-date and today responses do, and those would mean 30 more
    requests for data that is identical on every Friday.
    """
    payload = _get(config, f"/mosques/{config.mosque_id}/jummah")
    slots: list[JummahSlot] = []
    for blob in (payload.get("data") or {}).get("jummah") or []:
        if not isinstance(blob, dict) or not blob.get("is_active", True):
            continue
        at = _parse_time(blob.get("khutbah_time")) or _parse_time(blob.get("iqamah_time"))
        if at is None:
            log.warning("jummah entry %r has no usable time; skipping", blob.get("id"))
            continue
        labels = blob.get("labels") or {}
        slots.append(
            JummahSlot(
                order=int(blob.get("order") or 0),
                at=at,
                label=str(labels.get("en") or "").strip(),
            )
        )
    slots.sort(key=lambda s: (s.order, s.at))
    return slots


def _warn_about_gaps(days: list[DayTimes], start: date, end: date) -> None:
    requested = (end - start).days + 1
    if len(days) < requested:
        missing = sorted(
            {start + timedelta(days=i) for i in range(requested)}
            - {d.day for d in days}
        )
        log.warning(
            "%d of %d day(s) not entered by the mosque admin: %s",
            len(missing),
            requested,
            ", ".join(d.isoformat() for d in missing[:8])
            + (" ..." if len(missing) > 8 else ""),
        )
    return days

