# Coway Local

A Home Assistant custom integration for local control of Coway air purifiers, tested with the **AP-1022K**.

Using this integration disconnects your fan from the Coway cloud. The fan is paired with Home Assistant directly: the integration runs its own MQTT broker, and the fan connects to that instead. The Coway mobile app no longer controls a fan set up this way.

Setup is done from a web page that uses Web Bluetooth to give the fan your Wi-Fi details and the address of your Home Assistant instance. Chrome only allows Web Bluetooth on HTTPS pages, so the setup page is hosted on GitHub Pages instead of being served by Home Assistant.

This is experimental. It implements the protocol observed on one AP-1022K, and the only other model it has been used with is the AP-1720G. See [Compatibility](#compatibility).

## What you get

- **Fan:** power, three manual speeds, smart and sleep modes.
- **Settings:** lighting level, timer, smart-mode sensitivity, pre-filter cleaning cycle.
- **Sensors:** PM2.5, ambient light, pre-filter remaining, MAX2 filter remaining.
- **Status:** a connection sensor, a refresh button, and a last-seen timestamp (disabled by default).

## Requirements

- Home Assistant Core **2026.10.2** or newer.
- A fixed LAN address for Home Assistant. Reserve it in your router: the address is written into the fan, and the fan must be set up again if it changes.
- **Chrome on a Mac or Android device** within Bluetooth range of the fan, for the one-time setup. Safari and iPhone browsers do not work.
- A 2.4 GHz Wi-Fi network for the fan.

## Install

**With HACS:** open the repository in HACS with the button below, install **Coway Local**, and restart Home Assistant.

[![Open the Coway Local repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=scottjg&repository=coway-local&category=integration)

If the button does not reach your Home Assistant, add `https://github.com/scottjg/coway-local` in HACS as a custom repository with category **Integration**.

**Manually:** copy `custom_components/coway_local` into your Home Assistant configuration directory and restart. The release ZIP has the same layout and can be extracted into `/config`.

After the restart, add the integration with the button below, or find **Coway Local** under **Settings → Devices & services → Add integration**.

[![Add the Coway Local integration to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=coway_local)

The form shows the LAN address that Home Assistant detected for itself. In most installations you only need to confirm it and submit.

- **Address:** change it only if the fan cannot reach Home Assistant there. With Docker bridge networking, enter the Docker host’s LAN address instead of the detected container address.
- **Advanced settings:** the three TCP ports the fan connects to (HTTP **8080**, HTTPS **9443**, MQTT **9883**), the listen address, and the setup page URL. The defaults suit most installations.

If the integration shows “setup retrying”, a port is probably in use. Choose another under **Configure → Server settings**.

## Add a fan

1. Open **Coway Local → Configure → Add or provision a fan**. The Bluetooth setup page opens in a new tab. If the browser blocks it, use the **Open website** button. Keep the Home Assistant dialog open.
2. Put the fan in Wi-Fi setup mode by holding down its **Mode** button, or whichever button controls the fan speed.
3. Follow the wizard: choose the fan, choose a Wi-Fi network and enter its password, review, and connect.
4. The browser returns to Home Assistant when Bluetooth setup succeeds. Home Assistant then waits for the fan to connect, which can take another 2 minutes. Leave the dialog open until **Fan ready** appears.

The setup link expires after 15 minutes. If it expires, or the fan leaves setup mode, start again from step 1.

The Wi-Fi password is typed into the setup page and sent to the fan over Bluetooth. Home Assistant never receives or stores it.

Some fans have no Bluetooth setup: in setup mode they create a Wi-Fi hotspot of their own, which the Coway app asks you to join. Set those up with [`scripts/softap`](scripts/softap/README.md), which sends the same settings over the fan's hotspot.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| The fan does not appear in the Bluetooth picker | The fan is in setup mode and you are using Chrome on a Mac or Android device. |
| Wi-Fi connection fails | The network is 2.4 GHz and the password is correct. Put the fan back in setup mode and try again. |
| Setup stalls after Wi-Fi connects | The fan cannot reach Home Assistant. Check the configured address, the three ports, and any firewall or VLAN rules between them. |
| The integration shows “setup retrying” | Usually a port conflict, or the credential storage is unreadable. |
| Controls are unavailable | Check the connection sensor, that the fan has power, and that it can still reach Home Assistant. Restarting the integration does not require setting the fan up again. |
| A repair notice says the address changed | Home Assistant no longer has the address the fan was given. Restore the address, or set the new one under **Server settings** and set the fan up again. |

## Networking

The fan connects to Home Assistant on the three configured TCP ports. Changing the address or ports means setting up each fan again.

| Deployment | What to do |
| --- | --- |
| Home Assistant OS | Nothing extra. The ports are opened on the Home Assistant machine itself. |
| Container, host network | Use the Docker host’s LAN address. |
| Container, bridge network | Use the Docker host’s LAN address, and publish all three ports with the same number inside and outside: `8080:8080`, `9443:9443`, `9883:9883`. |
| Fans on a separate VLAN | Allow the fans to reach Home Assistant on the three ports. |

Keep these ports on your trusted LAN. Do not forward them from the Internet.

Home Assistant itself can stay on plain HTTP. The setup page talks to the fan over Bluetooth and then sends the browser back to Home Assistant with a one-use link; it makes no other requests to Home Assistant.

<details>
<summary>What each port carries</summary>

- **HTTP (8080):** the fan downloads the local certificate authority’s public certificate. The fan’s firmware does this over plain HTTP.
- **HTTPS (9443):** the fan enrolls and downloads its own certificate and private key. Each setup link is valid for 15 minutes and for one fan.
- **MQTT over TLS (9883):** the fan’s ongoing connection. It must present its enrolled certificate. The firmware needs TLS 1.2 with `AES128-SHA256`.

Each fan can publish and subscribe only on its own topics. The broker serves these fans only; it does not replace Home Assistant’s MQTT integration.

</details>

## The setup page

The setup page is a static site with no backend, analytics, or third-party scripts. By default the integration uses the copy published from this repository at `https://scottjg.github.io/coway-local/`.

To host your own:

- **On a static HTTPS host:** publish the `web/` directory and enter its URL as the setup page URL in the integration’s advanced settings.
- **On the computer you run setup from:** serve it locally, set the setup page URL to `http://localhost:18765/`, and open the setup link in Chrome on that same computer. This also works without Internet access.

  ```sh
  python3 -m http.server 18765 --bind 127.0.0.1 --directory web
  ```

The page is versioned by setup-link format. The integration appends `v1/` to the configured URL, so each installed release opens a page that understands its links. Enter the URL without the `v1/`.

## Backup and removal

Certificates and keys are stored in `/config/.storage/coway_local/`. Include that directory in your backups. If it is lost, each fan must be set up again.

- **Restarting or reloading** the integration disconnects the fans briefly. They reconnect on their own.
- **Removing a fan** in Home Assistant revokes its access and deletes its credentials. It does not reset the fan itself.
- **Removing the integration** keeps the stored credentials, so reinstalling it restores your fans.

## Compatibility

Tested on one AP-1022K (MCU firmware `V1.0.0.1`, Wi-Fi firmware `000011`), set up from Chrome on macOS into Home Assistant OS 18.3 with Core 2026.10.2. Setup, control, sensors, and reconnecting after a restart work on that fan.

Also used with three AP-1720G (Airmega 250S) on Home Assistant Core 2026.11.1, firmware versions not recorded:

- **Product code `02FWK`**, set up over Bluetooth like the AP-1022K.
- **Product code `02F9R`**, two fans with no Bluetooth setup, set up over their own hotspot with [`scripts/softap`](scripts/softap/README.md). These need the certificate server's replies in compact JSON and their reports accepted on the `MEvent` topic. [HOTSPOT_SETUP.md](HOTSPOT_SETUP.md) has the details.

On all three, sensors and reconnecting after a Home Assistant restart work, and fan speed control was tried on a `02F9R`. The AP-1720G's own modes, such as rapid mode, and its light settings are not mapped: the controls use the AP-1022K's.

Not tested: other models, other firmware, and Home Assistant Container networking.

Other models may get through Bluetooth setup, because the picker accepts any device that advertises like a Coway fan in setup mode ([how discovery works](BLE_DISCOVERY.md)). That does not make them compatible: the controls and sensors use the AP-1022K’s mappings.

Not implemented: PM10 and other air-quality metrics, filter notification flags, and firmware updates.

### Sensor mappings

The sensors read the fan’s periodic `A1020` reports.

| Sensor | Attribute | Meaning |
| --- | --- | --- |
| PM2.5 | `A1020.0001` | Concentration in μg/m³ |
| Ambient light | `A1020.0007` | Illuminance in lx. The scaling has not been calibrated. |
| Pre-filter remaining | `A1020.0011` | `100 − usage`: percent remaining until cleaning |
| MAX2 filter remaining | `A1020.0012` | `100 − usage`: percent remaining until replacement |

PM2.5 is identified by [Coway’s public sensor definitions](https://iocare2.coway.com/_next/static/chunks/3kv1xb4at9t7g.js). The light and filter mappings agree with [PyCoway’s sensor codes](https://github.com/Antonio112009/pycoway/blob/9d5929bee7f4d3a61e45275bb6486669b4b3474e/src/pycoway/constants.py) and [filter conversion](https://github.com/Antonio112009/pycoway/blob/9d5929bee7f4d3a61e45275bb6486669b4b3474e/src/pycoway/devices/parser.py). The filter percentages have not been compared side by side with the Coway app.

A sensor shows “unknown” until the fan’s first report after connecting, and when the fan reports a value that is out of range.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
node --test tests/web.test.mjs
.venv/bin/python scripts/package.py
```

The Python tests run the service and a real Home Assistant instance against loopback HTTP, TLS, and MQTT sockets with temporary credentials. No test needs a fan.

To try the setup wizard with a simulated fan, run `python tests/browser/serve.py` and open `http://localhost:18765/preview`.

Things to know before changing code:

- **aMQTT is pinned to 0.12.1** because the broker overrides its private hooks. Re-run the tests before changing the pin.
- **The setup page deploys from `main`.** Any change under `web/` goes live through the GitHub Pages workflow, for every installed release on that link format. A change that older releases cannot handle needs a new `web/v2/` directory and a bump to `SETUP_FORMAT` in `const.py`; `web/v1/` stays published.
- **The site root serves the 0.4.2 page**, which releases up to 0.4.2 open. The workflow builds it from the `v0.4.2` tag.

## License

MIT. The broker includes behavior adapted from aMQTT, whose copyright and license are preserved in `LICENSE.amqtt`.

This project is not affiliated with Coway and is not an official Home Assistant integration.
