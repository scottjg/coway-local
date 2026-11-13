#!/usr/bin/env node
// Sets up a Coway fan that takes its Wi-Fi settings over its own hotspot
// instead of Bluetooth. It sends the setup page's P001/P002 messages over TCP,
// so the fan enrolls with Home Assistant as one set up from the page does.
// HOTSPOT_SETUP.md at the repository root describes the exchange.
//
// Usage: coway-softap.mjs --ha URL [--name NAME] [--host IP] [--mac MAC]
//        coway-softap.mjs [--name NAME] [--host IP] [--mac MAC] [--no-open] SETUP_URL
import { execFileSync } from 'node:child_process';
import net from 'node:net';
import { createInterface } from 'node:readline/promises';
import { parseArgs } from 'node:util';
import { canonicalMac, decodeProto, p001, p002, parseScan, randomLetters } from '../../web/v1/protocol.mjs';
import { decodeSetup, wifiConfig } from '../../web/v1/bootstrap.mjs';

const SCAN_PORT = 5001, SETUP_PORT = 5040;

const { values: opts, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    ha: { type: 'string' },
    name: { type: 'string' },
    host: { type: 'string', default: '192.168.12.101' },
    mac: { type: 'string' },
    'no-open': { type: 'boolean', default: false },
    help: { type: 'boolean', short: 'h', default: false },
  },
});

const USAGE = `Usage: coway-softap.mjs --ha URL [options]
       coway-softap.mjs [options] SETUP_URL

With --ha, run this on your usual network. It starts Coway Local's
"Add or provision a fan" through Home Assistant's API, using a long-lived
access token from HA_TOKEN or a prompt, then asks you to join the fan's
hotspot, and once the Mac is back it waits for Home Assistant to report the
fan ready.

Given a SETUP_URL instead, the setup link from that dialog including its
#setup= part, it expects the Mac to be on the fan's hotspot already.

Options:
  --ha URL      Home Assistant's address, such as http://192.168.1.10:8123
  --name NAME   name for the fan in Home Assistant (default: Coway + end of its MAC)
  --host IP     the fan's address on its hotspot (default: 192.168.12.101)
  --mac MAC     the fan's MAC, if ARP cannot find it
  --no-open     with SETUP_URL, print Home Assistant's return link instead of opening it`;

function fail(message) {
  console.error(`coway-softap: ${message}`);
  process.exit(1);
}

function connect(host, port) {
  return new Promise((resolve, reject) => {
    const socket = net.connect({ host, port, timeout: 10000 });
    socket.once('connect', () => { socket.setTimeout(0); resolve(socket); });
    socket.once('timeout', () => {
      socket.destroy();
      reject(new Error(`no answer from ${host}:${port}. Is this Mac on the fan's hotspot, and the fan in setup mode?`));
    });
    socket.once('error', reject);
  });
}

// Returns until(done, ms, what): waits for done(buffer) to report how many
// buffered bytes make up a complete message, and returns those bytes.
function reader(socket) {
  let buffer = Buffer.alloc(0), wake = () => {}, closed = false, error;
  socket.on('data', data => { buffer = Buffer.concat([buffer, data]); wake(); });
  socket.on('end', () => { closed = true; wake(); });
  socket.on('error', e => { error = e; wake(); });
  return async function until(done, ms, what) {
    const deadline = Date.now() + ms;
    for (;;) {
      const n = done(buffer);
      if (n) { const message = buffer.subarray(0, n); buffer = buffer.subarray(n); return message; }
      if (error) throw error;
      if (closed) throw new Error(`the fan closed the connection while ${what}`);
      const left = deadline - Date.now();
      if (left <= 0) throw new Error(`timed out ${what}`);
      await new Promise(resolve => {
        const timer = setTimeout(resolve, left);
        wake = () => { clearTimeout(timer); resolve(); };
      });
    }
  };
}

const write = (socket, data) => new Promise((resolve, reject) => socket.write(data, e => e ? reject(e) : resolve()));

// The fan's replies are bare protobuf messages (cmd, ok, code) with no length
// prefix. Each starts with the cmd tag, 0x08, which marks where one ends.
function statusLength(buffer) {
  for (let n = 2; n <= buffer.length; n++) {
    if (n < buffer.length && buffer[n] !== 0x08) continue;
    try {
      const fields = decodeProto(buffer.subarray(0, n));
      if (fields[1] !== undefined && fields[2] !== undefined) return n;
    } catch {}
  }
  return 0;
}

function scanLength(buffer) {
  const end = buffer.indexOf('[OK]\r\n');
  return end < 0 ? 0 : end + 6;
}

function fanMac(host) {
  if (opts.mac) return canonicalMac(opts.mac);
  // arp exits non-zero when it lacks an entry, so read its output either way.
  let out;
  try { out = execFileSync('/usr/sbin/arp', ['-an'], { encoding: 'utf8' }); } catch (error) { out = String(error.stdout ?? ''); }
  const line = out.split('\n').find(l => l.includes(`(${host})`));
  const match = line?.match(/ at ([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5}) /i);
  if (!match) throw new Error(`ARP has no MAC for ${host}${line ? ` (${line.trim()})` : ''}; pass the fan's MAC with --mac`);
  // macOS drops leading zeros: 84:72:7:62:94:f3.
  return match[1].split(':').map(octet => octet.padStart(2, '0')).join('').toUpperCase();
}

// Reads the fan's MAC while connected, when ARP is sure to have it. Joining
// the hotspot can take a while after Return, so keep trying for a minute.
async function scan(host) {
  const deadline = Date.now() + 60000;
  let socket;
  for (;;) {
    try { socket = await connect(host, SCAN_PORT); break; } catch (error) {
      if (Date.now() > deadline) throw error;
      process.stdout.write('.');
      await sleep(3000);
    }
  }
  try {
    const until = reader(socket);
    const mac = fanMac(host);
    await write(socket, 'AT*ICT*SCAN\r');
    return { mac, networks: parseScan(await until(scanLength, 25000, 'waiting for the Wi-Fi scan')) };
  } finally { socket.destroy(); }
}

// One line reader for the whole run, so piped answers queue up rather than
// being lost between prompts.
let lines;
function nextLine() {
  if (!lines) {
    lines = { queue: [], waiting: [], closed: false, rl: createInterface({ input: process.stdin, terminal: false }) };
    lines.rl.on('line', line => lines.waiting.length ? lines.waiting.shift().resolve(line) : lines.queue.push(line));
    lines.rl.on('close', () => {
      lines.closed = true;
      for (const waiter of lines.waiting.splice(0)) waiter.reject(new Error('input ended before an answer'));
    });
  }
  if (lines.queue.length) return Promise.resolve(lines.queue.shift());
  if (lines.closed) return Promise.reject(new Error('input ended before an answer'));
  return new Promise((resolve, reject) => lines.waiting.push({ resolve, reject }));
}

async function ask(question) {
  process.stdout.write(question);
  return (await nextLine()).trim();
}

// On a terminal, raw mode stops the typing being echoed. The reader still
// gets the characters, ending the line at Return.
async function askHidden(prompt) {
  process.stdout.write(prompt);
  const tty = process.stdin.isTTY;
  const onData = data => {
    if (!String(data).includes('\u0003')) return;
    process.stdin.setRawMode(false); process.stdout.write('\n'); process.exit(130);
  };
  if (tty) { process.stdin.setRawMode(true); process.stdin.on('data', onData); }
  try {
    let value = '';
    for (const ch of await nextLine()) value = ch === '\u007f' || ch === '\b' ? [...value].slice(0, -1).join('') : value + ch;
    return value;
  } finally {
    if (tty) { process.stdin.off('data', onData); process.stdin.setRawMode(false); process.stdout.write('\n'); }
  }
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function chooseNetwork(networks) {
  const usable = networks.filter(n => n.ssid && n.channel <= 14).sort((a, b) => b.rssi - a.rssi);
  if (!usable.length) throw new Error('the fan found no 2.4 GHz networks');
  usable.forEach((n, i) => {
    const bssid = n.bssid.match(/../g).join(':');
    console.log(`  ${String(i + 1).padStart(2)}  ${n.ssid.padEnd(32)}  ch ${String(n.channel).padStart(2)}  ${n.rssi} dBm  ${bssid}${n.security ? '' : '  (open)'}`);
  });
  const answer = (await ask('Network [1]: ')) || '1';
  const network = usable[Number(answer) - 1];
  if (!network) throw new Error(`no network ${answer}`);
  return network;
}

async function provision(host, config) {
  const socket = await connect(host, SETUP_PORT);
  try {
    const until = reader(socket);
    const key = randomLetters(20);
    const message = await p002(config, key);
    await write(socket, p001(key));
    const hello = decodeProto(await until(statusLength, 15000, 'waiting for the fan to accept the setup key'));
    console.log(`Setup key: cmd ${hello[1]}, ok ${hello[2]}, code ${hello[3] ?? 0}`);
    if (hello[1] !== 1 || hello[2] !== 1) throw new Error('the fan rejected the setup key');
    await write(socket, message);
    const ack = decodeProto(await until(statusLength, 15000, 'waiting for the fan to accept the settings'));
    console.log(`Settings: cmd ${ack[1]}, ok ${ack[2]}, code ${ack[3] ?? 0}`);
    if (ack[1] !== 2 || ack[2] !== 1) throw new Error('the fan rejected the settings');
  } finally { socket.destroy(); }
}

// The fan leaves hotspot mode once it has the settings, and this Mac should
// rejoin its usual network. Home Assistant answering means it has.
async function waitForHomeAssistant(origin, ms = 180000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    try { await fetch(origin, { signal: AbortSignal.timeout(3000), redirect: 'manual' }); return true; } catch {}
    await sleep(3000);
  }
  return false;
}

async function haRequest(ha, method, path, body) {
  let response;
  try {
    response = await fetch(new URL(path, ha.base), {
      method,
      headers: { Authorization: `Bearer ${ha.token}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });
  } catch (error) {
    // fetch's own message is just "fetch failed"; the reason is in its cause.
    throw new Error(`cannot reach Home Assistant at ${ha.base}: ${error.cause?.message ?? error.message}`);
  }
  if (!response.ok) throw new Error(`Home Assistant ${method} ${path}: HTTP ${response.status} ${await response.text()}`.trim());
  return response.json();
}

// Coway Local → Configure → Add or provision a fan, as the frontend does it.
// Its external step carries the setup link the Bluetooth page would be given.
async function startPairing(ha) {
  const entries = await haRequest(ha, 'GET', '/api/config/config_entries/entry?domain=coway_local');
  const entry = entries.find(e => e.state === 'loaded') ?? entries[0];
  if (!entry) throw new Error('Coway Local is not set up in this Home Assistant');
  const menu = await haRequest(ha, 'POST', '/api/config/config_entries/options/flow', { handler: entry.entry_id });
  const step = await haRequest(ha, 'POST', `/api/config/config_entries/options/flow/${menu.flow_id}`, { next_step_id: 'add_fan' });
  if (step.type !== 'external' || !step.url) throw new Error(`Coway Local did not start pairing: ${step.reason ?? step.type}`);
  return { flowId: step.flow_id, setupUrl: step.url };
}

// The return link resumes the flow, which then waits for the fan to enroll
// and ends with an abort whose reason is the outcome, fan_ready on success.
async function finishPairing(ha, flowId, returnUrl, ms) {
  let back;
  try {
    back = await fetch(returnUrl, { signal: AbortSignal.timeout(15000), redirect: 'manual' });
  } catch (error) {
    throw new Error(`cannot open Home Assistant's return link ${returnUrl}: ${error.cause?.message ?? error.message}`);
  }
  if (!back.ok) throw new Error(`Home Assistant's return link: HTTP ${back.status} ${await back.text()}`.trim());
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const step = await haRequest(ha, 'GET', `/api/config/config_entries/options/flow/${flowId}`);
    if (step.type === 'abort') return step.reason;
    await sleep(3000);
  }
  throw new Error('timed out waiting for Home Assistant to report the fan');
}

async function main() {
  if (opts.help) { console.log(USAGE); return; }
  let ha, setupUrl;
  if (opts.ha) {
    if (positionals.length) fail('give --ha or a setup URL, not both');
    ha = { base: opts.ha, token: process.env.HA_TOKEN ?? await askHidden('Home Assistant long-lived access token: ') };
    ({ flowId: ha.flowId, setupUrl } = await startPairing(ha));
  } else if (positionals.length === 1) {
    setupUrl = positionals[0];
  } else {
    fail(`expected --ha or one setup URL\n\n${USAGE}`);
  }
  if (!setupUrl.includes('#setup=')) fail('the setup URL has no #setup= part, which holds the setup details; use --ha instead');
  const bundle = decodeSetup(setupUrl.slice(setupUrl.indexOf('#')));
  console.log(`Home Assistant: ${new URL(bundle.certSvrUrl).host}; setup link expires at ${new Date(bundle.expiresAt).toLocaleTimeString()}`);
  if (ha) await ask("Put the fan in Wi-Fi setup mode, join this Mac to the fan's hotspot, then press Enter: ");

  console.log(`Asking the fan at ${opts.host} for nearby networks…`);
  const { mac, networks } = await scan(opts.host);
  const name = opts.name ?? `Coway ${mac.slice(-6)}`;
  console.log(`Fan MAC ${mac.match(/../g).join(':')}, named "${name}"`);

  const network = await chooseNetwork(networks);
  const password = !network.security ? ''
    : process.env.COWAY_WIFI_PASSWORD ?? await askHidden(`Password for ${network.ssid}: `);
  // The Coway app sends the BSSID in capitals; parseScan gives lower case.
  const config = wifiConfig(bundle, { mac, name }, { ...network, bssid: network.bssid.toUpperCase() }, password);

  await provision(opts.host, config);
  console.log('The fan has its settings and should now leave hotspot mode and join your Wi-Fi.');

  if (opts['no-open'] && !ha) {
    console.log(`Once this Mac is back on your network, open:\n${bundle.returnUrl}`);
    return;
  }
  console.log('Waiting for this Mac to reach Home Assistant again…');
  if (!await waitForHomeAssistant(new URL(bundle.returnUrl).origin)) {
    fail(`Home Assistant is not reachable. Rejoin your network, then open:\n${bundle.returnUrl}`);
  }
  if (!ha) {
    execFileSync('/usr/bin/open', [bundle.returnUrl]);
    console.log('Opened Home Assistant, which will wait for the fan to connect.');
    return;
  }
  console.log('Waiting for the fan to enroll with Home Assistant…');
  const reason = await finishPairing(ha, ha.flowId, bundle.returnUrl, Math.max(0, bundle.expiresAt - Date.now()) + 60000);
  if (reason !== 'fan_ready') fail(`Home Assistant ended setup without the fan: ${reason}`);
  console.log(`Fan ready: "${name}" is in Home Assistant.`);
}

main().then(() => lines?.rl.close(), error => fail(error.message));
