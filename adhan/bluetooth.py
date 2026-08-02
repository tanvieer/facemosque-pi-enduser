"""Keeping the Bluetooth speaker connected.

Echo devices drop the A2DP link when idle, and a speaker or router reboot drops
it too, so the link is re-established on a backoff rather than assumed.
bluetoothctl is driven as a subprocess: it is the one interface that behaves
the same across BlueZ versions.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
import time

from .config import Config

log = logging.getLogger(__name__)

_DEVICE_LINE = re.compile(r"^Device\s+([0-9A-F:]{17})\s+(.*)$", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _run(*args: str, timeout: int = 25) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", args[0], exc)
        return ""


def session(steps: list[tuple[str, float]], extra_timeout: int = 30) -> str:
    """Drive one interactive bluetoothctl session.

    Each step is (command, seconds to wait afterwards). A single session is
    required because `agent`/`default-agent` do not persist between separate
    bluetoothctl invocations -- pairing silently fails without them.
    """
    parts: list[str] = []
    for command, wait in steps:
        parts.append(f"echo {shlex.quote(command)}")
        parts.append(f"sleep {wait}")
    script = "{ " + "; ".join(parts) + "; } | bluetoothctl"
    budget = int(sum(wait for _, wait in steps)) + extra_timeout
    return _ANSI.sub("", _run("bash", "-c", script, timeout=budget))


def scan(seconds: int = 20) -> list[tuple[str, str]]:
    """Discoverable devices as (mac, name), newest scan wins.

    Devices whose name is just their own MAC are dropped: they are unnamed
    neighbours, never something you meant to pair with.
    """
    session(
        [
            ("power on", 1),
            ("agent NoInputNoOutput", 1),
            ("default-agent", 1),
            ("scan on", seconds),
            ("quit", 0.5),
        ]
    )

    devices: list[tuple[str, str]] = []
    for line in _run("bluetoothctl", "devices").splitlines():
        match = _DEVICE_LINE.match(_ANSI.sub("", line).strip())
        if not match:
            continue
        mac, name = match.group(1).upper(), match.group(2).strip()
        if name.replace("-", ":").upper() == mac:
            continue
        devices.append((mac, name))
    return devices


def pair(mac: str) -> tuple[bool, list[str]]:
    """Pair, trust and connect. Returns (paired, interesting log lines).

    Keyboards ask for a passkey to be typed on the device itself, so the agent
    is KeyboardDisplay and any passkey shown is surfaced to the caller.
    """
    output = session(
        [
            ("power on", 1),
            ("agent KeyboardDisplay", 1),
            ("default-agent", 1),
            ("scan on", 8),
            (f"pair {mac}", 25),
            (f"trust {mac}", 2),
            (f"connect {mac}", 10),
            ("quit", 0.5),
        ]
    )
    notable = [
        line.strip()
        for line in output.splitlines()
        if re.search(r"passkey|pairing successful|failed|connection successful", line, re.I)
    ]
    return "Paired: yes" in _run("bluetoothctl", "info", mac), notable


class BluetoothLink:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._mac = config.bt_sink_mac
        self._backoff = 0
        self._next_attempt = 0.0
        self._was_connected = False

    @property
    def available(self) -> bool:
        return bool(self._mac) and shutil.which("bluetoothctl") is not None

    def state(self) -> dict[str, str]:
        """Parsed `bluetoothctl info`, e.g. {'Paired': 'yes', ...}."""
        info: dict[str, str] = {}
        for line in _run("bluetoothctl", "info", self._mac, timeout=10).splitlines():
            if line.startswith("\t") and ":" in line:
                key, _, value = line.strip().partition(":")
                info[key.strip()] = value.strip()
        return info

    def is_connected(self) -> bool:
        return bool(self._mac) and self.state().get("Connected") == "yes"

    def connect(self) -> bool:
        log.info("connecting to %s", self._mac)
        return "Connection successful" in _run(
            "bluetoothctl", "connect", self._mac, timeout=30
        )

    def tick(self) -> bool:
        """Called from the main loop. Returns True while the link is up."""
        if not self.available:
            return False

        if self.is_connected():
            if not self._was_connected:
                log.info("bluetooth link up (%s)", self._mac)
                self._was_connected = True
            self._backoff = 0
            return True

        if self._was_connected:
            log.warning("bluetooth link lost")
            self._was_connected = False

        now = time.monotonic()
        if now < self._next_attempt:
            return False

        if self.connect():
            self._was_connected = True
            self._backoff = 0
            log.info("bluetooth reconnected")
            return True

        self._backoff = (
            self._config.bt_backoff_min
            if self._backoff == 0
            else min(self._backoff * 2, self._config.bt_backoff_max)
        )
        self._next_attempt = now + self._backoff
        log.warning("reconnect failed; next attempt in %ds", self._backoff)
        return False
