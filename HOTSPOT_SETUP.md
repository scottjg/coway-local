# Hotspot setup evidence

Observed in November 2026 with the Coway IoCare iOS app and two AP-1720G
(Airmega 250S) with product code `02F9R`, whose Wi-Fi modules carry I&C
Technology's MAC prefix `84:72:07`. These fans have no Bluetooth setup. In
setup mode they create an open Wi-Fi network instead, and the app sends them
their settings over it.

The phone's traffic was captured from a Mac with `rvictl` and `tcpdump`, and
its HTTPS traffic with an on-device proxy. After setup, the fans' traffic to
Coway Local was captured on the Home Assistant host and the TLS sessions
decrypted with the integration's server key, which the RSA key exchange of
`AES128-SHA256` allows.

## What the app does

1. Before setup, the app calls `POST /api/v1/com/temp-token` on
   `iocare.iotsvc.coway.com`. The reply carries `caUrl`
   (`http://rootca-fs.iocareus.com.s3.ap-northeast-2.amazonaws.com/AmazonRootCA1.pem`),
   `certSvrUrl` (`https://cert.iotp.coway.com/iot/v1/cert/r0`) and a 48-character
   `certToken`: the values a Bluetooth fan receives in `P002`.
2. Holding **Filter Reset** and **Wi-Fi** for 3 seconds puts the fan in setup
   mode. It serves an open network at `192.168.12.101`, which is also the
   network's DHCP server and DNS server. The phone joins it.
3. On TCP port 5001 the app sends `AT*ICT*SCAN\r`. The fan replies
   `*ICT*SCANRESULT:`, 48-byte entries and `[OK]\r\n`, the format the setup
   page reads from the Bluetooth scan characteristic.
4. On TCP port 5040 the app sends `P001` and then `P002`, byte for byte the
   setup page's messages, without the Bluetooth fragment headers. The fan
   answers each with a 6-byte protobuf: field 1 is the command (1, then 2),
   field 2 is 1 for success, field 3 a status code.
5. The app reports its progress to `POST /api/v1/com/pairing-step-history`,
   with result codes such as `A_TYPE_SOCKET_CONN_OK`, `A_WIFI_SEARCH` and
   `A_WIFI_SEARCH_OK`.

Decrypted with the setup page's scheme, the 20-byte key from `P001` followed
by the fan's MAC in capitals, the app's `P002` holds the fields the setup page
sends: `cmd` 2, `ssid`, `pwd`, `bssid`, `type` 1, `macAddr`, `caUrl`,
`certSvrUrl`, `certToken` and `serial`. Differences from the setup page:

- The app's `P001` key is 20 random bytes, not 20 letters. The fan accepts either.
- The app writes `bssid` in capitals. The setup page writes it in lower case.
- `serial` is the fan's own Coway serial, not a `LOCAL0` serial.

The fan's MAC on its hotspot is its MAC on the home network.

## The status code in the reply to `P001`

The reply's field 3 varied: 61 when the fan had been connected to Coway's
cloud, then 2, 5 and 0 on later attempts. These match `STATUS` in
`web/v1/protocol.mjs` read as the fan's last state (61 broker connected,
2 Wi-Fi association failed, 5 DHCP failed), but that reading is inferred.
The setup page's Bluetooth path waits for 72, which these fans never sent.
`scripts/softap` accepts any code with field 2 set to 1.

## Enrollment with Coway Local

With the settings delivered, a `02F9R` fan downloads the CA from `caUrl` and
posts `{"macAddr":…,"certToken":…,"serial":…}` to `certSvrUrl`, as other fans do.
Two things then differ from the AP-1022K:

- **It rejects an enrollment reply written with spaces.** Given aiohttp's
  default `json_response` output (`"success": true`), the fan never requests
  `certUrl` or `privateUrl`. It posts the same enrollment again every 11
  seconds until setup expires, keeps its hotspot up, and its Wi-Fi light
  flashes twice. Given the same reply in compact JSON, it downloads its
  certificate and key and connects to the broker within 6 seconds, and
  chimes.
- **It reports on `MEvent`.** It subscribes to `IoCare/MControl/<service>/<serial>`
  like the AP-1022K, but publishes its `A1010`, `A1020` and `A1050` reports on
  `IoCare/MEvent/<service>/<serial>` rather than BEvent. The reports
  themselves have the same attribute codes.

A fan whose setup never completed left the network: once after a power
cycle, and once on its own a few minutes after its enrollment attempts
stopped. It may keep new Wi-Fi settings only once setup completes, but that
is inferred, not observed.

## Coway's cloud, for comparison

The router's flow log for a fan paired by the app shows the same sequence
against Coway's hosts: the CA from `rootca-fs.iocareus.com` on S3, enrollment
at `cert.iotp.coway.com`, credentials from `icert-fs.iocareus.com`, then MQTT
to `*-ats.iot.ap-northeast-2.amazonaws.com`.
