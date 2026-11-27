#!/usr/bin/env python3
"""codex-use history: read-only queries over local Codex history.

Standard library only (Python 3.8+). Never writes to Codex storage. Locates the
newest state_N.sqlite / thread_history_N.sqlite, adapts to the columns present,
and reads plain or zstd-compressed rollout JSONL.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

INT_RE = re.compile(r'^-?\d+$')
PARSE_LIMIT = 4 * 1024 * 1024  # Parse at most this many characters of one JSON item or line.


class UsageError(Exception):
    pass


class Timeout(Exception):
    pass


# ------------------------------------------------------------------ storage discovery

def codex_home(args):
    return Path(args.codex_home or os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')).expanduser()


def newest_db(home, stem):
    """Return the highest-numbered <stem>_N.sqlite, so a storage migration does not break queries."""
    best = None
    for path in home.glob(stem + '_*.sqlite'):
        m = re.match(re.escape(stem) + r'_(\d+)\.sqlite$', path.name)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), path)
    if best:
        return best[1]
    plain = home / (stem + '.sqlite')
    return plain if plain.exists() else None


class Store:
    def __init__(self, args):
        self.args = args
        self.home = codex_home(args)
        self.deadline = time.monotonic() + args.timeout
        self.paths = {'state': args.state_db and Path(args.state_db) or newest_db(self.home, 'state'),
                      'history': args.history_db and Path(args.history_db) or newest_db(self.home, 'thread_history')}
        self.conns = {}
        self.cols = {}

    def db(self, name):
        if name not in self.conns:
            path = self.paths.get(name)
            if not path or not Path(path).exists():
                raise UsageError('No %s database found under %s; pass --%s-db.' % (name, self.home, name))
            con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1,
                                  isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            # A progress handler bounds CPU work; the connect timeout bounds lock waits.
            con.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 10000)
            self.conns[name] = con
        return self.conns[name]

    def columns(self, name, table):
        key = (name, table)
        if key not in self.cols:
            quoted = table.replace('"', '""')
            self.cols[key] = [r[1] for r in self.db(name).execute('PRAGMA table_info("%s")' % quoted)]
        return self.cols[key]

    def has_table(self, name, table):
        return bool(self.columns(name, table))

    def query(self, name, sql, params=()):
        try:
            return self.db(name).execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if 'interrupt' in str(e).lower():
                raise Timeout('Query exceeded --timeout %ss; narrow the scope.' % self.args.timeout)
            raise

    def close(self):
        for con in self.conns.values():
            con.close()


# ------------------------------------------------------------------ formatting helpers

def clip(value, chars):
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + '…[+%d chars]' % (len(value) - chars)
    return value


def clip_tail(value, chars):
    if isinstance(value, str) and len(value) > chars:
        return '[%d chars]…' % (len(value) - chars) + value[-chars:]
    return value


def iso(value, ms=False):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000 if ms else value, timezone.utc).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError, OSError):
        return value


def row_dict(row, chars):
    return {k: clip(row[k], chars) for k in row.keys()}


def pick(store, table, wanted):
    have = set(store.columns('state', table))
    return [c for c in wanted if c in have]


def resolve_rollout(path):
    """Codex may compress a rollout to <path>.zst without updating the recorded path."""
    if not path:
        return {'path': None, 'state': 'unrecorded'}
    for candidate, state in ((path, 'plain'), (path + '.zst', 'zstd')):
        if os.path.exists(candidate):
            return {'path': candidate, 'state': state}
    if path.endswith('.zst') and os.path.exists(path[:-4]):
        return {'path': path[:-4], 'state': 'plain'}
    return {'path': path, 'state': 'missing'}


def summarize_item(item, chars):
    """Compact view of a thread item; unknown types fall back to a JSON excerpt."""
    kind = item.get('type')
    out = {}
    if kind == 'userMessage':
        texts = [c.get('text') for c in item.get('content') or [] if isinstance(c, dict) and c.get('text')]
        out['text'] = clip('\n'.join(texts), chars)
    elif kind == 'agentMessage':
        out['phase'] = item.get('phase')
        out['text'] = clip(item.get('text'), chars)
    elif kind == 'commandExecution':
        for key in ('command', 'cwd', 'status', 'exitCode'):
            out[key] = clip(item.get(key), chars)
        if item.get('aggregatedOutput'):
            out['outputTail'] = clip_tail(item['aggregatedOutput'], chars)
    else:
        out['excerpt'] = clip(json.dumps(item, ensure_ascii=False), chars)
    return out


def bind(value):
    return int(value) if INT_RE.match(value) else value


# ------------------------------------------------------------------ commands

THREAD_COLUMNS = ['id', 'name', 'title', 'cwd', 'updated_at_ms', 'updated_at', 'model', 'reasoning_effort',
                  'archived', 'history_mode', 'source', 'git_branch', 'rollout_path', 'preview']


def cmd_info(store, args):
    out = {'codexHome': str(store.home), 'databases': {}}
    for name, path in store.paths.items():
        entry = {'path': str(path) if path else None}
        if path and Path(path).exists():
            entry['bytes'] = Path(path).stat().st_size
            tables = [r[0] for r in store.query(name, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            entry['tables'] = {t: store.columns(name, t) for t in tables if not t.startswith(('sqlite_', '_'))}
        out['databases'][name] = entry
    have_zstd = _zstd_module() is not None
    out['zstd'] = 'python' if have_zstd else ('cli' if shutil.which('zstd') else 'unavailable')
    return out


def cmd_threads(store, args):
    cols = pick(store, 'threads', THREAD_COLUMNS)
    have = set(store.columns('state', 'threads'))
    time_col = 'updated_at_ms' if 'updated_at_ms' in have else 'updated_at'
    scale = 1000 if time_col == 'updated_at_ms' else 1
    where, params = [], []
    if 'archived' in have:
        where.append('archived=?')
        params.append(1 if args.archived else 0)
    if args.cwd:
        p = Path(args.cwd).expanduser()
        raw = str(p.absolute())
        candidates = [raw]
        if p.exists():
            resolved = str(p.resolve())
            if resolved != raw:
                candidates.insert(0, resolved)
        if len(candidates) == 1:
            where.append('cwd=?')
            params.append(candidates[0])
        else:
            where.append('cwd IN (?, ?)')
            params.extend(candidates)
    if args.days > 0:
        where.append('%s>=?' % time_col)
        params.append(int((time.time() - args.days * 86400) * scale))
    if args.text:
        text_cols = [c for c in ('name', 'title', 'preview', 'first_user_message') if c in have]
        hay = " || ' ' || ".join("coalesce(%s,'')" % c for c in text_cols) or "''"
        where.append('instr(lower(%s), lower(?))>0' % hay)
        params.append(args.text)
    sql = 'SELECT %s FROM threads%s ORDER BY %s DESC, id DESC LIMIT ?' % (
        ', '.join(cols), (' WHERE ' + ' AND '.join(where)) if where else '', time_col)
    rows = []
    for r in store.query('state', sql, params + [args.limit]):
        d = row_dict(r, args.chars)
        if 'preview' in d:
            d['preview'] = clip(r['preview'], 240)
        d['updated'] = iso(r[time_col], ms=scale == 1000)
        d['rollout'] = resolve_rollout(r['rollout_path']) if 'rollout_path' in d else None
        d.pop('rollout_path', None)
        rows.append(d)
    return {'threads': rows, 'filters': {'archived': bool(args.archived), 'cwd': args.cwd, 'days': args.days,
                                         'text': args.text}}


def cmd_thread(store, args):
    rows = store.query('state', 'SELECT * FROM threads WHERE id=?', (args.thread_id,))
    if not rows:
        raise UsageError('Thread not found in local metadata: ' + args.thread_id)
    meta = row_dict(rows[0], args.chars)
    out = {'thread': meta, 'rollout': resolve_rollout(rows[0]['rollout_path'] if 'rollout_path' in meta else None)}
    if store.paths.get('history') and store.has_table('history', 'thread_turns'):
        stats = store.query('history', 'SELECT count(*) AS turns, max(rollout_ordinal) AS last_ordinal '
                                       'FROM thread_turns WHERE thread_id=?', (args.thread_id,))[0]
        out['history'] = dict(stats)
        if store.has_table('history', 'thread_history_projection_state'):
            proj = store.query('history', 'SELECT * FROM thread_history_projection_state WHERE thread_id=?',
                               (args.thread_id,))
            out['history']['projection'] = dict(proj[0]) if proj else None
    return out


def cmd_turns(store, args):
    have = set(store.columns('history', 'thread_turns'))
    wanted = ['turn_id', 'status', 'started_at', 'completed_at', 'duration_ms', 'rollout_ordinal',
              'first_user_item_id', 'final_agent_item_id', 'error_json']
    cols = [c for c in wanted if c in have]
    sql = 'SELECT %s FROM thread_turns WHERE thread_id=?' % ', '.join(cols)
    params = [args.thread_id]
    if args.before is not None:
        sql += ' AND rollout_ordinal<?'
        params.append(args.before)
    rows = []
    for r in store.query('history', sql + ' ORDER BY rollout_ordinal DESC LIMIT ?', params + [args.limit]):
        d = row_dict(r, args.chars)
        for key in ('started_at', 'completed_at'):
            if key in d:
                d[key] = iso(r[key])
        rows.append(d)
    return {'threadId': args.thread_id, 'turns': rows}


def cmd_items(store, args):
    where, params = ['thread_id=?'], [args.thread_id]
    if args.turn:
        where.append('turn_id=?')
        params.append(args.turn)
    if args.type:
        where.append('item_type=?')
        params.append(args.type)
    if args.before is not None:
        where.append('rollout_ordinal<?')
        params.append(args.before)
    sql = ('SELECT turn_id, item_id, item_type, rollout_ordinal, created_at_ms, length(item_json) AS chars, '
           'CASE WHEN length(item_json)<=? THEN item_json ELSE substr(item_json,1,?) END AS body '
           'FROM thread_items WHERE %s ORDER BY rollout_ordinal DESC LIMIT ?' % ' AND '.join(where))
    rows = []
    for r in store.query('history', sql, [PARSE_LIMIT, args.chars] + params + [args.limit]):
        d = {'turnId': r['turn_id'], 'itemId': r['item_id'], 'type': r['item_type'],
             'ordinal': r['rollout_ordinal'], 'created': iso(r['created_at_ms'], ms=True), 'chars': r['chars']}
        if r['chars'] <= PARSE_LIMIT:
            try:
                d.update(summarize_item(json.loads(r['body']), args.chars))
            except ValueError:
                d['excerpt'] = clip(r['body'], args.chars)
        else:
            d['excerpt'] = clip(r['body'], args.chars)
            d['note'] = 'Item too large to summarize; use the item command.'
        rows.append(d)
    nxt = rows[-1]['ordinal'] if len(rows) == args.limit else None
    return {'threadId': args.thread_id, 'items': rows, 'nextBefore': nxt}


def cmd_item(store, args):
    rows = store.query('history', 'SELECT item_type, item_json FROM thread_items '
                                  'WHERE thread_id=? AND turn_id=? AND item_id=?',
                       (args.thread_id, args.turn_id, args.item_id))
    if not rows:
        raise UsageError('Item not found in local history.')
    return {'type': rows[0]['item_type'], 'item': json.loads(rows[0]['item_json'])}


def cmd_search(store, args):
    where, params = ['thread_id=?'], [args.thread_id]
    if args.from_ordinal is not None:
        where.append('rollout_ordinal>=?')
        params.append(args.from_ordinal)
    if args.to_ordinal is not None:
        where.append('rollout_ordinal<?')
        params.append(args.to_ordinal)
    if args.type:
        where.append('item_type=?')
        params.append(args.type)
    window = max(80, args.chars // 2)
    sql = ('SELECT turn_id, item_id, item_type, rollout_ordinal, '
           'substr(item_json, max(1, instr(lower(item_json), lower(?))-?), ?) AS snippet '
           'FROM thread_items WHERE %s AND instr(lower(item_json), lower(?))>0 '
           'ORDER BY rollout_ordinal DESC LIMIT ?' % ' AND '.join(where))
    rows = store.query('history', sql, [args.text, window, args.chars] + params + [args.text, args.limit])
    return {'threadId': args.thread_id, 'searched': {'from': args.from_ordinal, 'to': args.to_ordinal},
            'note': 'Raw JSON text match; keys can match and text may be JSON-escaped.',
            'matches': [{'turnId': r['turn_id'], 'itemId': r['item_id'], 'type': r['item_type'],
                         'ordinal': r['rollout_ordinal'], 'snippet': r['snippet']} for r in rows]}


def _zstd_module():
    try:
        from compression import zstd  # Python 3.14+
        return zstd
    except ImportError:
        return None


class RolloutReader:
    def __init__(self, info):
        self.proc = None
        if info['state'] == 'plain':
            self.stream = open(info['path'], 'rb')
            return
        module = _zstd_module()
        if module is not None:
            self.stream = module.open(info['path'], 'rb')
            return
        exe = shutil.which('zstd')
        if not exe:
            raise UsageError('Rollout is zstd-compressed; install the zstd command or use Python 3.14+.')
        self.proc = subprocess.Popen([exe, '-dcq', '--', info['path']], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.stream = self.proc.stdout

    def close(self):
        self.stream.close()
        if self.proc:
            if self.proc.poll() is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
            self.proc.wait()


def describe_line(raw, number, needle, chars, ignore_case):
    line = raw.decode('utf-8', 'replace').rstrip('\r\n')
    entry = {'line': number, 'chars': len(line)}
    if len(line) <= PARSE_LIMIT:
        try:
            obj = json.loads(line)
            payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
            entry.update({'timestamp': obj.get('timestamp'), 'ordinal': obj.get('ordinal'),
                          'type': obj.get('type'), 'payloadType': payload.get('type')})
        except ValueError:
            entry['note'] = 'Line is not complete JSON.'
    start = 0
    if needle:
        pos = (line.lower() if ignore_case else line).find(needle)
        start = max(0, pos - chars // 3)
    entry['preview'] = ('…' if start else '') + clip(line[start:], chars)
    return entry


def cmd_rollout(store, args):
    rows = store.query('state', 'SELECT rollout_path FROM threads WHERE id=?', (args.thread_id,))
    if not rows:
        raise UsageError('Thread not found in local metadata: ' + args.thread_id)
    info = resolve_rollout(rows[0]['rollout_path'])
    if info['state'] in ('missing', 'unrecorded'):
        raise UsageError('Rollout file not found: %s' % info['path'])
    needle = args.text.lower() if (args.text and args.ignore_case) else args.text
    needle_bytes = needle.encode('utf-8') if needle else None
    reader = RolloutReader(info)
    kept, number, timed_out, stopped_early = [], 0, False, False
    try:
        for raw in reader.stream:
            number += 1
            if time.monotonic() > store.deadline:
                timed_out = True
                break
            if needle_bytes is not None:
                hay = raw.decode('utf-8', 'replace').lower().encode('utf-8') if args.ignore_case else raw
                if needle_bytes not in hay:
                    continue
            kept.append((number, raw))
            if args.tail:
                if len(kept) > args.limit:
                    kept.pop(0)
            elif len(kept) >= args.limit:
                stopped_early = True
                break
    finally:
        reader.close()
    out = {'threadId': args.thread_id, 'rollout': info, 'scannedLines': number,
           'complete': not timed_out and not stopped_early,
           'matches': [describe_line(raw, n, needle, args.chars, args.ignore_case) for n, raw in kept]}
    if timed_out:
        out['timedOut'] = True
    return out


def cmd_sql(store, args):
    con = store.db(args.database)
    con.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY
                       if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK)
    try:
        cur = con.execute(args.query, [bind(p) for p in args.params])
        rows = cur.fetchmany(args.limit + 1)
    except sqlite3.OperationalError as e:
        if 'interrupt' in str(e).lower():
            raise Timeout('Query exceeded --timeout %ss; narrow the scope.' % args.timeout)
        raise
    except (sqlite3.Warning, sqlite3.ProgrammingError) as e:
        raise UsageError('%s (run exactly one statement per call)' % e)
    return {'database': str(store.paths[args.database]), 'truncated': len(rows) > args.limit,
            'rows': [row_dict(r, args.chars) for r in rows[:args.limit]]}


# ------------------------------------------------------------------ CLI

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return value


def build_parser():
    p = Parser(prog='codex_history.py', description=__doc__.splitlines()[0])
    p.add_argument('--codex-home', help='Codex home (default: $CODEX_HOME or ~/.codex)')
    p.add_argument('--state-db', help='Override the thread metadata database path')
    p.add_argument('--history-db', help='Override the thread history database path')
    p.add_argument('--timeout', type=float, default=10, help='Total time budget in seconds (default: 10)')
    p.add_argument('--chars', type=positive_int, default=2000, help='Clip long text to N characters (default: 2000)')
    sub = p.add_subparsers(dest='command', parser_class=Parser)
    sub.required = True

    sub.add_parser('info', help='Show discovered databases, tables, columns and zstd support')

    s = sub.add_parser('threads', help='Find threads by directory, recency and text')
    s.add_argument('--cwd', help='Exact working directory recorded for the thread')
    s.add_argument('--days', type=float, default=7, help='Updated within N days; 0 = any time (default: 7)')
    s.add_argument('--text', help='Case-insensitive literal match on name/title/preview')
    s.add_argument('--archived', action='store_true', help='Search archived threads instead')
    s.add_argument('--limit', type=positive_int, default=30)

    s = sub.add_parser('thread', help='Metadata, rollout file and history coverage for one thread')
    s.add_argument('thread_id')

    s = sub.add_parser('turns', help='Recent turns of a thread')
    s.add_argument('thread_id')
    s.add_argument('--before', type=int, help='Only turns with rollout_ordinal below this value')
    s.add_argument('--limit', type=positive_int, default=10)

    s = sub.add_parser('items', help='Recent items of a thread, summarized')
    s.add_argument('thread_id')
    s.add_argument('--turn', help='Only items of this turn ID')
    s.add_argument('--type', help='Only this item type, e.g. userMessage, agentMessage, commandExecution')
    s.add_argument('--before', type=int, help='Keyset page: rollout_ordinal below this value (see nextBefore)')
    s.add_argument('--limit', type=positive_int, default=30)

    s = sub.add_parser('item', help='One complete item')
    s.add_argument('thread_id')
    s.add_argument('turn_id')
    s.add_argument('item_id')

    s = sub.add_parser('search', help='Literal text search in one thread history')
    s.add_argument('thread_id')
    s.add_argument('--text', required=True)
    s.add_argument('--from', dest='from_ordinal', type=int, help='Lowest rollout_ordinal to search')
    s.add_argument('--to', dest='to_ordinal', type=int, help='Search below this rollout_ordinal')
    s.add_argument('--type', help='Only this item type')
    s.add_argument('--limit', type=positive_int, default=20)

    s = sub.add_parser('rollout', help='Read or search the rollout JSONL (plain or .zst)')
    s.add_argument('thread_id')
    s.add_argument('--text', help='Literal text to match (default: every line)')
    s.add_argument('--ignore-case', action='store_true')
    s.add_argument('--tail', action='store_true', help='Keep the last matches instead of the first')
    s.add_argument('--limit', type=positive_int, default=20)

    s = sub.add_parser('sql', help='Run one read-only SQL statement with bound parameters')
    s.add_argument('database', choices=['state', 'history'])
    s.add_argument('query')
    s.add_argument('params', nargs='*', help='Bound in order; integer-looking values bind as integers')
    s.add_argument('--limit', type=positive_int, default=100)
    return p


COMMANDS = {'info': cmd_info, 'threads': cmd_threads, 'thread': cmd_thread, 'turns': cmd_turns,
            'items': cmd_items, 'item': cmd_item, 'search': cmd_search, 'rollout': cmd_rollout, 'sql': cmd_sql}


def main(argv=None):
    store = None
    try:
        args = build_parser().parse_args(argv)
        if not args.timeout > 0:
            raise UsageError('timeout must be positive')
        store = Store(args)
        out = COMMANDS[args.command](store, args)
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 2 if out.get('timedOut') else 0
    except Timeout as e:
        print(json.dumps({'error': str(e), 'timedOut': True}), file=sys.stderr)
        return 2
    except UsageError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # Report every failure as one JSON line, never a traceback.
        print(json.dumps({'error': '%s: %s' % (type(e).__name__, e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    sys.exit(main())
