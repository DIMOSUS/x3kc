"""Debug info of a compiled KC object (the ".kcdbg" sidecar, docs/debugging.md) and the mapping of raw script
locations back to the source.

    x3kc symbolize [<log>... | -] [--dbg FILE|DIR]... [-o OUT] [--raw]

`x3kc compile` writes <out>.kcdbg next to every object it writes (collect + write below); the object's bytes do not
depend on it. A reader (DebugInfo) maps a code offset (pc) to its method, source file, line and column, names the
cells of a frame (arguments, locals, the frame cells), the fields of a class and the globals. `symbolize` rewrites the
engine's raw locations `kc[<object> 0x<pc> ...]` in a log to `File.kc:line:col Class::Method`; the live debugger
(stage 2) uses the same DebugInfo / Symbolizer.

The pc of a raw location is the task's resume pc: the address after the last instruction the frame executed (a
caller frame: its return address). Its line is the line of pc - 1 (pc itself when it is the method's entry: a task
that has not run yet). DebugInfo.location(pc) does that; location(pc, resume=False) looks the pc itself up.
"""
import argparse
import bisect
import gzip
import hashlib
import json
import os
import re
import sys
import zlib

from .kcsym import NO_PARENT

FORMAT = 'x3kc-kcdbg'
VERSION = 1
SUFFIX = '.kcdbg'


def sidecar_path(obj_path):
    """<name>.obj -> <name>.kcdbg (any other name: + .kcdbg)"""
    root, ext = os.path.splitext(obj_path)
    return root + SUFFIX if ext.lower() == '.obj' else obj_path + SUFFIX


def identity(data):
    """the identity of an object file's exact bytes: size, crc32 (8 hex digits: what the engine logs at load) and
    sha256"""
    return {'size': len(data), 'crc32': '%08x' % (zlib.crc32(data) & 0xffffffff),
            'sha256': hashlib.sha256(data).hexdigest()}


# ---- writing ------------------------------------------------------------------------------------------------------
def _split_loc(text):
    f, _, n = text.rpartition(':')
    return f, int(n)


def collect(o, fns, toks, loc, name):
    """What the compiler knows, as the sidecar's dict without the object's identity (write adds it): o the compiled
    KcObj (o.unit: the parsed unit), fns the compiled functions in code order (kcc.compile_source: f.entry, f.dbg the
    FuncGen), toks the joined source's tokens, loc(line) -> 'part.kc:line', name the object's name (x3story)."""
    syms = o.unit.syms
    files, file_ix = [], {}

    def file_of(f):
        i = file_ix.get(f)
        if i is None:
            i = file_ix[f] = len(files)
            files.append(f)
        return i

    classes = []
    for cid in sorted(syms.classes):
        if cid == 0:
            continue
        k = syms.classes[cid]
        fields = []
        for c in reversed(syms.chain(cid)):
            fields += [fd.name for fd in syms.classes[c].fields]
        classes.append({'id': cid, 'name': k.name, 'parent': None if k.parent == NO_PARENT else k.parent,
                        'fields': fields})
    g = syms.classes.get(0)
    globals_ = [fd.name for fd in g.fields] if g is not None else []
    methods = []
    for i, f in enumerate(fns):
        end = fns[i + 1].entry if i + 1 < len(fns) else len(o.code)
        part, line = _split_loc(loc(f.line))
        fi = file_of(part)
        gen = getattr(f, 'dbg', None)
        lines, cells = [], []
        if gen is not None:
            n = gen.nparams
            for pc, at in gen.lines:
                t = toks[f.start + at]
                tf, tl = _split_loc(loc(t.line))
                if tf != part:          # (a statement is in its function's part; kept exact if not)
                    lines.append([pc, tl, t.col + 1, file_of(tf)])
                else:
                    lines.append([pc, tl, t.col + 1])
            for vname, kind, slot, start, stop in gen.dbg_vars:
                cells.append([vname, kind, n + 1 - slot, start, stop])
        methods.append({'class': f.cls, 'name': f.name, 'entry': f.entry, 'end': end, 'file': fi, 'line': line,
                        'frame': getattr(f, 'frame_size', None), 'params': list(f.params), 'lines': lines,
                        'vars': cells})
    consts = {}
    table = syms.consts
    if table is not None:
        for cid, defs in sorted(table.consts.items()):
            vals = {n: d.value for n, d in sorted(defs.items()) if isinstance(d.value, (int, str))}
            if vals:
                consts[str(cid)] = vals
    return {'format': FORMAT, 'version': VERSION, 'object': {'name': name, 'build': o.build},
            'files': files, 'globals': globals_, 'classes': classes, 'constants': consts, 'methods': methods}


def write(path, info, data):
    """the sidecar of the object bytes data at path (via path.tmp: no half-written sidecar): one JSON object, one
    method per line"""
    info = dict(info)
    obj = dict(info['object'])
    obj.update(identity(data))
    info['object'] = obj
    methods = info.pop('methods')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as h:
        head = json.dumps(info, ensure_ascii=False, separators=(',', ':'))
        h.write(head[:-1] + ',"methods":[\n')
        for i, m in enumerate(methods):
            h.write(json.dumps(m, ensure_ascii=False, separators=(',', ':')))
            h.write(',\n' if i + 1 < len(methods) else '\n')
        h.write(']}\n')
    os.replace(tmp, path)


# ---- reading ------------------------------------------------------------------------------------------------------
class Location:
    __slots__ = ('file', 'line', 'col', 'method')

    def __init__(self, file, line, col, method):
        self.file, self.line, self.col, self.method = file, line, col, method

    def __str__(self):
        where = '%s:%d:%d' % (self.file, self.line, self.col) if self.col else '%s:%d' % (self.file, self.line)
        return '%s %s' % (where, self.method.qualname)


class Method:
    """One compiled function: cls (class id, 0: global), cls_name, name, entry / end (code offsets, end exclusive),
    file, line (its header), frame (the frame instruction's operand), params, lines [(pc, line, col, file)], vars
    [(name, kind, cell, start, end)]: cell relative to the frame's return-offset cell (docs/debugging.md "Frames")."""
    __slots__ = ('cls', 'cls_name', 'name', 'entry', 'end', 'file', 'line', 'frame', 'params', 'lines', 'vars',
                 '_pcs', 'origin', 'reloaded')

    @property
    def qualname(self):
        return '%s::%s' % (self.cls_name, self.name) if self.cls else self.name


class DebugInfo:
    def __init__(self, data, path=None):
        if data.get('format') != FORMAT:
            raise ValueError('%s: not an x3kc debug info file' % (path or 'data'))
        if data.get('version') != VERSION:
            raise ValueError('%s: debug info version %r, this x3kc reads %d' % (path or 'data', data.get('version'),
                                                                               VERSION))
        self.path = path
        o = data['object']
        self.name, self.build = o['name'], o.get('build')
        self.size, self.crc32, self.sha256 = o.get('size'), o.get('crc32'), o.get('sha256')
        self.files = data['files']
        self.globals = data['globals']
        self.classes = {k['id']: k for k in data['classes']}
        self.constants = {int(c): v for c, v in data.get('constants', {}).items()}
        self.methods = []
        for m in data['methods']:
            x = Method()
            x.cls, x.name, x.entry, x.end = m['class'], m['name'], m['entry'], m['end']
            x.cls_name = self.classes[x.cls]['name'] if x.cls in self.classes else str(x.cls)
            x.file, x.line, x.frame, x.params = self.files[m['file']], m['line'], m.get('frame'), m['params']
            x.lines = [(r[0], r[1], r[2], self.files[r[3]] if len(r) > 3 else x.file) for r in m['lines']]
            x._pcs = [r[0] for r in x.lines]
            x.vars = [tuple(v) for v in m['vars']]
            # a reload's sidecar (x3kc dbg reload): origin the loaded entry callers call (a method the reload put into
            # the game's code tail has a jump there), reloaded its bytes replaced in the running game
            x.origin = m.get('origin', x.entry)
            x.reloaded = bool(m.get('reloaded'))
            self.methods.append(x)
        self.methods.sort(key=lambda x: x.entry)
        self._entries = [x.entry for x in self.methods]

    @classmethod
    def load(cls, path):
        """a .kcdbg, plain or gzipped (a release's, kept by x3_src tools/kcrelease.py as .kcdbg.gz)"""
        with _open_text(path) as h:
            return cls(json.load(h), path)

    def matches(self, size=None, crc32=None, data=None):
        """is this the debug info of that object? (its bytes, or the size and crc32 the engine logs)"""
        if data is not None:
            return identity(data)['sha256'] == self.sha256
        return (size is None or size == self.size) and (crc32 is None or crc32.lower() == self.crc32)

    def method_at(self, pc):
        i = bisect.bisect_right(self._entries, pc) - 1
        if i < 0:
            return None
        m = self.methods[i]
        return m if pc < m.end else None

    def location(self, pc, resume=True):
        """the source location of pc (resume: pc is a resume pc, the line is pc - 1's); None outside every method"""
        m = self.method_at(pc)
        if m is None:
            return None
        if resume and pc > m.entry:
            pc -= 1
        i = bisect.bisect_right(m._pcs, pc) - 1
        if i < 0:
            return Location(m.file, m.line, 0, m)
        _, line, col, f = m.lines[i]
        return Location(f, line, col, m)

    def frame_vars(self, method, pc, resume=True):
        """the variables of a frame of method stopped at pc that hold a value there: [(name, kind, cell)], cell
        relative to the frame's return-offset cell. Arguments and frame cells always, locals in their pc range."""
        if resume and pc > method.entry:
            pc -= 1
        return [(n, k, c) for n, k, c, s, e in method.vars if k != 'local' or s <= pc < e]

    def class_name(self, cid):
        k = self.classes.get(cid)
        return k['name'] if k else None

    def field_name(self, cid, slot):
        k = self.classes.get(cid)
        if k is None or not 0 <= slot < len(k['fields']):
            return None
        return k['fields'][slot]

    def global_name(self, slot):
        return self.globals[slot] if 0 <= slot < len(self.globals) else None


def _open_text(path):
    with open(path, 'rb') as h:
        packed = h.read(2) == b'\x1f\x8b'
    return gzip.open(path, 'rt', encoding='utf-8') if packed else open(path, encoding='utf-8')


def head(path):
    """the sidecar's "object" (name, build, size, crc32, sha256) from the start of the file alone (write puts it
    before every other entry), or None"""
    try:
        with _open_text(path) as h:
            first = h.read(4096)
    except (OSError, EOFError, UnicodeDecodeError):
        return None
    m = re.search(r'"object":(\{[^{}]*\})', first)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def _is_sidecar_of(file_name, name):
    """<name>.kcdbg, <name>.<build>.kcdbg, the same with .gz"""
    f = file_name.lower()
    if f.endswith('.gz'):
        f = f[:-3]
    if not f.endswith(SUFFIX):
        return False
    stem = f[:-len(SUFFIX)]
    n = name.lower()
    return stem == n or (stem.startswith(n + '.') and stem[len(n) + 1:].isdigit())


def find_all(name, dirs):
    """every sidecar of the object name in dirs (folders, or .kcdbg[.gz] files), in their order: <name>.kcdbg,
    <name>.<build>.kcdbg and their .gz"""
    out = []
    for d in dirs:
        if os.path.isfile(d):
            if _is_sidecar_of(os.path.basename(d), name):
                out.append(d)
        elif os.path.isdir(d):
            out += [os.path.join(d, f) for f in sorted(os.listdir(d)) if _is_sidecar_of(f, name)]
    return out


def find(name, dirs, size=None, crc32=None):
    """the first sidecar of name in dirs whose identity is that size and crc32 (None: any), else None"""
    for p in find_all(name, dirs):
        o = head(p)
        if o is not None and o.get('name', name).lower() == name.lower() and \
                (size is None or o.get('size') == size) and (crc32 is None or o.get('crc32') == crc32.lower()):
            return p
    return None


# the engine's raw script location (src/script/task_walk.cpp): kc[<object> 0x<pc> <class id>:<method> ...]
RAW = re.compile(r'kc\[(?P<obj>[A-Za-z0-9_.-]+) 0x(?P<pc>[0-9a-fA-F]+)(?P<rest>[^\]]*)\]')
# the engine's load line (src/script/vm.cpp Vm::loadObject):
#   kc object <name> loaded: build <n>, <size> bytes, crc32 <8 hex>[, file <path>]
LOADED = re.compile(r'kc object (?P<obj>[A-Za-z0-9_.-]+) loaded: build (?P<build>\d+), (?P<size>\d+) bytes, '
                    r'crc32 (?P<crc>[0-9a-fA-F]{8})(?:, file (?P<file>.+?))?\s*$')
_METHOD = re.compile(r'^\s*-?\d+:\S+')
# the live debugger's reload lines (x3_src debugger.cpp): from there on the object runs the reloaded layout, whose sidecar
# x3kc dbg reload wrote as <name>-<pid>-r<serial>.kcdbg (%TEMP%/x3kc_dbg, or a --dbg folder)
RELOADED = re.compile(r'script debugger: reload (?P<serial>\d+) of (?P<obj>\w+):')
STARTED = re.compile(r'x3rw log started .*, process (?P<pid>\d+)')


class Symbolizer:
    """Raw locations -> source locations, with the sidecars found in dirs (folders or .kcdbg files) or named by the
    log's load lines (the object's file: its .kcdbg beside it). A load line also fixes the identity the sidecar must
    have from there on: a stale sidecar (other bytes) is not used, its locations stay raw and one warning is given."""

    def __init__(self, dirs=(), warn=None, raw=False):
        self.dirs = list(dirs)
        self.warn = warn or (lambda text: print('warning: ' + text, file=sys.stderr))
        self.raw = raw
        self.loaded = {}        # object name -> (size, crc32, file) of its last load line
        self.infos = {}         # (path, identity) -> DebugInfo | None
        self.found = {}         # (name, size, crc32) -> the sidecar in dirs (find), None: none
        self.warned = set()
        self.pid = None
        self.reload = {}        # object name -> the sidecar of its reloaded layout (its last reload line)

    def info(self, name):
        """the DebugInfo of an object as the log has it now, or None: the sidecar beside the loaded file, else the
        one in dirs with the loaded identity (a release's base/<name>.<build>.kcdbg.gz among others); without a load
        line, the first one found, its identity unchecked (one warning)"""
        size, crc, path = self.loaded.get(name, (None, None, None))
        cands = []
        if self.reload.get(name):
            cands.append(self.reload[name])
        if path:
            cands.append(sidecar_path(path))
        if (name, size, crc) not in self.found:
            found = find(name, self.dirs, size, crc)
            if found is None and size is not None:
                found = find(name, self.dirs)   # (none of that identity: the first, reported stale below)
            self.found[(name, size, crc)] = found
        found = self.found[(name, size, crc)]
        if found:
            cands.append(found)
        if size is None and cands:
            self.once(('unchecked', name), 'the log has no load line of %s: its debug info (%s) is used without '
                                           'checking that it is the loaded object' % (name, cands[0]))
        for p in cands:
            key = (os.path.abspath(p), size, crc)
            if key not in self.infos:
                info = None
                if os.path.isfile(p):
                    try:
                        info = DebugInfo.load(p)
                    except (OSError, ValueError, KeyError) as x:
                        self.once(p, 'cannot read %s: %s' % (p, x))
                if info is not None and not info.matches(size, crc):
                    self.once(key, '%s is stale: the game loaded %s with %s bytes, crc32 %s; the sidecar is of %d '
                                   'bytes, crc32 %s (rebuild the object): its locations stay raw'
                              % (p, name, size, crc, info.size, info.crc32))
                    info = None
                self.infos[key] = info
            if self.infos[key] is not None:
                return self.infos[key]
        self.once(('none', name, size, crc), 'no debug info for %s (--dbg names the folder of its .kcdbg)' % name)
        return None

    def once(self, key, text):
        if key not in self.warned:
            self.warned.add(key)
            self.warn(text)

    def location(self, name, pc, resume=True):
        info = self.info(name)
        return info.location(pc, resume) if info is not None else None

    def line(self, text):
        """one log line: a load line is noted, every raw location rewritten"""
        m = LOADED.search(text)
        if m:
            self.loaded[m['obj']] = (int(m['size']), m['crc'].lower(), m['file'])
            self.reload.pop(m['obj'], None)             # (a load: its own layout again)
        m = STARTED.search(text)
        if m:
            self.pid = int(m['pid'])
        m = RELOADED.search(text)
        if m:
            name = '%s-%s-r%s.kcdbg' % (m['obj'], self.pid, m['serial'])
            import tempfile
            dirs = [d for d in self.dirs if os.path.isdir(d)] + [os.path.join(tempfile.gettempdir(), 'x3kc_dbg')]
            found = [os.path.join(d, name) for d in dirs if os.path.isfile(os.path.join(d, name))]
            if found:
                self.reload[m['obj']] = found[0]
            else:
                self.once(('reload', name), '%s was reloaded (reload %s): its sidecar %s is not found: locations of '
                          'reloaded methods may be wrong' % (m['obj'], m['serial'], name))
                self.reload.pop(m['obj'], None)

        def sub(r):
            loc = self.location(r['obj'], int(r['pc'], 16))
            if loc is None:
                return r.group(0)
            rest = _METHOD.sub('', r['rest']).strip()       # the engine's class:method, now in the location
            out = str(loc)
            if rest:
                out += ' (%s)' % rest
            if self.raw:
                out += ' [%s 0x%s]' % (r['obj'], r['pc'])
            return out
        return RAW.sub(sub, text)


def main():
    ap = argparse.ArgumentParser(prog='x3kc symbolize', description='raw script locations kc[<object> 0x<pc> ...] '
                                 'of a game log -> File.kc:line:col Class::Method')
    ap.add_argument('log', nargs='*', default=['-'], help='the logs in their order (x3rw_previous.log x3rw.log: the '
                    'load lines of an earlier one give the identities for the later ones); - or nothing: stdin')
    ap.add_argument('--dbg', action='append', default=[], metavar='FILE|DIR',
                    help='a .kcdbg file or a folder of them (repeatable); the load lines of the log name the object '
                         'files too (their .kcdbg beside them)')
    ap.add_argument('-o', '--out', help='write here (default: stdout)')
    ap.add_argument('--raw', action='store_true', help='keep the raw location after the symbolized one')
    a = ap.parse_args()
    S = Symbolizer(a.dbg, raw=a.raw)
    out = open(a.out, 'w', encoding='utf-8', newline='') if a.out else sys.stdout
    try:
        for log in a.log or ['-']:
            src = sys.stdin if log == '-' else open(log, encoding='utf-8', errors='replace')
            try:
                for line in src:
                    out.write(S.line(line))
            finally:
                if src is not sys.stdin:
                    src.close()
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
