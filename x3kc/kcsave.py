"""Save-game compatibility of a modified object with saves made on a base object.

How the game uses it (Script::LoadVmState 0x004a0880):
  * a save stores the build id of the object; when it differs from the loaded object's build, the PTCH record of
    that build is looked up; no record -> the save is refused;
  * a PTCH record is a piecewise shift of code offsets: sorted pairs (old, new); a pc uses the last pair with
    old <= pc and becomes pc + (new - old); new == 0 marks the range as gone; pcs below the first pair stay;
    this is applied to the pc of every task and to every return address (tag 3 cell) on its stack, and the result
    must again follow a native call (task pc) or a call instruction (return address);
  * a field (class field or global) whose stamp is newer than the save's build is not read from the save: it keeps
    its initial value. So fields may be added at the end of a class, never inserted or removed.
So a modified object needs: a new build id, the new build as stamp of every added field and class, a PTCH record
for the base build (base offsets -> new offsets), and every older record re-targeted to the new offsets.
"""
import difflib
import time

from . import kcisa

BIG = 1 << 32


def method_spans(o):
    rows = [(r[0], 0, r) for r in o.symb] + [(r[0], c.id, r) for c in o.classes for r in c.methods]
    rows.sort()
    out = {}
    for k, (e, cid, r) in enumerate(rows):
        end = rows[k + 1][0] if k + 1 < len(rows) else len(o.code)
        out[(cid, o.string(r[1]))] = (e, end)
    return out


def insn_keys(o, entry, end, names):
    """Instructions of a method as comparable keys: jump targets relative to the entry, calls by method,
    strings and names by text."""
    keys = []
    for i in kcisa.walk(o.code, entry, end):
        f = kcisa.FMT.get(i.op)
        if f == 't':
            a = None                    # targets move with any insertion; the opcode is enough to align
        elif f == 'c':
            a = names.get(i.arg)
        elif f in ('s', 'n'):
            a = o.string(i.arg)
        elif i.op == 0x78:
            a = (i.cases[0], len(i.cases[2]))
        elif i.op == 0x79:
            d, ints, strs = i.cases
            a = (tuple(k for k, _ in ints), tuple(o.string(k) for k, _ in strs))
        else:
            a = i.arg
        keys.append((i.pc, (i.op, a)))
    return keys


def code_map(base, new):
    """Segments [(old_start, delta or None)] mapping base code offsets to new ones, method by method;
    instructions inside changed parts of a method map to None (gone)."""
    bs, ns = method_spans(base), method_spans(new)
    bnames = {e: k for k, (e, _) in bs.items()}
    nnames = {e: k for k, (e, _) in ns.items()}
    segs = [(0, 0)]
    for key in sorted(bs, key=lambda k: bs[k][0]):
        be, bend = bs[key]
        if key not in ns:
            segs.append((be, None))
            continue
        ne, nend = ns[key]
        a = insn_keys(base, be, bend, bnames)
        b = insn_keys(new, ne, nend, nnames)
        segs.append((be, None))
        sm = difflib.SequenceMatcher(None, [k for _, k in a], [k for _, k in b], autojunk=False)
        blocks = [blk for blk in sm.get_matching_blocks() if blk[2]]
        for i, j, n in blocks:
            # a saved pc is always the address after an instruction (a return address, or a task pc after a
            # native): the start of instruction k belongs to instruction k-1, so it moves with k-1's block
            d = b[j][0] - a[i][0]
            if i == 0:
                segs.append((a[0][0], d))       # the method entry
            segs.append((a[i][0] + 1, d))
            after = a[i + n][0] if i + n < len(a) else bend
            segs.append((after, d))
            segs.append((after + 1, None))      # overwritten when the next block starts right there
    segs.append((len(base.code), None))
    return normalize(segs)


def insn_map(base, new):
    """{base instruction offset: new offset} for instructions that match exactly (same method, aligned)."""
    bs, ns = method_spans(base), method_spans(new)
    bnames = {e: k for k, (e, _) in bs.items()}
    nnames = {e: k for k, (e, _) in ns.items()}
    out = {}
    for key, (be, bend) in bs.items():
        if key not in ns:
            continue
        a = insn_keys(base, be, bend, bnames)
        b = insn_keys(new, ns[key][0], ns[key][1], nnames)
        sm = difflib.SequenceMatcher(None, [k for _, k in a], [k for _, k in b], autojunk=False)
        for i, j, n in sm.get_matching_blocks():
            for t in range(n):
                out[a[i + t][0]] = b[j + t][0]
    return out


def normalize(segs):
    segs = sorted(dict(segs).items())
    out = []
    for s, d in segs:
        if out and out[-1][1] == d:
            continue
        out.append((s, d))
    return out


def lookup(segs, pc):
    lo, hi = 0, len(segs)
    while lo < hi:
        m = (lo + hi) // 2
        if segs[m][0] <= pc:
            lo = m + 1
        else:
            hi = m
    return segs[lo - 1][1] if lo else 0


def pairs_to_segs(pairs):
    segs = [(0, 0)]
    for o, n in pairs:
        segs.append((o, None if n == 0 else n - o))
    return normalize(segs)


def segs_to_pairs(segs):
    out = []
    for s, d in segs:
        if s == 0 and d == 0:
            continue
        n = 0 if d is None else s + d
        if d is not None and n == 0:
            n = 0
        out.append((s, n))
    return out


def compose(first, then):
    """Segments of then(first(pc))."""
    out = []
    bounds = [s for s, _ in first] + [BIG]
    tb = [s for s, _ in then]
    for (a, d), b in zip(first, bounds[1:]):
        if d is None:
            out.append((a, None))
            continue
        cuts = [a] + [m - d for m in tb if a + d < m < b + d]
        for x in cuts:
            d2 = lookup(then, x + d)
            out.append((x, None if d2 is None else d + d2))
    return normalize(out)


def new_build_id(base_build):
    return max(int(time.time()), base_build + 1)


def make_compatible(o, base, build, explicit_stamps):
    """Adjust the compiled object o for saves made with base: build id, stamps of added fields/classes, PTCH.
    explicit_stamps(kind, cid, index) -> True when the source gave that stamp itself."""
    report = []
    o.build = build
    bcls = {c.id: c for c in base.classes}
    for c in o.classes:
        b = bcls.get(c.id)
        if b is None:
            if not explicit_stamps('class', c.id, None):
                c.name = build
            report.append('new class %d' % c.id)
            for j, r in enumerate(c.fields):
                if not explicit_stamps('field', c.id, j):
                    r[3] = build
            continue
        for j, r in enumerate(c.fields):
            if j >= len(b.fields):
                if not explicit_stamps('field', c.id, j):
                    r[3] = build
                report.append('new field %d of class %d' % (j, c.id))
            elif r[2] != b.fields[j][2] or (r[2] != 2 and (r[1] & 0xffffffff) != b.fields[j][1]):
                report.append('WARNING: field %d of class %d changed its initial value (old saves keep theirs)'
                              % (j, c.id))
        if len(c.fields) < len(b.fields):
            report.append('WARNING: class %d lost fields: old saves will not load' % c.id)
        if c.parent != b.parent:
            # inherited fields come first: a new parent shifts every field of the class
            report.append('WARNING: class %d changed its parent: old saves will not load' % c.id)
    for cid in sorted(set(bcls) - {c.id for c in o.classes}):
        report.append('WARNING: class %d was removed: old saves with its objects will not load (an object of it '
                      'crashes LoadVmState, static fields of it make the game refuse the save)' % cid)
    report.extend(lost_inits(o, base))
    for j, r in enumerate(o.vars):
        if j >= len(base.vars) and not explicit_stamps('field', 0, j):
            r[3] = build
    if len(o.vars) < len(base.vars):
        report.append('WARNING: global variables removed: old saves will not load')
    m = code_map(base, o)
    ptch = []
    for b, pairs in base.ptch:          # older saves were mapped to the base offsets: re-target them
        if b == build:
            continue
        ptch.append((b, segs_to_pairs(compose(pairs_to_segs(pairs), m))))
    if base.build != build:
        ptch = [p for p in ptch if p[0] != base.build]
        ptch.append((base.build, segs_to_pairs(m)))
    o.ptch = ptch
    if 'PTCH' not in o.order:
        o.order.insert(o.order.index('STRG') if 'STRG' in o.order else len(o.order), 'PTCH')
    gone = sum(1 for _, d in m if d is None)
    report.append('PTCH for base build %d: %d pairs (%d ranges gone)' % (base.build, len(segs_to_pairs(m)), gone))
    return report


def lost_inits(o, base):
    """B19: LoadVmState walks the levels of an object's class from the root down and leaves the walk after a level
    that is not the object's own class once the saved fields are used up (0x004a15c4). Stamped (new) fields on the
    levels below the last level with saved fields keep null instead of their initial value. Run after stamping."""
    cls = {c.id: c for c in o.classes}
    out = []
    for c in o.classes:
        if c.id not in {b.id for b in base.classes}:
            continue                    # no old object of a new class
        chain, k = [], c
        while k is not None and k.id not in [x.id for x in chain]:
            chain.append(k)
            k = cls.get(k.parent)
        chain.reverse()
        last = 0
        for n, k in enumerate(chain):
            if any((r[3] & 0xffffffff) <= base.build for r in k.fields):
                last = n
        for k in chain[last + 1:]:
            new = [j for j, r in enumerate(k.fields) if (r[3] & 0xffffffff) > base.build]
            if new:
                out.append('WARNING: objects of class %d from old saves get null, not the initial value, in the new '
                           'fields %s of class %d (B19: LoadVmState stops at class %d, the last level with saved '
                           'fields); add the fields to a class that already has fields, or set them in code'
                           % (c.id, ','.join(map(str, new)), k.id, chain[last].id))
    return out


def frames(o):
    """Sorted [(entry, end, (frame, nparams))] of every method: a suspended task keeps that many stack cells."""
    rows = sorted([r for r in o.symb] + [r for c in o.classes for r in c.methods])
    return [(r[0], rows[k + 1][0] if k + 1 < len(rows) else len(o.code), (r[2], r[3])) for k, r in enumerate(rows)]


def frame_at(fr, pc):
    lo, hi = 0, len(fr)
    while lo < hi:
        m = (lo + hi) // 2
        if fr[m][0] <= pc:
            lo = m + 1
        else:
            hi = m
    return fr[lo - 1][2] if lo else None


CALLS = (0x82, 0x84, 0x85, 0x86, 0x87, 0x88)


def load_sync(path=None):
    """Natives that always answer at once (sync_natives.txt): no task ever waits in them."""
    import os
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync_natives.txt')
    return {l.strip() for l in open(path, encoding='utf-8') if l.strip() and not l.startswith('#')}


def load_known():
    """Every native name the exe has (natives.txt); a call to any other name answers int 0 at once."""
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'natives.txt')
    return {l.strip() for l in open(p, encoding='utf-8') if l.strip() and not l.startswith('#')}


def waiting_sites(o, sync=None, known=None):
    """Call sites of o at which a task can be suspended, i.e. whose return address can be in a savegame:
    natives that may wait (not in `sync`; unknown names answer at once) and calls of methods that can reach one,
    transitively (a call by name counts when any method of that name can). Returns the set of instruction ends."""
    sync = load_sync() if sync is None else sync
    known = load_known() if known is None else known
    spans = method_spans(o)
    by_entry = {e: (e, end) for _, (e, end) in spans.items()}
    by_name = {}
    for (cid, name), (e, _) in spans.items():
        by_name.setdefault(name, []).append(e)
    calls = {}          # method entry -> [(insn end, kind, target)]
    for e, end in by_entry.values():
        lst = []
        for i in kcisa.walk(o.code, e, end):
            if i.op == 0x82:
                n = o.string(i.arg)
                lst.append((i.end, 'native', n in known and n not in sync))
            elif i.op in (0x85, 0x87):
                lst.append((i.end, 'name', o.string(i.arg)))
            elif i.op in (0x84, 0x86, 0x88):
                lst.append((i.end, 'entry', i.arg))
        calls[e] = lst
    waits = set()
    changed = True
    while changed:
        changed = False
        for e, lst in calls.items():
            if e in waits:
                continue
            for _, kind, t in lst:
                if (kind == 'native' and t) or (kind == 'entry' and t in waits) or \
                        (kind == 'name' and any(x in waits for x in by_name.get(t, ()))):
                    waits.add(e)
                    changed = True
                    break
    sites = set()
    for e, lst in calls.items():
        for end, kind, t in lst:
            if (kind == 'native' and t) or (kind == 'entry' and t in waits) or \
                    (kind == 'name' and any(x in waits for x in by_name.get(t, ()))):
                sites.add(end)
    return sites


def check_return_sites(base, new, segs, sync=None):
    """Every return site of base where a task can be suspended (waiting_sites) must map to the same kind of site in
    new (or to nothing), inside a method with the same frame: the saved stack holds the old number of locals, so an
    added or removed local shifts every local of a suspended call. Calls that always come back at once (synchronous
    natives, methods that never wait) are never in a savegame and are not checked."""
    bad = ok = gone = 0
    live = waiting_sites(base, sync)
    bnames = {e: k for k, (e, _) in method_spans(base).items()}
    nnames = {e: k for k, (e, _) in method_spans(new).items()}

    def key(o, names, i):
        if i.op in (0x82, 0x85, 0x87):
            return o.string(i.arg)              # natives and calls by name
        if kcisa.FMT.get(i.op) == 'c':
            return names.get(i.arg, i.arg)      # direct calls: the callee
        return None
    nkeys = {}
    for i in kcisa.walk(new.code, 1):
        nkeys[i.end] = (i.op, key(new, nnames, i))
    bfr, nfr = frames(base), frames(new)
    for i in kcisa.walk(base.code, 1):
        if i.op not in CALLS or i.end not in live:
            continue
        d = lookup(segs, i.end)
        if d is None:
            gone += 1
            continue
        k = nkeys.get(i.end + d)
        if k is None or k[0] != i.op or k[1] != key(base, bnames, i) \
                or frame_at(bfr, i.end - 1) != frame_at(nfr, i.end + d - 1):
            bad += 1
        else:
            ok += 1
    return ok, gone, bad
