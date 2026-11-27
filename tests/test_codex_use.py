import base64
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import codex_history  # noqa: E402
import codex_use  # noqa: E402


def _ws_send(conn, opcode, payload, fin=True):
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        header = struct.pack('!BB', b0, n)
    elif n < 65536:
        header = struct.pack('!BBH', b0, 126, n)
    else:
        header = struct.pack('!BBQ', b0, 127, n)
    conn.sendall(header + payload)


def _ws_recv(conn):
    def take(n):
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    head = take(2)
    if not head:
        return None, None
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack('!H', take(2))[0]
    elif length == 127:
        length = struct.unpack('!Q', take(8))[0]
    mask = take(4) if masked else None
    payload = take(length)
    if mask:
        payload = codex_use._xor(payload, mask)
    return opcode, payload


class FakeAppServer:
    """Minimal RFC 6455 Unix-socket JSON-RPC server for unit tests."""

    def __init__(self, handler):
        self.handler = handler
        self.tmpdir = tempfile.mkdtemp(prefix='cu-')
        self.sock_path = os.path.join(self.tmpdir, 's.sock')
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop_event = threading.Event()
        self.pongs = []
        self.client_errors = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(5)
            raw = b''
            while b'\r\n\r\n' not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                raw += chunk
            headers = {}
            for line in raw.split(b'\r\n\r\n', 1)[0].decode('latin-1').split('\r\n')[1:]:
                k, _, v = line.partition(':')
                headers[k.strip().lower()] = v.strip()
            accept = base64.b64encode(
                hashlib.sha1((headers['sec-websocket-key'] + codex_use.WS_GUID).encode('ascii')).digest()
            ).decode('ascii')
            conn.sendall((
                'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                'Sec-WebSocket-Accept: %s\r\n\r\n' % accept
            ).encode('ascii'))

            while not self.stop_event.is_set():
                opcode, payload = _ws_recv(conn)
                if opcode is None:
                    return
                if opcode == 0x8:
                    _ws_send(conn, 0x8, struct.pack('!H', 1000))
                    return
                if opcode == 0xA:
                    self.pongs.append(payload)
                    continue
                if opcode != 0x1:
                    continue
                msg = json.loads(payload.decode('utf-8'))
                if 'id' in msg and 'error' in msg:
                    self.client_errors.append(msg)
                    continue
                if 'id' not in msg:
                    continue
                self.handler(conn, msg)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop_event.set()
        self.srv.close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def run_cli(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class CodexUseTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.state = {
            'thread_status': {'type': 'idle'},
            'turns': [{'id': 'turn-1', 'status': 'completed'}],
            'items': [{'item': {'type': 'agentMessage', 'text': 'All clear.'}}],
            'send_ping_and_fragment': False,
            'send_server_request': False,
        }
        self.server = FakeAppServer(self._dispatch)
        self.addCleanup(self.server.close)

    def _send_json(self, conn, obj, fragment=False):
        raw = json.dumps(obj).encode('utf-8')
        if fragment and len(raw) > 8:
            mid = len(raw) // 2
            _ws_send(conn, 0x1, raw[:mid], fin=False)
            _ws_send(conn, 0x0, raw[mid:], fin=True)
        else:
            _ws_send(conn, 0x1, raw)

    def _dispatch(self, conn, msg):
        method = msg['method']
        params = msg.get('params') or {}
        rid = msg['id']
        self.calls.append((method, params))

        if method == 'initialize':
            self._send_json(conn, {'id': rid, 'result': {'ok': True}})
            return

        if self.state['send_server_request'] and method == 'model/list':
            self._send_json(conn, {'id': 999, 'method': 'item/commandExecution/requestApproval', 'params': {}})
            return

        if self.state['send_ping_and_fragment'] and method == 'model/list':
            _ws_send(conn, 0x9, b'ping-payload')
            self._send_json(conn, {'method': 'server/notification', 'params': {'note': 'ignored'}})

        if method == 'model/list':
            if params.get('cursor') is None:
                res = {
                    'data': [{
                        'id': 'gpt-6.2-sol',
                        'model': 'gpt-6.2-sol',
                        'displayName': 'GPT 6.2 Sol',
                        'defaultReasoningEffort': 'high',
                        'supportedReasoningEfforts': [
                            {'reasoningEffort': 'high'},
                            {'reasoningEffort': 'max'},
                        ],
                    }],
                    'nextCursor': 'page-2',
                }
            else:
                res = {
                    'data': [{
                        'id': 'custom-model',
                        'model': 'custom-model',
                        'displayName': 'Custom Model',
                        'defaultReasoningEffort': 'medium',
                        'supportedReasoningEfforts': [
                            {'reasoningEffort': 'low'},
                            {'reasoningEffort': 'medium'},
                        ],
                    }],
                    'nextCursor': None,
                }
            self._send_json(conn, {'id': rid, 'result': res}, fragment=self.state['send_ping_and_fragment'])
            return

        if method == 'thread/start':
            self._send_json(conn, {'id': rid, 'result': {'thread': {'id': 'thr-1'}}})
            return

        if method == 'thread/name/set':
            self._send_json(conn, {'id': rid, 'result': {'ok': True}})
            return

        if method == 'turn/start':
            self._send_json(conn, {'id': rid, 'result': {'turn': {'id': 'turn-new', 'status': 'inProgress'}}})
            return

        if method == 'thread/read':
            self._send_json(conn, {'id': rid, 'result': {'thread': {
                'id': params['threadId'],
                'name': 'Demo',
                'cwd': '/tmp',
                'status': self.state['thread_status'],
                'model': 'gpt-6.2-sol',
                'reasoningEffort': 'high',
                'canAcceptDirectInput': True,
            }}})
            return

        if method == 'thread/resume':
            self._send_json(conn, {'id': rid, 'result': {
                'thread': {'id': params['threadId'], 'status': self.state['thread_status']},
                'model': 'gpt-6.2-sol',
                'reasoningEffort': 'high',
            }})
            return

        if method == 'thread/turns/list':
            self._send_json(conn, {'id': rid, 'result': {'data': self.state['turns'], 'nextCursor': None}})
            return

        if method == 'thread/items/list':
            self._send_json(conn, {'id': rid, 'result': {'data': self.state['items'], 'nextCursor': None}})
            return

        if method == 'turn/steer':
            self._send_json(conn, {'id': rid, 'result': {'turnId': params['expectedTurnId']}})
            return

        if method == 'turn/interrupt':
            self._send_json(conn, {'id': rid, 'result': True})
            return

        self._send_json(conn, {'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}})

    def test_help_lists_all_subcommands(self):
        help_text = codex_use.build_parser().format_help()
        for sub in ('models', 'start', 'follow-up', 'steer', 'interrupt', 'status', 'wait'):
            self.assertIn(sub, help_text)
        self.assertIn('Start another turn on an idle thread', help_text)

    def test_missing_codex_cli_reports_install_guidance(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as empty_home:
            with mock.patch.dict(os.environ, {'CODEX_HOME': empty_home}), \
                 mock.patch.object(codex_use.shutil, 'which', return_value=None):
                code, _, err = run_cli(codex_use, ['models'])
        self.assertEqual(code, 1)
        self.assertIn('Install Codex CLI first', err)

    def test_models_pagination_ping_and_fragmentation(self):
        self.state['send_ping_and_fragment'] = True
        code, out, err = run_cli(codex_use, ['--sock', self.server.sock_path, 'models'])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        ids = [m['id'] for m in data['models']]
        self.assertEqual(ids, ['gpt-6.2-sol', 'custom-model'])
        self.assertIn(b'ping-payload', self.server.pongs)

    def test_server_initiated_request_refused(self):
        self.state['send_server_request'] = True
        code, _, err = run_cli(codex_use, ['--sock', self.server.sock_path, 'models'])
        self.assertEqual(code, 1)
        self.assertIn('interactive handling', err)
        time.sleep(0.05)
        self.assertEqual(len(self.server.client_errors), 1)
        self.assertEqual(self.server.client_errors[0]['error']['code'], -32601)

    def test_start_with_kind_and_explicit_model_without_kind(self):
        with tempfile.TemporaryDirectory() as cwd:
            code, out, err = run_cli(codex_use, [
                '--sock', self.server.sock_path, 'start',
                '--cwd', cwd, '--kind', 'simple', '--name', 'Review', '--message', 'Check diff',
            ])
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(res['model'], 'gpt-6.2-sol')
            self.assertEqual(res['effort'], 'max')
            self.assertIn('"thread_created"', err)

            # Explicit --model without --kind should use the model's catalog default effort.
            code2, out2, err2 = run_cli(codex_use, [
                '--sock', self.server.sock_path, 'start',
                '--cwd', cwd, '--model', 'custom-model', '--message', 'Run check',
            ])
            self.assertEqual(code2, 0, err2)
            res2 = json.loads(out2)
            self.assertEqual(res2['model'], 'custom-model')
            self.assertEqual(res2['effort'], 'medium')

            # Neither --kind nor --model should fail cleanly.
            code3, _, err3 = run_cli(codex_use, [
                '--sock', self.server.sock_path, 'start',
                '--cwd', cwd, '--message', 'Missing selector',
            ])
            self.assertEqual(code3, 1)
            self.assertIn('--kind or --model', err3)

    def test_follow_up_and_steer_preconditions(self):
        self.state['thread_status'] = {'type': 'active'}
        self.state['turns'] = [{'id': 'turn-active', 'status': 'inProgress'}]

        # follow-up must reject active threads.
        code, _, err = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'follow-up', 'thr-1', '--message', 'Next',
        ])
        self.assertEqual(code, 1)
        self.assertIn('Thread is active', err)

        # steer with stale --turn-id must be rejected.
        code, _, err = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'steer', 'thr-1', '--turn-id', 'turn-old', '--message', 'Pivot',
        ])
        self.assertEqual(code, 1)
        self.assertIn('Expected turn differs', err)

        # steer with matching --turn-id succeeds.
        code, out, err = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'steer', 'thr-1', '--turn-id', 'turn-active', '--message', 'Pivot',
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['turnId'], 'turn-active')

    def test_wait_exit_codes(self):
        # Completed -> 0
        code, out, _ = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'wait', 'thr-1', '--turn-id', 'turn-1', '--seconds', '1',
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['messages'][0]['text'], 'All clear.')

        # Interrupted -> 3
        self.state['turns'] = [{'id': 'turn-1', 'status': 'interrupted'}]
        code, _, _ = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'wait', 'thr-1', '--turn-id', 'turn-1', '--seconds', '1',
        ])
        self.assertEqual(code, 3)

        # Timeout -> 2
        self.state['turns'] = [{'id': 'turn-1', 'status': 'inProgress'}]
        self.state['thread_status'] = {'type': 'active'}
        code, out, _ = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'wait', 'thr-1', '--turn-id', 'turn-1', '--seconds', '0',
        ])
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(out)['timedOut'])

        # Needs attention -> 4
        self.state['thread_status'] = {'type': 'active', 'activeFlags': ['waitingApproval']}
        code, out, _ = run_cli(codex_use, [
            '--sock', self.server.sock_path, 'wait', 'thr-1', '--turn-id', 'turn-1', '--seconds', '0',
        ])
        self.assertEqual(code, 4)
        self.assertTrue(json.loads(out)['needsAttention'])


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='ch-'))
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

        self.real_repo = (self.tmpdir / 'real-repo').resolve()
        self.real_repo.mkdir()
        self.link_repo = self.tmpdir / 'link-repo'
        self.link_repo.symlink_to(self.real_repo)

        # Older state_4.sqlite should be ignored in favor of state_5.sqlite.
        sqlite3.connect(str(self.tmpdir / 'state_4.sqlite')).close()

        self.rollout = self.tmpdir / 'rollout-1.jsonl'
        self.rollout.write_text(
            json.dumps({'timestamp': '2026-11-27T00:00:00Z', 'ordinal': 1, 'type': 'session_meta',
                        'payload': {'id': 'thr-1', 'cwd': str(self.real_repo)}}) + '\n' +
            json.dumps({'timestamp': '2026-11-27T00:00:05Z', 'ordinal': 2, 'type': 'response_item',
                        'payload': {'type': 'message', 'text': 'Retry race fixed'}}) + '\n',
            encoding='utf-8',
        )

        now_ms = int(time.time() * 1000)
        state_db = sqlite3.connect(str(self.tmpdir / 'state_5.sqlite'))
        state_db.execute(
            'CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, cwd TEXT, '
            'updated_at_ms INTEGER, model TEXT, reasoning_effort TEXT, archived INTEGER, '
            'history_mode TEXT, rollout_path TEXT, preview TEXT)'
        )
        state_db.execute(
            'INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('thr-1', 'Retry fix', 'Debug retry race', str(self.real_repo), now_ms,
             'gpt-6.2-sol', 'high', 0, 'paginated', str(self.rollout), 'Fix retry race'),
        )
        state_db.commit()
        state_db.close()

        hist_db = sqlite3.connect(str(self.tmpdir / 'thread_history_1.sqlite'))
        hist_db.execute(
            'CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER, '
            'completed_at INTEGER, duration_ms INTEGER, rollout_ordinal INTEGER, '
            'first_user_item_id TEXT, final_agent_item_id TEXT, error_json TEXT)'
        )
        hist_db.execute(
            'INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('thr-1', 'turn-1', 'completed', 1700000000, 1700000010, 10000, 1, 'u1', 'a1', None),
        )
        hist_db.execute(
            'CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, item_type TEXT, '
            'rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT)'
        )
        hist_db.execute(
            'INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?)',
            ('thr-1', 'turn-1', 'a1', 'agentMessage', 2, now_ms,
             json.dumps({'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Found retry race.'})),
        )
        hist_db.commit()
        hist_db.close()

    def test_newest_db_and_symlink_cwd_query(self):
        code, out, err = run_cli(codex_history, ['--codex-home', str(self.tmpdir), 'info'])
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertTrue(info['databases']['state']['path'].endswith('state_5.sqlite'))

        code, out, err = run_cli(codex_history, [
            '--codex-home', str(self.tmpdir), 'threads', '--cwd', str(self.link_repo),
        ])
        self.assertEqual(code, 0, err)
        threads = json.loads(out)['threads']
        self.assertEqual([t['id'] for t in threads], ['thr-1'])

    def test_items_search_rollout_and_readonly_sql(self):
        code, out, err = run_cli(codex_history, [
            '--codex-home', str(self.tmpdir), 'search', 'thr-1', '--text', 'retry race',
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)['matches']), 1)

        code, out, err = run_cli(codex_history, [
            '--codex-home', str(self.tmpdir), 'rollout', 'thr-1', '--text', 'retry race', '--ignore-case',
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)['matches']), 1)

        # Read-only SQL must reject writes and ATTACH.
        code, _, _ = run_cli(codex_history, [
            '--codex-home', str(self.tmpdir), 'sql', 'state', 'DELETE FROM threads',
        ])
        self.assertEqual(code, 1)

        code, _, _ = run_cli(codex_history, [
            '--codex-home', str(self.tmpdir), 'sql', 'state', "ATTACH DATABASE ':memory:' AS x",
        ])
        self.assertEqual(code, 1)


class SkillSpecTests(unittest.TestCase):
    def test_skill_frontmatter_and_relative_links(self):
        import re

        text = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertTrue(text.startswith('---\n'))
        end = text.index('\n---\n', 4)
        frontmatter = text[4:end]
        body = text[end + 5:]

        fields = {}
        current = None
        for line in frontmatter.splitlines():
            if not line.startswith(' ') and ':' in line:
                key, _, val = line.partition(':')
                current = key.strip()
                fields[current] = val.strip()
            elif current is not None:
                fields[current] = (fields[current] + ' ' + line.strip()).strip()

        allowed = {'name', 'description', 'license', 'compatibility', 'metadata', 'allowed-tools'}
        self.assertTrue(set(fields).issubset(allowed))
        self.assertEqual(fields.get('name'), ROOT.name)
        self.assertRegex(fields['name'], r'^[a-z0-9]+(-[a-z0-9]+)*$')
        self.assertGreater(len(fields.get('description', '')), 0)
        self.assertLessEqual(len(fields['description']), 1024)
        self.assertLessEqual(len(fields.get('compatibility', '')), 500)
        self.assertEqual(fields.get('license'), 'MIT')

        # Ensure no hardcoded install path remains and all relative links exist.
        self.assertNotIn('$HOME/.agents/skills/', body)
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', body):
            if target.startswith(('http://', 'https://', '#')):
                continue
            self.assertTrue((ROOT / target).exists(), 'Missing linked file: ' + target)


if __name__ == '__main__':
    unittest.main()
