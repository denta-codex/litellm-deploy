import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { BrowserSession } from '../dist/session.js';

function plain(r) { return r.content.filter(c => c.type === 'text').map(c => c.text).join('\n'); }
async function ok(session, name, args = {}, signal) {
  const r = await session.call(name, args, signal);
  assert.equal(r.isError, undefined, plain(r));
  return r;
}
async function fixture(t, idleMs = 60_000) {
  const dir = await mkdtemp(join(tmpdir(), 'browser-test-'));
  const server = createServer((req, res) => {
    if (req.url === '/login') {
      res.setHeader('Set-Cookie', 'fixture=saved; Max-Age=86400; Path=/; HttpOnly');
      res.end('signed in'); return;
    }
    if (req.url === '/account') { res.end(req.headers.cookie?.includes('fixture=saved') ? 'SIGNED_IN' : 'SIGNED_OUT'); return; }
    if (req.url === '/download') {
      res.setHeader('Content-Disposition', 'attachment; filename="report.txt"'); res.end('DOWNLOAD_OK'); return;
    }
    res.setHeader('Content-Type', 'text/html');
    res.end(`<title>Fixture</title><label>Name<input id="name"></label><button onclick='document.querySelector("output").textContent=document.querySelector("input").value'>Submit</button><output></output><a href="/account" target="_blank">Account</a><button onclick='alert("HELLO")'>Dialog</button><a href="/download">Download</a>`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const sessions = [];
  const session = () => { const s = new BrowserSession({ stateDir: join(dir, 'state'), workspace: dir, idleMs }); sessions.push(s); return s; };
  t.after(async () => { await Promise.all(sessions.map(s => s.dispose())); await new Promise(resolve => server.close(resolve)); await rm(dir, { recursive: true, force: true }); });
  return { dir, session, url: `http://127.0.0.1:${server.address().port}` };
}

test('discovery is lazy, browser tools are bounded, and profile names cannot escape storage', async t => {
  const { session } = await fixture(t);
  const s = session();
  const tools = await s.listTools();
  assert.ok(tools.some(t => t.name === 'browser_take_screenshot'));
  assert.ok(!tools.some(t => /unsafe|install|webmcp/.test(t.name)));
  assert.equal(s.status().running, false);
  const processes = execFileSync('ps', ['-eo', 'pid,ppid,args'], { encoding: 'utf8' });
  assert.ok(!processes.split('\n').some(line => line.includes('--user-data-dir') && line.includes(s.directory)));
  await assert.rejects(s.call('browser_session', { action: 'open', mode: 'saved', profile: '../escape' }));
  await assert.rejects(s.call('browser_run_code_unsafe', { code: 'return 1' }));
});

test('real browser navigation, form, images, dialogs, tabs, downloads, and isolation', async t => {
  const { session, url } = await fixture(t);
  const a = session(), b = session();
  await ok(a, 'browser_navigate', { url });
  let snapshot = plain(await ok(a, 'browser_snapshot'));
  const ref = snapshot.match(/textbox "Name" \[ref=(\w+)\]/)?.[1];
  assert.ok(ref, snapshot);
  await ok(a, 'browser_type', { target: ref, text: 'FORM_OK' });
  snapshot = plain(await ok(a, 'browser_snapshot'));
  const submit = snapshot.match(/button "Submit" \[ref=(\w+)\]/)?.[1];
  await ok(a, 'browser_click', { target: submit });
  assert.match(plain(await ok(a, 'browser_evaluate', { function: '() => document.querySelector("output").textContent' })), /FORM_OK/);
  const screenshot = await ok(a, 'browser_take_screenshot', {});
  assert.ok(screenshot.content.some(c => c.type === 'image' && c.data.length > 100));
  const env = await readFile(`/proc/${a.transport.pid}/environ`, 'utf8');
  assert.ok(!env.includes('LITELLM_PROXY_KEY='));
  snapshot = plain(await ok(a, 'browser_snapshot'));
  await ok(a, 'browser_click', { target: snapshot.match(/button "Dialog" \[ref=(\w+)\]/)[1] });
  await ok(a, 'browser_handle_dialog', { accept: true });
  await ok(a, 'browser_navigate', { url: url + '/download' });
  assert.equal(await readFile(join(a.directory, 'artifacts', 'report.txt'), 'utf8'), 'DOWNLOAD_OK');
  await ok(a, 'browser_tabs', { action: 'new' });
  assert.match(plain(await ok(a, 'browser_tabs', { action: 'list' })), /about:blank/);
  await ok(a, 'browser_navigate', { url: url + '/login' });
  await ok(a, 'browser_navigate', { url: url + '/account' });
  assert.match(plain(await ok(a, 'browser_snapshot')), /SIGNED_IN/);
  await ok(b, 'browser_navigate', { url: url + '/account' });
  assert.match(plain(await ok(b, 'browser_snapshot')), /SIGNED_OUT/);
});

test('saved profiles persist and exclusive ownership is released on close', async t => {
  const { session, url } = await fixture(t);
  const a = session(), b = session();
  await ok(a, 'browser_session', { action: 'open', mode: 'saved', profile: 'work' });
  await assert.rejects(b.call('browser_session', { action: 'open', mode: 'saved', profile: 'work' }), /profile unavailable/);
  await ok(a, 'browser_navigate', { url: url + '/login' });
  await ok(a, 'browser_session', { action: 'close' });
  await ok(b, 'browser_session', { action: 'open', mode: 'saved', profile: 'work' });
  await ok(b, 'browser_navigate', { url: url + '/account' });
  assert.match(plain(await ok(b, 'browser_snapshot')), /SIGNED_IN/);
  await ok(b, 'browser_session', { action: 'open', mode: 'isolated' });
  const reset = await ok(b, 'browser_navigate', { url: url + '/account' });
  assert.match(plain(reset), /previous tabs are gone/);
  assert.match(plain(await ok(b, 'browser_snapshot')), /SIGNED_OUT/);
});

test('idle shutdown discards isolated state and reports the reset', async t => {
  const { session, url } = await fixture(t, 200);
  const s = session();
  await ok(s, 'browser_navigate', { url: url + '/login' });
  for (let i = 0; i < 80 && s.status().running; i++) await new Promise(r => setTimeout(r, 100));
  assert.equal(s.status().running, false);
  const result = await ok(s, 'browser_navigate', { url: url + '/account' });
  assert.match(plain(result), /idle timeout/);
  assert.match(plain(await ok(s, 'browser_snapshot')), /SIGNED_OUT/);
});

test('crashed upstream does not replay an action and next session is explicit', async t => {
  const { session, url } = await fixture(t);
  const s = session();
  await ok(s, 'browser_navigate', { url });
  process.kill(s.transport.pid, 'SIGKILL');
  await new Promise(r => setTimeout(r, 100));
  const result = await s.call('browser_click', { target: 'e2' });
  assert.equal(result.isError, true);
  assert.match(plain(result), /uncertain/);
  assert.match(plain(await ok(s, 'browser_navigate', { url })), /previous tabs are gone/);
});

test('cancellation closes the session and metadata refuses cross-conversation reuse', async t => {
  const { session, url } = await fixture(t);
  const s = session();
  await s.call('browser_session', { action: 'status' }, undefined, { 'x-codex-turn-metadata': JSON.stringify({ thread_id: 'one' }) });
  await assert.rejects(s.call('browser_session', { action: 'status' }, undefined, { 'x-codex-turn-metadata': JSON.stringify({ thread_id: 'two' }) }), /another conversation/);
  await ok(s, 'browser_navigate', { url });
  const abort = new AbortController();
  const operation = s.call('browser_wait_for', { text: 'WILL_NEVER_APPEAR' }, abort.signal);
  setTimeout(() => abort.abort(), 100);
  assert.equal((await operation).isError, true);
  assert.equal(s.status().running, false);
});
