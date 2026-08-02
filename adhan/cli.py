"""Command line entry point: python3 -m adhan.cli <command>  (or ./adhanctl)"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

from .api import ApiError, ApiUnavailable
from .bluetooth import BluetoothLink, pair, scan
from .config import ENV_PATH, PRAYER_LABELS, Config
from .player import Player, find_bluetooth_sink
from .schedule import Schedule
from .service import AdhanService

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def _run(*args: str, timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return (result.stdout + result.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _print_window(schedule: Schedule, prayers) -> None:
    header = f"{'date':<12} " + " ".join(f"{PRAYER_LABELS[p]:>8}" for p in prayers)
    print(header)
    print("-" * len(header))
    for day in schedule.sorted_days:
        cells = [
            f"{day.adhan[p].strftime('%H:%M'):>8}" if day.adhan.get(p) else f"{'--':>8}"
            for p in prayers
        ]
        print(f"{day.day.isoformat():<12} " + " ".join(cells))


# --------------------------------------------------------------- commands


def cmd_doctor(config: Config) -> int:
    """Check everything a fresh Pi needs. Each of these was a real failure
    during bring-up, and none of them announces itself clearly at runtime."""
    failures = 0

    def check(
        label: str,
        ok: bool,
        fact: str = "",
        hint: str = "",
        fatal: bool = True,
    ) -> None:
        """`fact` is shown when the check passes, `hint` when it fails."""
        nonlocal failures
        mark = OK if ok else (BAD if fatal else WARN)
        detail = fact if ok else (hint or fact)
        print(f"[{mark}] {label}" + (f"  — {detail}" if detail else ""))
        if not ok and fatal:
            failures += 1

    check(
        "mpv installed",
        shutil.which("mpv") is not None,
        hint="sudo apt install mpv mpv-mpris",
    )
    check(
        "audio file present",
        config.audio_path.is_file(),
        fact=str(config.audio_path),
        hint=f"missing: {config.audio_path} (set AUDIO_PATH in .env)",
    )

    active = _run("systemctl", "--user", "is-active", "pipewire")
    check(
        "pipewire running",
        active == "active",
        hint=f"{active or 'inactive'} — systemctl --user start pipewire",
    )
    active = _run("systemctl", "--user", "is-active", "wireplumber")
    check(
        "wireplumber running",
        active == "active",
        hint=f"{active or 'inactive'} — systemctl --user start wireplumber",
    )

    check(
        "user in 'bluetooth' group",
        "bluetooth" in _run("id", "-nG").split(),
        hint="sudo usermod -aG bluetooth $USER  (needed for org.bluez.Media1)",
    )

    linger = _run("loginctl", "show-user", _run("id", "-u"), "-p", "Linger")
    check(
        "lingering enabled",
        "yes" in linger.lower(),
        hint="sudo loginctl enable-linger $USER — else PipeWire dies at logout",
        fatal=False,
    )

    check(
        "A2DP endpoint registered",
        "0000110a" in _run("bluetoothctl", "show").lower(),
        fact="Audio Source UUID present",
        hint="no 'Audio Source' UUID — set monitor.bluez.seat-monitoring=disabled",
    )

    if config.bt_sink_mac:
        state = BluetoothLink(config).state()
        check(
            "speaker paired",
            state.get("Paired") == "yes",
            fact=f"{config.bt_sink_name} [{config.bt_sink_mac}]",
            hint="run ./adhanctl pair",
        )
        check(
            "speaker connected",
            state.get("Connected") == "yes",
            fact=config.bt_sink_mac,
            hint="not connected — the service reconnects on its own",
            fatal=False,
        )
        sink = find_bluetooth_sink(config.bt_sink_mac)
        check(
            "pipewire sink present",
            sink is not None,
            fact=sink or "",
            hint="no bluez_output node — connect the speaker first",
            fatal=False,
        )
    else:
        check("speaker configured", False, hint="run ./adhanctl pair")

    print()
    print("all good" if failures == 0 else f"{failures} blocking problem(s)")
    return 1 if failures else 0


def cmd_pair(config: Config) -> int:
    print('Put the speaker in pairing mode now (on an Echo: "Alexa, pair Bluetooth").')
    print("Scanning for 20s...")
    devices = scan(20)
    if not devices:
        print("nothing found. Is the speaker in pairing mode?")
        return 1

    for index, (mac, name) in enumerate(devices, 1):
        print(f"  {index:2d}. {name}  [{mac}]")
    try:
        choice = int(input("select a device number: ").strip())
        mac, name = devices[choice - 1]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("cancelled")
        return 1

    print(f"pairing with {name} ({mac})...")
    paired, notable = pair(mac)
    for line in notable:
        print("  " + line)

    if not paired:
        print("pairing failed — try again with the speaker freshly in pairing mode")
        return 1

    _write_env("BT_SINK_MAC", mac)
    _write_env("BT_SINK_NAME", name)
    print(f"paired. wrote BT_SINK_MAC={mac} to {ENV_PATH}")
    return 0


def _write_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def cmd_play(config: Config, prayer: str) -> int:
    player = Player(config)
    if not player.play(prayer):
        return 1
    print("playing — Ctrl-C to stop")
    try:
        while player.is_playing():
            time.sleep(0.5)
    except KeyboardInterrupt:
        player.stop()
    return 0


def cmd_fetch(config: Config, schedule: Schedule, today) -> int:
    try:
        schedule.refresh(today)
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 1
    except ApiUnavailable as exc:
        print(f"network unavailable: {exc}", file=sys.stderr)
        return 1
    _print_window(schedule, config.prayers)
    return 0


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adhanctl")
    parser.add_argument(
        "command",
        choices=("run", "doctor", "pair", "fetch", "show", "next", "play", "stop"),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "run":
        return AdhanService(config).run()
    if args.command == "doctor":
        return cmd_doctor(config)
    if args.command == "pair":
        return cmd_pair(config)
    if args.command == "play":
        return cmd_play(config, "manual")
    if args.command == "stop":
        subprocess.run(["pkill", "-f", "mpv --no-video"], check=False)
        return 0

    schedule = Schedule(config)
    schedule.load()
    now = datetime.now(config.timezone)

    if args.command == "fetch":
        return cmd_fetch(config, schedule, now.date())
    if args.command == "show":
        if not len(schedule):
            print("cache is empty; run `./adhanctl fetch`")
            return 1
        print(f"cached {len(schedule)} day(s), fetched {schedule.fetched_on}")
        _print_window(schedule, config.prayers)
        return 0
    if args.command == "next":
        upcoming = schedule.upcoming(now)
        if upcoming is None:
            print("nothing scheduled in the cached window")
            return 1
        prayer, when = upcoming
        hours, rem = divmod(int((when - now).total_seconds()), 3600)
        print(
            f"next: {PRAYER_LABELS[prayer]} at {when:%Y-%m-%d %H:%M} "
            f"(in {hours}h {rem // 60}m)"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
