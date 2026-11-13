# Hotspot setup

Sets up a Coway fan that has no Bluetooth setup and takes its Wi-Fi details
over a hotspot of its own instead, such as the `02F9R` revision of the
AP-1720G (Airmega 250S). The fan takes the setup page's messages unchanged,
sent over TCP rather than Bluetooth, and this script sends them. It then
enrolls with Home Assistant like a fan set up from the setup page.
[HOTSPOT_SETUP.md](../../HOTSPOT_SETUP.md) describes the exchange.

## Use

Needs Node 20 or newer, Coway Local installed in Home Assistant, and a
long-lived access token for an admin user (your profile → **Security** →
**Long-lived access tokens**). The token is only used during setup; delete it
afterwards.

1. On your usual network, run:

   ```sh
   scripts/softap/coway-softap.mjs --ha http://homeassistant.local:8123 --name Upstairs
   ```

   It asks for the token, unless `HA_TOKEN` holds it, and starts
   **Add or provision a fan** through Home Assistant's API, which gives it
   the setup link the setup page would get. The link expires after 15
   minutes.
2. When asked, put the fan in Wi-Fi setup mode, join this computer to the
   fan's hotspot, and press Return. On the AP-1720G, hold **Filter Reset** and
   **Wi-Fi** together for 3 seconds, until the Wi-Fi light blinks.
3. The script lists the networks the fan can see, asks which to use and its
   password, and sends the settings.
4. Rejoin your usual network. The script waits for Home Assistant, resumes
   the setup there, and waits up to about 2 minutes for **Fan ready**.

The computer running the script has to leave your network for step 2. If
something on it needs the network meanwhile, run the script on another one.

Given a setup link instead of `--ha`, it skips the Home Assistant steps:
`coway-softap.mjs '<link>'`, with the computer already on the fan's hotspot,
opens Home Assistant's return link in the browser at the end. The link must
include its `#setup=` part, which the setup page removes from its own address
as it loads, so `--ha` is the usual way.

| Option | Use |
| --- | --- |
| `--ha URL` | Home Assistant's address, to start and finish the setup there. |
| `--name NAME` | The fan's name in Home Assistant. Defaults to `Coway` and the end of its MAC. |
| `--host IP` | The fan's address on its hotspot, `192.168.12.101` by default. |
| `--mac MAC` | The fan's Wi-Fi MAC, if ARP cannot find it. |
| `--no-open` | With a setup link, print the return link instead of opening it. |

`COWAY_WIFI_PASSWORD` in the environment supplies the Wi-Fi password without
a prompt.

The fan's MAC is part of the encryption key for the settings. The script
reads it from ARP while connected to the fan, which on macOS does not always
list the hotspot's address. Then pass it with `--mac`, from the label, your
router or the Coway app. A wrong MAC fails silently: the fan cannot decrypt
the settings and never joins your network.

To go back to Coway's cloud, put the fan in setup mode and pair it in the
Coway app again.

## Testing

`test/fake-fan.mjs` stands in for a fan on `127.0.0.1`. It answers the scan
and the two setup messages as a real `02F9R` did, and prints the settings it
decrypts. `test/fake-ha.mjs` stands in for Home Assistant's API on
`127.0.0.1:18123`, accepting the token `test`:

```sh
node scripts/softap/test/fake-fan.mjs 84:72:07:00:00:01 &
node scripts/softap/test/fake-ha.mjs &
HA_TOKEN=test scripts/softap/coway-softap.mjs --ha http://127.0.0.1:18123 \
  --host 127.0.0.1 --mac 84:72:07:00:00:01 --name Test
```
