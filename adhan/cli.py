"""Command line entry point: python3 -m adhan.cli <command>  (or ./adhanctl)"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from datetime import date, datetime

from .api import ApiError, ApiUnavailable
from .bluetooth import BluetoothLink, pair, scan
from .config import ENV_PATH, PRAYER_LABELS, REPO_ROOT, Config, read_env_file
from .player import Player, find_bluetooth_sink
from .schedule import Schedule, fetch_years, select_jummah, write_bundle
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


def _print_window(
    schedule: Schedule, config: Config, today: date, limit: int | None = None
) -> None:
    prayers = config.prayers
    header = f"{'date':<12} " + " ".join(f"{PRAYER_LABELS[p]:>8}" for p in prayers)
    print(header)
    print("-" * len(header))
    # The bundled fallback runs from January, and nobody needs to be shown
    # prayer times that have already happened.
    days = [d for d in schedule.sorted_days if d.day >= today]
    shown = days[:limit] if limit else days
    for day in shown:
        cells = [
            f"{day.adhan[p].strftime('%H:%M'):>8}" if day.adhan.get(p) else f"{'--':>8}"
            for p in prayers
        ]
        print(f"{day.day.isoformat():<12} " + " ".join(cells))
    if not shown:
        print("(nothing from today onwards)")
    elif len(shown) < len(days):
        print(f"... and {len(days) - len(shown)} more day(s), to {days[-1].day}")
    _print_jummah(schedule, config)


def _print_jummah(schedule: Schedule, config: Config) -> None:
    slots = schedule.jummah
    if not slots:
        return
    playing = {s.key for s in select_jummah(slots, config.jummah_choice)}
    print()
    print("Jummah — on Fridays these replace Dhuhr:")
    for slot in slots:
        mark = "plays" if slot.key in playing else "skipped"
        print(f"  {slot.at.strftime('%H:%M')}  {slot.label:<28} {mark}")
    if config.jummah_choice is None:
        print("  (JUMMAH is blank, so every jummah plays)")


# --------------------------------------------------------------- commands

HELP = """\
adhanctl — plays the adhan at each waqt through a Bluetooth speaker.

  Every day
    adhanctl next                 when the next adhan is due
    adhanctl show                 the schedule, and the jummah times
    adhanctl play                 play the adhan now — a speaker test
    adhanctl stop                 stop playback

  Settings — each of these restarts the service for you
    adhanctl set                  every setting, with its short name
    adhanctl set key fm_...       the Facemosque API key
    adhanctl set mosque 7         which mosque (a number; the API rejects slugs)
    adhanctl set tz Asia/Dhaka    the timezone the mosque is in
    adhanctl set fajr=            clear a setting

  Friday — the jummah adhan replaces dhuhr
    adhanctl jummah               which jummah the mosque holds, and which play
    adhanctl jummah 2             only the 2nd
    adhanctl jummah all           every one

  Speaker
    adhanctl pair                 scan, pair, and remember a speaker
                                  (put it in pairing mode first: on an Echo,
                                  say "Alexa, pair Bluetooth")
    There is no volume setting. Turn the speaker itself up or down.

  Prayer times
    adhanctl fetch                refresh the 30-day window now
    adhanctl bundle               rebuild the offline fallback in data/
    They refresh every 3 days on their own, and a year of times ships in the
    repo, so the adhan still plays with no internet at all.

  When something is wrong
    adhanctl doctor               every prerequisite, with the fix for each
    journalctl --user -u adhan -f            watch the log
    systemctl --user restart adhan           restart it
    systemctl --user status adhan            is it running?

  Add -v to any command for debug logging.

The service starts by itself at boot — nobody has to log in.
"""


def cmd_help() -> int:
    print(HELP, end="")
    print(f"Installed in:  {REPO_ROOT}")
    print(f"Settings:      {ENV_PATH}")
    return 0



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
    check(
        "offline fallback times",
        config.fallback_path.is_file(),
        fact=config.fallback_path.name,
        hint=f"no {config.fallback_path.name} — run ./adhanctl bundle",
        fatal=False,
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


def _restart_service(start_if_stopped: bool = False) -> int:
    """A config change means nothing until the service re-reads it.

    `start_if_stopped` is for the case that finishes an install: the service
    could not run without an API key, so supplying one should start it rather
    than leave the user to work out that a last step remains.
    """
    if _run("systemctl", "--user", "is-active", "adhan") != "active":
        if start_if_stopped:
            _run("systemctl", "--user", "enable", "--now", "adhan", timeout=30)
            time.sleep(3)
            if _run("systemctl", "--user", "is-active", "adhan") == "active":
                print("service started, and it will start on its own after a reboot")
                return 0
            print("service did not start — see: journalctl --user -u adhan -n 20")
            return 1
        print("the service is not running; this applies when it next starts")
        return 0
    # Restarting kills the cgroup, and mpv with it. Not mid-adhan.
    if _run("pgrep", "-f", "[m]pv --no-video"):
        print("an adhan is playing right now — apply this once it finishes:")
        print("  systemctl --user restart adhan")
        return 0
    _run("systemctl", "--user", "restart", "adhan", timeout=30)
    print("service restarted")
    return 0


# Short names for the settings people actually change. EXPO_PUBLIC_API_KEY is
# the API's own name for it and has to stay that in .env, but nobody should
# have to type it.
ALIASES = {
    "key": "EXPO_PUBLIC_API_KEY",
    "url": "EXPO_PUBLIC_API_BASE_URL",
    "mosque": "MOSQUE_ID",
    "tz": "TIMEZONE",
    "timezone": "TIMEZONE",
    "audio": "AUDIO_PATH",
    "fajr": "AUDIO_PATH_FAJR",
    "speaker": "BT_SINK_MAC",
    "mac": "BT_SINK_MAC",
    "name": "BT_SINK_NAME",
    "alexa": "ALEXA_ENABLED",
    "prayers": "PRAYERS",
    "jummah": "JUMMAH",
    "days": "SCHEDULE_DAYS",
    "refresh": "REFRESH_INTERVAL_DAYS",
    "fallback": "FALLBACK_PATH",
}


def _resolve_key(name: str, known: dict) -> str | None:
    """Short name, full name, or any unambiguous part of one."""
    lowered = name.strip().lower()
    if lowered.upper() in known:
        return lowered.upper()
    if lowered in ALIASES and ALIASES[lowered] in known:
        return ALIASES[lowered]
    matches = [k for k in known if lowered in k.lower()]
    return matches[0] if len(matches) == 1 else None


def _short_name(key: str) -> str:
    for alias, full in ALIASES.items():
        if full == key:
            return alias
    return ""


def _mask(key: str, value: str) -> str:
    """Never print a whole API key: this output gets pasted into chats."""
    if not value or "KEY" not in key:
        return value
    return value[:7] + "…" if len(value) > 10 else "…"


def cmd_set(words: list[str]) -> int:
    """Show or change a setting in .env, without opening an editor.

    Takes `set key=value` or `set key value`, since people type both.
    """
    known = read_env_file(REPO_ROOT / ".env.example")
    current = read_env_file(ENV_PATH)

    if not words:
        print(f"{ENV_PATH}")
        print()
        for key in known:
            print(
                f"  {_short_name(key):<9} {key}={_mask(key, current.get(key, ''))}"
            )
        print()
        print("change one with:  adhanctl set key fm_...   (short name or full)")
        return 0

    if len(words) == 1:
        name, separator, value = words[0].partition("=")
        if not separator:
            print(
                f"no value given. Use:  adhanctl set {name} <value>\n"
                f"          to clear:  adhanctl set {name}=",
                file=sys.stderr,
            )
            return 1
    else:
        name, value = words[0].rstrip("="), " ".join(words[1:])

    key = _resolve_key(name, known)
    value = value.strip()
    if key is None:
        print(f"unknown setting {name!r} — `adhanctl set` lists them", file=sys.stderr)
        return 1

    _write_env(key, value)
    print(f"{key}={_mask(key, value)}")
    # Supplying the key is what makes a half-finished install runnable.
    return _restart_service(
        start_if_stopped=(key == "EXPO_PUBLIC_API_KEY" and bool(value))
    )


def cmd_jummah(config: Config, schedule: Schedule, value: str | None) -> int:
    """Show or set which Friday prayer gets an adhan."""
    slots = schedule.jummah
    if not slots:
        print("no jummah times known for this mosque — run `adhanctl fetch` first")
        return 1

    if value is None:
        _print_jummah(schedule, config)
        print()
        print("change it with:  adhanctl jummah 2      (only the 2nd)")
        print("                 adhanctl jummah all    (every one)")
        return 0

    if value.strip().lower() in {"all", "every", "any"}:
        _write_env("JUMMAH", "")
        print(f"every jummah will play ({len(slots)} of them)")
        return _restart_service()

    try:
        number = int(value)
    except ValueError:
        print(f"'{value}' is not a number — use a number, or 'all'", file=sys.stderr)
        return 1
    if number < 1:
        print("the jummah number counts from 1", file=sys.stderr)
        return 1

    _write_env("JUMMAH", str(number))
    picked = select_jummah(slots, number)[0]
    if number > len(slots):
        print(
            f"the mosque holds {len(slots)} jummah, so {number} means the last one"
        )
    print(f"{picked.label} at {picked.at:%H:%M} will play")
    return _restart_service()


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
    _print_window(schedule, config, today, limit=10)
    return 0


def cmd_bundle(config: Config, today: date) -> int:
    """Rewrite the offline fallback that ships in the repo.

    The running service maintains its own copy monthly, in the state
    directory. This one is the baseline a fresh clone starts from, so it is a
    deliberate act: run it and commit the result when the mosque publishes a
    new year, or when you point the repo at a different mosque.
    """
    try:
        days, jummah = fetch_years(config, today)
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 1
    except ApiUnavailable as exc:
        print(f"network unavailable: {exc}", file=sys.stderr)
        return 1

    if not days:
        print("nothing to bundle", file=sys.stderr)
        return 1

    path = write_bundle(config.fallback_path, config.mosque_id, days, jummah, today)
    print(
        f"wrote {path} — {len(days)} day(s) "
        f"({days[0].day} .. {days[-1].day}), {len(jummah)} jummah"
    )
    print("commit it so a fresh clone with no internet still knows the times.")
    return 0


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adhanctl", add_help=False)
    parser.add_argument(
        "command",
        nargs="?",
        default="help",
        choices=(
            "help", "run", "doctor", "pair", "fetch", "bundle",
            "show", "next", "set", "jummah", "play", "stop",
        ),
    )
    parser.add_argument(
        "value",
        nargs="*",
        help="for `set`: `key value` or `key=value`. For `jummah`: a number, "
        "or 'all'. Omit either to see the current setting.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    # -h/--help is handled by hand so it prints the same guide as `help`,
    # rather than argparse's one-line usage.
    parser.add_argument("-h", "--help", action="store_true", dest="want_help")
    args = parser.parse_args(argv)

    if args.want_help or args.command == "help":
        return cmd_help()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Before Config.load(): without an API key the config will not load at all,
    # and that is exactly when someone needs to set one.
    if args.command == "set":
        return cmd_set(args.value)

    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        print("fix it with:  adhanctl set KEY=value", file=sys.stderr)
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
        # The brackets keep the pattern from matching the shell that is running
        # this command: pkill -f sees full command lines, and a script whose
        # own line contains "mpv --no-video" would otherwise kill itself. As a
        # regex, "[m]pv" matches "mpv" but not the literal text "[m]pv".
        _run("pkill", "-f", "[m]pv --no-video")
        return 0

    schedule = Schedule(config)
    schedule.load()
    now = datetime.now(config.timezone)

    if args.command == "fetch":
        return cmd_fetch(config, schedule, now.date())
    if args.command == "bundle":
        return cmd_bundle(config, now.date())
    if args.command == "jummah":
        return cmd_jummah(config, schedule, args.value[0] if args.value else None)
    if args.command == "show":
        if not len(schedule):
            print("nothing known; run `./adhanctl fetch`")
            return 1
        print(
            f"{len(schedule)} day(s) known, last fetched "
            f"{schedule.fetched_on or 'never — using the bundled times'}"
        )
        due = " (rebuild due)" if schedule.needs_bundle_refresh(now.date()) else ""
        print(
            f"offline fallback built {schedule.fallback_generated or 'never'}{due}, "
            f"rebuilt monthly"
        )
        _print_window(schedule, config, now.date(), limit=14)
        return 0
    if args.command == "next":
        upcoming = schedule.upcoming(now)
        if upcoming is None:
            print("nothing scheduled in the known window")
            return 1
        slot, when = upcoming
        hours, rem = divmod(int((when - now).total_seconds()), 3600)
        print(
            f"next: {slot.label} at {when:%Y-%m-%d %H:%M} "
            f"(in {hours}h {rem // 60}m)"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
