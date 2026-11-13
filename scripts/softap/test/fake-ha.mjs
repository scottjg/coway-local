// A stand-in for Home Assistant's API with Coway Local installed, for testing
// coway-softap --ha. Listens on 127.0.0.1:18123, accepts the token "test",
// and reports the fan ready a few seconds after the return link is opened.
//
// Usage: node scripts/softap/test/fake-ha.mjs
import { randomBytes } from 'node:crypto';
import http from 'node:http';

const BASE = 'http://127.0.0.1:18123';
const flowId = randomBytes(16).toString('hex');
const state = randomBytes(24).toString('base64url');
let returnedAt;

function setupUrl() {
  const bundle = {
    v: 1, model: 'AP-1022K',
    caUrl: 'http://192.168.1.10:8080/ca.pem', certSvrUrl: 'https://192.168.1.10:9443/n',
    certToken: randomBytes(24).toString('base64url'), expiresAt: Date.now() + 900000,
    returnUrl: `${BASE}/api/coway_local/provisioning/return?flow_id=${flowId}&state=${state}`,
  };
  return `https://scottjg.github.io/coway-local/v1/#setup=${Buffer.from(JSON.stringify(bundle)).toString('base64url')}`;
}

http.createServer(async (req, res) => {
  const url = new URL(req.url, BASE);
  const send = (status, body) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); };
  console.log(`${req.method} ${url.pathname}${url.search}`);
  if (url.pathname === '/api/coway_local/provisioning/return') {
    if (url.searchParams.get('state') !== state) return send(400, { message: 'bad state' });
    returnedAt = Date.now();
    res.writeHead(200, { 'Content-Type': 'text/html' }); return res.end('<p>Returning to Home Assistant.</p>');
  }
  if (url.pathname === '/') { res.writeHead(200); return res.end(); }
  if (req.headers.authorization !== 'Bearer test') return send(401, { message: 'unauthorized' });
  if (req.method === 'GET' && url.pathname === '/api/config/config_entries/entry')
    return send(200, [{ entry_id: 'entry1', domain: 'coway_local', state: 'loaded' }]);
  if (req.method === 'POST' && url.pathname === '/api/config/config_entries/options/flow')
    return send(200, { type: 'menu', flow_id: flowId, step_id: 'init', menu_options: ['add_fan', 'server'] });
  if (url.pathname === `/api/config/config_entries/options/flow/${flowId}`) {
    if (req.method === 'POST') return send(200, { type: 'external', flow_id: flowId, step_id: 'pairing', url: setupUrl() });
    if (!returnedAt) return send(200, { type: 'external', flow_id: flowId, step_id: 'pairing' });
    if (Date.now() - returnedAt < 4000) return send(200, { type: 'progress', flow_id: flowId, step_id: 'verify' });
    return send(200, { type: 'abort', flow_id: flowId, reason: 'fan_ready' });
  }
  send(404, { message: 'not found' });
}).listen(18123, '127.0.0.1', () => console.log(`Fake Home Assistant on ${BASE}`));
