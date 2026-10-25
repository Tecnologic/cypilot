# cypilot on a Raspberry Pi on the cart

The Pi on the cart runs the web app on the drives' CAN bus. Phones, tablets
and laptops use it over Wi-Fi (`docs/web_plan.md`, section 7).

## Hardware

- Raspberry Pi 4 or 5, Raspberry Pi OS 64-bit (Bookworm or later).
- CAN FD HAT with the MCP2518FD on SPI, for example the Waveshare 2-CH CAN FD
  HAT or a PiCAN FD. Check its crystal (40 MHz on most, 20 MHz on some) and its
  interrupt pin (GPIO 25 on most). The bus needs its 120 Ω termination at both
  ends; most HATs have a jumper for it.
- A USB SSD for the runs is better than the SD card: runs write all the
  time, a few MB per minute of driving.
- The Pi has no real-time clock. Runs are stamped with the system time, which
  is only right once NTP has set it, so set the clock on a known network or
  fit an RTC.

## Setup

On the Pi, with internet access for the setup (the cart is offline later):

```sh
git clone https://github.com/Tecnologic/cypilot && cd cypilot
sudo scripts/pi-setup.sh --runs /mnt/ssd/runs      # see --help for the HAT, bit rates and names
sudo reboot                                       # for the CAN HAT overlay
```

The script:

- puts `dtparam=spi=on` and
  `dtoverlay=mcp251xfd,spi0-0,oscillator=40000000,interrupt=25` into
  `/boot/firmware/config.txt` (`--spi`, `--oscillator`, `--interrupt`);
- has systemd-networkd bring up `can0` at 1 Mbit/s, CAN FD without bit rate
  switch (`--bitrate`, `--dbitrate`), and restart it after a bus-off;
- installs cypilot into `/opt/cypilot` for a user `cypilot`, and fetches the
  DSDL of the pinned UNIMOC release and its register description file now,
  since cypilot cannot download them on the cart;
- starts `cypilot.service`, which runs
  `cypilot-ctl -i socketcan:can0 serve --host 0.0.0.0 --port 80` at boot and
  again when it stops. Its settings are in `/etc/cypilot/cypilot.env`: the
  login PIN, the runs directory and the cache;
- sets up the Wi-Fi: the Pi joins a known network when one is in range
  (`nmcli device wifi connect <SSID> password <key>` once). Otherwise, 45 s
  after the boot, it opens its own access point (`--ssid`, `--psk`;
  `--no-ap` for none);
- sets the host name (`--hostname`, default `cypilot`) for mDNS.

It ends with the address, the PIN and the access point key. A second run keeps
the PIN and the key unless you give new ones.

## Use

- On a known network: `http://cypilot.local/` (or the Pi's address).
- On the access point: connect to the Wi-Fi `cypilot`, then
  `http://10.42.0.1/`.
- Log in with the PIN. **Take control** on the device that drives the cart.
  The other devices watch, plot and record, and can always use STOP ALL and
  E-STOP. When the operator's phone loses the Wi-Fi, the drives go to sleep
  within the lease time (0.5 s).
- `journalctl -u cypilot -f` shows the log; `candump can0` shows the bus.

## Wi-Fi in the web app

The **Wi-Fi** view, shown only on the Pi, displays the current network and
the address. With control taken, you can:

- **Join a network:** scan, pick one, enter its password. The Pi then
  remembers it as a known network. Devices on the access point lose cypilot
  until they join that network too (`http://cypilot.local/`). If joining
  fails, the Pi opens its access point again, so the cart is never left
  without a network.
- **Forget** a known network.
- **Change the access point:** its name and key apply the next time it
  opens. **Open it now** switches to it at once.

cypilot runs `nmcli` for this (`cypilot-ctl serve --wifi`, `CYPILOT_WIFI=1`
in `/etc/cypilot/cypilot.env`). The polkit rule
`/etc/polkit-1/rules.d/50-cypilot-network.rules` lets the user `cypilot` use
NetworkManager. A second run of the setup keeps the access point key as the
web app last set it.

## Load test

`scripts/load_test.py` runs the server with a recording run, and opens
browsers on the plots of every node:

```sh
python scripts/load_test.py                                   # 4 demo devices, 2 browsers, 60 s
python scripts/load_test.py -i socketcan:can0 --browsers 0 --seconds 3600   # on the Pi, browsers elsewhere
```

It prints the CPU load of the server process, the latency of `/api/status`,
the run's frames, samples and size, and the events each browser received. On
a laptop, with 4 demo devices and 2 browsers, the server process (with
Playwright) took about 30 % of one core. The median status latency was 47 ms.

The W6 acceptance test is a day on the cart bench with the drives; that is
done on the cart, not in CI.
