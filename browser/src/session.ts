import { mkdir, mkdtemp, rm, lstat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { ListRootsRequestSchema, type CallToolResult, type Tool } from '@modelcontextprotocol/sdk/types.js';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const cli = join(dirname(require.resolve('@playwright/mcp')), 'cli.js');
export const allowedTools = new Set([
  'browser_navigate', 'browser_navigate_back', 'browser_navigate_forward', 'browser_reload',
  'browser_snapshot', 'browser_click', 'browser_drag', 'browser_hover', 'browser_select_option',
  'browser_check', 'browser_uncheck', 'browser_fill_form', 'browser_press_key', 'browser_type',
  'browser_wait_for', 'browser_take_screenshot', 'browser_tabs', 'browser_handle_dialog',
  'browser_file_upload', 'browser_console_messages', 'browser_network_requests', 'browser_evaluate',
  'browser_resize', 'browser_mouse_move_xy', 'browser_mouse_down', 'browser_mouse_up',
  'browser_mouse_wheel', 'browser_mouse_click_xy', 'browser_mouse_drag_xy',
]);

export type Mode = { mode: 'isolated' } | { mode: 'saved'; profile: string };
export type Options = { stateDir?: string; idleMs?: number; executable?: string; workspace?: string };
const text = (value: unknown): CallToolResult => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });

export class BrowserSession {
  private client?: Client;
  private transport?: StdioClientTransport;
  private directory?: string;
  private tools?: Tool[];
  private timer?: NodeJS.Timeout;
  private queue: Promise<unknown> = Promise.resolve();
  private mode: Mode = { mode: 'isolated' };
  private running = false;
  private resetReason?: string;
  private closed = false;
  private owner?: string;
  readonly stateDir: string;
  readonly idleMs: number;
  readonly executable: string;
  readonly workspace: string;

  constructor(options: Options = {}) {
    this.stateDir = resolve(options.stateDir ?? process.env.CODEX_BROWSER_STATE_DIR ?? join(homedir(), '.local/state/codex-browser'));
    this.idleMs = options.idleMs ?? Number(process.env.CODEX_BROWSER_IDLE_MS ?? 900_000);
    if (!Number.isSafeInteger(this.idleMs) || this.idleMs < 100) throw new Error('Invalid browser idle timeout');
    this.executable = options.executable ?? process.env.CODEX_BROWSER_EXECUTABLE ?? '/usr/bin/chromium';
    this.workspace = resolve(options.workspace ?? process.cwd());
  }

  private serial<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.queue.then(fn);
    this.queue = next.catch(() => {});
    return next;
  }

  private async privateDir(path: string) {
    await mkdir(path, { recursive: true, mode: 0o700 });
    const stat = await lstat(path);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) || stat.uid !== process.getuid?.())
      throw new Error('Browser state directory must be private and owned by this user');
  }

  private async start() {
    if (this.closed) throw new Error('Browser connection is closed');
    if (this.client) return;
    await this.privateDir(this.stateDir);
    await this.privateDir(join(this.stateDir, 'sessions'));
    this.directory = await mkdtemp(join(this.stateDir, 'sessions', 'session-'));
    const args = [cli, '--headless', '--sandbox', '--executable-path', this.executable,
      '--caps', 'vision', '--no-webmcp', '--idle-timeout', '0', '--file-paths', 'absolute',
      '--output-dir', join(this.directory, 'artifacts'), '--output-max-size', '52428800'];
    let command = process.execPath;
    if (this.mode.mode === 'saved') {
      await this.privateDir(join(this.stateDir, 'profiles'));
      await this.privateDir(join(this.stateDir, 'locks'));
      const profile = join(this.stateDir, 'profiles', this.mode.profile);
      await this.privateDir(profile);
      args.push('--user-data-dir', profile);
      // The lock survives for precisely the upstream process lifetime, even if this wrapper dies.
      args.unshift('--no-fork', '--nonblock', join(this.stateDir, 'locks', this.mode.profile + '.lock'), command);
      command = '/usr/bin/flock';
    } else args.push('--isolated');
    const transport = new StdioClientTransport({ command, args, cwd: this.workspace,
      // SDK inherits only basic OS variables. No proxy keys, tokens, NODE_OPTIONS or debug flags.
      env: { HOME: homedir(), PATH: '/usr/local/bin:/usr/bin:/bin', LANG: 'C.UTF-8' }, stderr: 'pipe' });
    transport.stderr?.on('data', () => {}); // Upstream errors may contain page data; never log them.
    const client = new Client({ name: 'codex-browser', version: '0.1.0' }, { capabilities: { roots: {} } });
    client.setRequestHandler(ListRootsRequestSchema, async () => ({ roots: [{ uri: pathToFileURL(this.workspace).href, name: 'workspace' }] }));
    this.transport = transport;
    this.client = client;
    try {
      await client.connect(transport, { timeout: 15_000 });
      const { tools } = await client.listTools();
      this.tools = tools.filter(t => allowedTools.has(t.name));
      const screenshot = this.tools.find(t => t.name === 'browser_take_screenshot');
      if (screenshot) screenshot.description = 'Capture the current page as an image. Inspect the image before using coordinate tools; prefer fresh browser_snapshot references for ordinary element actions.';
      for (const required of ['browser_navigate', 'browser_snapshot', 'browser_take_screenshot'])
        if (!this.tools.some(t => t.name === required)) throw new Error('Upstream browser tool missing');
    } catch {
      await this.stop('startup failed');
      throw new Error(this.mode.mode === 'saved'
        ? 'Saved profile unavailable: it may be in use by another conversation, or browser startup failed. No action was replayed.'
        : 'Browser tool service failed to start. No action was replayed.');
    }
  }

  private armTimer() {
    clearTimeout(this.timer);
    if (this.client) this.timer = setTimeout(() => {
      void this.serial(() => this.stop('idle timeout; previous tabs and isolated cookies are gone'));
    }, this.idleMs).unref();
  }

  private async stop(reason: string) {
    clearTimeout(this.timer);
    const client = this.client;
    this.client = undefined;
    if (client) {
      // close is idempotent and not a website mutation; graceful close flushes saved profiles.
      if (this.running) await client.callTool({ name: 'browser_close', arguments: {} }, undefined, { timeout: 5000 }).catch(() => {});
      await client.close().catch(() => {});
    }
    this.transport = undefined;
    this.running = false;
    if (this.directory) await rm(this.directory, { recursive: true, force: true });
    this.directory = undefined;
    this.resetReason = reason;
  }

  async listTools(): Promise<Tool[]> {
    return this.serial(async () => {
      // Discover schemas once. Upstream tools/list does not launch Chromium.
      if (!this.tools) { await this.start(); this.armTimer(); }
      return this.tools!;
    });
  }

  async call(name: string, args: Record<string, unknown>, signal?: AbortSignal, meta?: Record<string, unknown>): Promise<CallToolResult> {
    return this.serial(async () => {
      if (this.closed || signal?.aborted) throw new Error('Browser call cancelled');
      // Refuse cross-thread sharing if a host reuses an MCP process unexpectedly.
      const raw = meta?.['x-codex-turn-metadata'];
      if (typeof raw === 'string') {
        const data = JSON.parse(raw);
        const owner = data.thread_id ?? data.session_id;
        if (owner && this.owner && owner !== this.owner) throw new Error('Browser connection belongs to another conversation');
        if (owner) this.owner = owner;
      }
      clearTimeout(this.timer);
      const cancel = () => { void this.transport?.close(); };
      signal?.addEventListener('abort', cancel, { once: true });
      try {
        if (name === 'browser_session') {
          if (args.action === 'status') return text(this.status());
          if (args.action === 'close') { await this.stop('explicit close; previous tabs are gone'); return text(this.status()); }
          if (args.action !== 'open') throw new Error('Expected action status, open, or close');
          const mode = args.mode ?? 'isolated';
          if (mode !== 'isolated' && mode !== 'saved') throw new Error('Expected isolated or saved mode');
          if (mode === 'saved' && (typeof args.profile !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(args.profile)))
            throw new Error('Saved profiles require a name of 1–64 lowercase letters, numbers, underscores or hyphens');
          if (mode === 'isolated' && args.profile !== undefined) throw new Error('Isolated mode does not take a profile');
          const next: Mode = mode === 'saved' ? { mode, profile: args.profile as string } : { mode };
          if (JSON.stringify(next) !== JSON.stringify(this.mode)) await this.stop('profile switched; previous tabs are gone');
          this.mode = next;
          await this.start(); // Select and reserve; Chromium still waits for a browser operation.
          return text(this.status());
        }
        if (!allowedTools.has(name)) throw new Error('Unknown or disabled browser tool');
        await this.start();
        const notice = this.resetReason;
        this.resetReason = undefined;
        this.running = true;
        try {
          // Do not forward caller-supplied internal Playwright overrides such as _meta.cwd.
          const { _meta: _ignored, ...arguments_ } = args;
          const result = await this.client!.callTool({ name, arguments: arguments_ }, undefined, { timeout: 90_000, signal }) as CallToolResult;
          if (notice) result.content.unshift({ type: 'text', text: `Session notice: ${notice}. This is a new browser; observe the page before acting.` });
          return result;
        } catch {
          await this.stop('connection lost or operation cancelled; previous tabs are gone');
          return { isError: true, content: [{ type: 'text', text: 'Browser operation interrupted. Its outcome may be uncertain. Do not replay a submission or other mutation automatically. Previous tabs are gone.' }] };
        }
      } finally {
        signal?.removeEventListener('abort', cancel);
        this.armTimer();
      }
    });
  }

  status() {
    return { ...this.mode, running: this.running, idleTimeoutMs: this.idleMs, resetReason: this.resetReason };
  }

  async dispose() {
    this.closed = true;
    clearTimeout(this.timer);
    // Close the pipe immediately so an outstanding request cannot delay shutdown indefinitely.
    await this.transport?.close();
    await this.serial(() => this.stop('connection closed'));
  }
}
