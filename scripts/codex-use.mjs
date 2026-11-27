#!/usr/bin/env node
// codex-use: small zero-dependency client for the shared Codex App Server.
// Requires Node.js >= 18. Speaks JSON-RPC over a WebSocket on the daemon's Unix socket.
import { createHash, randomBytes } from 'node:crypto';
import { readFileSync, realpathSync, statSync } from 'node:fs';
import { request } from 'node:http';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { parseArgs } from 'node:util';

const CLIENT_NAME = 'codex_use';
const CLIENT_VERSION = '1.0.0';
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const MAX_MESSAGE_BYTES = 256 * 1024 * 1024;
const PRESETS = {
  research: ['gpt-6.2-sol', 'high'],
  simple: ['gpt-6.2-sol', 'max'],
  'complex-review': ['gpt-6.2-astra', 'xhigh'],
};
const SANDBOXES = ['read-only', 'workspace-write'];

class RPCError extends Error {}
class UsageError extends Error {}
class RPCTimeoutError extends Error {}

// ---------------------------------------------------------------- WebSocket

function openSocket(path, timeoutMs) {
  return new Promise((resolve, reject) => {
    const key = randomBytes(16).toString('base64');
    const req = request({
      socketPath: path,
      path: '/',
      method: 'GET',
      headers: {
        Host: 'localhost',
        Connection: 'Upgrade',
        Upgrade: 'websocket',
        'Sec-WebSocket-Key': key,
        'Sec-WebSocket-Version': '13',
      },
    });
    const timer = setTimeout(() => req.destroy(new RPCTimeoutError('App Server connection timed out.')), timeoutMs);
    req.on('upgrade', (res, socket, head) => {
      clearTimeout(timer);
      const expected = createHash('sha1').update(key + WS_GUID).digest('base64');
      if (res.headers['sec-websocket-accept'] !== expected) {
        socket.destroy();
        reject(new RPCError('Invalid WebSocket handshake from App Server.'));
        return;
      }
      resolve({ socket, head });
    });
    req.on('response', (res) => {
      clearTimeout(timer);
      res.resume();
      reject(new RPCError(`App Server refused the WebSocket upgrade: HTTP ${res.statusCode}`));
    });
    req.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
    req.end();
  });
}

class WebSocketConnection {
  constructor(socket, head, { onMessage, onClose }) {
    this.socket = socket;
    this.onMessage = onMessage;
    this.onClose = onClose;
    this.chunks = [];
    this.size = 0;
    this.needed = 2;
    this.fragments = null;
    this.fragmentOpcode = 0;
    this.closed = false;
    this.closeSent = false;
    this.closeWaiters = [];
    socket.on('data', (chunk) => this.receive(chunk));
    socket.on('error', (err) => this.finish(err));
    socket.on('close', () => this.finish(new RPCError('App Server connection closed; inspect before retrying a mutation.')));
    if (head && head.length) this.receive(head);
  }

  receive(chunk) {
    if (this.closed) return;
    this.chunks.push(chunk);
    this.size += chunk.length;
    if (this.size < this.needed) return;
    let buf = this.chunks.length === 1 ? this.chunks[0] : Buffer.concat(this.chunks, this.size);
    try {
      while (!this.closed) {
        const frame = this.parseFrame(buf);
        if (!frame) break;
        buf = buf.subarray(frame.end);
        this.handleFrame(frame);
      }
    } catch (err) {
      this.finish(err);
      return;
    }
    this.chunks = buf.length ? [buf] : [];
    this.size = buf.length;
  }

  parseFrame(buf) {
    if (buf.length < 2) {
      this.needed = 2;
      return null;
    }
    const fin = (buf[0] & 0x80) !== 0;
    const opcode = buf[0] & 0x0f;
    const masked = (buf[1] & 0x80) !== 0;
    let length = buf[1] & 0x7f;
    let offset = 2;
    if (length === 126) {
      if (buf.length < 4) return this.need(4);
      length = buf.readUInt16BE(2);
      offset = 4;
    } else if (length === 127) {
      if (buf.length < 10) return this.need(10);
      const big = buf.readBigUInt64BE(2);
      if (big > BigInt(MAX_MESSAGE_BYTES)) throw new RPCError('App Server WebSocket frame is too large.');
      length = Number(big);
      offset = 10;
    }
    let mask = null;
    if (masked) {
      if (buf.length < offset + 4) return this.need(offset + 4);
      mask = buf.subarray(offset, offset + 4);
      offset += 4;
    }
    const end = offset + length;
    if (buf.length < end) return this.need(end);
    this.needed = 2;
    const payload = Buffer.from(buf.subarray(offset, end));
    if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
    return { fin, opcode, payload, end };
  }

  need(bytes) {
    this.needed = bytes;
    return null;
  }

  handleFrame({ fin, opcode, payload }) {
    if (opcode === 0x8) {
      if (!this.closeSent) this.sendFrame(0x8, payload.subarray(0, 2));
      this.socket.end();
      this.finish(new RPCError('App Server closed the WebSocket; inspect before retrying a mutation.'));
      return;
    }
    if (opcode === 0x9) {
      this.sendFrame(0xa, payload);
      return;
    }
    if (opcode === 0xa) return;
    if (opcode === 0x1 || opcode === 0x2) {
      if (this.fragments) throw new RPCError('WebSocket protocol error: interleaved message.');
      if (fin) {
        this.deliver(payload);
      } else {
        this.fragments = [payload];
        this.fragmentOpcode = opcode;
      }
      return;
    }
    if (opcode === 0x0) {
      if (!this.fragments) throw new RPCError('WebSocket protocol error: unexpected continuation.');
      this.fragments.push(payload);
      const total = this.fragments.reduce((n, part) => n + part.length, 0);
      if (total > MAX_MESSAGE_BYTES) throw new RPCError('App Server WebSocket message is too large.');
      if (fin) {
        const message = Buffer.concat(this.fragments, total);
        this.fragments = null;
        this.deliver(message);
      }
      return;
    }
    throw new RPCError(`WebSocket protocol error: unknown opcode ${opcode}.`);
  }

  deliver(payload) {
    this.onMessage(payload.toString('utf8'));
  }

  sendFrame(opcode, payload) {
    if (this.closed || this.closeSent) throw new RPCError('App Server connection is closed; inspect before retrying a mutation.');
    const length = payload.length;
    let header;
    if (length < 126) {
      header = Buffer.alloc(2);
      header[1] = 0x80 | length;
    } else if (length < 65536) {
      header = Buffer.alloc(4);
      header[1] = 0x80 | 126;
      header.writeUInt16BE(length, 2);
    } else {
      header = Buffer.alloc(10);
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(length), 2);
    }
    header[0] = 0x80 | opcode;
    const mask = randomBytes(4);
    const body = Buffer.allocUnsafe(length);
    for (let i = 0; i < length; i++) body[i] = payload[i] ^ mask[i & 3];
    if (opcode === 0x8) this.closeSent = true;
    this.socket.write(Buffer.concat([header, mask, body]));
  }

  sendText(text) {
    this.sendFrame(0x1, Buffer.from(text, 'utf8'));
  }

  finish(err) {
    if (this.closed) return;
    this.closed = true;
    for (const resolve of this.closeWaiters) resolve();
    this.closeWaiters = [];
    this.onClose(err);
  }

  close(timeoutMs = 1000) {
    if (this.closed) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.socket.destroy();
        resolve();
      }, timeoutMs);
      this.closeWaiters.push(() => {
        clearTimeout(timer);
        this.socket.destroy();
        resolve();
      });
      try {
        const code = Buffer.alloc(2);
        code.writeUInt16BE(1000, 0);
        this.sendFrame(0x8, code);
      } catch {
        clearTimeout(timer);
        this.socket.destroy();
        resolve();
      }
    });
  }
}

// ---------------------------------------------------------------- JSON-RPC client

class Client {
  static async open(sockPath, timeoutSeconds) {
    const timeoutMs = timeoutSeconds * 1000;
    const { socket, head } = await openSocket(sockPath, timeoutMs);
    const client = new Client(timeoutMs);
    client.ws = new WebSocketConnection(socket, head, {
      onMessage: (text) => client.onMessage(text),
      onClose: (err) => client.fail(err),
    });
    try {
      await client.call('initialize', {
        clientInfo: { name: CLIENT_NAME, version: CLIENT_VERSION },
        capabilities: { experimentalApi: true },
      });
      client.send({ method: 'initialized' });
    } catch (err) {
      await client.close();
      throw err;
    }
    return client;
  }

  constructor(timeoutMs) {
    this.timeoutMs = timeoutMs;
    this.seq = 0;
    this.pending = new Map();
    this.failure = null;
    this.closing = false;
  }

  send(obj) {
    this.ws.sendText(JSON.stringify(obj));
  }

  onMessage(text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      this.fail(new RPCError('App Server sent invalid JSON.'));
      return;
    }
    const hasId = Object.hasOwn(msg, 'id');
    if (hasId && Object.hasOwn(msg, 'method')) {
      // Never silently grant approvals or fabricate answers to interactive requests.
      try {
        this.send({ id: msg.id, error: { code: -32601, message: 'Noninteractive codex-use does not handle client requests' } });
      } catch {
        // The connection is already failing; the error below is what matters.
      }
      this.fail(new RPCError('Server requested interactive handling: ' + msg.method));
      return;
    }
    if (!hasId) return; // Notifications are not needed by this client.
    const waiter = this.pending.get(msg.id);
    if (!waiter) return;
    this.pending.delete(msg.id);
    clearTimeout(waiter.timer);
    if (Object.hasOwn(msg, 'error')) waiter.reject(new RPCError(JSON.stringify(msg.error)));
    else waiter.resolve(msg.result ?? {});
  }

  fail(err) {
    if (this.closing) return;
    if (!this.failure) this.failure = err;
    for (const waiter of this.pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(this.failure);
    }
    this.pending.clear();
  }

  call(method, params) {
    if (this.failure) return Promise.reject(this.failure);
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RPCTimeoutError('App Server response timed out; mutation outcome may be unknown. Inspect before retrying.'));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.send({ id, method, params });
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }

  async close() {
    this.closing = true;
    for (const waiter of this.pending.values()) clearTimeout(waiter.timer);
    this.pending.clear();
    if (this.ws) await this.ws.close();
  }

  async thread(threadId) {
    return (await this.call('thread/read', { threadId })).thread;
  }

  turns(threadId, cursor) {
    const params = { threadId, limit: 50, sortDirection: 'desc', itemsView: 'notLoaded' };
    if (cursor) params.cursor = cursor;
    return this.call('thread/turns/list', params);
  }

  async active(threadId) {
    const thread = await this.thread(threadId);
    if (thread.status.type !== 'active') {
      throw new RPCError('No active turn on this server: ' + JSON.stringify(thread.status));
    }
    for (const turn of (await this.turns(threadId)).data) {
      if (turn.status === 'inProgress') return turn.id;
    }
    throw new RPCError('Active turn ID unavailable; retry status, not the mutation.');
  }

  async models() {
    const data = [];
    let cursor = null;
    for (;;) {
      const params = { limit: 100, includeHidden: false };
      if (cursor) params.cursor = cursor;
      const page = await this.call('model/list', params);
      data.push(...page.data);
      cursor = page.nextCursor;
      if (!cursor) return data;
    }
  }

  async validateModel(model, effort) {
    const entries = await this.models();
    const match = entries.find((m) => m.model === model || m.id === model);
    if (!match) throw new RPCError('Requested model is not in this server catalog: ' + model);
    const supported = (match.supportedReasoningEfforts ?? []).map((x) => x.reasoningEffort);
    if (effort && !supported.includes(effort)) {
      throw new RPCError(`Effort ${effort} is unsupported for ${model}; supported: ${JSON.stringify(supported)}`);
    }
    return match;
  }
}

// ---------------------------------------------------------------- commands

const utf8 = new TextDecoder('utf-8', { fatal: true });

function readPrompt(args) {
  if (args['task-file'] !== undefined) {
    const source = args['task-file'] === '-' ? 0 : args['task-file'];
    try {
      return utf8.decode(readFileSync(source));
    } catch (err) {
      if (err instanceof TypeError) throw new UsageError('Task file is not valid UTF-8.');
      throw err;
    }
  }
  return args.message;
}

function textInput(text) {
  if (!text || !text.trim()) throw new UsageError('Task/message must not be empty');
  return [{ type: 'text', text, text_elements: [] }];
}

function pick(obj, keys) {
  return Object.fromEntries(keys.map((k) => [k, obj?.[k] ?? null]));
}

async function resultItems(client, threadId, turnId) {
  const page = await client.call('thread/items/list', { threadId, turnId, limit: 100, sortDirection: 'desc' });
  const messages = page.data.map((row) => row.item).filter((item) => item?.type === 'agentMessage');
  // Keep the normal final response intact; the bounded page avoids loading an entire tool trace.
  return { messages: messages.reverse(), olderItemsCursor: page.nextCursor ?? null };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function run(cmd, args, threadId, connect) {
  if (cmd === 'models') {
    const c = await connect();
    const keys = ['id', 'model', 'displayName', 'defaultReasoningEffort', 'supportedReasoningEfforts'];
    return { models: (await c.models()).map((m) => pick(m, keys)) };
  }

  if (cmd === 'start') {
    const [presetModel, presetEffort] = PRESETS[args.kind];
    const model = args.model || presetModel;
    const effort = args.effort || presetEffort;
    let cwd;
    try {
      cwd = realpathSync(args.cwd.replace(/^~(?=$|\/)/, homedir()));
    } catch (err) {
      throw new UsageError(`cwd does not exist: ${args.cwd} (${err.code ?? err.message})`);
    }
    if (!statSync(cwd).isDirectory()) throw new UsageError('cwd must be a directory');
    const input = textInput(readPrompt(args));
    const c = await connect();
    await c.validateModel(model, effort);
    const started = await c.call('thread/start', {
      cwd,
      model,
      config: { model_reasoning_effort: effort },
      approvalPolicy: 'never',
      sandbox: args.sandbox,
      allowProviderModelFallback: false,
    });
    const tid = started.thread.id;
    // Emit recovery metadata before a subsequent operation can fail.
    process.stderr.write(JSON.stringify({ event: 'thread_created', threadId: tid, cwd, model, effort }) + '\n');
    if (args.name) await c.call('thread/name/set', { threadId: tid, name: args.name });
    const turn = (await c.call('turn/start', { threadId: tid, input, model, effort })).turn;
    return { threadId: tid, turnId: turn.id, status: turn.status, model, effort, cwd };
  }

  if (cmd === 'status') {
    const c = await connect();
    const t = await c.thread(threadId);
    const keys = ['id', 'name', 'cwd', 'status', 'model', 'reasoningEffort', 'canAcceptDirectInput'];
    return { thread: pick(t, keys), recentTurns: (await c.turns(threadId)).data.slice(0, 5) };
  }

  if (cmd === 'follow-up') {
    const input = textInput(readPrompt(args));
    const c = await connect();
    const t = await c.thread(threadId);
    if (t.status.type === 'active') {
      throw new RPCError('Thread is active. Use steer for current-turn input or wait before follow-up.');
    }
    if (t.status.type === 'systemError') throw new RPCError('Thread has a system error; inspect before resuming.');
    const resumed = await c.call('thread/resume', { threadId, excludeTurns: true });
    if (resumed.thread.status.type === 'active') throw new RPCError('Thread became active; follow-up not sent.');
    const model = args.model || resumed.model;
    const effort = args.effort || resumed.reasoningEffort || null;
    await c.validateModel(model, effort);
    const params = { threadId, input };
    if (args.model) params.model = args.model;
    if (args.effort) params.effort = args.effort;
    const turn = (await c.call('turn/start', params)).turn;
    return { threadId, turnId: turn.id, status: turn.status, model, effort };
  }

  if (cmd === 'steer' || cmd === 'interrupt') {
    const input = cmd === 'steer' ? textInput(readPrompt(args)) : null;
    const c = await connect();
    const active = await c.active(threadId);
    if (args['turn-id'] && args['turn-id'] !== active) {
      throw new RPCError('Expected turn differs from current active turn; no mutation sent.');
    }
    if (cmd === 'steer') {
      const result = await c.call('turn/steer', { threadId, expectedTurnId: active, input });
      return { threadId, ...result };
    }
    return { threadId, turnId: active, accepted: await c.call('turn/interrupt', { threadId, turnId: active }) };
  }

  if (cmd === 'wait') {
    const c = await connect();
    const deadline = Date.now() + args.seconds * 1000;
    let target = args['turn-id'];
    for (;;) {
      const t = await c.thread(threadId);
      let page = await c.turns(threadId);
      if (!target && page.data.length) target = page.data[0].id;
      let turn = page.data.find((x) => x.id === target);
      while (target && !turn && page.nextCursor) {
        page = await c.turns(threadId, page.nextCursor);
        turn = page.data.find((x) => x.id === target);
      }
      if (turn && turn.status !== 'inProgress') {
        return { threadId, turn, ...(await resultItems(c, threadId, target)) };
      }
      const flags = t.status.activeFlags ?? [];
      if (flags.length || t.status.type === 'systemError') {
        return { threadId, turnId: target ?? null, needsAttention: true, status: t.status };
      }
      if (Date.now() >= deadline) return { threadId, turnId: target ?? null, timedOut: true, status: t.status };
      await sleep(Math.min(2000, Math.max(0, deadline - Date.now())));
    }
  }

  throw new UsageError('Unknown command: ' + cmd);
}

// ---------------------------------------------------------------- CLI

const USAGE = `Usage: codex-use.mjs [--sock PATH] [--rpc-timeout SECONDS] COMMAND [options]

Commands:
  models
  start --cwd DIR --kind {research,simple,complex-review} (--message TEXT | --task-file FILE|-)
        [--name NAME] [--sandbox {read-only,workspace-write}] [--model MODEL] [--effort EFFORT]
  follow-up THREAD_ID (--message TEXT | --task-file FILE|-) [--model MODEL] [--effort EFFORT]
  steer THREAD_ID [--turn-id TURN_ID] (--message TEXT | --task-file FILE|-)
  interrupt THREAD_ID [--turn-id TURN_ID]
  status THREAD_ID
  wait THREAD_ID [--turn-id TURN_ID] [--seconds 0..60]

Global options:
  --sock PATH          Control socket of the owning App Server (default: shared daemon)
  --rpc-timeout SECS   Timeout per RPC (default: 30)

Exit codes: 0 ok, 1 validation/protocol/transport error, 2 wait timed out,
3 targeted turn failed or was interrupted, 4 needs attention, 130 client interrupted.
`;

const OPTIONS = {
  sock: { type: 'string' },
  'rpc-timeout': { type: 'string' },
  cwd: { type: 'string' },
  kind: { type: 'string' },
  name: { type: 'string' },
  sandbox: { type: 'string' },
  'turn-id': { type: 'string' },
  seconds: { type: 'string' },
  message: { type: 'string' },
  'task-file': { type: 'string' },
  model: { type: 'string' },
  effort: { type: 'string' },
  help: { type: 'boolean', short: 'h' },
};
const GLOBAL = ['sock', 'rpc-timeout', 'help'];
const INPUT = ['message', 'task-file'];
const COMMANDS = {
  models: { thread: false, allowed: [] },
  start: { thread: false, allowed: ['cwd', 'kind', 'name', 'sandbox', 'model', 'effort', ...INPUT] },
  'follow-up': { thread: true, allowed: ['model', 'effort', ...INPUT] },
  steer: { thread: true, allowed: ['turn-id', ...INPUT] },
  interrupt: { thread: true, allowed: ['turn-id'] },
  status: { thread: true, allowed: [] },
  wait: { thread: true, allowed: ['turn-id', 'seconds'] },
};

function parseCli(argv) {
  let parsed;
  try {
    parsed = parseArgs({ args: argv, options: OPTIONS, allowPositionals: true, strict: true });
  } catch (err) {
    throw new UsageError(err.message);
  }
  const { values, positionals } = parsed;
  if (values.help) return { help: true };
  const [cmd, threadId, ...extra] = positionals;
  const spec = COMMANDS[cmd];
  if (!spec) throw new UsageError(cmd ? `Unknown command: ${cmd}` : 'A command is required');
  if (extra.length || (!spec.thread && threadId)) throw new UsageError('Unexpected positional argument');
  if (spec.thread && !threadId) throw new UsageError(`${cmd} requires THREAD_ID`);
  for (const key of Object.keys(values)) {
    if (!GLOBAL.includes(key) && !spec.allowed.includes(key)) throw new UsageError(`--${key} is not valid for ${cmd}`);
  }
  for (const key of ['sock', 'cwd', 'name', 'model', 'effort', 'turn-id', 'task-file']) {
    if (values[key] !== undefined && values[key] === '') throw new UsageError(`--${key} must not be empty`);
  }
  if (spec.allowed.includes('message')) {
    const count = INPUT.filter((k) => values[k] !== undefined).length;
    if (count !== 1) throw new UsageError('Exactly one of --message or --task-file is required');
  }
  if (cmd === 'start') {
    if (!values.cwd) throw new UsageError('start requires --cwd');
    if (!PRESETS[values.kind]) throw new UsageError('start requires --kind {research,simple,complex-review}');
    values.sandbox ??= 'read-only';
    if (!SANDBOXES.includes(values.sandbox)) throw new UsageError('--sandbox must be read-only or workspace-write');
  }
  const rpcTimeout = values['rpc-timeout'] === undefined ? 30 : Number(values['rpc-timeout']);
  if (!Number.isFinite(rpcTimeout) || rpcTimeout <= 0) throw new UsageError('rpc-timeout must be positive');
  if (cmd === 'wait') {
    values.seconds = values.seconds === undefined ? 30 : Number(values.seconds);
    if (!Number.isFinite(values.seconds) || values.seconds < 0 || values.seconds > 60) {
      throw new UsageError('wait seconds must be 0..60');
    }
  }
  return { cmd, threadId, args: values, rpcTimeout };
}

function defaultSocket() {
  const home = process.env.CODEX_HOME || join(homedir(), '.codex');
  return join(home, 'app-server-control', 'app-server-control.sock');
}

function exitCodeFor(out) {
  if (['failed', 'interrupted'].includes(out?.turn?.status)) return 3;
  if (out?.needsAttention) return 4;
  if (out?.timedOut) return 2;
  return 0;
}

async function main() {
  process.on('SIGINT', () => {
    process.stderr.write(JSON.stringify({ error: 'Client interrupted; remote task was not cancelled. Use interrupt explicitly.' }) + '\n');
    process.exit(130);
  });
  let client = null;
  try {
    const cli = parseCli(process.argv.slice(2));
    if (cli.help) {
      process.stdout.write(USAGE);
      return 0;
    }
    const connect = async () => {
      client ??= await Client.open(cli.args.sock || defaultSocket(), cli.rpcTimeout);
      return client;
    };
    const out = await run(cli.cmd, cli.args, cli.threadId, connect);
    process.stdout.write(JSON.stringify(out) + '\n');
    return exitCodeFor(out);
  } catch (err) {
    process.stderr.write(JSON.stringify({ error: err?.message ?? String(err) }) + '\n');
    if (err instanceof UsageError) process.stderr.write('Run with --help for usage.\n');
    return 1;
  } finally {
    if (client) await client.close();
  }
}

process.exitCode = await main();
