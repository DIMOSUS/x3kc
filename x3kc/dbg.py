"""The live debugger's client: `x3kc dbg <command> ...` talks to a running x3_src ScriptDebug game over its pipe
\\\\.\\pipe\\x3dbg-<pid> (the protocol: docs/debugging.md "Live debugger"). Every command is one process: it connects,
sends its requests, prints the answer (text, or one JSON object per line with --json) and exits; the breakpoints and
the stop live in the game. Names come from the loaded object's .kcdbg sidecar (kcdbg.DebugInfo): source lines ->
code offsets, variables -> stack cells, fields and globals -> slots; the game knows no names.

    x3kc dbg [--pid N] [--json] [--dbg DIR]... <command> [args]
    games | hello | tasks | list | events | wait [--timeout S] | stack | locals | print PATH... | continue |
    step [over|into|out|msci] | pause | clear [ID | --all] | detach | config --stop-timeout S |
    break LOCATION [--if PATH OP VALUE]... [--log PATH[,PATH...]] [--count N] [--once] [--this ID] |
    msci break SCRIPT LINE [...] | msci vars | msci stack | run FILE

Exit codes: 0 done, 1 an error (the game's or the client's), 2 wait's timeout.
"""
import argparse
import json
import os
import re
import shlex
import sys
import time

from . import kcdbg

PIPE_DIR = '\\\\.\\pipe\\'
PIPE_PREFIX = 'x3dbg-'


class DbgError(Exception):
    pass


# ---- the connection ------------------------------------------------------------------------------------------------
def list_games():
    try:
        names = os.listdir(PIPE_DIR)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(PIPE_PREFIX) and n[len(PIPE_PREFIX):].isdigit():
            out.append(int(n[len(PIPE_PREFIX):]))
    return sorted(out)


class DbgTimeout(DbgError):
    pass


DEFAULT_DEADLINE = 30.0   # seconds a reply is awaited (--deadline, X3KC_DBG_DEADLINE)


class Conn:
    reply_timeout = DEFAULT_DEADLINE

    def __init__(self, pid=None, timeout=10.0):
        if pid is None:
            games = list_games()
            if not games:
                raise DbgError('no ScriptDebug game is running (no pipe %sx3dbg-<pid>)' % PIPE_DIR)
            if len(games) > 1:
                raise DbgError('several games run (%s): --pid N' % ', '.join(map(str, games)))
            pid = games[0]
        self.pid = pid
        path = '%s%s%d' % (PIPE_DIR, PIPE_PREFIX, pid)
        start = time.time()
        while True:
            try:
                self.f = open(path, 'r+b', buffering=0)
                break
            except OSError as x:
                # busy (another client) or not yet listening again after the last client: retry for a while
                if time.time() - start > timeout:
                    raise DbgError('cannot open %s: %s' % (path, x))
                time.sleep(0.05)
        self.next_id = 1
        self.buffer = b''

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass

    def available(self):
        """the bytes waiting in the pipe (PeekNamedPipe: never blocks); -1 when the game closed it"""
        import ctypes
        import msvcrt
        from ctypes import wintypes
        n = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.PeekNamedPipe(wintypes.HANDLE(msvcrt.get_osfhandle(self.f.fileno())), None, 0,
                                                  None, ctypes.byref(n), None)
        return n.value if ok else -1

    def request(self, cmd, deadline=None, **args):
        """one request and its reply; the reply is awaited until the deadline (seconds; default: the Conn's
        reply_timeout, --deadline): the game answers in its next frame or at once at a stop, a loading screen or a
        hung game answers late or never - DbgError then"""
        args['cmd'] = cmd
        args['id'] = self.next_id
        self.next_id += 1
        self.f.write((json.dumps(args, separators=(',', ':')) + '\n').encode('utf-8'))
        end = time.time() + (self.reply_timeout if deadline is None else deadline)
        while b'\n' not in self.buffer:
            n = self.available()
            if n < 0:
                raise DbgError('the game closed the pipe')
            if n == 0:
                if time.time() > end:
                    raise DbgTimeout('%s: no reply from the game in %g s (a loading screen, or the game hangs)'
                                     % (cmd, self.reply_timeout if deadline is None else deadline))
                time.sleep(0.01)
                continue
            chunk = self.f.read(n)   # (n bytes are there: the read returns at once)
            if not chunk:
                raise DbgError('the game closed the pipe')
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b'\n')
        reply = json.loads(line.decode('utf-8', 'replace'))
        return reply

    def call(self, cmd, deadline=None, **args):
        r = self.request(cmd, deadline=deadline, **args)
        if not r.get('ok'):
            raise DbgError('%s: %s' % (cmd, r.get('error', 'failed')))
        return r


# ---- the session: the game's objects and their sidecars -------------------------------------------------------------
class Session:
    def __init__(self, conn, dirs, out):
        self.conn = conn
        self.dirs = list(dirs)
        self.out = out
        self.hello = conn.call('hello')
        self.infos = {}

    def objects(self):
        return self.hello.get('objects', [])

    def main_object(self):
        objs = self.objects()
        for o in objs:
            if o.get('main'):
                return o
        if objs:
            return objs[0]
        raise DbgError('the game has no KC object loaded')

    def object_named(self, name):
        for o in self.objects():
            if o['name'].lower() == name.lower():
                return o
        raise DbgError('the game has no object %s loaded' % name)

    def info(self, obj):
        """the DebugInfo of a loaded object (its identity checked): beside its -kc file, else in --dbg folders"""
        key = (obj['name'], obj['size'], obj['crc32'])
        if key in self.infos:
            return self.infos[key]
        cands = []
        if obj.get('file'):
            cands.append(kcdbg.sidecar_path(obj['file']))
        found = kcdbg.find(obj['name'], self.dirs, obj['size'], obj['crc32'])
        if found:
            cands.append(found)
        for p in cands:
            if os.path.isfile(p):
                info = kcdbg.DebugInfo.load(p)
                if info.matches(obj['size'], obj['crc32']):
                    self.infos[key] = info
                    return info
        raise DbgError('no sidecar of %s (%d bytes, crc32 %s): a -kc object has its .kcdbg beside it, else --dbg DIR'
                       % (obj['name'], obj['size'], obj['crc32']))

    def main_info(self):
        return self.info(self.main_object())

    def info_named(self, name):
        return self.info(self.object_named(name))


def location_text(info, pc, resume):
    loc = info.location(pc, resume) if info is not None else None
    if loc is None:
        return '0x%x' % pc
    return '%s:%d %s' % (loc.file, loc.line, loc.method.qualname)


# ---- values ---------------------------------------------------------------------------------------------------------
def class_fields(info, cid):
    k = info.classes.get(cid) if info is not None else None
    return k['fields'] if k else []


def format_value(info, v, indent=0):
    if v is None:
        return '?'
    if 'error' in v:
        return '<%s>' % v['error']
    t = v.get('t')
    if t == 'int':
        return str(v['v'])
    if t in ('double', 'int64'):
        return str(v['v'])
    if t == 'empty':
        return 'null'
    if t == 'ret':
        return '<return 0x%x>' % v['v']
    if t == 'str':
        s = json.dumps(v['v'], ensure_ascii=False)
        if 'length' in v:
            s += '... (%d chars)' % v['length']
        return s
    if t == 'obj':
        if v.get('null'):
            return 'null object'
        cname = info.class_name(v.get('class')) if info is not None and 'class' in v else None
        head = '%s#%d' % (cname or ('class %s' % v['class'] if 'class' in v else ''), v['id'])
        if v.get('static'):
            head = 'class %s' % (cname or v['class'])
        if v.get('deleted'):
            return '#%d (deleted)' % v['id']
        if 'fields' not in v:
            return head
        names = class_fields(info, v.get('class'))
        parts = []
        for i, f in enumerate(v['fields']):
            n = names[i] if i < len(names) else 'f%d' % i
            parts.append('%s%s: %s' % (' ' * (indent + 2), n, format_value(info, f, indent + 2)))
        more = '\n%s... (%d fields)' % (' ' * (indent + 2), v['size']) if v['size'] > len(v['fields']) else ''
        return '%s {\n%s%s\n%s}' % (head, '\n'.join(parts), more, ' ' * indent)
    if t == 'array':
        if v.get('null'):
            return 'null array'
        if 'items' not in v:
            return 'array(%d)' % v['size']
        items = [format_value(info, x, indent) for x in v['items']]
        more = ', ... (%d)' % v['size'] if v['size'] > len(items) else ''
        return '[%s%s]' % (', '.join(items), more)
    if t == 'table':
        if v.get('null'):
            return 'null table'
        if 'items' not in v:
            return 'table(%d)' % v['size']
        items = ['%s: %s' % (format_value(info, k), format_value(info, x, indent)) for k, x in v['items']]
        more = ', ... (%d)' % v['size'] if v['size'] > len(items) else ''
        return '{%s%s}' % (', '.join(items), more)
    return '<type %s>' % v.get('type')


# ---- paths ----------------------------------------------------------------------------------------------------------
# PATH: a base, then accessors. Bases: NAME (a variable of the frame, else a field of `this`, else a global),
# local.NAME / param.NAME, this, this.NAME, global.NAME, #ID, class.CLASS, frame-relative cell @CELL. Accessors:
# .FIELD, [N], ["KEY"], and a cast :CLASS (the class whose field names follow: needed in --if / --log paths where the
# object's class is not known before the run).
TOKEN = re.compile(r'\s*(?:(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<num>-?\d+|0x[0-9a-fA-F]+)|(?P<str>"(?:[^"\\]|\\.)*")|'
                   r'(?P<op>[.#\[\]:@]))')


def tokenize(text):
    pos, out = 0, []
    while pos < len(text):
        if text[pos:].strip() == '':
            break
        m = TOKEN.match(text, pos)
        if not m:
            raise DbgError('path %r: cannot read at %r' % (text, text[pos:]))
        pos = m.end()
        for k in ('name', 'num', 'str', 'op'):
            if m.group(k) is not None:
                v = m.group(k)
                if k == 'num':
                    v = int(v, 0)
                elif k == 'str':
                    v = json.loads(v)
                out.append((k, v))
    return out


class Frame:
    """a frame of the stop (or a task): its method in the sidecar, pc, resume, receiver"""

    def __init__(self, info, raw, index):
        self.info, self.raw, self.index = info, raw, index
        self.pc, self.resume = raw['pc'], raw['resume']
        self.method = info.method_at(raw['pc']) if info is not None else None

    def vars(self):
        if self.method is None:
            return []
        return self.info.frame_vars(self.method, self.pc, self.resume)

    def where(self):
        return location_text(self.info, self.pc, self.resume)


class PathCompiler:
    """PATH text -> the raw steps the game reads; dynamic: a field of an object whose class the sidecar cannot know
    beforehand is named after reading the object's class from the game (print); static (--if, --log): a cast is
    needed then"""

    def __init__(self, sess, info, frame=None, dynamic=True, task=None):
        self.sess, self.info, self.frame, self.dynamic, self.task = sess, info, frame, dynamic, task

    def class_id(self, name):
        for cid, k in self.info.classes.items():
            if k['name'] == name:
                return cid
        raise DbgError('no class %s in %s' % (name, self.info.name))

    def field_slot(self, cid, name):
        fields = class_fields(self.info, cid)
        if name in fields:
            return len(fields) - 1 - fields[::-1].index(name)  # (an own field shadows an inherited one)
        raise DbgError('class %s has no field %s' % (self.info.class_name(cid) or cid, name))

    def runtime_class(self, steps):
        if not self.dynamic:
            return None
        args = {'paths': [steps], 'depth': 0}
        if self.task:
            args['task'] = self.task
        v = self.sess.conn.call('read', **args)['values'][0]
        if v.get('t') == 'obj' and 'class' in v:
            return v['class']
        if 'error' in v:
            raise DbgError(v['error'])
        raise DbgError('not an object: %s' % format_value(self.info, v))

    def frame_var(self, name, kinds=None):
        if self.frame is None:
            return None
        for n, k, cell in self.frame.vars():
            if n == name and (kinds is None or k in kinds):
                return ['frame', self.frame.index, cell]
        return None

    def compile(self, text):
        toks = tokenize(text)
        if not toks:
            raise DbgError('an empty path')
        i = 0
        steps, cls = [], None

        def take():
            nonlocal i
            if i >= len(toks):
                raise DbgError('path %r ends early' % text)
            t = toks[i]
            i += 1
            return t

        def peek():
            return toks[i] if i < len(toks) else (None, None)

        k, v = take()
        if k == 'op' and v == '#':
            k2, n = take()
            if k2 != 'num':
                raise DbgError('#ID: a number')
            steps.append(['object', n])
        elif k == 'op' and v == '@':
            k2, n = take()
            if k2 != 'num' or self.frame is None:
                raise DbgError('@CELL: a number, at a stop')
            steps.append(['frame', self.frame.index, n])
        elif k == 'name' and v == 'this':
            if self.frame is None:
                raise DbgError('this: at a stop (or --task)')
            steps.append(['this', self.frame.index])
            m = self.frame.method
            cls = m.cls if m is not None and m.cls else None
        elif k == 'name' and v in ('local', 'param') and peek() == ('op', '.'):
            take()
            _, name = take()
            s = self.frame_var(name)
            if s is None:
                raise DbgError('no live variable %s in %s' % (name, self.frame.where() if self.frame else 'no frame'))
            steps.append(s)
        elif k == 'name' and v == 'global' and peek() == ('op', '.'):
            take()
            _, name = take()
            if name not in self.info.globals:
                raise DbgError('no global %s' % name)
            steps.append(['global', self.info.globals.index(name)])
        elif k == 'name' and v == 'class' and peek() == ('op', '.'):
            take()
            _, name = take()
            cls = self.class_id(name)
            steps.append(['class', cls])
        elif k == 'name':
            s = self.frame_var(v)
            if s is not None:
                steps.append(s)
            elif self.frame is not None and self.frame.method is not None and self.frame.method.cls and \
                    v in class_fields(self.info, self.frame.method.cls):
                steps += [['this', self.frame.index], ['field', self.field_slot(self.frame.method.cls, v)]]
            elif v in self.info.globals:
                steps.append(['global', self.info.globals.index(v)])
            else:
                raise DbgError('%s: no variable, field of this or global of that name here' % v)
        else:
            raise DbgError('path %r: a name, this, #ID or class.NAME first' % text)
        while i < len(toks):
            k, v = take()
            if k == 'op' and v == ':':
                _, name = take()
                cls = self.class_id(name)
            elif k == 'op' and v == '.':
                _, name = take()
                if cls is None:
                    cls = self.runtime_class(steps)
                    if cls is None:
                        raise DbgError('%s: the class of %s is not known before the run: cast it (%s:Class.%s)'
                                       % (text, text[:text.rfind('.')], text[:text.rfind('.')], name))
                steps.append(['field', self.field_slot(cls, name)])
                cls = None
            elif k == 'op' and v == '[':
                k2, key = take()
                if take() != ('op', ']'):
                    raise DbgError('%s: ] expected' % text)
                steps.append(['index', key] if k2 == 'num' else ['key', key])
                cls = None
            else:
                raise DbgError('path %r: unexpected %r' % (text, v))
        return steps


# ---- the commands ---------------------------------------------------------------------------------------------------
class Cli:
    def __init__(self, args):
        self.args = args
        self.json = args.json
        self.conn = None
        self.sess = None

    def session(self):
        if self.sess is None:
            self.conn = Conn(self.args.pid)
            self.sess = Session(self.conn, self.args.dbg, sys.stdout)
        return self.sess

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.conn = self.sess = None

    def emit(self, obj, text):
        if self.json:
            print(json.dumps(obj, ensure_ascii=False))
        else:
            print(text)

    # -- the stop --
    def stop_state(self):
        r = self.session().conn.call('events', max=0)
        return r.get('stop') if r.get('state') == 'stopped' else None

    def frames(self, task=None):
        s = self.session()
        info = s.main_info()
        args = {'task': task} if task else {}
        raw = s.conn.call('stack', **args)['frames']
        return info, [Frame(info, f, i) for i, f in enumerate(raw)]

    def frame_of(self, task, index):
        info, frames = self.frames(task)
        if not frames:
            raise DbgError('no frames')
        if not 0 <= index < len(frames):
            raise DbgError('frame %d: the stack has %d' % (index, len(frames)))
        return info, frames[index]

    def describe_event(self, info, e):
        kind = e.get('event')
        if kind in ('stop', 'log') or (kind is None and 'pc' in e):
            where = location_text(info, e['pc'], e.get('resume', False)) if 'pc' in e else '?'
            e = dict(e)
            e['location'] = where
            if kind == 'log':
                vals = ['%s = %s' % (l, format_value(info, v).replace('\n', ' '))
                        for l, v in zip(e.get('labels', []), e.get('values', []))]
                return e, 'log bp %d task %d at %s%s%s' % (e['bp'], e['task'], where,
                                                           ': ' if vals else '', ', '.join(vals))
            text = 'stopped (%s%s) task %d at %s' % (e.get('reason', 'stop'),
                                                      ' bp %d' % e['bp'] if 'bp' in e else '', e['task'], where)
            if e.get('msci'):
                text += ' msci=%s:%d' % (e['msci']['script'], e['msci']['index'])
            return e, text
        if kind == 'resumed':
            return e, 'resumed (%s)' % e.get('reason')
        if kind == 'stepEnded':
            return e, 'step ended (%s)' % e.get('reason')
        if kind == 'dropped':
            return e, 'breakpoint %d dropped: %s' % (e['bp'], e.get('reason'))
        return e, json.dumps(e)

    def c_games(self, a):
        games = list_games()
        rows = []
        for pid in games:
            try:
                c = Conn(pid, timeout=2.0)
                h = c.call('hello', deadline=5.0)
                c.close()
                rows.append(h)
            except DbgError as x:
                rows.append({'pid': pid, 'error': str(x)})
        if self.json:
            for r in rows:
                print(json.dumps(r))
        elif not rows:
            print('no ScriptDebug game runs')
        for r in rows if not self.json else []:
            if 'error' in r:
                print('%d: %s' % (r['pid'], r['error']))
            else:
                objs = ', '.join('%s build %d' % (o['name'], o['build']) for o in r.get('objects', []))
                print('%d: %s %s, %s, %d breakpoints' % (r['pid'], r['edition'], r['state'], objs,
                                                         r['breakpoints']))
        return 0

    def c_hello(self, a):
        h = self.session().hello
        self.emit(h, json.dumps(h, indent=1))
        return 0

    def bp_request(self, a, info, pc, method, tag):
        obj = self.session().object_named(info.name)
        req = {'object': obj['name'], 'size': obj['size'], 'crc32': obj['crc32'], 'pc': pc, 'tag': tag}
        frame = None
        if method is not None:
            frame = Frame(info, {'pc': pc, 'resume': False}, 0)
        comp = PathCompiler(self.session(), info, frame, dynamic=False)
        conds = []
        for p, op, value in a.cond or []:
            conds.append({'path': comp.compile(p), 'op': op, 'value': parse_literal(value)})
        if conds:
            req['if'] = conds
        if a.log:
            labels = [x.strip() for x in ','.join(a.log).split(',') if x.strip()]
            req['log'] = True
            req['paths'] = [comp.compile(x) for x in labels]
            req['labels'] = labels
            req['depth'] = a.depth
        if a.count:
            req['count'] = a.count
        if a.once:
            req['once'] = True
        if a.this:
            req['this'] = a.this
        return req

    def resolve_location(self, info, text):
        """LOCATION -> [(pc, method)]: File.kc:LINE, Class::Method / Function (its first statement), 0xPC"""
        if re.match(r'^0x[0-9a-fA-F]+$', text):
            pc = int(text, 16)
            return [(pc, info.method_at(pc))]
        m = re.match(r'^(.+\.kc):(\d+)$', text, re.I)
        if m:
            fname, line = m.group(1), int(m.group(2))
            # the sidecar's file: the one named so, else the one whose path ends with the given path (a folder
            # prefix tells two equal names apart); exactly one
            want = fname.replace('\\', '/').lower()
            names = sorted({f for meth in info.methods for _, _, _, f in meth.lines})
            exact = [f for f in names if f.replace('\\', '/').lower() == want]
            match = exact or [f for f in names if f.replace('\\', '/').lower().endswith('/' + want) or
                              os.path.basename(f.replace('\\', '/')).lower() == want]
            if not match:
                raise DbgError('%s: no such source file in %s' % (fname, info.name))
            if len(match) > 1:
                raise DbgError('%s: several source files match (%s): give its folder too' % (fname, ', '.join(match)))
            target = match[0]
            best = None
            for meth in info.methods:
                for pc, ln, col, f in meth.lines:
                    if f != target:
                        continue
                    if ln >= line and (best is None or (ln, pc) < (best[0], best[1])):
                        best = (ln, pc, meth)
            if best is None:
                raise DbgError('%s: no statement at or after line %d' % (fname, line))
            ln, _, meth = best
            if ln - line > 20:
                raise DbgError('%s:%d: no statement there (the next is at line %d)' % (fname, line, ln))
            pcs = [(pc, meth2) for meth2 in info.methods for pc, l2, c2, f in meth2.lines
                   if l2 == ln and f == target]
            pcs.sort(key=lambda x: x[0])
            # the line's first statement in each method (a line can hold a loop's init, test and step: the first)
            seen, out = set(), []
            for pc, meth2 in pcs:
                if meth2.entry not in seen:
                    seen.add(meth2.entry)
                    out.append((pc, meth2))
            if ln != line:
                print('note: line %d has no statement: the breakpoint is at line %d' % (line, ln), file=sys.stderr)
            return out
        cls, _, name = text.rpartition('::')
        found = [x for x in info.methods if x.name == name and (cls == '' and not x.cls or x.cls_name == cls)]
        if not found:
            raise DbgError('%s: no such method in %s' % (text, info.name))
        return [(x.lines[0][0] if x.lines else x.entry, x) for x in found]

    def c_break(self, a):
        s = self.session()
        info = s.info_named(a.object) if a.object else s.main_info()
        outs = []
        for pc, meth in self.resolve_location(info, a.location):
            tag = location_text(info, pc, False)
            r = s.conn.call('break', **self.bp_request(a, info, pc, meth, tag))
            outs.append(r)
            extra = ' (unbound: %s)' % ', '.join(r['unbound']) if r.get('unbound') else ''
            self.emit(r, 'breakpoint %d at %s%s%s' % (r['bp'], tag, ' (log)' if r.get('log') else '', extra))
        return 0

    def msci_layout(self, info):
        s = self.session()
        head = s.hello.get('msciHead', 0)
        if not head:
            raise DbgError('the game does not know the MSCI line loop of this build (its __runScript is not a known '
                           'build): no MSCI breakpoints')
        meth = info.method_at(head)
        if meth is None or meth.name != '__runScript' or head not in meth._pcs:
            raise DbgError('the MSCI loop head 0x%x is not a statement of __runScript in the sidecar' % head)
        cells = {n: c for n, k, c in info.frame_vars(meth, head, resume=False)}
        for n in ('script', 'pc', 'varValues', 'varTypes', 'scriptInstance'):
            if n not in cells:
                raise DbgError('__runScript has no variable %s at its loop head (sidecar)' % n)
        return head, meth, cells

    def script_lines(self, info, script_path):
        """the editor lines of a script's code indices, as ScriptDatabase.GetEditorLineFromCodeIndex counts them"""
        s = self.session()
        code = s.conn.call('read', paths=[script_path + [['index', 6]]], depth=2, items=100000)['values'][0]
        if code.get('t') != 'array':
            raise DbgError('the script has no code array')
        comments = s.conn.call('read', paths=[script_path + [['index', 8]]], depth=2, items=100000)['values'][0]
        cmds = [x['items'][0]['v'] if x.get('t') == 'array' and x.get('items') else None for x in code['items']]
        db = PathCompiler(s, info).class_id('ScriptDatabase')
        slot = PathCompiler(s, info).field_slot(db, 'cmdDefs')
        ids = sorted(set(c for c in cmds if c is not None))
        flags = {}
        if ids:
            vals = s.conn.call('read', paths=[[['class', db], ['field', slot], ['index', c], ['index', 1]]
                                              for c in ids], depth=0)['values']
            flags = {c: (v.get('v', 0) if v.get('t') == 'int' else 0) for c, v in zip(ids, vals)}
        cpos = []
        if comments.get('t') == 'array':
            for x in comments.get('items', []):
                if x.get('t') == 'array' and x.get('items') and x['items'][0].get('t') == 'int':
                    cpos.append(x['items'][0]['v'])
        lines = []
        for i in range(len(cmds)):
            line = i + 1 - sum(1 for j in range(i) if cmds[j] is not None and flags.get(cmds[j], 0) & 131072)
            line += sum(1 for c in cpos if c <= i)
            lines.append(line)
        return lines

    def loaded_script_path(self, info, name):
        s = self.session()
        pc = PathCompiler(s, info)
        db = pc.class_id('ScriptDatabase')
        return [['class', db], ['field', pc.field_slot(db, 'scripts')], ['key', name]]

    def c_msci(self, a):
        s = self.session()
        info = s.main_info()
        if a.what == 'break':
            if len(a.rest) != 2:
                raise DbgError('msci break SCRIPT LINE')
            script, line = a.rest[0], int(a.rest[1])
            head, meth, cells = self.msci_layout(info)
            path = self.loaded_script_path(info, script)
            lines = self.script_lines(info, path)
            idx = [i for i, l in enumerate(lines) if l == line]
            if not idx:
                raise DbgError('%s: no code at editor line %d (lines %d..%d)' % (script, line, min(lines or [0]),
                                                                                max(lines or [0])))
            obj = s.main_object()
            req = self.bp_request(a, info, head, meth, 'msci %s:%d' % (script, line))
            conds = [{'path': [['frame', 0, cells['script']], ['index', 0]], 'op': '==', 'value': script},
                     {'path': [['frame', 0, cells['pc']]], 'op': '==', 'value': idx[0]}]
            req['if'] = conds + req.get('if', [])
            req['object'], req['size'], req['crc32'] = obj['name'], obj['size'], obj['crc32']
            r = s.conn.call('break', **req)
            extra = ' (unbound: %s)' % ', '.join(r['unbound']) if r.get('unbound') else ''
            self.emit(r, 'breakpoint %d at msci %s:%d (code index %d)%s' % (r['bp'], script, line, idx[0], extra))
            return 0
        # vars / stack: the __runScript frames of the stop
        _, frames = self.frames(a.task)
        head, meth, cells = self.msci_layout(info)
        msci = [f for f in frames if f.method is not None and f.method.entry == meth.entry]
        if not msci:
            raise DbgError('no MSCI script on this stack (no 702::__runScript frame)')
        out = []
        for f in (msci if a.what == 'stack' else msci[a.frame:a.frame + 1]):
            fv = {n: c for n, k, c in f.vars()}
            if 'script' not in fv or 'pc' not in fv:
                continue
            paths = [[['frame', f.index, fv['script']], ['index', 0]], [['frame', f.index, fv['pc']]]]
            vals = s.conn.call('read', paths=paths, depth=0, **({'task': a.task} if a.task else {}))['values']
            name = vals[0].get('v', '?')
            pcv = vals[1].get('v', 0)
            index = pcv - 1 if f.index != 0 or f.pc != head else pcv  # (in the loop: pc is past the running line)
            try:
                lines = self.script_lines(info, [['frame', f.index, fv['script']]])
                line = lines[index] if 0 <= index < len(lines) else None
            except DbgError:
                line = None
            rec = {'frame': f.index, 'script': name, 'index': index, 'line': line}
            if a.what == 'vars':
                rp = [[['frame', f.index, fv['script']], ['index', 5]], [['frame', f.index, fv['varValues']]],
                      [['frame', f.index, fv['varTypes']]]]
                names, values, types = s.conn.call('read', paths=rp, depth=2, items=1000,
                                                   **({'task': a.task} if a.task else {}))['values']
                vs = []
                for i, v in enumerate(values.get('items', [])):
                    n = names['items'][i]['v'] if names.get('items') and i < len(names['items']) else 'var%d' % i
                    t = types['items'][i].get('v') if types.get('items') and i < len(types['items']) else None
                    vs.append({'name': n, 'type': t, 'value': v})
                rec['vars'] = vs
            out.append(rec)
        for rec in out:
            text = 'frame %d: %s line %s (code index %d)' % (rec['frame'], rec['script'], rec['line'], rec['index'])
            if 'vars' in rec:
                text += ''.join('\n  $%s = %s (type %s)' % (v['name'], format_value(info, v['value']), v['type'])
                                for v in rec['vars'])
            self.emit(rec, text)
        return 0

    def c_clear(self, a):
        s = self.session()
        r = s.conn.call('clear', all=True) if a.all or a.id is None else s.conn.call('clear', bp=a.id)
        self.emit(r, 'cleared %d' % r['cleared'])
        return 0

    def c_list(self, a):
        s = self.session()
        r = s.conn.call('list')
        if self.json:
            print(json.dumps(r))
            return 0
        for b in r['breakpoints']:
            flags = [x for x in ('log', 'once') if b.get(x)]
            if b.get('count'):
                flags.append('count %d' % b['count'])
            if b.get('this'):
                flags.append('this #%d' % b['this'])
            if b.get('conditions'):
                flags.append('%d conditions' % b['conditions'])
            if not b['applied']:
                flags.append('not applied')
            if b.get('unbound'):
                flags.append('unbound: ' + ', '.join(b['unbound']))
            print('%d: %s [0x%x] hits %d%s' % (b['bp'], b.get('tag', ''), b['pc'], b['hits'],
                                                (' (' + ', '.join(flags) + ')') if flags else ''))
        print('%d patched pcs, %d unbound native bindings' % (r['patched'], r['unboundBindings']))
        return 0

    def print_events(self, events):
        info = self.session().main_info()
        for e in events:
            obj, text = self.describe_event(info, e)
            self.emit(obj, text)

    def c_events(self, a):
        r = self.session().conn.call('events', max=1000)
        self.print_events(r['events'])
        if r.get('dropped'):
            print('(%d events dropped: the queue was full)' % r['dropped'], file=sys.stderr)
        return 0

    def c_wait(self, a):
        # one connection per poll: the game serves one client at a time, so another x3kc dbg command (a clear, a
        # pause) gets in between the polls of a waiting one
        s = self.session()
        info, pid = s.main_info(), s.conn.pid
        s.conn.close()
        end = time.time() + a.timeout
        while True:
            # the pipe keeps listening between frames, so the open succeeds even in a loading screen; the reply comes
            # with the game's next frame: it is awaited only until the deadline (a hung game: the timeout too)
            try:
                c = Conn(pid, timeout=max(0.1, end - time.time()))
            except DbgError:
                if pid not in list_games():
                    raise DbgError('the game (pid %d) is gone' % pid)
                if time.time() > end:
                    self.emit({'timeout': True}, 'timeout: no stop or log in %g s' % a.timeout)
                    return 2
                continue
            try:
                r = c.call('events', deadline=max(0.1, end - time.time()), max=1 if not a.all else 1000)
            except DbgTimeout:
                self.emit({'timeout': True}, 'timeout: no stop or log in %g s (the game did not answer)' % a.timeout)
                return 2
            finally:
                c.close()
            events = r['events'] or ([dict(r['stop'], event='stop')] if r['state'] == 'stopped' else [])
            if events:
                for e in events:
                    obj, text = self.describe_event(info, e)
                    self.emit(obj, text)
                return 0
            if time.time() > end:
                self.emit({'timeout': True}, 'timeout: no stop or log in %g s' % a.timeout)
                return 2
            time.sleep(0.05)

    def c_stack(self, a):
        info, frames = self.frames(a.task)
        for f in frames:
            r = dict(f.raw, location=f.where())
            extra = ''
            if f.raw.get('this') is not None:
                extra += ' this=#%d' % f.raw['this']
            if f.raw.get('nativeBody'):
                extra += ' (native body)'
            if f.raw.get('msci'):
                extra += ' msci=%s:%d' % (f.raw['msci']['script'], f.raw['msci']['index'])
            self.emit(r, '#%d %s%s' % (f.index, f.where(), extra))
        return 0

    def c_locals(self, a):
        s = self.session()
        info, f = self.frame_of(a.task, a.frame)
        vs = f.vars()
        paths = [['frame', f.index, c] for n, k, c in vs]
        rows = []
        if f.raw.get('this') is not None:
            vs = vs + [('this', 'this', None)]
            paths.append(['this', f.index])
        if paths:
            args = {'paths': [[p] for p in paths], 'depth': a.depth}
            if a.task:
                args['task'] = a.task
            vals = s.conn.call('read', **args)['values']
            for (n, k, c), v in zip(vs, vals):
                rows.append({'name': n, 'kind': k, 'cell': c, 'value': v})
        self.emit({'frame': f.index, 'location': f.where(), 'vars': rows}, '#%d %s' % (f.index, f.where()))
        if not self.json:
            for r in rows:
                print('  %s %s = %s' % (r['kind'], r['name'], format_value(info, r['value'], 2)))
        return 0

    def c_print(self, a):
        s = self.session()
        info = s.main_info()
        frame = None
        stopped = self.stop_state() is not None
        if stopped or a.task:
            _, frame = self.frame_of(a.task, a.frame)
        comp = PathCompiler(s, info, frame, dynamic=True, task=a.task)
        for text in a.paths:
            try:
                steps = comp.compile(text)
                args = {'paths': [steps], 'depth': a.depth, 'items': a.items}
                if a.task:
                    args['task'] = a.task
                v = s.conn.call('read', **args)['values'][0]
            except DbgError as x:
                v = {'error': str(x)}
            self.emit({'path': text, 'value': v}, '%s = %s' % (text, format_value(info, v)))
        return 0

    def c_tasks(self, a):
        s = self.session()
        info = s.main_info()
        r = s.conn.call('tasks', max=a.max)
        if self.json:
            for t in r['tasks']:
                if 'pc' in t:
                    t['location'] = location_text(info, t['pc'], t.get('resume', True))
                print(json.dumps(t))
            return 0
        for t in r['tasks']:
            where = location_text(info, t['pc'], t.get('resume', True)) if 'pc' in t else '?'
            obj = ' #%d' % t['object'] if 'object' in t else ''
            ms = ' msci=%s:%d' % (t['msci']['script'], t['msci']['index']) if t.get('msci') else ''
            print('task %d %s%s at %s (%d frames)%s' % (t['task'], t['state'], obj, where, t['frames'], ms))
        print('%d tasks' % r['count'])
        return 0

    def c_continue(self, a):
        r = self.session().conn.call('continue')
        self.emit(r, 'running')
        return 0

    def c_step(self, a):
        s = self.session()
        info, frames = self.frames()
        if not frames:
            raise DbgError('not stopped')
        f0 = frames[0]
        caller = frames[1] if len(frames) > 1 else None
        targets = []
        mode = a.mode
        if mode == 'msci':
            head, meth, cells = self.msci_layout(info)
            if not any(f.method is not None and f.method.entry == meth.entry for f in frames):
                raise DbgError('not in an MSCI script (no __runScript frame)')
            targets.append({'pc': head, 'depth': 'le'})
        else:
            if f0.method is None:
                raise DbgError('the stop is in no method of the sidecar')
            if mode in ('over', 'into'):
                here = info.location(f0.pc, resume=False)
                for pc, line, col, fname in f0.method.lines:
                    if (fname, line) != (here.file, here.line):
                        targets.append({'pc': pc, 'depth': 'le'})
            if caller is not None and caller.pc:
                targets.append({'pc': caller.pc, 'depth': 'lt'})
        r = s.conn.call('step', targets=targets, into=(mode == 'into'))
        self.emit(r, 'stepping (%s): %d targets' % (mode, r['targets']))
        return 0

    def c_pause(self, a):
        r = self.session().conn.call('pause')
        self.emit(r, 'pausing at the next method entry')
        return 0

    def c_detach(self, a):
        r = self.session().conn.call('detach')
        self.emit(r, 'detached: %d breakpoints cleared, the game runs' % r['cleared'])
        return 0

    def c_config(self, a):
        args = {}
        if a.stop_timeout:
            args['stopTimeout'] = a.stop_timeout
        r = self.session().conn.call('config', **args)
        self.emit(r, 'stop timeout %d s' % r['stopTimeout'])
        return 0

    def c_run(self, a):
        rc = 0
        with open(a.file, encoding='utf-8') as h:
            for n, line in enumerate(h, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                words = shlex.split(line, posix=True)
                if not self.json:
                    print('> ' + line)
                try:
                    rc = dispatch(self, build_parser().parse_args(words, namespace=argparse.Namespace(
                        pid=self.args.pid, json=self.json, dbg=self.args.dbg)))
                except DbgError as x:
                    print('error: %s:%d: %s' % (a.file, n, x), file=sys.stderr)
                    return 1
                except SystemExit as x:
                    print('error: %s:%d: bad command' % (a.file, n), file=sys.stderr)
                    return 1
                if rc == 1:
                    return 1
        return rc


def parse_literal(text):
    if text in ('null', 'none'):
        return None
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return json.loads(text)
    return text


def add_bp_options(p):
    p.add_argument('--if', dest='cond', nargs=3, action='append', metavar=('PATH', 'OP', 'VALUE'),
                   help='stop / log only when PATH OP VALUE (== != < <= > >=; a number, "text" or null); repeatable: '
                        'all must hold')
    p.add_argument('--log', action='append', metavar='PATHS', help='a tracepoint: log these paths (comma-separated) '
                   'into the event stream and go on (no stop)')
    p.add_argument('--depth', type=int, default=1, help='--log: the depth of the logged values (default 1)')
    p.add_argument('--count', type=int, default=0, help='only from the Nth hit on (the hits that pass --if/--this)')
    p.add_argument('--once', action='store_true', help='removed after its first stop / log')
    p.add_argument('--this', type=int, default=0, metavar='ID', help='only when the receiver is object #ID')


def build_parser():
    ap = argparse.ArgumentParser(prog='x3kc dbg', description='the live script debugger of an x3_src ScriptDebug game')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('games', help='the running ScriptDebug games')
    sub.add_parser('hello', help='the game: state, loaded objects')
    p = sub.add_parser('break', help='a breakpoint: File.kc:LINE, Class::Method, Function, 0xPC')
    p.add_argument('location')
    p.add_argument('--object', help='the object (default: the main one, x3story)')
    add_bp_options(p)
    p = sub.add_parser('msci', help='MSCI scripts: break SCRIPT LINE (editor line), vars, stack')
    p.add_argument('what', choices=['break', 'vars', 'stack'])
    p.add_argument('rest', nargs='*')
    p.add_argument('--frame', type=int, default=0, help='vars: the Nth script on the stack (0: the innermost)')
    p.add_argument('--task', type=int, default=0)
    add_bp_options(p)
    p = sub.add_parser('clear', help='remove a breakpoint (no id or --all: every one)')
    p.add_argument('id', type=int, nargs='?')
    p.add_argument('--all', action='store_true')
    sub.add_parser('list', help='the breakpoints')
    sub.add_parser('events', help='the queued events (no wait)')
    p = sub.add_parser('wait', help='the next stop or log (at once when stopped)')
    p.add_argument('--timeout', type=float, default=60.0)
    p.add_argument('--all', action='store_true', help='every queued event, not only the next')
    for name in ('stack', 'locals', 'print'):
        p = sub.add_parser(name)
        p.add_argument('--task', type=int, default=0, help='a task (default: the stopped one)')
        if name != 'stack':
            p.add_argument('--frame', type=int, default=0)
            p.add_argument('--depth', type=int, default=1)
        if name == 'print':
            p.add_argument('paths', nargs='+')
            p.add_argument('--items', type=int, default=50)
    p = sub.add_parser('tasks', help='the tasks of the main VM')
    p.add_argument('--max', type=int, default=200)
    sub.add_parser('continue')
    p = sub.add_parser('step')
    p.add_argument('mode', nargs='?', default='over', choices=['over', 'into', 'out', 'msci'])
    sub.add_parser('pause', help='stop at the next method a task enters')
    sub.add_parser('detach', help='clear every breakpoint and go on')
    p = sub.add_parser('config')
    p.add_argument('--stop-timeout', type=int, default=0, help='seconds without a request at a stop before the '
                   'game clears the breakpoints and goes on (default 120)')
    p = sub.add_parser('run', help='a batch of commands, one per line')
    p.add_argument('file')
    return ap


def dispatch(cli, a):
    return getattr(cli, 'c_' + a.cmd)(a)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    top = argparse.ArgumentParser(prog='x3kc dbg', add_help=False)
    top.add_argument('--pid', type=int)
    top.add_argument('--json', action='store_true')
    top.add_argument('--dbg', action='append', default=[])
    top.add_argument('--deadline', type=float, default=float(os.environ.get('X3KC_DBG_DEADLINE', DEFAULT_DEADLINE)))
    known, rest = top.parse_known_args(argv)
    if not rest or rest[0] in ('-h', '--help'):
        build_parser().print_help()
        print('\nglobal options: --pid N (the game; default: the only one running), --json (one JSON object per '
              'line), --dbg DIR (more places for .kcdbg sidecars), --deadline S (how long a reply is awaited, '
              'default %g s or X3KC_DBG_DEADLINE: a loading screen or a hung game gives "no reply", exit 1; wait '
              'uses its own --timeout)' % DEFAULT_DEADLINE)
        return 0
    Conn.reply_timeout = max(0.1, known.deadline)
    a = build_parser().parse_args(rest)
    a.pid, a.json, a.dbg = known.pid, known.json, known.dbg
    cli = Cli(a)
    try:
        return dispatch(cli, a)
    except DbgError as x:
        print('error: %s' % x, file=sys.stderr)
        return 1
    finally:
        cli.close()


if __name__ == '__main__':
    sys.exit(main())
