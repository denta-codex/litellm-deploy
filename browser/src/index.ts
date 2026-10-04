import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema, type Tool } from '@modelcontextprotocol/sdk/types.js';
import { BrowserSession } from './session.js';

process.umask(0o077);
const session = new BrowserSession();
const server = new Server({ name: 'browser', version: '0.1.0', title: 'Browser',
  description: 'Interact with websites using isolated sessions or explicitly selected saved profiles.' }, { capabilities: { tools: {} } });
const sessionTool: Tool = {
  name: 'browser_session',
  description: 'Inspect, select, or close this conversation’s browser session. Default is isolated. Select a named saved profile only when requested. Opening reserves the session; Chromium starts on the first browser action.',
  inputSchema: { type: 'object', properties: {
    action: { type: 'string', enum: ['status', 'open', 'close'] },
    mode: { type: 'string', enum: ['isolated', 'saved'] },
    profile: { type: 'string', pattern: '^[a-z0-9][a-z0-9_-]{0,63}$' },
  }, required: ['action'], additionalProperties: false },
};
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [sessionTool, ...await session.listTools()] }));
server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
  try { return await session.call(request.params.name, request.params.arguments ?? {}, extra.signal, request.params._meta); }
  catch (error) { return { isError: true, content: [{ type: 'text', text: error instanceof Error ? error.message : 'Browser request failed' }] }; }
});
let stopping = false;
async function shutdown() {
  if (stopping) return;
  stopping = true;
  await session.dispose();
  await server.close();
}
server.onclose = () => { void shutdown(); };
process.stdin.on('end', () => { void shutdown(); });
for (const signal of ['SIGINT', 'SIGTERM'] as const) process.on(signal, () => { void shutdown(); });
await server.connect(new StdioServerTransport());
