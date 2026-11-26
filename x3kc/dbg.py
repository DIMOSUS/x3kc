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
        if self.hello.get('stage', 0) >= 2:
            # the session's natives a debugger code run may call: the edition's at-once list, and for read-only runs
            # the side-effect-free ones (native_effects.txt: pure, and writes:N - the game's container primitives refuse
            # their write into a container the run did not create); sent once (the game keeps them until detach)
            from . import targets
            args = {}
            if not self.hello.get('syncNatives'):
                args['syncNatives'] = sorted(targets.sync('x3_src-' + self.hello.get('edition', 'AP').lower()))
            if self.hello.get('pureNatives') == 0:
                eff = targets.effects()
                args['pureNatives'] = sorted(n for n, v in eff.items() if v == 'pure' or isinstance(v, tuple))
            if args:
                conn.call('config', **args)

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
        if obj.get('sidecar') and os.path.isfile(obj['sidecar']):
            # a reload's sidecar (the reloaded layout, written by x3kc dbg reload): the one of the code as it runs now
            info = kcdbg.DebugInfo.load(obj['sidecar'])
            self.infos[key] = info
            return info
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


def undecorate(name):
    """a decorated MSVC name from the .map (?f@ns@@...) -> scope::name (dbghelp's UnDecorateSymbolName); else as is,
    marked (mangled) when it cannot be read"""
    if not name.startswith('?'):
        return name
    try:
        import ctypes
        buf = ctypes.create_string_buffer(1024)
        n = ctypes.windll.dbghelp.UnDecorateSymbolName(name.encode('latin-1'), buf, 1024, 0x1000)  # UNDNAME_NAME_ONLY
        if n:
            return buf.value.decode('latin-1')
    except (OSError, AttributeError):
        pass
    return name + ' (mangled)'


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


def change_text(info, c):
    """a container watch's change (the event's `change`, made before the write): the container, its operation, the
    writer, the element / key's old -> new value, the size"""
    def fv(v):
        return format_value(info, v).replace('\n', ' ')
    w = c.get('writer') or {}
    who = ('native %s' % w['native'] if 'native' in w else 'the MSCI case' if w.get('case') else
           'engine %s' % w['engine'] if 'engine' in w else 'bytecode')
    head = '%s #%s %s by %s' % (c.get('kind', 'container'), c.get('container', '?'), c.get('op', 'set'), who)
    parts = []
    key = c.get('key')
    ktext = fv(key) if isinstance(key, dict) else json.dumps(key) if key is not None else None
    if 'old' in c or 'new' in c:
        parts.append('%s%s -> %s' % ('[%s] ' % ktext if ktext else '', fv(c['old']) if 'old' in c else '-',
                                     fv(c['new']) if 'new' in c else '-'))
    elif ktext:
        parts.append('[%s]' % ktext)
    if 'newSize' in c:
        parts.append('size %d -> %d' % (c.get('size', 0), c['newSize']))
    elif 'size' in c:
        parts.append('size %d' % c['size'])
    return head + (': ' + ', '.join(parts) if parts else '')


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
                values = e.get('values', [])
                if isinstance(e.get('value'), dict):        # (log expressions: one array of their values)
                    v = e['value']
                    values = v.get('items', []) if v.get('t') == 'array' else [v]
                vals = ['%s = %s' % (l, format_value(info, v).replace('\n', ' '))
                        for l, v in zip(e.get('labels', []), values)]
                if 'old' in e:
                    vals.insert(0, 'write %s -> %s' % (format_value(info, e['old']).replace('\n', ' '),
                                                       format_value(info, e['new']).replace('\n', ' ')))
                if 'change' in e:
                    vals.insert(0, change_text(info, e['change']))
                return e, 'log bp %d task %d at %s%s%s' % (e['bp'], e['task'], where,
                                                           ': ' if vals else '', ', '.join(vals))
            text = 'stopped (%s%s) task %d at %s' % (e.get('reason', 'stop'),
                                                      ' bp %d' % e['bp'] if 'bp' in e else '', e['task'], where)
            if e.get('msci'):
                text += ' msci=%s:%d' % (e['msci']['script'], e['msci']['index'])
            if 'old' in e:
                text += ': write %s -> %s' % (format_value(info, e['old']).replace('\n', ' '),
                                              format_value(info, e['new']).replace('\n', ' '))
            if 'change' in e:
                text += ': ' + change_text(info, e['change'])
            return e, text
        if kind == 'watch':
            return e, 'watch bp %d (no frame to stop in): %s' % (e.get('bp', 0), change_text(info, e.get('change', {})))
        if kind == 'error':
            return e, 'bp %d: its compiled condition / log failed: %s' % (e.get('bp', 0), e.get('error'))
        if kind == 'hang':
            # (the watchdog's first safepoint of a hang: the task's stack there, its innermost frame first)
            e = dict(e)
            lines = []
            for i, f in enumerate(e.get('stack', [])):
                where = location_text(info, f['pc'], f.get('resume', i != 0))
                ms = ' msci=%s:%d' % (f['msci']['script'], f['msci']['index']) if f.get('msci') else ''
                lines.append('  #%d %s%s' % (i, where, ms))
            if e.get('frames', 0) > len(e.get('stack', [])):
                lines.append('  ... %d more frames' % (e['frames'] - len(e['stack'])))
            # (a hang inside a native's script run: the natives in progress outside, each with its calling task)
            for o in e.get('outer', []):
                if o.get('gone'):
                    lines.append('  in native %s of task %d (gone)' % (o['native'], o['task']))
                    continue
                for i, f in enumerate(o.get('stack', [])):
                    where = location_text(info, f['pc'], f.get('resume', i != 0))
                    ms = ' msci=%s:%d' % (f['msci']['script'], f['msci']['index']) if f.get('msci') else ''
                    lines.append(('  in native %s of task %d at %s%s' % (o['native'], o['task'], where, ms)) if i == 0
                                 else '    %s%s' % (where, ms))
            e['location'] = location_text(info, e['pc'], False) if 'pc' in e else '?'
            # (stalledMs: the run's time since its last frame, stop or hold - the running task may be newer)
            outer = e.get('outer') or []
            text = 'hang: a script run has gone %.1f s without waiting, running task %d at %s%s%s' % (
                e.get('stalledMs', 0) / 1000.0, e['task'], e['location'],
                ' inside native %s of task %d' % (outer[0]['native'], outer[0]['task']) if outer else '',
                ' (stopping there)' if e.get('stopping') else ' (served: pause stops it)')
            return e, '\n'.join([text] + lines)
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
        self.compiled_parts(a, info, req, frame, comp)
        if a.count:
            req['count'] = a.count
        if a.once:
            req['once'] = True
        if a.this:
            req['this'] = a.this
        return req

    def sync_natives(self):
        """the natives a debugger code run may call: the at-once ones of the game's edition (x3kc/targets.py)"""
        from . import targets
        ed = self.session().hello.get('edition', 'AP').lower()
        return sorted(targets.sync('x3_src-' + ed))

    def snippet(self, info, text, frame=None, cls=None, block=False, read_only=False):
        """a KC expression / block compiled against the sidecar (kcsnip): frame - its variables are the snippet's
        outer names, its method's class `this`; cls - the class without a frame; read_only - its own native calls
        must be side-effect-free or write only into containers (native_effects.txt; the game checks the rest: the
        methods it calls, the containers' owners)"""
        from . import kcsnip
        outer = {}
        if frame is not None:
            outer = {n: c for n, k, c in frame.vars() if k in ('param', 'local')}
            if cls is None and frame.method is not None:
                cls = frame.method.cls
        try:
            snip = kcsnip.compile_snippet(info, text, block=block, cls=cls or 0, outer=outer)
        except kcsnip.SnipError as x:
            raise DbgError(str(x))
        from . import targets
        known = targets.known('x3_src-' + self.session().hello.get('edition', 'AP').lower())
        unknown = [n for n in snip['natives'] if n not in known]
        if unknown:
            # (a call of a name no method of the class chain, function or native has compiles as a native call)
            raise DbgError('<eval>: %s: no method of %s, function or native of the exe has that name'
                           % (', '.join(unknown), info.class_name(cls) if cls else 'the globals'))
        sync = set(self.sync_natives())
        waits = [n for n in snip['natives'] if n not in sync]
        if waits:
            raise DbgError('<eval>: %s may wait: debugger code calls only natives that answer at once' % ', '.join(waits))
        if read_only:
            eff = targets.effects()
            bad = [n for n in snip['natives'] if not (eff.get(n) == 'pure' or isinstance(eff.get(n), tuple))]
            if bad:
                raise DbgError('<eval>: read-only: %s %s side effects (x3kc/native_effects.txt); a writable run: '
                               'eval without --read-only, break / watch --writable'
                               % (', '.join(bad), 'has' if len(bad) == 1 else 'have'))
        # (a read-only run is not checked for what the methods it calls can reach: the game ends it at a fork or a
        # native that may wait as it ends it at a write, before anything is done - there is no work to leave half
        # done; MD conditions reach GS_Route* through Cue::CheckCondition's switch, md why runs them read-only)
        graph = self.call_graph(info) if not read_only else None
        why = graph.refusal(snip, sync, known) if graph is not None else None
        if why:
            raise DbgError('<eval>: refused before it runs: %s (the game would end the run inside that method, its work '
                           'half done)' % why)
        return snip

    def call_graph(self, info):
        """the loaded object's call graph (kcsnip.CallGraph), cached in %TEMP%/x3kc_dbg by the object's identity"""
        import pickle
        import tempfile
        from . import kcsnip
        obj = self.session().object_named(info.name)
        try:
            path = self.object_file(obj, info)
        except DbgError:
            print('note: no object file: the methods the snippet calls are not checked (the game still refuses a '
                  'waiting native or a fork when it gets there)', file=sys.stderr)
            return None
        d = os.path.join(tempfile.gettempdir(), 'x3kc_dbg')
        os.makedirs(d, exist_ok=True)
        cache = os.path.join(d, 'graph-%s-%s-%d.pickle' % (obj['name'], obj['crc32'], obj['size']))
        if os.path.isfile(cache):
            with open(cache, 'rb') as h:
                g = pickle.load(h)
        else:
            g = kcsnip.CallGraph(info, path)
            with open(cache + '.tmp', 'wb') as h:
                pickle.dump(g, h, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(cache + '.tmp', cache)
        # the methods the game runs reloaded: their new bodies (written by x3kc dbg reload beside its sidecar)
        from . import kcreload
        if obj.get('reloads') and obj.get('sidecar') and os.path.isfile(kcreload.bodies_path(obj['sidecar'])):
            with open(kcreload.bodies_path(obj['sidecar']), encoding='utf-8') as h:
                g.overlay(json.load(h))
        return g

    def compiled_parts(self, a, info, req, frame, comp, cls=None):
        """--if (one KC expression, or the old PATH OP VALUE) and --log (paths, or KC expressions) of a breakpoint /
        watchpoint into its request"""
        conds = []
        ro = not getattr(a, 'writable', False)  # (compiled conditions and log expressions run read-only by default)
        if not ro:
            req['writableParts'] = True
        for words in a.cond or []:
            if len(words) == 3 and words[1] in ('==', '!=', '<', '<=', '>', '>='):
                try:
                    conds.append({'path': comp.compile(words[0]), 'op': words[1], 'value': parse_literal(words[2])})
                    continue
                except DbgError:
                    pass
            if 'ifCode' in req:
                raise DbgError('one --if expression (join them with &&)')
            s = self.snippet(info, ' '.join(words), frame, cls, read_only=ro)
            req['ifCode'] ={k: s[k] for k in ('code', 'jumps', 'strings', 'low', 'high')}
        if conds:
            req['if'] = conds
        if a.log:
            items = split_top(','.join(a.log))
            req['log'] = True
            req['labels'] = items
            req['depth'] = a.depth
            try:
                req['paths'] = [comp.compile(x) for x in items]
            except DbgError:
                # not all paths: the items as one KC array, compiled and run at each hit
                s = self.snippet(info, '{%s}' % ', '.join(items), frame, cls, read_only=ro)
                req['logCode'] = {k: s[k] for k in ('code', 'jumps', 'strings', 'low', 'high')}

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
        return self.script_lines_cmds(info, script_path)[0]

    def script_lines_cmds(self, info, script_path):
        """(the editor lines, the command ids) of a script's code indices"""
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
        return lines, cmds

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
        if 'codeBlocks' in r:
            print('debugger code: %d blocks (%d bytes), %d bytes of added strings, %d reloaded methods, %d bindings '
                  'retired by reloads' % (r['codeBlocks'], r['codeBlockBytes'], r['poolTailBytes'], r['reloads'],
                                          r['retiredBindings']))
        if 'histories' in r:
            print('object histories: %d (%d records kept, %d bytes), write hook %s'
                  % (r['histories'], r['historyRecords'], r['historyBytes'], 'on' if r.get('writeHook') else 'off'))
        return 0

    def print_events(self, events):
        info = self.session().main_info()
        for e in events:
            obj, text = self.describe_event(info, e)
            self.emit(obj, text)

    @staticmethod
    def warn_dropped(r):
        """the game's dropped-event count of a reply (since the last read; by kind) as a warning on stderr"""
        if r.get('dropped'):
            kinds = r.get('droppedKinds') or {}
            print('warning: %d events dropped since the last read (the game\'s queue was full)%s' % (
                r['dropped'], ': ' + ', '.join('%s %d' % kv for kv in sorted(kinds.items(), key=lambda kv: -kv[1]))
                if kinds else ''), file=sys.stderr)

    def c_events(self, a):
        conn = self.session().conn
        while True:
            r = conn.call('events', max=1000 if not a.all else 1000000)
            self.print_events(r['events'])
            self.warn_dropped(r)
            if not r.get('more'):
                return 0
            if not a.all:
                print('(more events are queued: x3kc dbg events --all reads them all)', file=sys.stderr)
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
            self.warn_dropped(r)
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
        r = self.session().conn.call('events', max=0)
        stopped = r.get('state') in ('stopped', 'hung')     # (a hang served: its frames stand for a stop's)
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
        self.emit(r, 'detached: %d breakpoints cleared, %d histories stopped, the game runs'
                  % (r['cleared'], r.get('histories', 0)))
        return 0

    def c_config(self, a):
        args = {}
        if a.stop_timeout:
            args['stopTimeout'] = a.stop_timeout
        r = self.session().conn.call('config', **args)
        self.emit(r, 'stop timeout %d s' % r['stopTimeout'])
        return 0

    def c_hang(self, a):
        # the hang watchdog (the game's): a run with no frame for the threshold is served at its next instruction
        args = {}
        if a.stop and a.log_only:
            raise DbgError('hang: --stop or --log-only')
        if a.stop or a.log_only:
            args['stopOnHang'] = bool(a.stop)
        if a.threshold is not None:
            if a.threshold < 0:
                raise DbgError('hang --threshold S: seconds (0: no watchdog)')
            args['hangThreshold'] = a.threshold
        r = self.session().conn.call('config', **args)
        if not r.get('hangWatch'):
            text = 'hang watchdog off (--threshold S starts it)'
        else:
            text = 'hang watchdog: threshold %g s, %s' % (r['hangThresholdMs'] / 1000.0, 'a hang stops there' if
                                                         r['stopOnHang'] else 'a hang is logged and served')
        self.emit(r, text + '; %d hangs so far' % r.get('hangs', 0))
        return 0

    # ---- stage 2b -----------------------------------------------------------------------------------------------
    def object_class(self, oid):
        v = self.session().conn.call('read', paths=[[['object', oid]] if oid < 0 else [['class', oid]]],
                                     depth=0)['values'][0]
        if v.get('t') != 'obj' or 'class' not in v:
            raise DbgError('#%d: %s' % (oid, v.get('error', 'not an object')))
        return v['class']

    def c_eval(self, a):
        s = self.session()
        info = s.main_info()
        text = open(a.file, encoding='utf-8').read() if a.file else ' '.join(a.expr)
        if not text.strip():
            raise DbgError('eval EXPR | --file F')
        block = bool(a.file) or a.block
        req = {'depth': a.depth, 'items': a.items}
        frame = None
        cls = 0
        if a.on is not None:
            cls = self.object_class(a.on)
            req['on'] = a.on
        elif self.stop_state() is not None:
            _, frame = self.frame_of(None, a.frame)
            req['frame'] = a.frame
        if a.budget:
            req['budget'] = a.budget
        if a.ms:
            req['ms'] = a.ms
        if a.write and a.read_only:
            raise DbgError('eval: --write and --read-only together (a read-only run writes nothing back)')
        snip = self.snippet(info, text, frame, cls if frame is None else None, block, read_only=a.read_only)
        req.update({k: snip[k] for k in ('code', 'jumps', 'strings', 'low', 'high')})
        if a.write:
            req['write'] = snip['written']      # (only the variables it assigns)
        if a.read_only:
            req['readOnly'] = True
        r = s.conn.call('eval', **req)
        if 'failed' in r:
            line = kcsnip_line(snip, r.get('pc'))
            raise DbgError('eval%s: %s (%d instructions)' % (' line %d' % line if line else '', r['failed'],
                                                             r.get('instructions', 0)))
        v = r.get('value')
        where = (' in %s' % frame.where()) if frame else (' on #%d' % a.on if a.on is not None else '')
        self.emit({'value': v, 'instructions': r.get('instructions'), 'natives': snip['natives']},
                  '%s%s' % (format_value(info, v), '' if not a.verbose else ' (%d instructions%s)'
                            % (r.get('instructions', 0), where)))
        return 0

    def watch_target(self, info, text):
        """this.FIELD | #ID.FIELD | class.CLASS.FIELD | global.NAME -> (request part, class of the object); a container's
        element or key PATH[N] / PATH["KEY"], or all of it PATH[*] (PATH: any print path that holds an array or table:
        this.cargo, #ID.table, global.gX, a local at a stop) -> the path and the key (the game watches the container
        the path reaches now, whatever path reaches it later)"""
        m = re.match(r'^(.+)\[\s*(\*|-?\d+|0x[0-9a-fA-F]+|"(?:[^"\\]|\\.)*")\s*\]$', text)
        if m:
            base, key = m.groups()
            frame = None
            if self.stop_state() is not None:
                _, frame = self.frame_of(None, 0)
            steps = PathCompiler(self.session(), info, frame).compile(base)
            if key == '*':
                return {'path': steps, 'all': True}, None
            k = json.loads(key) if key.startswith('"') else int(key, 0)
            return {'path': steps, 'key': [['key', k] if isinstance(k, str) else ['index', k]]}, None
        m = re.match(r'^global\.([A-Za-z_]\w*)$', text)
        if m:
            if m.group(1) not in info.globals:
                raise DbgError('no global %s' % m.group(1))
            return {'global': info.globals.index(m.group(1))}, None
        m = re.match(r'^(this|#-?\d+|class\.[A-Za-z_]\w*)\.([A-Za-z_]\w*)$', text)
        if not m:
            raise DbgError('watch this.FIELD | #ID.FIELD | class.CLASS.FIELD | global.NAME | PATH[N] | PATH["KEY"] | '
                           'PATH[*]')
        base, field = m.groups()
        pc = PathCompiler(self.session(), info)
        if base == 'this':
            stop = self.stop_state()
            if stop is None or stop.get('this') is None:
                raise DbgError('this: at a stop whose frame has a receiver (else #ID.FIELD)')
            oid = stop['this']
            cid = self.object_class(oid)
        elif base.startswith('#'):
            oid = int(base[1:])
            cid = self.object_class(oid)
        else:
            cid = pc.class_id(base[6:])
            oid = cid
        return {'on': oid, 'slot': pc.field_slot(cid, field)}, cid

    def c_watch(self, a):
        s = self.session()
        info = s.main_info()
        obj = s.main_object()
        part, cid = self.watch_target(info, a.target)
        if 'path' in part and a.writable:
            raise DbgError("watch %s: a container watch's --if / --log run read-only (its hit is inside the container's "
                           "change): no --writable" % a.target)
        req = {'object': obj['name'], 'tag': 'watch ' + a.target}
        req.update(part)
        if a.change:
            req['change'] = True
        if a.count:
            req['count'] = a.count
        if a.once:
            req['once'] = True
        # --if / --log run in the writer's frame: for a field the writer is the object itself (its `this`), a global's
        # writer is any method - a global function's names then (no frame variables)
        comp = PathCompiler(s, info, None, dynamic=False)
        self.compiled_parts(a, info, req, None, comp, cls=cid or 0)
        r = s.conn.call('watch', **req)
        what = ''
        if r.get('container') is not None:
            # (containers are shared by reference: the watch is on this container, not on the path)
            what = ' (%s #%d%s)' % (r.get('kind'), r['container'], '' if r.get('watch') == 'container' else
                                   ' [%s]' % json.dumps(r.get('key')))
        self.emit(r, 'watchpoint %d on %s%s%s' % (r['bp'], a.target, what, ' (log)' if r.get('log') else ''))
        return 0

    # ---- object histories -----------------------------------------------------------------------------------------
    def history_target(self, text):
        """#ID | this (the stop's receiver) | PATH (a print path that holds an object) -> the object's id"""
        m = re.match(r'^#(-?\d+)$', text.strip())
        if m:
            return int(m.group(1))
        s = self.session()
        info = s.main_info()
        frame = None
        if self.stop_state() is not None:
            _, frame = self.frame_of(None, 0)
        if text.strip() == 'this' and frame is None:
            raise DbgError('this: at a stop (else #ID or a path)')
        steps = PathCompiler(s, info, frame).compile(text)
        v = s.conn.call('read', paths=[steps], depth=0)['values'][0]
        if v.get('t') != 'obj' or v.get('null') or v.get('deleted'):
            raise DbgError('%s: not a live object (%s)' % (text, format_value(info, v)))
        return v['id']

    def c_history(self, a):
        s = self.session()
        info = s.main_info()
        if a.what == 'list':
            r = s.conn.call('history', op='list')
            if self.json:
                print(json.dumps(r, ensure_ascii=False))
                return 0
            if not r['histories']:
                print('no history')
            for h in r['histories']:
                print(self.history_head(info, h))
            return 0
        if a.what == 'dump':
            if not a.output:
                raise DbgError('history dump -o FILE')
            hs = s.conn.call('history', op='list')['histories']
            with open(a.output, 'w', encoding='utf-8') as out:
                for h in hs:
                    r = s.conn.call('history', op='show', on=h['on'])
                    for c in r['changes']:
                        c['text'] = self.change_line(info, r, c)
                    out.write(json.dumps(r, ensure_ascii=False) + '\n')
            self.emit({'histories': len(hs), 'file': a.output}, '%d histories written to %s' % (len(hs), a.output))
            return 0
        if a.what == 'stop' and a.all:
            r = s.conn.call('history', op='stop', all=True)
            self.emit(r, 'stopped %d histories (%d records, %d bytes freed)' % (r['stopped'], r['records'], r['freed']))
            return 0
        if not a.target:
            raise DbgError('history %s #ID | this | PATH' % a.what)
        oid = self.history_target(a.target)
        if a.what == 'start':
            cid = self.object_class(oid)
            req = {'op': 'start', 'object': s.main_object()['name'], 'on': oid, 'depth': a.depth, 'size': a.size}
            if a.fields:
                comp = PathCompiler(s, info)
                req['slots'] = [comp.field_slot(cid, f.strip()) for f in a.fields.split(',') if f.strip()]
            if a.cond:
                # (run read-only on the object before each change: its fields by name, this->Method(), natives)
                snip = self.snippet(info, ' '.join(sum(a.cond, [])), None, cid, read_only=True)
                req['ifCode'] = {k: snip[k] for k in ('code', 'jumps', 'strings', 'low', 'high')}
            r = s.conn.call('history', **req)
            self.emit(r, 'history of #%d (%s): %d records kept, depth %d, %d containers tracked%s'
                      % (oid, info.class_name(cid) or cid, r['size'], r['depth'], r['containers'],
                         ', capped %d' % r['capped'] if r.get('capped') else ''))
            return 0
        if a.what == 'stop':
            r = s.conn.call('history', op='stop', on=oid)
            self.emit(r, 'stopped the history of #%d (%d records, %d bytes freed)' % (oid, r['records'], r['freed']))
            return 0
        # show
        req = {'op': 'show', 'on': oid}
        if a.last:
            req['last'] = a.last
        if a.changed:
            req['changed'] = True   # (the game leaves out the records it flagged `same`; --last counts after it)
        r0 = None
        if a.field:
            r0 = s.conn.call('history', op='show', on=oid, last=0)
            req['slot'] = PathCompiler(s, info).field_slot(r0['class'], a.field)
        r = s.conn.call('history', **req)
        if self.json:
            for c in r['changes']:
                c['text'] = self.change_line(info, r, c)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        print(self.history_head(info, r))
        for c in r['changes']:
            print('  ' + self.change_line(info, r, c))
        return 0

    def history_head(self, info, h):
        kept = h.get('kept', 0)
        text = '#%d %s: %s, %d changes recorded, the last %d kept (ring %d), depth %d, %d containers tracked' % (
            h['on'], info.class_name(h['class']) or h['class'], h['state'], h['records'], kept, h['size'],
            h['depth'], h['containers'])
        if h.get('capped'):
            text += ' (%d left out: the limits)' % h['capped']
        if h.get('filtered'):
            text += ', %d left out by --if' % h['filtered']
        if h.get('ifError'):
            text += ', --if failed: %s' % h['ifError']
        if h.get('slots'):
            names = class_fields(info, h['class'])
            text += ', fields ' + ','.join(names[x] if x < len(names) else 'f%d' % x for x in h['slots'])
        return text + ', %d bytes' % h.get('bytes', 0)

    def msci_editor_line(self, info, script, index):
        """an MSCI script's code index -> its editor line (the loaded script; None: not known)"""
        cache = self.__dict__.setdefault('_msci_lines', {})
        if script not in cache:
            try:
                cache[script] = self.script_lines(info, self.loaded_script_path(info, script))
            except DbgError:
                cache[script] = None
        lines = cache[script]
        return lines[index] if lines and 0 <= index < len(lines) else None

    def msci_line_cmd(self, info, script, index):
        """an MSCI script's code index -> (editor line, command id) of the loaded script (None: not known)"""
        cache = self.__dict__.setdefault('_msci_line_cmds', {})
        if script not in cache:
            try:
                cache[script] = self.script_lines_cmds(info, self.loaded_script_path(info, script))
            except DbgError:
                cache[script] = None
        got = cache[script]
        if not got or not 0 <= index < len(got[0]):
            return None
        return got[0][index], got[1][index]

    # -- the script profiler (dbgprof; docs/debugging.md "Script profiler") --
    def profile_dump(self):
        """the game's profile (its dump, symbolized with the MSCI lines resolved while the scripts are loaded) or
        None when the game has none"""
        s = self.session()
        st = s.conn.call('profile', op='status')
        if st.get('state') in ('none', 'armed'):
            return None
        d = s.conn.call('profile', op='dump', deadline=max(Conn.reply_timeout, 120.0))
        for k in ('id', 'ok'):
            d.pop(k, None)
        o = d.get('object', {})
        info = None
        try:
            info = s.info(dict(o, file=next((x.get('file') for x in s.objects() if x['name'] == o.get('name')), None)))
            d['sidecar'] = info.path
        except DbgError:
            pass
        # (the MSCI nodes' editor lines and commands now: the scripts are loaded; a saved dump keeps them)
        lines = {}
        if info is not None:
            for n in d.get('nodes', []):
                if n[0] == 3 and n[2] >= 0:
                    got = self.msci_line_cmd(info, n[1], n[2])
                    if got is not None:
                        lines.setdefault(n[1], {})[str(n[2])] = list(got)
        d['msciLines'] = lines
        return d

    def profile_of(self, a):
        """the profile a report reads: --from FILE, else the game's, else the newest saved one"""
        from . import dbgprof
        path = getattr(a, 'source', None)
        dump = None
        if not path:
            try:
                dump = self.profile_dump()
            except DbgError as x:
                if 'no ScriptDebug game' not in str(x):
                    raise
            if dump is None:
                path = dbgprof.newest_saved()
                if path is None:
                    raise DbgError('no profile: the game has none (profile start) and none is saved')
                print('(the saved profile %s)' % path, file=sys.stderr)
        if path:
            with open(path, encoding='utf-8') as h:
                dump = json.load(h)
        ml = dump.get('msciLines', {})

        def msci_line(script, index):
            got = ml.get(script, {}).get(str(index))
            return tuple(got) if got else None
        return dbgprof.Profile(dump, dbgprof.find_info(dump, self.args.dbg), msci_line)

    def c_profile(self, a):
        from . import dbgprof
        if a.what == 'start':
            args = {'hz': a.hz, 'frames': a.frames, 'untimed': a.untimed}
            arms = [(k, v) for k, v in (('atFrame', a.at_frame), ('atGameMs', a.at_game_ms),
                                        ('afterFrames', a.after_frames)) if v is not None]
            if len(arms) > 1:
                raise DbgError('profile start: one of --at-frame, --at-game-ms, --after-frames')
            if any(v < 0 for _, v in arms):
                raise DbgError('profile start: --at-frame, --at-game-ms, --after-frames take 0 or more')
            args.update(arms)
            r = self.session().conn.call('profile', op='start', **args)
            if r.get('state') == 'armed':
                self.emit(r, 'profile armed: it starts %s (now: frame %d, game time %d ms), %s, %d Hz%s' % (
                    self.armed_text(r), r.get('gameFrameNow', -1), r.get('gameMsNow', -1),
                    'timed' if r.get('timed') else 'untimed', r['hz'], ', %d frames' % a.frames if a.frames else ''))
                return 0
            self.emit(r, 'profile started: %s, %d Hz%s' % ('timed' if r.get('timed') else 'untimed', r['hz'],
                                                            ', %d frames' % a.frames if a.frames else ''))
            return 0
        if a.what == 'status':
            r = self.session().conn.call('profile', op='status')
            if r['state'] == 'none':
                self.emit(r, 'no profile')
                return 0
            if r['state'] == 'armed':
                self.emit(r, 'profile armed: it starts %s (now: frame %d, game time %d ms, debugger frame %d)' % (
                    self.armed_text(r), r.get('gameFrameNow', -1), r.get('gameMsNow', -1),
                    r.get('debuggerFrameNow', -1)))
                return 0
            p = dbgprof.Profile(r)
            self.emit(r, '%s\n%d nodes, %d stacks, %d bytes' % (p.header(), r['nodes'], r['stacks'], r['bytes']))
            return 0
        if a.what in ('stop', 'dump'):
            s = self.session()
            dump = self.profile_dump()
            path = a.output or dbgprof.saved_path(s.conn.pid)
            if dump is not None:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as h:
                    json.dump(dump, h, separators=(',', ':'))
            elif a.what == 'dump':
                raise DbgError('the game has no profile')
            if a.what == 'dump':
                self.emit({'file': path}, 'profile written to %s' % path)
                return 0
            r = s.conn.call('profile', op='stop')
            self.emit(dict(r, file=path if dump is not None else None),
                      'profile stopped: %d samples, %d bytes given back (%d left)%s'
                      % (r['samples'], r['freed'], r['bytes'], ', saved to %s' % path if dump is not None else ''))
            return 0
        p = self.profile_of(a)
        if a.what == 'report':
            if self.json:
                data = {'method': p.by_method, 'native': p.by_native, 'msci': p.by_msci, 'task': p.by_task,
                        'body': p.by_cxx}[a.by]()
                print(json.dumps({'header': p.header(), 'by': a.by, 'rows': data[:a.top] if isinstance(data, list)
                                  else {k: v[:a.top] for k, v in data.items()}}, ensure_ascii=False))
                return 0
            print('\n'.join(dbgprof.text_report(p, a.by, a.top)))
            return 0
        if a.what == 'frames':
            if self.json:
                print(json.dumps({'header': p.header(), 'distribution': p.distribution(), 'split': p.split(),
                                  'frameHist': p.d.get('frameHist') if a.hist else None, 'frames': p.frames(a.top)},
                                 ensure_ascii=False))
                return 0
            print('\n'.join(dbgprof.text_frames(p, a.top, a.series if a.hist else None)))
            return 0
        if a.what == 'flame':
            if not a.output:
                raise DbgError('profile flame -o FILE (.json: speedscope, else folded stacks)')
            fmt = a.format or ('speedscope' if a.output.lower().endswith('.json') else 'folded')
            if fmt == 'speedscope':
                doc = p.speedscope()
                errs = dbgprof.check_speedscope(doc)
                if errs:
                    raise DbgError('the speedscope file would be invalid: %s' % ', '.join(errs))
                with open(a.output, 'w', encoding='utf-8') as h:
                    json.dump(doc, h, ensure_ascii=False)
                n = len(doc['profiles'][0]['samples'])
            else:
                lines = p.folded()
                with open(a.output, 'w', encoding='utf-8') as h:
                    h.write('\n'.join(lines) + '\n')
                n = len(lines)
            self.emit({'file': a.output, 'format': fmt, 'stacks': n}, '%d stacks written to %s (%s)'
                      % (n, a.output, fmt))
            return 0
        raise DbgError('profile %s' % a.what)

    @staticmethod
    def armed_text(r):
        at, v = r.get('armedAt'), r.get('armedValue', 0)
        return ('at the engine\'s frame %d' % v if at == 'frame' else 'at game time %d ms' % v if at == 'gameMs'
                else 'at the debugger\'s frame %d' % v)

    def map_symbol(self, offset):
        """an exe offset -> the function the .map beside the game's exe names (None: no map)"""
        if not hasattr(self, '_map'):
            self._map = None
            exe = self.session().hello.get('exe')
            path = os.path.splitext(exe)[0] + '.map' if exe else None
            if path and os.path.isfile(path):
                base, syms = 0x140000000, []
                with open(path, encoding='latin-1') as h:
                    for line in h:
                        if 'Preferred load address is' in line:
                            base = int(line.split()[-1], 16)
                        p = line.split()
                        if len(p) >= 3 and re.match(r'^[0-9a-f]{4}:[0-9a-f]{8}$', p[0], re.I) and \
                                re.match(r'^[0-9a-f]{16}$', p[2], re.I):
                            syms.append((int(p[2], 16) - base, p[1]))
                syms.sort()
                self._map = syms
        if not self._map:
            return None
        import bisect
        i = bisect.bisect_right(self._map, (offset, '￿')) - 1
        return undecorate(self._map[i][1]) if i >= 0 else None

    def change_line(self, info, h, c):
        """one record: time, what changed (old -> new), the writer (its line, caller, MSCI script, native, engine)"""
        def fv(v):
            return format_value(info, v).replace('\n', ' ') if v is not None else '-'
        names = class_fields(info, h['class'])
        field = (names[c['slot']] if 0 <= c.get('slot', -1) < len(names) else 'f%d' % c.get('slot', -1))
        head = 't=%.3fs f%d ' % (c['time'] / 1000.0, c['frame'])
        k = c['kind']
        if k == 'field':
            what = '%s: %s -> %s' % (field, fv(c.get('old')), fv(c.get('new')))
        elif k == 'container':
            key = c.get('key')
            ktext = fv(key) if isinstance(key, dict) else str(key) if key is not None else None
            what = '%s%s %s #%d %s' % (field, '' if c['level'] == 1 else ' (level %d)' % c['level'], c['ctype'],
                                       c['container'], c['op'])
            if c['op'] in ('set', 'remove'):
                what += ' [%s] %s -> %s' % (ktext, fv(c.get('old')), fv(c.get('new')) if 'new' in c else '-')
            elif c['op'] == 'insert':
                what += ' [%s] %s' % (ktext if ktext is not None and ktext != '-1' else 'end', fv(c.get('new')))
            elif c['op'] == 'resize':
                what += ' size %d -> %d' % (c['size'], c.get('newSize', 0))
            what += ' (size %d)' % c['size'] if c['op'] != 'resize' else ''
        elif k == 'deleted':
            what = 'deleted'
        else:
            where = location_text(info, c['taskPc'], c['taskResume'])
            what = 'task %d %s at %s' % (c['taskId'], 'started' if k == 'taskStart' else 'ended', where)
        w = c.get('writer', {})
        wk = w.get('kind')
        if wk == 'none':
            return head + what
        if wk == 'engine':
            e = w.get('engine', '')
            sym = None
            m = re.match(r'^x3 exe\+0x([0-9a-f]+)$', e)
            if m:
                sym = self.map_symbol(int(m.group(1), 16))
            return head + what + '  by engine %s%s' % (e or '(C++)', ' (%s)' % sym if sym else '')
        by = location_text(info, w['pc'], False) if 'pc' in w else '?'   # (the writer's instruction itself)
        if wk == 'native':
            by = 'native %s at %s' % (w.get('native'), by)
        elif wk == 'case':
            by = 'the MSCI case at %s' % by
        elif wk == 'debugger':
            by = 'a debugger run (eval) task %d%s' % (w.get('task', 0), ' at %s' % by if 'pc' in w else '')
        text = head + what + '  by ' + by
        if 'callerPc' in w and wk != 'debugger':
            text += ' <- ' + location_text(info, w['callerPc'], True)
        if w.get('msci'):
            line = self.msci_editor_line(info, w['msci']['script'], w['msci']['index'])
            text += '  msci %s:%s' % (w['msci']['script'], line if line is not None else 'index %d' % w['msci']['index'])
        if wk != 'debugger' and w.get('task'):
            text += '  task %d' % w['task']
        if w.get('this') and w['this'] != h['on']:
            text += ' this=#%d' % w['this']
        return text

    def c_snapshot(self, a):
        s = self.session()
        info = s.main_info()
        req = {'chars': a.chars}
        if a.objects:
            pc = PathCompiler(s, info)
            names = [x.strip() for x in a.objects.split(',') if x.strip()]
            ids = set()
            for n in names:
                cid = pc.class_id(n)
                ids.add(cid)
                ids |= {c for c in info.classes if cid in class_chain(info, c)}
            req['classes'] = sorted(ids)
        t0 = time.time()
        r = s.conn.call('snapshot', deadline=max(Conn.reply_timeout, 300), **req)
        took = time.time() - t0
        snap = named_snapshot(info, r)
        data = json.dumps(snap, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        with open(a.output, 'w', encoding='utf-8') as h:
            h.write(data)
        self.emit({'file': a.output, 'bytes': len(data), 'objects': len(snap['objects']),
                   'arrays': len(snap['arrays']), 'tables': len(snap['tables']), 'tasks': len(snap['tasks']),
                   'seconds': round(took, 2)},
                  '%s: %d objects, %d arrays, %d tables, %d tasks, %d globals; %.1f MB (%.1f s)'
                  % (a.output, len(snap['objects']), len(snap['arrays']), len(snap['tables']), len(snap['tasks']),
                     len(snap['globals']), len(data) / 1e6, took))
        return 0

    def c_diff(self, a):
        with open(a.a, encoding='utf-8') as h:
            A_ = json.load(h)
        with open(a.b, encoding='utf-8') as h:
            B_ = json.load(h)
        lines = snapshot_diff(A_, B_, a.max)
        if self.json:
            print(json.dumps({'changes': lines}))
        else:
            print('\n'.join(lines) if lines else 'no changes')
        return 0

    def c_reload(self, a):
        from . import kcreload
        s = self.session()
        info = s.main_info()
        obj = s.main_object()
        try:
            plan = kcreload.plan(info, obj, a.src, a.what, self.object_file(obj, info),
                                 edition=s.hello.get('edition'))
        except kcreload.ReloadError as x:
            raise DbgError(str(x))
        if not plan['methods']:
            self.emit({'reloaded': 0}, 'nothing changed in %s' % (a.what or a.src))
            return 0
        # the breakpoints in the methods replaced: dropped by the game (set them again by their tags)
        listed = s.conn.call('list')['breakpoints']
        ranges = [(m['entry'], m['end']) for m in plan['methods']]
        hit = [b for b in listed if b.get('watch') is None and info.method_at(b['pc']) is not None and
               any(info.method_at(b['pc']).entry == e for e, _ in ranges)]
        # the reload's serial (the game's next): its sidecar's name, the game log's `reload N` line
        side = kcreload.sidecar_path(s.conn.pid, obj, obj.get('reloadSerial', 0) + 1)
        r = s.conn.call('reload', object=obj['name'], size=obj['size'], crc32=obj['crc32'],
                        methods=[{k: m[k] for k in ('entry', 'end', 'frame', 'code', 'jumps', 'strings', 'revert')}
                                 for m in plan['methods']], sidecar=side)
        s.hello = s.conn.call('hello')     # (the reloaded state for the rest of this process: a `run` file's later lines)
        s.infos = {}
        now = [o for o in s.hello['objects'] if o['name'] == obj['name']][0]
        kcreload.write_sidecar(side, plan, now.get('reloads', []), obj, r.get('serial'))
        names = ', '.join(m['name'] for m in plan['methods'])
        self.emit(dict(r, methods=[m['name'] for m in plan['methods']], sidecar=side),
                  'reloaded %d methods (%s)%s' % (r['reloaded'], names,
                                                 ''.join('\n  breakpoint %d dropped: set it again: x3kc dbg break %s'
                                                         % (b['bp'], b.get('tag', '0x%x' % b['pc'])) for b in hit)))
        return 0

    def object_file(self, obj, info):
        """the loaded object's .obj: its -kc file, else beside its sidecar"""
        if obj.get('file') and os.path.isfile(obj['file']):
            return obj['file']
        if info.path:
            p = os.path.splitext(info.path[:-3] if info.path.endswith('.gz') else info.path)[0] + '.obj'
            if os.path.isfile(p):
                return p
        raise DbgError('the object file of %s is not found (a -kc object, or the .obj beside its sidecar)'
                       % obj['name'])

    def c_md(self, a):
        from . import dbgmd
        return dbgmd.run(self, a)

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


def kcsnip_line(snip, pc):
    from . import kcsnip
    return kcsnip.line_of(snip, pc) if pc is not None else None


def split_top(text):
    """comma-separated items, commas inside (), [], {} and strings kept"""
    out, depth, cur, q = [], 0, '', None
    for ch in text:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in '"\'':
            q = ch
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        elif ch == ',' and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ''
            continue
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def class_chain(info, cid):
    out, seen = [], set()
    while cid in info.classes and cid not in seen:
        seen.add(cid)
        out.append(cid)
        cid = info.classes[cid].get('parent')
    return out


def named_snapshot(info, r):
    """the game's snapshot reply with the sidecar's names: objects {id: {class, fields {name: value}}}, globals
    {name: value}, statics {class: {field: value}}, arrays {id: [values]}, tables {id: [[key, value]]}, tasks
    {id: {state, object, location}}"""
    def fields_of(cid, values):
        names = class_fields(info, cid)
        return {(names[i] if i < len(names) else 'f%d' % i): v for i, v in enumerate(values)}
    out = {'format': 'x3kc-snapshot', 'version': 1, 'object': r.get('object'), 'state': r.get('state'),
           'globals': {(info.globals[i] if i < len(info.globals) else 'g%d' % i): v for i, v in enumerate(r.get('f', []))},
           'statics': {}, 'objects': {}, 'arrays': {}, 'tables': {}, 'tasks': {}}
    for k in r.get('classes', []):
        out['statics'][info.class_name(k['id']) or str(k['id'])] = fields_of(k['id'], k['f'])
    for o in r.get('objects', []):
        out['objects'][str(o['id'])] = {'class': info.class_name(o['c']) or str(o['c']), 'fields': fields_of(o['c'], o['f'])}
    for x in r.get('arrays', []):
        out['arrays'][str(x['id'])] = x['v']
    for x in r.get('tables', []):
        out['tables'][str(x['id'])] = x['v']
    for t in r.get('tasks', []):
        rec = {'state': t.get('state'), 'frames': t.get('frames')}
        if 'object' in t:
            rec['object'] = t['object']
        if 'pc' in t:
            rec['location'] = location_text(info, t['pc'], t.get('resume', True))
        if t.get('msci'):
            rec['msci'] = '%s:%d' % (t['msci']['script'], t['msci']['index'])
        out['tasks'][str(t['task'])] = rec
    return out


def snap_text(v):
    if v is None:
        return 'null'
    if isinstance(v, dict):
        k, x = next(iter(v.items()))
        return {'s': lambda: json.dumps(x, ensure_ascii=False), 'd': lambda: repr(x), 'l': lambda: '%sL' % x,
                'o': lambda: '#%s' % x, 'a': lambda: 'array#%s' % x, 'm': lambda: 'table#%s' % x,
                'r': lambda: '<ret 0x%x>' % x}.get(k, lambda: json.dumps(v))()
    return json.dumps(v)


def snapshot_diff(A_, B_, limit=200):
    """what changed from snapshot A to B, one line each (objects created / deleted, fields, globals, statics,
    containers, tasks); at most limit lines per section, the rest counted"""
    out = []

    def section(title, rows):
        if not rows:
            return
        out.append('%s (%d):' % (title, len(rows)))
        out.extend('  ' + r for r in rows[:limit])
        if len(rows) > limit:
            out.append('  ... %d more' % (len(rows) - limit))

    def changed(a, b):
        return ['%s: %s -> %s' % (k, snap_text(a.get(k)), snap_text(b.get(k)))
                for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    section('globals', changed(A_['globals'], B_['globals']))
    st = []
    for c in sorted(set(A_['statics']) | set(B_['statics'])):
        st += ['%s.%s' % (c, x) for x in changed(A_['statics'].get(c, {}), B_['statics'].get(c, {}))]
    section('static fields', st)
    oa, ob = A_['objects'], B_['objects']
    section('objects created', ['#%s %s' % (i, ob[i]['class']) for i in sorted(set(ob) - set(oa), key=int)])
    section('objects deleted', ['#%s %s' % (i, oa[i]['class']) for i in sorted(set(oa) - set(ob), key=int)])
    fl = []
    for i in sorted(set(oa) & set(ob), key=int):
        fl += ['#%s %s.%s' % (i, ob[i]['class'], x) for x in changed(oa[i]['fields'], ob[i]['fields'])]
    section('fields changed', fl)
    for kind in ('arrays', 'tables'):
        ca, cb = A_[kind], B_[kind]
        rows = ['%s#%s created (%d)' % (kind[:-1], i, len(cb[i])) for i in sorted(set(cb) - set(ca), key=int)]
        rows += ['%s#%s deleted' % (kind[:-1], i) for i in sorted(set(ca) - set(cb), key=int)]
        for i in sorted(set(ca) & set(cb), key=int):
            if ca[i] != cb[i]:
                if kind == 'arrays' and len(ca[i]) == len(cb[i]):
                    d = ['[%d] %s -> %s' % (k, snap_text(x), snap_text(y)) for k, (x, y) in enumerate(zip(ca[i], cb[i]))
                         if x != y]
                    rows.append('array#%s: %s%s' % (i, ', '.join(d[:5]), ' ...' if len(d) > 5 else ''))
                else:
                    rows.append('%s#%s: %d -> %d elements' % (kind[:-1], i, len(ca[i]), len(cb[i])))
        section(kind, rows)
    ta, tb = A_['tasks'], B_['tasks']
    rows = ['task %s started at %s' % (i, tb[i].get('location')) for i in sorted(set(tb) - set(ta), key=int)]
    rows += ['task %s ended (was at %s)' % (i, ta[i].get('location')) for i in sorted(set(ta) - set(tb), key=int)]
    rows += ['task %s: %s %s -> %s %s' % (i, ta[i]['state'], ta[i].get('location'), tb[i]['state'], tb[i].get('location'))
             for i in sorted(set(ta) & set(tb), key=int)
             if (ta[i]['state'], ta[i].get('location')) != (tb[i]['state'], tb[i].get('location'))]
    section('tasks', rows)
    return out


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
    p.add_argument('--if', dest='cond', nargs='+', action='append', metavar='EXPR',
                   help='stop / log only when the KC expression EXPR is true (compiled against the sidecar, run in the '
                        'hit frame: its variables, this, fields, globals, constants, calls of at-once natives); the '
                        'old form PATH OP VALUE (== != < <= > >=; a number, "text" or null) is a path compare; '
                        'repeatable: all must hold (one expression)')
    p.add_argument('--log', action='append', metavar='ITEMS', help='a tracepoint: log these paths or KC expressions '
                   '(comma-separated) into the event stream and go on (no stop)')
    p.add_argument('--depth', type=int, default=1, help='--log: the depth of the logged values (default 1)')
    p.add_argument('--count', type=int, default=0, help='only from the Nth hit on (the hits that pass --if/--this)')
    p.add_argument('--once', action='store_true', help='removed after its first stop / log')
    p.add_argument('--this', type=int, default=0, metavar='ID', help='only when the receiver is object #ID')
    p.add_argument('--writable', action='store_true', help='--if / --log expressions may write (fields, globals, '
                   'containers, natives with side effects); by default they run read-only: such a write ends the run '
                   'before it is done (an error event, the condition false)')


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
    p = sub.add_parser('events', help='the queued events (no wait; 1000 at most, a note when more are queued)')
    p.add_argument('--all', action='store_true', help='every queued event (reads until the queue is empty)')
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
    sub.add_parser('pause', help='stop at the next method a task enters (a hung task: at its next instruction, once '
                   'the hang watchdog serves it)')
    sub.add_parser('detach', help='clear every breakpoint and go on')
    p = sub.add_parser('config')
    p.add_argument('--stop-timeout', type=int, default=0, help='seconds without a request at a stop before the '
                   'game clears the breakpoints and goes on (default 120)')
    p = sub.add_parser('hang', help='the hang watchdog: a task that runs without waiting for the threshold is '
                       'logged, served (stack, locals, print, eval, pause answer) and with --stop stopped')
    p.add_argument('--stop', action='store_true', help='stop at a hang\'s first safepoint as at a breakpoint (until '
                   'clear --all, detach or a stop\'s timeout)')
    p.add_argument('--log-only', action='store_true', help='log and serve a hang, no stop (the default)')
    p.add_argument('--threshold', type=float, help='seconds without a frame in one script run (default 2; 0: the '
                   'watchdog off)')
    p = sub.add_parser('run', help='a batch of commands, one per line')
    p.add_argument('file')
    p = sub.add_parser('eval', help='a KC expression / block run in the stopped frame (or --on #ID, or the globals)')
    p.add_argument('expr', nargs='*')
    p.add_argument('--file', help='a statement block from a file (`return X;` gives the value)')
    p.add_argument('--block', action='store_true', help='EXPR is a statement block')
    p.add_argument('--frame', type=int, default=0, help='the frame of the stop (0: the innermost)')
    p.add_argument('--on', type=int, help='run on object #ID (its fields, this->Method()) instead of a frame')
    p.add_argument('--write', action='store_true', help='write the frame variables the block assigned back')
    p.add_argument('--read-only', action='store_true', help='refuse every write but into the snippet\'s own locals and '
                   'the containers it creates: a field, a global, another container, a new / deleted object or a '
                   'native with side effects ends the run before it is done')
    p.add_argument('--depth', type=int, default=1)
    p.add_argument('--items', type=int, default=50)
    p.add_argument('--budget', type=int, default=0, help='instructions (default 10 million)')
    p.add_argument('--ms', type=int, default=0, help='wall time (default 2000)')
    p.add_argument('--verbose', '-v', action='store_true')
    p = sub.add_parser('watch', help='a watchpoint: this.FIELD | #ID.FIELD | class.CLASS.FIELD | global.NAME | '
                       'PATH[N] | PATH["KEY"] | PATH[*] (an array / table element or key, all of it)')
    p.add_argument('target')
    p.add_argument('--change', action='store_true', help='only writes that change the value')
    add_bp_options(p)
    p = sub.add_parser('history', help="an object's change history, recorded by the game without stopping: start, "
                       'show, stop, list, dump')
    p.add_argument('what', choices=['start', 'show', 'stop', 'list', 'dump'])
    p.add_argument('target', nargs='?', help='#ID | this (the stop\'s receiver) | PATH (a print path to an object)')
    p.add_argument('--depth', type=int, default=0, help='start: also the containers reachable from its fields, N '
                   'levels deep (0: the fields only; at most 4)')
    p.add_argument('--size', type=int, default=1024, help='start: the records kept (a ring; at most 16384)')
    p.add_argument('--fields', help='start: only these fields (comma-separated; their containers with --depth)')
    p.add_argument('--if', dest='cond', nargs='+', action='append', metavar='EXPR',
                   help='start: record only when the KC expression is true, run read-only on the object before '
                        'each change (its fields by name)')
    p.add_argument('--last', type=int, default=0, help='show: the newest N records (default all kept)')
    p.add_argument('--field', help='show: only the records of this field (and its containers)')
    p.add_argument('--changed', action='store_true', help='show: only writes that changed the value (a store of '
                   'the value the cell had is left out)')
    p.add_argument('--all', action='store_true', help='stop: every history')
    p.add_argument('-o', '--output', help='dump: the file (one JSON line per history, the records symbolized)')
    p = sub.add_parser('profile', help='the script profiler: start, stop (saves the profile, frees the game\'s), '
                       'status, dump, report, flame, frames')
    p.add_argument('what', choices=['start', 'stop', 'status', 'dump', 'report', 'flame', 'frames'])
    p.add_argument('--hz', type=int, default=2000, help='start: samples a second (0: the timing only; at most 20000)')
    p.add_argument('--frames', type=int, default=0, help='start: collect N frames, then stop collecting (the data '
                   'stays until stop)')
    p.add_argument('--at-frame', type=int, help='start: armed - collecting starts at the engine\'s frame N (the '
                   'frame count histories show as fN; deterministic on the stand)')
    p.add_argument('--at-game-ms', type=int, help='start: armed - collecting starts at the first frame whose game '
                   'time is T ms or later')
    p.add_argument('--after-frames', type=int, help='start: armed - collecting starts N frames from now')
    p.add_argument('--hist', action='store_true', help='frames: the per-frame histogram of a series (--series)')
    p.add_argument('--series', default='scripts', choices=['frame', 'scripts', 'client', 'world'],
                   help='frames --hist: the series (default: the script time a frame)')
    p.add_argument('--untimed', action='store_true', help='start: the samples only - no exact times of natives, '
                   'bodies, cases and task runs (less overhead: docs/debugging.md "Script profiler")')
    p.add_argument('--top', type=int, default=25, help='report / frames: the rows shown')
    p.add_argument('--by', default='method', choices=['method', 'native', 'msci', 'task', 'body'],
                   help='report: by KC method (exclusive / inclusive), native (timed, with its callers), MSCI script / '
                        'line / command, task root (timed runs), body (native bodies and cases, timed)')
    p.add_argument('--format', choices=['folded', 'speedscope'], help='flame: the format (default by the extension: '
                   '.json speedscope, else folded stacks)')
    p.add_argument('--from', dest='source', help='report / flame / frames: a saved profile (profile stop / dump -o)')
    p.add_argument('-o', '--output', help='stop / dump: where the profile is saved (default %%TEMP%%\\x3kc_dbg\\'
                   'profile-<pid>.json); flame: the file')
    p = sub.add_parser('snapshot', help='the script world into a JSON file (objects, globals, containers, tasks)')
    p.add_argument('--objects', help='only the objects of these classes (and their subclasses), comma-separated')
    p.add_argument('--chars', type=int, default=400, help='the characters of a string kept')
    p.add_argument('-o', '--output', required=True)
    p = sub.add_parser('diff', help='what changed between two snapshots')
    p.add_argument('a')
    p.add_argument('b')
    p.add_argument('--max', type=int, default=200, help='lines per section')
    p = sub.add_parser('reload', help='recompile and replace changed methods in the running game')
    p.add_argument('what', nargs='?', help='File.kc (its changed methods) or Class::Method; none: every changed one')
    p.add_argument('--src', required=True, help='the object\'s KC project (kc/ap/x3story: the folder or its .kcs)')
    from . import dbgmd
    dbgmd.add_parser(sub)
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
