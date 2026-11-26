"""Hot reload for the live debugger (x3kc dbg reload, docs/debugging.md "Hot reload"): the object's project compiled
again, its methods compared with the loaded object's bytes, the changed ones sent to the game relocatable (compiled at
offset 0: jumps relative, calls the loaded object's entries, strings by text - x3_src debugger.cpp installCode).

Refused (ReloadError, naming why) when the new compile does not keep the loaded object's layout: the classes (ids,
parents), the field slots of every class, the globals, the methods (class, name, parameter count) - saves, tasks and
the bindings rely on them. The game itself refuses a method a task has a frame in. The reloaded layout's sidecar is
written to %TEMP%/x3kc_dbg/<name>-<pid>.kcdbg; the game keeps its path (hello `sidecar`) so every later x3kc dbg command
reads it."""
import json
import os
import struct
import tempfile

from . import kcc
from . import kccache
from . import kcdbg
from . import kcgen
from . import kcisa
from . import kcobj
from . import kcparse
from . import kcpre
from .kcsym import NO_PARENT


class ReloadError(Exception):
    pass


def _relocatable(code, entry, end, new_names, old_entries, pool_text, old_index):
    """the bytes of a method of the new code [entry, end): jumps made relative to its start, calls the loaded
    object's entries, strings as relocations. -> (bytes, jumps, strings, as_loaded) where as_loaded is the method as it
    would be at the loaded entry with the loaded object's string offsets (None when a string is not in its pool)"""
    out = bytearray(code[entry:end])
    jumps, strings = [], []
    missing = False
    for i in kcisa.walk(code, entry, end):
        p = i.pc - entry
        f = kcisa.FMT.get(i.op)
        if f == 't':
            struct.pack_into('>I', out, p + 1, i.arg - entry)
            jumps.append(p + 1)
        elif f == 'c':
            key = new_names.get(i.arg)
            if key not in old_entries:
                raise ReloadError('a call of a method the loaded object lacks (%s)' % (key,))
            struct.pack_into('>I', out, p + 1, old_entries[key])
        elif f in ('s', 'n'):
            text = pool_text(i.arg)
            strings.append([p + 1, text])
            struct.pack_into('>I', out, p + 1, 0)
        elif i.op == 0x78:
            base, default, tg = i.cases
            struct.pack_into('>i', out, p + 7, default - entry)
            jumps.append(p + 7)
            for k, t in enumerate(tg):
                struct.pack_into('>I', out, p + 11 + 4 * k, t - entry)
                jumps.append(p + 11 + 4 * k)
        elif i.op == 0x79:
            ni, ns = i.arg
            default, ints, strs = i.cases
            struct.pack_into('>I', out, p + 5, default - entry)
            jumps.append(p + 5)
            for k, (key, t) in enumerate(ints + strs):
                q = p + 9 + 8 * k
                if k >= ni:
                    strings.append([q, pool_text(key)])
                    struct.pack_into('>I', out, q, 0)
                struct.pack_into('>I', out, q + 4, t - entry)
                jumps.append(q + 4)
    return bytes(out), jumps, strings


def _pushed_strings(code, entry, end, pool_text):
    return [pool_text(i.arg) for i in kcisa.walk(code, entry, end) if i.op == 0x0b]


def _heap_string_method(f, fns, pool, collected, new_names, old_entries, pool_text):
    """f compiled again with its string literals as heap strings (kcsnip._heap_strings); its sidecar entry (lines,
    vars, frame) replaced by the new code's. -> (relocatable bytes, jumps, strings, frame)"""
    from .kcsnip import _heap_strings
    _heap_strings(f.ast.body)
    g = kcgen.FuncGen(f.ast, f.entry, kcgen.Env(pool, {(x.cls, x.name): x.entry for x in fns}))
    g.function()
    body = g.resolve()
    buf = bytes(f.entry) + body
    rel, jumps, strings = _relocatable(buf, f.entry, f.entry + len(body), new_names, old_entries, pool.text, None)
    cm = [x for x in collected['methods'] if x['entry'] == f.entry][0]
    rows = {at: row for (_, at), row in zip(f.dbg.lines, cm['lines'])}
    cm['lines'] = [[pc] + list(rows[at][1:]) for pc, at in g.lines if at in rows]
    n = g.nparams
    cm['vars'] = [[v[0], v[1], n + 1 - v[2], v[3], v[4]] for v in g.dbg_vars]
    cm['frame'] = g.frame
    return rel, jumps, strings, g.frame


def _as_loaded(rel, jumps, strings, at, old_index):
    b = bytearray(rel)
    for p in jumps:
        struct.pack_into('>I', b, p, struct.unpack_from('>I', b, p)[0] + at)
    for p, text in strings:
        off = old_index.get(text)
        if off is None or (off == 0 and text == ''):
            return None
        struct.pack_into('>I', b, p, off)
    return bytes(b)


def plan(info, obj, src, what, objpath):
    """the methods to reload: [{entry, end (the loaded method), frame, code (hex), jumps, strings, name, new}] plus
    what write_sidecar needs"""
    old = kcobj.load(objpath)
    # the methods the game runs reloaded now (the current sidecar a reload wrote): sent again even when the source is
    # back to the loaded object's (a revert)
    running = {(m.cls or 0, m.name) for m in info.methods if getattr(m, 'reloaded', False)}
    previous = _raw(info.path) if running and info.path else None    # (the reloaded layout's sidecar now used)
    info_path = info.path if running else None
    base_side = kcdbg.sidecar_path(objpath)
    base_raw = _raw(base_side) if os.path.isfile(base_side) else None
    if os.path.isfile(base_side):
        base = kcdbg.DebugInfo.load(base_side)  # (the loaded object's own sidecar: its entries, not a reload's)
        if base.matches(obj['size'], obj['crc32']):
            info = base
    target = 'x3_src-' + ('fl' if 'fl' in os.path.normpath(src).lower().split(os.sep) else 'ap')
    cache = kccache.Cache(os.path.join(tempfile.gettempdir(), 'x3kc_dbg', 'cache'))
    try:
        o, pool, fns = kcc.compile_source(src, cache, {}, quiet=True, target=target)
    except (kcc.CompileError, kcparse.ParseError, kcpre.PreError) as x:
        raise ReloadError('compile: %s' % x)
    syms = o.unit.syms
    # the layout: classes, fields, globals, methods
    why = []
    new_classes = {cid for cid in syms.classes if cid != 0}
    if new_classes != set(info.classes):
        why.append('classes added or removed: %s' % sorted(new_classes ^ set(info.classes)))
    for cid in sorted(new_classes & set(info.classes)):
        k = syms.classes[cid]
        parent = None if k.parent == NO_PARENT else k.parent
        if parent != info.classes[cid].get('parent'):
            why.append('class %s: another parent' % k.name)
        fields = []
        for c in reversed(syms.chain(cid)):
            fields += [fd.name for fd in syms.classes[c].fields]
        if fields != info.classes[cid]['fields']:
            why.append('class %s: its field slots changed' % k.name)
    if [fd.name for fd in syms.classes[0].fields] != info.globals:
        why.append('the globals changed')
    new_methods = {(f.cls, f.name): f for f in fns}
    old_methods = {(m.cls or 0, m.name): m for m in info.methods}
    if set(new_methods) != set(old_methods):
        why.append('methods added or removed: %s' % sorted('%s::%s' % k for k in set(new_methods) ^ set(old_methods)))
    for key in set(new_methods) & set(old_methods):
        if len(new_methods[key].params) != len(old_methods[key].params):
            why.append('%s::%s: its parameter count changed' % key)
    if why:
        raise ReloadError('the new compile changes the loaded object\'s layout (a reload keeps classes, field slots, '
                          'globals and methods): ' + '; '.join(why[:8]))
    ends = {}
    order = sorted(fns, key=lambda f: f.entry)
    for i, f in enumerate(order):
        ends[f.entry] = order[i + 1].entry if i + 1 < len(order) else len(o.code)
    new_names = {f.entry: (f.cls, f.name) for f in fns}
    old_entries = {k: m.entry for k, m in old_methods.items()}
    old_index = kcgen.Pool(old.strings).index
    collected = o.collect_debug()
    files = collected['files']
    out = []
    identical = set()       # new-compile entries of methods whose bytes are the loaded object's
    for f in order:
        m = old_methods[(f.cls, f.name)]
        rel, jumps, strings = _relocatable(o.code, f.entry, ends[f.entry], new_names, old_entries, o.string,
                                           old_index)
        loaded = _as_loaded(rel, jumps, strings, m.entry, old_index)
        same = loaded == bytes(old.code[m.entry:m.end])
        if same:
            identical.add(f.entry)
        if same and (f.cls, f.name) not in running:
            continue
        frame = f.frame_size
        if any(old_index.get(i_text) is None for i_text in _pushed_strings(o.code, f.entry, ends[f.entry], o.string)):
            # a literal the loaded object's pool lacks: compiled as a heap string ("text" + ""), so no pool string of
            # the game's tail reaches the game's state (a save stores pool strings by offset)
            rel, jumps, strings, frame = _heap_string_method(f, fns, pool, collected, new_names, old_entries, o.string)
        name = '%s::%s' % (m.cls_name, m.name) if m.cls else m.name
        if what:
            nm = [x for x in collected['methods'] if x['entry'] == f.entry][0]
            fname = files[nm['file']]
            w = what.replace('\\', '/').lower()
            if '::' in what or not what.lower().endswith('.kc'):
                if w != name.lower() and w != m.name.lower():
                    continue
            elif not (fname.replace('\\', '/').lower() == w or fname.replace('\\', '/').lower().endswith('/' + w) or
                      os.path.basename(fname).lower() == os.path.basename(w)):
                continue
        out.append({'entry': m.entry, 'end': m.end, 'frame': frame, 'code': rel.hex(), 'jumps': jumps,
                    'strings': strings, 'name': name, 'new': f.entry, 'revert': same,
                    'body': _body(rel, strings)})
    return {'methods': out, 'collected': collected, 'old_entries': {f.entry: old_methods[(f.cls, f.name)].entry
                                                                     for f in fns},
            'old_ends': {f.entry: old_methods[(f.cls, f.name)].end for f in fns}, 'build': info.build,
            'identical': identical, 'previous': previous, 'base': base_raw,
            'previous_path': info_path,
            'keys': {f.entry: (f.cls, f.name) for f in fns}}


def _body(rel, strings):
    """what a reloaded method reaches (kcsnip.CallGraph's body): [natives, forks, entries called, names called]"""
    text = {p: s for p, s in strings}
    natives, fork, entries, names = set(), False, set(), set()
    for i in kcisa.walk(rel, 0):
        if i.op == 0x82:
            natives.add(text.get(i.pc + 1, ''))
        elif i.op == 0x6f:
            fork = True
        elif i.op in (0x84, 0x86, 0x88):
            entries.add(i.arg)
        elif i.op in (0x85, 0x87):
            names.add(text.get(i.pc + 1, ''))
    return [sorted(natives), fork, sorted(entries), sorted(names)]


def bodies_path(sidecar):
    return sidecar + '.calls.json'


def _raw(path):
    with kcdbg._open_text(path) as h:
        return json.load(h)


def sidecar_path(pid, obj, serial):
    d = os.path.join(tempfile.gettempdir(), 'x3kc_dbg')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, '%s-%d-r%d.kcdbg' % (obj['name'], pid, serial))


def write_sidecar(path, plan_, table, obj, serial):
    """the reloaded layout's sidecar, from the game's whole reload table ({entry, placement, size} of every method it
    runs reloaded, hello `reloads` after this reload): a method reloaded now gets the new compile's lines at its
    placement; one reloaded before keeps its entry of the previous reload sidecar; one the game runs as loaded gets the
    new compile's lines when its bytes are the loaded ones, else the loaded object's own sidecar entry. The identity is
    the loaded object's, `reload` the serial (the game log's `reload N` line)"""
    d = plan_['collected']
    where = {t['entry']: t for t in table}
    sent = {m['new'] for m in plan_['methods']}
    keyed = lambda side: {(m['class'], m['name']): m for m in side['methods']} if side else {}
    previous, base = keyed(plan_.get('previous')), keyed(plan_.get('base'))
    methods = []
    for m in d['methods']:
        new = m['entry']
        key = plan_['keys'][new]
        origin = plan_['old_entries'][new]
        if new not in sent and origin in where and key in previous:
            methods.append(dict(previous[key]))                 # reloaded before, as it was written then
            continue
        if new not in sent and new not in plan_['identical'] and key in base:
            x = dict(base[key])                                 # changed in the source, not reloaded: as loaded
            x['origin'] = origin
            methods.append(x)
            continue
        x = dict(m)
        at, end = origin, plan_['old_ends'][new]
        if origin in where:
            p = where[origin]
            at = p['placement']
            if p['placement'] != origin:
                end = at + p['size']
            x['reloaded'] = True
        delta = at - new
        x['origin'] = origin
        x['entry'], x['end'] = at, end
        x['lines'] = [[r[0] + delta] + list(r[1:]) for r in m['lines']]
        x['vars'] = [[v[0], v[1], v[2], v[3] + delta, v[4] + delta] for v in m['vars']]
        methods.append(x)
    # the bodies of the methods the game runs reloaded (the call graph of later snippets: kcsnip.CallGraph.overlay)
    bodies = {}
    prev = plan_.get('previous_path')
    if prev and os.path.isfile(bodies_path(prev)):
        with open(bodies_path(prev), encoding='utf-8') as h:
            bodies = json.load(h)
    for m in plan_['methods']:
        bodies[str(m['entry'])] = m['body']
    bodies = {e: b for e, b in bodies.items() if int(e) in where}
    with open(bodies_path(path), 'w', encoding='utf-8') as h:
        json.dump(bodies, h)
    side = dict(d)
    side['object'] = {'name': obj['name'], 'build': plan_.get('build'), 'size': obj['size'], 'crc32': obj['crc32'],
                      'reload': serial}
    side['methods'] = methods
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as h:
        json.dump(side, h, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)