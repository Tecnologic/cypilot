#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Sets up a Raspberry Pi (Raspberry Pi OS 64-bit, Bookworm or later) as the
# cypilot box on the cart (docs/pi.md):
#
#   - the MCP2518FD CAN FD HAT: SPI and the mcp251xfd overlay in config.txt,
#     can0 at 1 Mbit/s CAN FD without bit rate switch by systemd-networkd;
#   - cypilot in a venv in /opt/cypilot, the DSDL and the register description
#     file fetched now (the cart is offline), a user "cypilot";
#   - the service cypilot.service: the web app on port 80 on can0, with a PIN,
#     restarted when it stops; runs in /var/lib/cypilot/runs (or --runs);
#   - Wi-Fi: a known network when it is in range, else the Pi's own access
#     point (NetworkManager hotspot), checked at every boot;
#   - the host name for http://<name>.local (mDNS by avahi).
#
#   sudo scripts/pi-setup.sh                         # all defaults
#   sudo scripts/pi-setup.sh --runs /mnt/ssd/runs --hostname cart
#   scripts/pi-setup.sh --dry-run --root /tmp/pi     # write the files under /tmp/pi, print the commands
#
# Run it again to change a setting; it replaces its own files. A reboot
# applies the overlay.
set -euo pipefail

usage() {
  sed -n '4,22p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Options:
  --source SPEC        what pip installs (default: $source)
  --runs DIR           where runs are recorded (default: $runs)
  --hostname NAME      host name for NAME.local (default: $hostname)
  --pin PIN            login PIN of the web app (default: kept, else 6 random digits)
  --ssid NAME          SSID of the access point (default: $ssid)
  --psk KEY            WPA2 key of the access point, 8 to 63 characters (default: kept, else random)
  --oscillator HZ      crystal of the MCP2518FD (default: $oscillator; 20000000 on some HATs)
  --interrupt GPIO     interrupt pin of the MCP2518FD (default: $interrupt)
  --spi DEV            SPI chip select of the HAT (default: $spi)
  --bitrate BPS        CAN bit rate (default: $bitrate)
  --dbitrate BPS       CAN FD data bit rate (default: $dbitrate, no bit rate switch)
  --no-ap              no access point: the known networks only
  --dry-run            print the commands, write the files under --root
  --root DIR           prefix for the files (default /; with --dry-run a temp directory)
  -h, --help           this text
EOF
}

source="cypilot @ git+https://github.com/Tecnologic/cypilot"
runs=/var/lib/cypilot/runs
hostname=cypilot
pin=""
ssid=cypilot
psk=""
oscillator=40000000
interrupt=25
spi=spi0-0
bitrate=1000000
dbitrate=1000000
access_point=yes
dry_run=no
root=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) source="$2"; shift 2 ;;
    --runs) runs="$2"; shift 2 ;;
    --hostname) hostname="$2"; shift 2 ;;
    --pin) pin="$2"; shift 2 ;;
    --ssid) ssid="$2"; shift 2 ;;
    --psk) psk="$2"; shift 2 ;;
    --oscillator) oscillator="$2"; shift 2 ;;
    --interrupt) interrupt="$2"; shift 2 ;;
    --spi) spi="$2"; shift 2 ;;
    --bitrate) bitrate="$2"; shift 2 ;;
    --dbitrate) dbitrate="$2"; shift 2 ;;
    --no-ap) access_point=no; shift ;;
    --dry-run) dry_run=yes; shift ;;
    --root) root="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "pi-setup.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [[ $dry_run == yes && -z $root ]]; then
  root="$(mktemp -d)"
fi
if [[ $dry_run == no && $EUID -ne 0 ]]; then
  echo "pi-setup.sh: run it as root (sudo), or with --dry-run" >&2
  exit 1
fi
if [[ -n $psk && (${#psk} -lt 8 || ${#psk} -gt 63) ]]; then
  echo "pi-setup.sh: the access point key has 8 to 63 characters" >&2
  exit 2
fi

venv=/opt/cypilot
env_file=/etc/cypilot/cypilot.env
cache=/var/lib/cypilot/cache

# Runs a command, or prints it in a dry run.
run() {
  if [[ $dry_run == yes ]]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Writes stdin to a file under the root.
write() {
  local path="$root$1"
  mkdir -p "$(dirname "$path")"
  cat >"$path"
  echo "wrote $1"
}

# A value of the existing environment file, so a second run keeps the PIN and the key.
kept() {
  [[ -f "$root$env_file" ]] && sed -n "s/^$1=//p" "$root$env_file" | head -n 1 || true
}

pin="${pin:-$(kept CYPILOT_PIN)}"
pin="${pin:-$(printf '%06d' $((10#$(od -An -N4 -tu4 /dev/urandom | tr -d ' ') % 1000000)))}"
psk="${psk:-$(kept CYPILOT_AP_PSK)}"
psk="${psk:-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')}"

# --- Packages -------------------------------------------------------------------

run apt-get update
run apt-get install -y python3-venv python3-pip git can-utils network-manager avahi-daemon

# --- The CAN FD HAT ---------------------------------------------------------------

config=/boot/firmware/config.txt
[[ -f "$root$config" || $dry_run == yes ]] || config=/boot/config.txt  # before Bookworm
mkdir -p "$(dirname "$root$config")"
touch "$root$config"
sed -i '/# cypilot: begin/,/# cypilot: end/d' "$root$config"
cat >>"$root$config" <<EOF
# cypilot: begin (scripts/pi-setup.sh)
dtparam=spi=on
dtoverlay=mcp251xfd,$spi,oscillator=$oscillator,interrupt=$interrupt
# cypilot: end
EOF
echo "wrote $config (the overlay applies after a reboot)"

write /etc/systemd/network/80-cypilot-can0.network <<EOF
# can0 for cypilot (scripts/pi-setup.sh): CAN FD, the UNIMOC bus.
[Match]
Name=can0

[CAN]
BitRate=$bitrate
DataBitRate=$dbitrate
FDMode=yes
RestartSec=100ms
EOF
run systemctl enable systemd-networkd

# --- cypilot ----------------------------------------------------------------------

if ! id cypilot >/dev/null 2>&1; then
  run useradd --system --home-dir /var/lib/cypilot --create-home --shell /usr/sbin/nologin cypilot
fi
run mkdir -p "$runs" "$cache"
run chown -R cypilot:cypilot /var/lib/cypilot "$runs"
run python3 -m venv "$venv"
run "$venv/bin/pip" install --upgrade pip
run "$venv/bin/pip" install --upgrade "$source"

write "$env_file" <<EOF
# cypilot.service (scripts/pi-setup.sh)
CYPILOT_PIN=$pin
CYPILOT_RUNS=$runs
CYPILOT_CACHE=$cache
CYPILOT_AP_PSK=$psk
EOF
run chmod 600 "$env_file"

# The cart is offline: the DSDL and the register description file are fetched now.
run sudo -u cypilot env "CYPILOT_CACHE=$cache" "$venv/bin/cypilot-ctl" dsdl
run sudo -u cypilot env "CYPILOT_CACHE=$cache" "$venv/bin/python" -c \
  "from cypilot.simulation import fetch_register_descriptions as f; print(f())"

write /etc/systemd/system/cypilot.service <<EOF
[Unit]
Description=cypilot web app on can0
After=network-online.target systemd-networkd.service
Wants=network-online.target

[Service]
User=cypilot
EnvironmentFile=$env_file
ExecStart=$venv/bin/cypilot-ctl --interface socketcan:can0 serve --host 0.0.0.0 --port 80
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

# --- Wi-Fi: a known network, else the access point ----------------------------------

if [[ $access_point == yes ]]; then
  run nmcli connection delete cypilot-ap || true
  run nmcli connection add type wifi ifname wlan0 con-name cypilot-ap autoconnect no ssid "$ssid" \
    802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk"
  write /usr/local/sbin/cypilot-wifi-fallback <<'EOF'
#!/bin/sh
# Opens the cypilot access point when no known Wi-Fi connected within 45 s of the boot
# (scripts/pi-setup.sh).
for _ in $(seq 45); do
  nmcli -t -f TYPE,STATE device | grep -q '^wifi:connected$' && exit 0
  sleep 1
done
exec nmcli connection up cypilot-ap
EOF
  run chmod 755 /usr/local/sbin/cypilot-wifi-fallback
  write /etc/systemd/system/cypilot-wifi-fallback.service <<EOF
[Unit]
Description=cypilot access point when no known Wi-Fi is in range
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cypilot-wifi-fallback

[Install]
WantedBy=multi-user.target
EOF
fi

# --- Host name and services --------------------------------------------------------

run hostnamectl set-hostname "$hostname"
run systemctl daemon-reload
run systemctl enable cypilot.service
if [[ $access_point == yes ]]; then
  run systemctl enable cypilot-wifi-fallback.service
fi
run systemctl restart cypilot.service

cat <<EOF

cypilot is set up. After a reboot (for the CAN HAT):
  http://$hostname.local/   PIN $pin
EOF
if [[ $access_point == yes ]]; then
  echo "  without a known Wi-Fi: access point \"$ssid\", key $psk, then http://10.42.0.1/"
fi
if [[ $dry_run == yes ]]; then
  echo "(dry run: the files are under $root)"
fi
