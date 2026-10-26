"""Decompile a KC object (x3story.obj ...) into a source project: <name>.kc, <name>.pool, <name>.ptch.

    x3kc decompile <obj> <outdir> [--name NAME] [--asm-all]

Every method is decompiled, regenerated from its AST and compared with the original bytes; a method that does not
match (or cannot be decompiled or printed) is written as `asm { }` so the project always rebuilds the same object.
"""
import argparse
import datetime
import os
import struct
import sys
from collections import Counter

from . import annotations
from . import kcast as A
from . import kcdec
from . import kcgen
from . import kcisa
from . import kcobj
from . import kcnames
from . import kcprint
from .kcsym import Field, Klass, Method, Symbols, NO_PARENT


def build_symbols(o):
    S = Symbols()
    g = Klass(0, '', NO_PARENT, None)
    for r in o.symb:
        g.methods.append(Method(o.string(r[1]), r[3], r[2], entry=r[0]))
    for r in o.vars:
        g.fields.append(Field('g%d' % r[0], r[2], r[1], r[3]))
    S.add(g)
    for c in o.classes:
        k = Klass(c.id, 'C%d' % c.id, c.parent, c.name)
        for r in c.methods:
            k.methods.append(Method(o.string(r[1]), r[3], r[2], entry=r[0]))
        k.fields = [Field(None, r[2], r[1], r[3]) for r in c.fields]
        S.add(k)
    for k in S.classes.values():
        if k.id:
            base = S.inherited(k.id)
            for j, f in enumerate(k.fields):
                f.name = 'm%d' % (base + j)
    return S


def s32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def field_text(o, f, default_stamp, doc=None):
    t, v = f.type, f.value
    if t == 1:
        init = ' = %d' % s32(v)
    elif t == 2 and v != 0 and o.string(v) is not None:     # offset 0 reads as "SB_String(0)", not ""
        init = ' = ' + kcprint.esc(o.string(v))
    elif t == 8 and v == 0:
        init = ' = {}'
    elif t == 9 and v == 0:
        init = ' = {:}'
    elif t == 0 and v == 0:
        init = ''
    else:
        init = ' = __init(%d, %d)' % (t, s32(v))
    st = '' if f.stamp == default_stamp else ' @0x%08x' % f.stamp
    tail = '  // ' + ' '.join(doc.split()) if doc else ''
    return 'var %s%s%s;%s' % (f.name, init, st, tail)


class AsmLinesError(Exception):
    pass


def asm_lines(o, code, entry, end, meth):
    """Instructions after `frame` as asm items ('label', name) / ('line', text)."""
    ins = list(kcisa.walk(code, entry, end))[1:]
    starts = {i.pc for i in ins}
    targets = set()
    for i in ins:
        targets.update(i.targets())
    bad = sorted(t for t in targets if t not in starts)
    if bad:
        raise AsmLinesError('jump into the middle of an instruction or out of the method (%06x)' % bad[0])
    lab = lambda t: 'L%04x' % (t - entry)

    def string(off):
        s = o.string(off)
        return str(off) if s is None else kcprint.esc(s)     # not a string start: the raw pool offset
    items = []
    for i in ins:
        if i.pc in targets:
            items.append(('label', lab(i.pc)))
        mn = kcisa.MN[i.op]
        f = kcisa.FMT.get(i.op)
        if f in ('b', 'h', 'H', 'i'):
            a = str(i.arg)
        elif f == 's':
            a = string(i.arg)
        elif f == 'n':
            a = o.string(i.arg)
            if a is None or not kcprint.re.fullmatch(r'[A-Za-z_]\w*', a):
                raise AsmLinesError('name operand at %06x is not an identifier in the pool' % i.pc)
        elif f == 't':
            a = lab(i.arg)
        elif f == 'c':
            if i.arg not in meth:
                raise AsmLinesError('call at %06x to %06x, which is no method entry' % (i.pc, i.arg))
            cid, name = meth[i.arg]
            a = ('::%s' % name) if cid == 0 else 'C%d::%s' % (cid, name)
        elif f == 'd':
            a = '0x' + i.arg.hex()
        elif i.op == 0x78:
            base, d, tg = i.cases
            a = '%d %s [%s]' % (base, lab(d), ', '.join(lab(t) for t in tg))
        elif i.op == 0x79:
            d, ints, strs = i.cases
            a = '%s {%s} {%s}' % (lab(d), ', '.join('%d: %s' % (k, lab(t)) for k, t in ints),
                                  ', '.join('%s: %s' % (string(k), lab(t)) for k, t in strs))
        else:
            a = ''
        items.append(('line', ('%s %s' % (mn, a)).rstrip()))
    return items


def pool_text(o):
    lines = ['// string pool of the object, in order (offsets are kept: saves store string offsets)']
    tab = o.string_table()
    for off in sorted(tab):
        lines.append(kcprint.esc(tab[off]))
    if o.strings and o.strings[-1] != 0:
        lines.append('// no-terminator')
    return '\n'.join(lines) + '\n'


def ptch_text(o):
    lines = ['// PTCH: for saves of older builds, return address in that build -> address in this build (0 = gone)']
    for build, pairs in o.ptch:
        lines.append('build 0x%08x %d' % (build, len(pairs)))
        for a, b in pairs:
            lines.append('%x %x' % (a, b))
    return '\n'.join(lines) + '\n'


def bundled_names(name):
    """data/<object>.names.json shipped with x3kc (x3story, x3galedit, x3intro)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', name.lower() + '.names.json')
    if not os.path.exists(p):
        print('names: no bundled names for %s' % name)
        return None
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('obj')
    ap.add_argument('outdir')
    ap.add_argument('--name')
    ap.add_argument('--asm-all', action='store_true', help='write every method as asm')
    ap.add_argument('--names', help='annotations (JSON, working or portable form; see kcnames.py and '
                                    'annotations.py), or "auto": the bundled names for this object, if any')
    ap.add_argument('--pcs', action='store_true', help='mark every statement with its code offset `// @xxxxxx`')
    a = ap.parse_args()
    raw = open(a.obj, 'rb').read()
    o = kcobj.KcObj(raw)
    name = a.name or os.path.splitext(os.path.basename(a.obj))[0]
    os.makedirs(a.outdir, exist_ok=True)
    S = build_symbols(o)
    report = []
    names = bundled_names(name) if a.names == 'auto' else a.names
    ann = kcnames.load(names) if names else {'classes': {}, 'globals': {}}
    ann = annotations.resolve(o, ann, report)
    cdoc, fdoc = kcnames.apply(S, ann, report)
    stamps = Counter([c.name for c in o.classes] + [r[3] for r in o.vars] +
                     [r[3] for c in o.classes for r in c.fields])
    default_stamp = stamps.most_common(1)[0][0] if stamps else 0

    rows = []
    for r in o.symb:
        rows.append((r[0], 0, r))
    for c in o.classes:
        for r in c.methods:
            rows.append((r[0], c.id, r))
    rows.sort()
    by_addr = {e: (cid, o.string(r[1])) for e, cid, r in rows}
    env = kcgen.Env(kcgen.Pool(o.strings), {v: k for k, v in by_addr.items()})
    ends = {e: (rows[k + 1][0] if k + 1 < len(rows) else len(o.code)) for k, (e, _, _) in enumerate(rows)}

    pr = kcprint.Printer(S, pcs=a.pcs)
    stats = Counter()

    def emit_function(e, cid, r, d):
        end = ends[e]
        mname = o.string(r[1])
        params = ['arg%d' % (k + 1) for k in range(r[3])]
        if not a.asm_all:
            try:
                fn = kcdec.decompile(o.code, e, end, r[3], by_addr, o.string)
                g = kcgen.FuncGen(fn, e, env).function()
                if g.resolve() != o.code[e:end]:
                    raise kcdec.DecompileError('regenerated bytes differ')
                ma = kcnames.method_ann(ann, cid, mname)
                ps, ls = kcnames.function_names(S, cid, r[3], ma, report, '%d::%s' % (cid, mname))
                ma['params'], ma['locals'] = ps, ls
                pr.function(fn, cid, mname, e, d, kcgen.frame_of(fn), ma)
                stats['source'] += 1
                return
            except (kcdec.DecompileError, kcgen.GenError, kcprint.PrintError) as x:
                stats['asm: ' + str(x).split(' at ')[0]] += 1
            except Exception as x:      # an unexpected input: fall back to asm, but say what happened
                stats['asm: %s %s' % (type(x).__name__, str(x).split(' at ')[0])] += 1
        try:
            items = asm_lines(o, o.code, e, end, by_addr)
        except AsmLinesError as x:
            raise SystemExit('%d::%s at %06x cannot be written as source or asm: %s' % (cid, mname, e, x))
        pr.asm_function(mname, params, r[2], e, items, d)
        stats['asm'] += 1

    L = pr.lines
    built = datetime.datetime.fromtimestamp(o.build, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    L.append('// %s: KC source decompiled by kcd.py (build %d, %s)' % (os.path.basename(a.obj), o.build, built))
    L.append('// rebuild: x3kc compile %s.kc -o %s.obj' % (name, name))
    L.append('#object %d %d' % (o.version, o.build))
    L.append('#sections %s' % ' '.join(o.order))
    L.append('#pool %s.pool' % name)
    if o.ptch or 'PTCH' in o.order:
        L.append('#patch %s.ptch' % name)
    L.append('#stamp 0x%08x' % default_stamp)
    if o.code[:1] != b'\0':
        raise SystemExit('CODE does not start with the 0 byte')
    L.append('')
    L.append('// ---- global variables (class 0 fields)')
    for j, f in enumerate(S.classes[0].fields):
        L.append(field_text(o, f, default_stamp, fdoc.get((0, j))))

    clas_order = [c.id for c in o.classes]
    emitted = set()
    first_entry = {}
    for e, cid, r in rows:
        first_entry.setdefault(cid, e)

    def emit_class(cid):
        k = S.classes[cid]
        c = next(x for x in o.classes if x.id == cid)
        L.append('')
        par = '' if c.parent == NO_PARENT else ' : ' + S.class_name(c.parent)
        st = '' if c.name == default_stamp else ' @0x%08x' % c.name
        if cid in cdoc:
            pr.comment(0, cdoc[cid])
        nm = k.name if k.name == 'C%d' % cid else '%s = %d' % (k.name, cid)
        L.append('class %s%s%s {' % (nm, par, st))
        for j, f in enumerate(k.fields):
            L.append('    ' + field_text(o, f, default_stamp, fdoc.get((cid, j))))
        for r in c.methods:
            L.append('')
            emit_function(r[0], cid, r, 1)
        L.append('}')
        emitted.add(cid)

    def emit_methodless_after(pos):
        q = pos
        while q < len(clas_order) and not next(x for x in o.classes if x.id == clas_order[q]).methods:
            emit_class(clas_order[q])
            q += 1

    emit_methodless_after(0)
    last_global = None
    for e, cid, r in rows:
        if cid == 0:
            L.append('')
            emit_function(e, 0, r, 0)
        elif cid not in emitted:
            emit_class(cid)
            emit_methodless_after(clas_order.index(cid) + 1)
    for cid in clas_order:
        if cid not in emitted:
            emit_class(cid)

    files = {name + '.kc': '\n'.join(L) + '\n', name + '.pool': pool_text(o)}
    if o.ptch or 'PTCH' in o.order:
        files[name + '.ptch'] = ptch_text(o)
    data = {f: t.encode('latin1') for f, t in files.items()}      # all encoded before any file is opened
    for f, b in data.items():
        with open(os.path.join(a.outdir, f), 'wb') as h:
            h.write(b)
    for k, v in stats.most_common():
        print('%6d  %s' % (v, k))
    for line in report:
        print('names:', line)
    for line in pr.unplaced:
        print('names: comment not placed (no statement starts there):', line)


if __name__ == '__main__':
    sys.exit(main())
