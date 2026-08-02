#!/usr/bin/env bash
#
# One-shot setup for a fresh Raspberry Pi. Idempotent -- safe to re-run.
#
# Most of what this does is not obvious from any single piece of documentation.
# On a headless Pi, Bluetooth audio fails in three separate ways that all
# report themselves as "Protocol not available" or nothing at all:
#
#   1. The user is not in the 'bluetooth' group, so D-Bus refuses the
#      org.bluez.Media1 calls PipeWire needs to register an A2DP endpoint.
#   2. WirePlumber only starts its bluez monitor when logind reports the seat
#      as active. A headless box has no seat that ever becomes active, so the
#      monitor waits forever and no endpoint is ever registered.
#   3. Without lingering, the user systemd instance -- and PipeWire with it --
#      is killed the moment the SSH session ends.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"

info()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note()  { printf '    %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not root. It will sudo where needed." >&2
  exit 1
fi

# ---------------------------------------------------------------- packages
info "Installing packages"
# Wait for the dpkg lock rather than hanging on it silently: unattended-upgrades
# or another apt someone left running at the console will hold it, and apt's
# default is to block with no explanation at all. Ten minutes, then say so.
APT_OPTS=(-o "DPkg::Lock::Timeout=600")
if ! sudo apt-get "${APT_OPTS[@]}" update -qq; then
  echo "apt is locked by another process. Wait for it to finish, then re-run." >&2
  exit 1
fi
# NEEDRESTART_MODE=a stops needrestart interrupting with a service-restart
# menu when this runs on a terminal.
sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
  apt-get "${APT_OPTS[@]}" install -y -qq \
  mpv mpv-mpris \
  pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth \
  bluez
note "mpv, pipewire, wireplumber, bluez"

# ------------------------------------------------------------------ groups
info "Bluetooth D-Bus permissions"
if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx bluetooth; then
  note "$USER_NAME is already in the 'bluetooth' group"
  GROUP_CHANGED=0
else
  sudo usermod -aG bluetooth "$USER_NAME"
  note "added $USER_NAME to the 'bluetooth' group"
  GROUP_CHANGED=1
fi

# --------------------------------------------------------------- lingering
info "Session lingering"
if loginctl show-user "$USER_NAME" -p Linger 2>/dev/null | grep -qi 'Linger=yes'; then
  note "already enabled"
else
  sudo loginctl enable-linger "$USER_NAME"
  note "enabled: user services now survive logout and start at boot"
fi

# ------------------------------------------------------------- wireplumber
info "WirePlumber headless fix"
WP_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$WP_DIR"
cat > "$WP_DIR/50-bluez-headless.conf" <<'EOF'
# Headless: there is no graphical seat, so logind never reports the seat as
# "active" and WirePlumber keeps its bluez monitor parked. With the monitor
# parked no A2DP endpoint is registered, and every connect attempt fails with
# "br-connection-profile-unavailable".
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
note "$WP_DIR/50-bluez-headless.conf"

# ----------------------------------------------------------- user services
info "Enabling user services"
systemctl --user enable --now pipewire.socket pipewire-pulse.socket \
  wireplumber.service >/dev/null 2>&1 || true
# Bridges Bluetooth AVRCP to MPRIS. Harmless if the speaker never sends
# AVRCP -- an Echo does not -- and useful for any speaker that does.
systemctl --user enable --now mpris-proxy.service >/dev/null 2>&1 || true
note "pipewire, wireplumber, mpris-proxy"

# ------------------------------------------------------------------- .env
info "Configuration"
if [[ -f "$REPO/.env" ]]; then
  note ".env already exists — leaving it alone"
else
  cp "$REPO/.env.example" "$REPO/.env"
  note "created $REPO/.env from the example — fill in your API key"
fi

# ---------------------------------------------------------------- service
info "Installing the adhan service"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|@REPO@|$REPO|g" "$REPO/systemd/adhan.service" > "$UNIT_DIR/adhan.service"
systemctl --user daemon-reload
note "$UNIT_DIR/adhan.service"

# ------------------------------------------------------------ on PATH
info "Command on PATH"
# adhanctl resolves its own real path, so a symlink works without it losing
# track of the repo it lives in. Non-fatal: the ./adhanctl form always works.
if sudo ln -sf "$REPO/adhanctl" /usr/local/bin/adhanctl 2>/dev/null; then
  note "adhanctl works from any directory"
else
  note "could not link into /usr/local/bin; use ./adhanctl from $REPO"
fi

if [[ $GROUP_CHANGED -eq 1 ]]; then
  info "Group membership changed"
  note "The running user session still has the old groups."
  note "Applying now:  sudo systemctl restart user@$(id -u).service"
  sudo systemctl restart "user@$(id -u).service" || true
  sleep 3
fi

# ------------------------------------------------------------- setup
# Everything below is what a person would otherwise have to do by hand after
# the install. Asking here is what makes `./install.sh` the only command:
# whoever is running it is sitting at a terminal, so ask them.
#
# Non-interactive runs (piped, or from a provisioning script) skip the
# questions and print what is left instead -- prompting into a closed stdin
# would hang forever.
env_value() { grep -E "^$1=" "$REPO/.env" | head -1 | cut -d= -f2-; }
set_env()   { sed -i "s|^$1=.*|$1=$2|" "$REPO/.env"; }
interactive() { [[ -t 0 ]]; }

info "Configuring"

# Values already in the environment win over asking. This is what makes a
# scripted install possible:
#   EXPO_PUBLIC_API_KEY=fm_... BT_SINK_MAC=AA:BB:.. ./install.sh
# and it is also the only way to install over a non-interactive SSH command.
for VAR in EXPO_PUBLIC_API_KEY MOSQUE_ID TIMEZONE BT_SINK_MAC BT_SINK_NAME JUMMAH; do
  if [[ -n "${!VAR:-}" && -z "$(env_value "$VAR")" ]]; then
    set_env "$VAR" "${!VAR}"
    note "$VAR taken from the environment"
  fi
done

if [[ -z "$(env_value EXPO_PUBLIC_API_KEY)" ]]; then
  if interactive; then
    printf '    Facemosque API key (Enter to skip): '
    read -r API_KEY
    [[ -n "$API_KEY" ]] && set_env EXPO_PUBLIC_API_KEY "$API_KEY" && note "saved to .env"
  else
    note "no API key in .env — add it, then re-run this script"
  fi
else
  note "API key already set"
fi

if [[ -z "$(env_value BT_SINK_MAC)" ]]; then
  if interactive; then
    printf '    Pair a Bluetooth speaker now? [Y/n] '
    read -r ANSWER
    if [[ ! "$ANSWER" =~ ^[Nn] ]]; then
      "$REPO/adhanctl" pair || note "not paired — run 'adhanctl pair' when ready"
    fi
  else
    note "no speaker configured — run 'adhanctl pair'"
  fi
else
  note "speaker already configured: $(env_value BT_SINK_NAME)"
fi

# ----------------------------------------------------------------- start
# Only with a key: without one the service cannot start, and systemd would
# restart it every 10s forever with nothing but a config error to show for it.
if [[ -n "$(env_value EXPO_PUBLIC_API_KEY)" ]]; then
  info "Starting"
  if "$REPO/adhanctl" fetch >/dev/null 2>&1; then
    note "prayer times fetched"
  else
    note "could not reach the API — the bundled times cover it, and it retries"
  fi
  systemctl --user enable --now adhan >/dev/null 2>&1
  sleep 3
  if [[ "$(systemctl --user is-active adhan)" == "active" ]]; then
    note "service running, and it will start again on its own after a reboot"
    "$REPO/adhanctl" next 2>/dev/null | sed 's/^/    /'
  else
    note "service did not start — see: journalctl --user -u adhan -n 20"
  fi
fi

info "Done"
echo "    adhanctl doctor    check every prerequisite"
echo "    adhanctl play      hear the adhan now (set the volume on the speaker)"
echo "    adhanctl next      when the next one is due"
if [[ -z "$(env_value EXPO_PUBLIC_API_KEY)" ]]; then
  echo
  echo "    Not started yet: put your API key in $REPO/.env and re-run ./install.sh"
fi
