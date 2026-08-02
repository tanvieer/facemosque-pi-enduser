"""Runtime configuration, read from .env at the repo root.

Nothing here touches the network, D-Bus or audio; importing this module is
safe from a laptop as well as from the Pi.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# Defaults for everything but the API key, so a clone with only a key in .env
# runs. These are Takaful Mosque, Chemnitz -- the mosque this was built for.
DEFAULT_BASE_URL = "https://facemosque.com"
DEFAULT_MOSQUE_ID = 4
DEFAULT_TIMEZONE = "Europe/Berlin"

# Every prayer the API exposes an adhan for, in the order the day runs. Jummah
# is not here: the API gives khutbah and iqamah for it, but no adhan.
ALL_PRAYERS: tuple[str, ...] = ("fajr", "dhuhr", "asr", "maghrib", "isha")

PRAYER_LABELS = {
    "fajr": "Fajr",
    "dhuhr": "Dhuhr",
    "asr": "Asr",
    "maghrib": "Maghrib",
    "isha": "Isha",
}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    mosque_id: int
    timezone: ZoneInfo

    audio_path: Path
    audio_path_fajr: Path | None
    fallback_path: Path

    bt_sink_mac: str
    bt_sink_name: str

    alexa_enabled: bool
    alexa_device_name: str
    alexa_port: int

    state_dir: Path
    prayers: tuple[str, ...] = ALL_PRAYERS

    # Which Friday prayer to play when the mosque holds more than one.
    # None means all of them. See select_jummah().
    jummah_choice: int | None = None

    schedule_days: int = 30
    refresh_interval_days: int = 3

    # An adhan older than this is skipped rather than played late, so a Pi that
    # was offline all morning stays quiet instead of firing a backlog.
    fire_tolerance_seconds: int = 90

    # Bluetooth reconnect backoff, seconds.
    bt_backoff_min: int = 5
    bt_backoff_max: int = 300

    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Config":
        env_path = env_path or ENV_PATH
        file_values = read_env_file(env_path)
        warnings: list[str] = []

        def get(key: str, default: str | None = None) -> str:
            value = os.environ.get(key, file_values.get(key, default))
            if value is None or value == "":
                raise RuntimeError(
                    f"{key} is not set. Add it to {env_path} (see .env.example)."
                )
            return value

        def optional(key: str, default: str = "") -> str:
            return os.environ.get(key, file_values.get(key, default)).strip()

        def as_int(key: str, default: str | None = None) -> int:
            raw = get(key, default)
            try:
                return int(raw.strip())
            except ValueError:
                raise RuntimeError(f"{key} must be a whole number, got {raw!r}") from None

        def resolve(raw: str) -> Path:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)

        audio_path = resolve(get("AUDIO_PATH", "audio/adhan.m4a"))
        fajr_raw = optional("AUDIO_PATH_FAJR")
        audio_path_fajr = resolve(fajr_raw) if fajr_raw else None

        prayers = tuple(
            p.strip().lower()
            for p in get("PRAYERS", ",".join(ALL_PRAYERS)).split(",")
            if p.strip()
        )
        unknown = [p for p in prayers if p not in ALL_PRAYERS]
        if unknown:
            warnings.append(
                f"PRAYERS contains unknown value(s): {', '.join(unknown)}; ignoring"
            )
            prayers = tuple(p for p in prayers if p in ALL_PRAYERS)

        jummah_raw = optional("JUMMAH")
        jummah_choice: int | None = None
        if jummah_raw:
            try:
                jummah_choice = int(jummah_raw)
            except ValueError:
                warnings.append(
                    f"JUMMAH={jummah_raw!r} is not a number; playing every jummah"
                )
            else:
                if jummah_choice < 1:
                    warnings.append(
                        "JUMMAH must be 1 or higher; playing every jummah"
                    )
                    jummah_choice = None

        bt_mac = optional("BT_SINK_MAC").upper()
        if not bt_mac:
            warnings.append(
                "BT_SINK_MAC is empty — run `./adhanctl pair` to select a speaker"
            )

        # The API key is the one thing with no default -- there is nothing
        # sensible to guess. Everything else falls back to the values Takaful
        # Mosque runs on, so a clone with only a key in it works. A wrong
        # value is still an error: defaults are for blanks, not for typos.
        api_key = get("EXPO_PUBLIC_API_KEY")
        base_url = get("EXPO_PUBLIC_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        mosque_id = as_int("MOSQUE_ID", str(DEFAULT_MOSQUE_ID))

        zone_name = get("TIMEZONE", DEFAULT_TIMEZONE)
        try:
            timezone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise RuntimeError(
                f"TIMEZONE={zone_name!r} is not an IANA zone "
                "(e.g. Europe/Berlin, Asia/Dhaka)"
            ) from None

        # Prayer times shipped in the repo, used when the API cannot be
        # reached and there is no cache yet. Named per mosque so a different
        # MOSQUE_ID never silently plays Takaful's times.
        fallback_raw = optional("FALLBACK_PATH")
        fallback_path = resolve(fallback_raw or f"data/mosque-{mosque_id}.json")

        return cls(
            base_url=base_url,
            api_key=api_key,
            mosque_id=mosque_id,
            timezone=timezone,
            audio_path=audio_path,
            audio_path_fajr=audio_path_fajr,
            fallback_path=fallback_path,
            bt_sink_mac=bt_mac,
            bt_sink_name=optional("BT_SINK_NAME", "Echo"),
            # Off unless asked for: current Echo firmware does not discover
            # local WeMo devices, so the listener would just idle. See README.
            alexa_enabled=_as_bool(get("ALEXA_ENABLED", "false")),
            alexa_device_name=optional("ALEXA_DEVICE_NAME", "Adhan"),
            alexa_port=as_int("ALEXA_PORT", "52000"),
            state_dir=Path(
                get("STATE_DIR", str(Path.home() / ".local/state/adhan"))
            ).expanduser(),
            prayers=prayers,
            jummah_choice=jummah_choice,
            schedule_days=as_int("SCHEDULE_DAYS", "30"),
            refresh_interval_days=as_int("REFRESH_INTERVAL_DAYS", "3"),
            warnings=tuple(warnings),
        )

    def audio_for(self, prayer: str) -> Path:
        if prayer == "fajr" and self.audio_path_fajr is not None:
            return self.audio_path_fajr
        return self.audio_path
