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
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
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

# ----------------------------------------------------------------- finish
if [[ $GROUP_CHANGED -eq 1 ]]; then
  info "Group membership changed"
  note "The running user session still has the old groups."
  note "Applying now:  sudo systemctl restart user@$(id -u).service"
  sudo systemctl restart "user@$(id -u).service" || true
  sleep 3
fi

info "Next steps"
cat <<EOF
    1. Edit .env           — API key, mosque id, timezone
    2. ./adhanctl pair        — pair the speaker (put it in pairing mode first)
    3. ./adhanctl doctor      — verify everything
    4. ./adhanctl fetch       — pull the prayer times
    5. systemctl --user enable --now adhan
    6. Say "Alexa, discover devices" so voice stop works
EOF
