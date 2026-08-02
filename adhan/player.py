"""Audio playback through the Bluetooth speaker.

mpv does the decoding, so whatever format the adhan file is in just works --
no transcoding step, no format constraints on what the mosque supplies.
Playback is a child process we can kill instantly, which is what makes "stop"
feel immediate.
"""

from __future__ import annotations

import glob
import json
import logging
import shutil
import signal
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


def _find_mpris_script() -> str | None:
    """mpv-mpris, if installed. Optional: it publishes an MPRIS interface so
    external controllers (AVRCP via mpris-proxy, playerctl) can drive us."""
    for pattern in (
        "/usr/lib/*/mpv/mpris.so",
        "/usr/lib/mpv/mpris.so",
        "/etc/mpv/scripts/mpris.so",
    ):
        found = glob.glob(pattern)
        if found:
            return found[0]
    return None


def find_bluetooth_sink_node(mac: str) -> tuple[str, int] | None:
    """(node name, object id) for a connected Bluetooth speaker, e.g.
    ("bluez_output.78_6C_84_9B_BE_F9.1", 74).

    Looked up rather than constructed: the profile suffix differs between
    PipeWire versions.
    """
    if not mac:
        return None
    wanted = mac.upper().replace(":", "_")
    try:
        dump = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=10
        ).stdout
        objects = json.loads(dump)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("pw-dump failed (%s); falling back to the default sink", exc)
        return None

    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name", "")
        if name.startswith("bluez_output.") and wanted in name.upper():
            return name, int(obj.get("id", -1))
    return None


def find_bluetooth_sink(mac: str) -> str | None:
    found = find_bluetooth_sink_node(mac)
    return found[0] if found else None


def _normalise_sink_volume(node_id: int) -> None:
    """Pin the sink at unity.

    The program deliberately has no volume control: loudness belongs to the
    speaker. This only makes sure the Pi is not quietly attenuating on the way
    out -- a leftover `wpctl set-volume` from any earlier session would
    otherwise halve everything with nothing in the config to explain it.
    """
    if node_id < 0:
        return
    subprocess.run(
        ["wpctl", "set-volume", str(node_id), "1.0"],
        capture_output=True,
        check=False,
        timeout=10,
    )


class Player:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._process: subprocess.Popen | None = None
        self._mpris_script = _find_mpris_script()
        self._current: str | None = None

    @property
    def available(self) -> bool:
        return shutil.which("mpv") is not None

    def is_playing(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def play(self, prayer: str) -> bool:
        """Start the adhan. Any playback already running is replaced."""
        path: Path = self._config.audio_for(prayer)
        if not path.is_file():
            log.error("audio file missing: %s", path)
            return False
        if not self.available:
            log.error("mpv is not installed; run install.sh")
            return False

        self.stop()

        command = [
            "mpv",
            "--no-video",
            "--really-quiet",
            "--no-terminal",
        ]
        found = find_bluetooth_sink_node(self._config.bt_sink_mac)
        if found:
            sink, node_id = found
            _normalise_sink_volume(node_id)
            # Target the speaker explicitly rather than trusting whatever
            # PipeWire currently calls the default sink.
            command.append(f"--audio-device=pipewire/{sink}")
        else:
            log.warning("bluetooth sink not found; using the default output")
        if self._mpris_script:
            command.append(f"--script={self._mpris_script}")
        command.append(str(path))

        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.error("could not start mpv: %s", exc)
            return False

        self._current = prayer
        log.info("playing %s adhan (%s)", prayer, path.name)
        return True

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            log.info("stopping playback")
            try:
                self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except OSError:
                pass
        self._process = None
        self._current = None

    def reap(self) -> None:
        """Clear the handle once playback has finished on its own."""
        if self._process is not None and self._process.poll() is not None:
            log.info("playback finished")
            self._process = None
            self._current = None
