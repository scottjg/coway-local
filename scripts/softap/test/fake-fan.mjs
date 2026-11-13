// A stand-in for a Coway fan in hotspot setup mode, for testing coway-softap
// without one. Listens on 127.0.0.1:5001 (Wi-Fi scan) and :5040 (P001/P002),
// replies as a real 02F9R fan did, and prints the settings it decrypts.
//
// Usage: node scripts/softap/test/fake-fan.mjs MAC
import { createDecipheriv } from 'node:crypto';
import net from 'node:net';
import { canonicalMac, decodeProto, FIELDS, text } from '../../../web/v1/protocol.mjs';

const mac = canonicalMac(process.argv[2] ?? '');
if (!/^[0-9A-F]{12}$/.test(mac)) { console.error('Usage: node scripts/softap/test/fake-fan.mjs MAC'); process.exit(1); }

function scanEntry(index, ssid, bssid, rssi, security, channel) {
  const entry = Buffer.alloc(48);
  entry.writeUInt16LE(index, 0);
  entry.writeUInt16LE(Buffer.byteLength(ssid), 2);
  entry.write(ssid, 4);
  Buffer.from(bssid, 'hex').copy(entry, 36);
  entry.writeInt32LE(rssi, 42);
  entry[46] = security; entry[47] = channel;
  return entry;
}

const SCAN_RESULT = Buffer.concat([
  Buffer.from('*ICT*SCANRESULT:'),
  scanEntry(1, 'home-2.4', '0a0000000001', -48, 4, 6),
  scanEntry(2, 'home-2.4', '0a0000000002', -71, 4, 11),
  scanEntry(3, 'guest', '0a0000000003', -60, 0, 1),
  Buffer.from('[OK]\r\n'),
]);

net.createServer(socket => {
  socket.on('data', data => {
    if (data.toString() === 'AT*ICT*SCAN\r') socket.end(SCAN_RESULT);
  });
}).listen(5001, '127.0.0.1');

net.createServer(socket => {
  let key, buffer = '', idle;
  socket.on('data', data => {
    buffer += data.toString('latin1');
    clearTimeout(idle);
    idle = setTimeout(() => {
      if (buffer.startsWith('P001')) {
        key = Buffer.from(text.decode(decodeProto(Buffer.from(buffer.slice(4), 'latin1'))[2]), 'base64');
        console.log(`P001: ${key.length}-byte key`);
        socket.write(Buffer.from([0x08, 0x01, 0x10, 0x01, 0x18, 0x3d]));
      } else if (buffer.startsWith('P002') && key) {
        const raw = Buffer.from(buffer.slice(4), 'base64');
        const decipher = createDecipheriv('aes-256-cbc', Buffer.concat([key, Buffer.from(mac)]), raw.subarray(0, 16));
        const fields = decodeProto(Buffer.concat([decipher.update(raw.subarray(16)), decipher.final()]));
        console.log('P002:');
        for (const [n, value] of Object.entries(fields)) {
          const shown = typeof value === 'number' ? value : text.decode(value);
          console.log(`  ${FIELDS[n] ?? n}: ${shown}`);
        }
        socket.write(Buffer.from([0x08, 0x02, 0x10, 0x01, 0x18, 0x00]));
      } else {
        console.log(`unexpected: ${JSON.stringify(buffer.slice(0, 40))}`);
      }
      buffer = '';
    }, 200);
  });
}).listen(5040, '127.0.0.1', () => console.log(`Fake fan ${mac} listening on 127.0.0.1:5001 and :5040`));
