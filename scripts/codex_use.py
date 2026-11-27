#!/usr/bin/env python3
"""codex-use: control client for a shared Codex App Server.

Standard library only (Python 3.8+). Speaks JSON-RPC over a WebSocket on the
daemon's Unix socket; no third-party packages are needed.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

CLIENT_NAME = 'codex_use'
CLIENT_VERSION = '1.0.0'
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_MESSAGE_BYTES = 256 * 1024 * 1024
PRESETS = {
    'research': ('gpt-6.2-sol', 'high'),
    'simple': ('gpt-6.2-sol', 'max'),
    'complex-review': ('gpt-6.2-astra', 'xhigh'),
}


class RPCError(Exception):
    pass


class RPCTimeout(Exception):
    pass


class UsageError(Exception):
    pass


# ------------------------------------------------------------------ WebSocket

def _xor(data, mask):
    """Apply a 4-byte WebSocket mask with one big-integer XOR (fast for large payloads)."""
    if not data:
        return b''
    repeated = (mask * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(repeated, 'big')).to_bytes(len(data), 'big')


class WebSocket:
    """Minimal RFC 6455 client over a Unix socket (masked frames, no extensions)."""

    def __init__(self, path, timeout):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buf = bytearray()
        self.closed = False
        self.close_sent = False
        try:
            self.sock.settimeout(timeout)
            try:
                self.sock.connect(path)
            except socket.timeout:
                raise RPCTimeout('Timed out connecting to App Server socket %s' % path)
            except OSError as e:
                raise RPCError('Cannot connect to App Server socket %s: %s' % (path, e.strerror or e))
            self._handshake(timeout)
        except BaseException:
            self.sock.close()
            raise

    def _handshake(self, timeout):
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        self.sock.sendall((
            'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
            'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % key).encode('ascii'))
        deadline = time.monotonic() + timeout
        while b'\r\n\r\n' not in self.buf:
            if len(self.buf) > 65536:
                raise RPCError('App Server sent an oversized HTTP response.')
            self._fill(deadline)
        end = self.buf.index(b'\r\n\r\n')
        head = bytes(self.buf[:end])
        del self.buf[:end + 4]
        lines = head.decode('latin-1').split('\r\n')
        parts = lines[0].split(' ', 2)
        if len(parts) < 2 or parts[1] != '101':
            raise RPCError('App Server refused the WebSocket upgrade: ' + lines[0])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected:
            raise RPCError('Invalid WebSocket handshake from App Server.')

    def _fill(self, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise RPCTimeout('App Server response timed out; mutation outcome may be unknown. Inspect before retrying.')
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(1 << 16)
        except socket.timeout:
            raise RPCTimeout('App Server response timed out; mutation outcome may be unknown. Inspect before retrying.')
        if not chunk:
            self.closed = True
            raise RPCError('App Server connection closed; inspect before retrying a mutation.')
        self.buf.extend(chunk)

    def _take(self, n, deadline):
        while len(self.buf) < n:
            self._fill(deadline)
        data = bytes(self.buf[:n])
        del self.buf[:n]  # bytearray front deletion is amortized O(1)
        return data

    def _send_frame(self, opcode, payload):
        if self.closed or self.close_sent:
            raise RPCError('App Server connection is closed; inspect before retrying a mutation.')
        length = len(payload)
        if length < 126:
            header = struct.pack('!BB', 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, length)
        mask = os.urandom(4)
        if opcode == 0x8:
            self.close_sent = True
        self.sock.settimeout(None)
        self.sock.sendall(header + mask + _xor(payload, mask))

    def send_text(self, text):
        self._send_frame(0x1, text.encode('utf-8'))

    def recv_text(self, deadline):
        fragments = None
        while True:
            b0, b1 = self._take(2, deadline)
            fin, opcode, length = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._take(2, deadline))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._take(8, deadline))[0]
            if length > MAX_MESSAGE_BYTES:
                raise RPCError('App Server WebSocket message is too large.')
            mask = self._take(4, deadline) if b1 & 0x80 else None
            payload = self._take(length, deadline)
            if mask:
                payload = _xor(payload, mask)
            if opcode == 0x8:
                if not self.close_sent:
                    try:
                        self._send_frame(0x8, payload[:2])
                    except OSError:
                        pass
                self.closed = True
                raise RPCError('App Server closed the WebSocket; inspect before retrying a mutation.')
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if fragments is not None:
                    raise RPCError('WebSocket protocol error: interleaved message.')
                if fin:
                    return payload.decode('utf-8')
                fragments = [payload]
                continue
            if opcode == 0x0:
                if fragments is None:
                    raise RPCError('WebSocket protocol error: unexpected continuation.')
                fragments.append(payload)
                if sum(len(p) for p in fragments) > MAX_MESSAGE_BYTES:
                    raise RPCError('App Server WebSocket message is too large.')
                if fin:
                    return b''.join(fragments).decode('utf-8')
                continue
            raise RPCError('WebSocket protocol error: unknown opcode %d.' % opcode)

    def close(self):
        if not self.closed and not self.close_sent:
            try:
                self._send_frame(0x8, struct.pack('!H', 1000))
                deadline = time.monotonic() + 1
                while True:  # Drain until the server answers the close frame.
                    self.recv_text(deadline)
            except (RPCError, RPCTimeout, OSError):
                pass
        self.closed = True
        self.sock.close()


# ------------------------------------------------------------------ JSON-RPC client

class Client:
    def __init__(self, sock_path, timeout):
        self.timeout = timeout
        self.seq = 0
        self.ws = WebSocket(sock_path, timeout)
        try:
            self.call('initialize', {'clientInfo': {'name': CLIENT_NAME, 'version': CLIENT_VERSION},
                                     'capabilities': {'experimentalApi': True}})
            self.ws.send_text(json.dumps({'method': 'initialized'}))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.ws.close()

    def call(self, method, params):
        self.seq += 1
        rid = self.seq
        self.ws.send_text(json.dumps({'id': rid, 'method': method, 'params': params}, ensure_ascii=False))
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                msg = json.loads(self.ws.recv_text(deadline))
            except ValueError:
                raise RPCError('App Server sent invalid JSON.')
            if 'id' in msg and 'method' in msg:
                # Never silently grant approvals or fabricate answers to interactive requests.
                self.ws.send_text(json.dumps({'id': msg['id'], 'error': {
                    'code': -32601, 'message': 'Noninteractive codex-use does not handle client requests'}}))
                raise RPCError('Server requested interactive handling: ' + str(msg['method']))
            if msg.get('id') != rid:
                continue  # Notifications are not needed by this client.
            if 'error' in msg:
                err = msg['error'] or {}
                text = json.dumps(err, ensure_ascii=False)
                if err.get('code') == -32601:
                    text += (' (this App Server does not support %s; compare `codex app-server daemon version`'
                             ' with the versions in references/app-server.md)' % method)
                raise RPCError(text)
            return msg.get('result') or {}

    def thread(self, tid):
        return self.call('thread/read', {'threadId': tid})['thread']

    def turns(self, tid, cursor=None):
        params = {'threadId': tid, 'limit': 50, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
        if cursor:
            params['cursor'] = cursor
        return self.call('thread/turns/list', params)

    def active(self, tid):
        thread = self.thread(tid)
        if thread['status']['type'] != 'active':
            raise RPCError('No active turn on this server: ' + json.dumps(thread['status']))
        for turn in self.turns(tid)['data']:
            if turn['status'] == 'inProgress':
                return turn['id']
        raise RPCError('Active turn ID unavailable; retry status, not the mutation.')

    def models(self):
        data, cursor = [], None
        while True:
            params = {'limit': 100, 'includeHidden': False}
            if cursor:
                params['cursor'] = cursor
            page = self.call('model/list', params)
            data.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return data

    def validate_model(self, model, effort, preset=False):
        entries = self.models()
        match = next((m for m in entries if m.get('model') == model or m.get('id') == model), None)
        if not match:
            hint = ''
            if preset:
                hint = ('; this is the --kind preset, so pass --model explicitly. Available: '
                        + ', '.join(m.get('model') or m.get('id') for m in entries))
            raise RPCError('Requested model is not in this server catalog: ' + model + hint)
        supported = [x['reasoningEffort'] for x in match.get('supportedReasoningEfforts') or []]
        if effort and effort not in supported:
            raise RPCError('Effort %s is unsupported for %s; supported: %s' % (effort, model, supported))
        return match


# ------------------------------------------------------------------ commands

def read_prompt(args):
    if args.task_file is not None:
        try:
            data = sys.stdin.buffer.read() if args.task_file == '-' else Path(args.task_file).read_bytes()
            return data.decode('utf-8')
        except UnicodeDecodeError:
            raise UsageError('Task file is not valid UTF-8.')
    return args.message


def text_input(text):
    if not text or not text.strip():
        raise UsageError('Task/message must not be empty')
    return [{'type': 'text', 'text': text, 'text_elements': []}]


def result_items(client, tid, turn_id):
    page = client.call('thread/items/list', {'threadId': tid, 'turnId': turn_id,
                                           'limit': 100, 'sortDirection': 'desc'})
    messages = [row['item'] for row in page['data'] if (row.get('item') or {}).get('type') == 'agentMessage']
    # Keep the normal final response intact; the bounded page avoids loading an entire tool trace.
    return {'messages': list(reversed(messages)), 'olderItemsCursor': page.get('nextCursor')}


def run(args, connect):
    cmd = args.command
    if cmd == 'models':
        keys = ('id', 'model', 'displayName', 'defaultReasoningEffort', 'supportedReasoningEfforts')
        return {'models': [{k: m.get(k) for k in keys} for m in connect().models()]}

    if cmd == 'start':
        preset_model, preset_effort = PRESETS[args.kind]
        model = args.model or preset_model
        effort = args.effort or preset_effort
        try:
            cwd = str(Path(args.cwd).expanduser().resolve(strict=True))
        except OSError as e:
            raise UsageError('cwd does not exist: %s (%s)' % (args.cwd, e.strerror or e))
        if not Path(cwd).is_dir():
            raise UsageError('cwd must be a directory')
        inputs = text_input(read_prompt(args))
        c = connect()
        c.validate_model(model, effort, preset=not args.model)
        started = c.call('thread/start', {'cwd': cwd, 'model': model,
                                          'config': {'model_reasoning_effort': effort},
                                          'approvalPolicy': 'never', 'sandbox': args.sandbox,
                                          'allowProviderModelFallback': False})
        tid = started['thread']['id']
        # Emit recovery metadata before a subsequent operation can fail.
        print(json.dumps({'event': 'thread_created', 'threadId': tid, 'cwd': cwd,
                          'model': model, 'effort': effort}, ensure_ascii=False), file=sys.stderr, flush=True)
        if args.name:
            c.call('thread/name/set', {'threadId': tid, 'name': args.name})
        turn = c.call('turn/start', {'threadId': tid, 'input': inputs, 'model': model, 'effort': effort})['turn']
        return {'threadId': tid, 'turnId': turn['id'], 'status': turn['status'],
                'model': model, 'effort': effort, 'cwd': cwd}

    tid = args.thread_id
    if cmd == 'status':
        c = connect()
        t = c.thread(tid)
        keys = ('id', 'name', 'cwd', 'status', 'model', 'reasoningEffort', 'canAcceptDirectInput')
        return {'thread': {k: t.get(k) for k in keys}, 'recentTurns': c.turns(tid)['data'][:5]}

    if cmd == 'follow-up':
        inputs = text_input(read_prompt(args))
        c = connect()
        t = c.thread(tid)
        if t['status']['type'] == 'active':
            raise RPCError('Thread is active. Use steer for current-turn input or wait before follow-up.')
        if t['status']['type'] == 'systemError':
            raise RPCError('Thread has a system error; inspect before resuming.')
        resumed = c.call('thread/resume', {'threadId': tid, 'excludeTurns': True})
        if resumed['thread']['status']['type'] == 'active':
            raise RPCError('Thread became active; follow-up not sent.')
        model = args.model or resumed.get('model')
        effort = args.effort or resumed.get('reasoningEffort')
        c.validate_model(model, effort)
        params = {'threadId': tid, 'input': inputs}
        if args.model:
            params['model'] = args.model
        if args.effort:
            params['effort'] = args.effort
        turn = c.call('turn/start', params)['turn']
        return {'threadId': tid, 'turnId': turn['id'], 'status': turn['status'], 'model': model, 'effort': effort}

    if cmd in ('steer', 'interrupt'):
        inputs = text_input(read_prompt(args)) if cmd == 'steer' else None
        c = connect()
        active = c.active(tid)
        if args.turn_id and args.turn_id != active:
            raise RPCError('Expected turn differs from current active turn; no mutation sent.')
        if cmd == 'steer':
            result = c.call('turn/steer', {'threadId': tid, 'expectedTurnId': active, 'input': inputs})
            return dict({'threadId': tid}, **result)
        return {'threadId': tid, 'turnId': active,
                'accepted': c.call('turn/interrupt', {'threadId': tid, 'turnId': active})}

    if cmd == 'wait':
        c = connect()
        deadline = time.monotonic() + args.seconds
        target = args.turn_id
        while True:
            t = c.thread(tid)
            page = c.turns(tid)
            if not target and page['data']:
                target = page['data'][0]['id']
            turn = next((x for x in page['data'] if x['id'] == target), None)
            while target and turn is None and page.get('nextCursor'):
                page = c.turns(tid, page['nextCursor'])
                turn = next((x for x in page['data'] if x['id'] == target), None)
            if turn and turn['status'] != 'inProgress':
                return dict({'threadId': tid, 'turn': turn}, **result_items(c, tid, target))
            if t['status'].get('activeFlags') or t['status']['type'] == 'systemError':
                return {'threadId': tid, 'turnId': target, 'needsAttention': True, 'status': t['status']}
            if time.monotonic() >= deadline:
                return {'threadId': tid, 'turnId': target, 'timedOut': True, 'status': t['status']}
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    raise UsageError('Unknown command: ' + cmd)


# ------------------------------------------------------------------ CLI

def codex_home():
    return Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex'))


def resolve_socket(explicit):
    if explicit:
        return explicit
    default = codex_home() / 'app-server-control' / 'app-server-control.sock'
    if default.exists():
        return str(default)
    # Ask the CLI where its daemon listens, in case the layout changed.
    codex = shutil.which('codex')
    if codex:
        try:
            out = subprocess.run([codex, 'app-server', 'daemon', 'version'], capture_output=True,
                                 text=True, timeout=20).stdout
            path = json.loads(out).get('socketPath')
            if path and Path(path).exists():
                return path
        except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
            pass
    raise RPCError('App Server socket not found at %s. Run `codex app-server daemon start`, '
                   'or pass --sock for another server.' % default)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    p = Parser(prog='codex_use.py', description=__doc__.splitlines()[0])
    p.add_argument('--sock', help='Control socket of the owning App Server (default: shared daemon)')
    p.add_argument('--rpc-timeout', type=float, default=30, help='Timeout per RPC in seconds (default: 30)')
    sub = p.add_subparsers(dest='command', parser_class=Parser)
    sub.required = True
    sub.add_parser('models', help='List models and supported efforts')
    start = sub.add_parser('start', help='Create a thread and start its first turn')
    start.add_argument('--cwd', required=True)
    start.add_argument('--kind', choices=sorted(PRESETS), required=True)
    start.add_argument('--name')
    start.add_argument('--sandbox', choices=['read-only', 'workspace-write'], default='read-only')
    for name in ('follow-up', 'steer', 'interrupt', 'status', 'wait'):
        s = sub.add_parser(name)
        s.add_argument('thread_id')
        if name in ('steer', 'interrupt', 'wait'):
            s.add_argument('--turn-id')
        if name == 'wait':
            s.add_argument('--seconds', type=float, default=30)
    for name in ('start', 'follow-up', 'steer'):
        g = sub.choices[name].add_mutually_exclusive_group(required=True)
        g.add_argument('--message')
        g.add_argument('--task-file', help='UTF-8 task file, or - for stdin')
    for name in ('start', 'follow-up'):
        s = sub.choices[name]
        s.add_argument('--model')
        s.add_argument('--effort', help='Any effort the server catalog lists for the model')
    return p


def exit_code_for(out):
    if (out.get('turn') or {}).get('status') in ('failed', 'interrupted'):
        return 3
    if out.get('needsAttention'):
        return 4
    if out.get('timedOut'):
        return 2
    return 0


def main(argv=None):
    client = None
    try:
        args = build_parser().parse_args(argv)
        if not args.rpc_timeout > 0:
            raise UsageError('rpc-timeout must be positive')
        if args.command == 'wait' and not 0 <= args.seconds <= 60:
            raise UsageError('wait seconds must be 0..60')

        def connect():
            nonlocal client
            if client is None:
                client = Client(resolve_socket(args.sock), args.rpc_timeout)
            return client

        out = run(args, connect)
        print(json.dumps(out, ensure_ascii=False))
        return exit_code_for(out)
    except KeyboardInterrupt:
        print(json.dumps({'error': 'Client interrupted; remote task was not cancelled. Use interrupt explicitly.'}),
              file=sys.stderr)
        return 130
    except UsageError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        print('Run with --help for usage.', file=sys.stderr)
        return 1
    except (RPCError, RPCTimeout, OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({'error': str(e) or type(e).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
