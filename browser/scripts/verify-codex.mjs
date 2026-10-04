// Isolated acceptance through the existing LiteLLM route. Never log request bodies or credentials.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, mkdir, writeFile, readFile, cp, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomInt } from 'node:crypto';
import { zstdDecompressSync, gunzipSync } from 'node:zlib';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const repo = dirname(root);
const home = await mkdtemp(join(repo, '.browser-acceptance-'));
const model = process.env.BROWSER_TEST_MODEL ?? 'chatgpt/gpt-6-astra';
const codex = process.env.BROWSER_TEST_CODEX ?? '/home/agent/.local/share/mise/installs/codex/0.159.2/bin/codex';
const key = execFileSync('/usr/bin/systemd-creds', ['decrypt', '--user', '--name=litellm-proxy-key', '/home/agent/.config/litellm/proxy-key.cred', '-'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
const marker = String(randomInt(100000, 999999));
const requests = [];
let process_, rpc, lastError;
const fixture = createServer((req, res) => {
  if (req.url === '/session-a' || req.url === '/session-b') {
    const owner = req.url === '/session-a' ? 'OWNER_A' : 'OWNER_B';
    res.setHeader('Set-Cookie', `owner=${owner}; Path=/; HttpOnly`);
    res.end(owner);
  } else if (req.url === '/session-read') {
    res.end(req.headers.cookie?.match(/owner=(OWNER_[AB])/)?.[1] ?? 'NO_OWNER');
  } else if (req.url === '/marker.svg') {
    res.setHeader('Content-Type', 'image/svg+xml');
    res.end(`<svg xmlns="http://www.w3.org/2000/svg" width="900" height="400"><rect width="900" height="400" fill="white"/><text x="100" y="220" font-family="sans-serif" font-size="110" fill="black">${marker}</text></svg>`);
  } else if (new URL(req.url, 'http://localhost').pathname === '/submitted') { res.end('SUBMISSION_OK'); }
  else {
    res.setHeader('Content-Type', 'text/html');
    res.end('<title>Browser acceptance</title><h1>Visual challenge</h1><img src="/marker.svg" alt="Visual challenge"><form action="/submitted"><label>Note<input name="note"></label><button>Submit</button></form>');
  }
});
const proxy = createServer(async (req, res) => {
  try {
    const chunks = []; for await (const c of req) chunks.push(c);
    let body = Buffer.concat(chunks);
    if (req.headers['content-encoding'] === 'zstd') body = zstdDecompressSync(body);
    if (req.headers['content-encoding'] === 'gzip') body = gunzipSync(body);
    const data = JSON.parse(body.toString());
    // Retain only booleans and tool names, never the transcript or wire body.
    const toolText = JSON.stringify(data.tools);
    const input = JSON.stringify(data.input);
    requests.push({
      tools: (data.tools ?? []).map(t => t.name ?? t.type),
      initialBrowserSchema: toolText.includes('mcp__browser__browser_navigate'),
      skillBody: JSON.stringify(data).includes('Opening a saved profile does not import personal-browser credentials'),
      hasImage: /"type":"input_image"/.test(input),
      discovered: input.includes('mcp__browser__browser_navigate'),
    });
    const upstream = await fetch('http://127.0.0.1:4000' + req.url, { method: 'POST',
      headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body });
    res.writeHead(upstream.status, { 'Content-Type': upstream.headers.get('content-type') ?? 'text/event-stream' });
    for await (const c of upstream.body) res.write(c);
    res.end();
  } catch (error) { lastError = error.constructor.name; res.writeHead(502); res.end(); }
});
async function listen(server) { await new Promise(r => server.listen(0, '127.0.0.1', r)); return server.address().port; }
class RPC {
  pending = new Map(); events = []; seq = 0;
  constructor(child) {
    this.child = child;
    createInterface({ input: child.stdout }).on('line', line => {
      let m; try { m = JSON.parse(line); } catch { return; }
      if (m.id !== undefined && !m.method) {
        const p = this.pending.get(m.id); if (p) { clearTimeout(p.timer); this.pending.delete(m.id); m.error ? p.reject(new Error('App-server RPC failed: ' + JSON.stringify(m.error))) : p.resolve(m.result); }
      } else if (m.id !== undefined) this.send({ id: m.id, error: { code: -32601, message: 'Unexpected interactive approval in isolated browser acceptance' } });
      else this.events.push(m);
    });
  }
  send(m) { this.child.stdin.write(JSON.stringify(m) + '\n'); }
  call(method, params) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('RPC timeout: ' + method)); }, 30_000);
      this.pending.set(id, { resolve, reject, timer }); this.send({ id, method, params });
    });
  }
  async turn(threadId, prompt) {
    const offset = this.events.length;
    const { turn } = await this.call('turn/start', { threadId, input: [{ type: 'text', text: prompt }], effort: 'low' });
    const deadline = Date.now() + 240_000;
    while (Date.now() < deadline) {
      const done = this.events.slice(offset).find(e => e.method === 'turn/completed' && e.params?.turn?.id === turn.id);
      if (done) {
        assert.equal(done.params.turn.status, 'completed', 'Codex turn failed');
        const items = this.events.slice(offset).filter(e => e.method === 'item/completed' && e.params.threadId === threadId).map(e => e.params.item);
        return { items, reply: items.filter(i => i.type === 'agentMessage').map(i => i.text).join('\n') };
      }
      await new Promise(r => setTimeout(r, 100));
    }
    throw new Error('Browser acceptance turn timed out');
  }
}
try {
  const proxyPort = await listen(proxy), fixturePort = await listen(fixture);
  await mkdir(join(home, 'skills'), { mode: 0o700 });
  await cp(join(root, 'skill/browser'), join(home, 'skills/browser'), { recursive: true });
  const q = JSON.stringify;
  await writeFile(join(home, 'config.toml'), `model = ${q(model)}
model_provider = "browser_acceptance"
model_catalog_json = "/home/agent/.config/litellm/codex-models.json"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
sandbox_mode = "workspace-write"
web_search = "live"
[features]
code_mode_host = true
shell_snapshot = false
[model_providers.browser_acceptance]
name = "browser_acceptance"
base_url = "http://127.0.0.1:${proxyPort}/v1"
wire_api = "responses"
env_key = "BROWSER_TEST_KEY"
requires_openai_auth = false
supports_websockets = false
[mcp_servers.browser]
command = ${q(process.execPath)}
args = [${q(join(root, 'dist/index.js'))}]
required = true
startup_timeout_sec = 30
[mcp_servers.browser.env]
CODEX_BROWSER_STATE_DIR = ${q(join(home, 'browser-state'))}
`);
  process_ = spawn(codex, ['app-server'], { cwd: home, env: { ...process.env, CODEX_HOME: home, BROWSER_TEST_KEY: key }, detached: true, stdio: ['pipe', 'pipe', 'ignore'] });
  rpc = new RPC(process_);
  await rpc.call('initialize', { clientInfo: { name: 'browser-acceptance', version: '1' }, capabilities: { experimentalApi: true } });
  rpc.send({ method: 'initialized', params: {} });
  const { thread } = await rpc.call('thread/start', { cwd: home, model, modelProvider: 'browser_acceptance', approvalPolicy: 'on-request', approvalsReviewer: 'auto_review', sandbox: 'workspace-write' });
  const status = await rpc.call('mcpServerStatus/list', {});
  assert.ok(status.data.some(s => s.name === 'browser' && s.tools.browser_navigate), 'MCP tools must be registered before deferral is checked');
  await rpc.turn(thread.id, 'Reply exactly READY. Do not use tools.');
  assert.ok(requests.length > 0);
  assert.equal(requests[0].initialBrowserSchema, false, 'Browser schemas leaked into initial tool definitions');
  assert.equal(requests[0].discovered, false, 'Browser tool definitions leaked into initial input');
  assert.equal(requests[0].skillBody, false, 'Full browser skill leaked into initial context');
  console.log('PASS initial request omits browser schemas and full skill; MCP registration verified');

  if (!process.argv.includes('--routing-only')) {
  const result = await rpc.turn(thread.id, `Open http://127.0.0.1:${fixturePort}/ in the browser and tell me the six-digit number shown in the image. Use the browser skill. Inspect a screenshot visually; do not fetch image source, use page evaluation, or read files to obtain the number. In code mode forward screenshot image content with image(). Leave the tab open for my follow-up.`);
  assert.ok(result.reply.includes(marker), 'Model did not read the image-only marker correctly');
  assert.ok(requests.some(r => r.discovered), 'Browser tools were never discovered');
  assert.ok(requests.some(r => r.skillBody), 'The browser skill was not loaded on demand');
  assert.ok(requests.some(r => r.hasImage), 'No image reached the model request');
  assert.ok(result.items.some(i => i.type === 'mcpToolCall' && i.tool === 'browser_take_screenshot') || JSON.stringify(result.items).includes('browser_take_screenshot'), 'Screenshot tool was not called');
  console.log('PASS deferred discovery, skill use, browser navigation and visual screenshot understanding through LiteLLM');
  const followup = await rpc.turn(thread.id, 'In the existing browser tab, enter FOLLOWUP_OK into Note and click Submit. Report the resulting page text and then close the browser session. Do not navigate back to the starting page.');
  assert.ok(followup.reply.includes('SUBMISSION_OK'), 'Follow-up did not retain and use the browser tab: ' + followup.reply.slice(-1200));
  console.log('PASS browser state retained across turns, form submission, and explicit cleanup');
  }

  const research = await rpc.turn(thread.id, 'Research where the official Playwright documentation explains browser contexts. Give one official documentation URL. This is research, not a request to interact with a website.');
  assert.ok(research.items.some(i => i.type === 'webSearch'), 'Research did not use hosted web search');
  assert.ok(!research.items.some(i => i.type === 'mcpToolCall' && i.server === 'browser'), 'Research unexpectedly opened the browser');
  console.log('PASS research uses hosted search rather than the browser');

  const other = (await rpc.call('thread/start', { cwd: home, model, modelProvider: 'browser_acceptance', approvalPolicy: 'on-request', approvalsReviewer: 'auto_review', sandbox: 'workspace-write' })).thread;
  const [a, b] = await Promise.all([
    rpc.turn(thread.id, `Use the browser to open http://127.0.0.1:${fixturePort}/session-a . Report the page text and leave this browser session open for a follow-up.`),
    rpc.turn(other.id, `Use the browser to open http://127.0.0.1:${fixturePort}/session-b . Then open http://127.0.0.1:${fixturePort}/session-read and report its page text. Close your browser session when finished.`),
  ]);
  assert.ok(a.reply.includes('OWNER_A') && b.reply.includes('OWNER_B'), 'Concurrent conversation setup failed');
  const retained = await rpc.turn(thread.id, `In your existing browser session, navigate to http://127.0.0.1:${fixturePort}/session-read and report the page text. Do not visit session-a or set any cookies. Then close your session.`);
  assert.ok(retained.reply.includes('OWNER_A'), 'Another conversation altered or closed the first browser session');
  console.log('PASS concurrent app-server conversations retain separate browser state');
  assert.equal(lastError, undefined, 'Inspection proxy failed');
} finally {
  if (process_?.pid) {
    process_.stdin.end();
    try { process.kill(-process_.pid, 'SIGTERM'); } catch {}
    await Promise.race([new Promise(r => process_.once('exit', r)), new Promise(r => setTimeout(r, 5000))]);
    try { process.kill(-process_.pid, 'SIGKILL'); } catch {}
  }
  for (const server of [proxy, fixture]) { server.closeAllConnections(); await new Promise(r => server.close(r)); }
  await rm(home, { recursive: true, force: true });
}
