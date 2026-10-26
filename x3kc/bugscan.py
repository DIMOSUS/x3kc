"""Static scan of a KC object for code patterns that are often bugs; output: candidates for review.

    x3kc bugscan <obj> [--names auto|names.json] -o candidates.jsonl

Every candidate: {"id", "kind", "cls", "method", "entry", "pc", "note"}; pc is the offset of the statement
(as `kcd --pcs` prints it). The patterns (each one found a real bug in x3story):
  fallthrough      a non-empty switch section that does not end in break/return/continue runs into the next one
  discarded        the result of a getter-like call (Get/Is/Has/Find/Calc/Count...) is thrown away
  not-bitop        `!x & n` / `!x | n`: the not applies to x only
  cmp-bitop        a comparison combined with a constant by & | ^ (`a != b | c`)
  dead-store       a variable or field assigned twice in a row, the first value never read
  same-branches    if and else (or two else-if conditions) identical
  self-op          `x == x`, `x - x`, `x = x`, `a && a`
  random-minus-1   SE_Random(n - 1): if SE_Random(n) is 0..n-1, the last value is never chosen
  size-compare     `i > SE_ArraySize(a)` / `i <= SE_ArraySize(a)`: off by one for an index
  set-then-read    SetX(...) followed by reading field x in the next statement (the setter may have changed it)
  same-body        two differently named getters of a class with the same non-trivial body (copy-paste)
"""
import argparse
import json
import os
import re
from collections import defaultdict

from . import annotations
from . import kcast as A
from . import kcd
from . import kcdec
from . import kcnames
from . import kcobj
from . import kcprint

GETTER = re.compile(r'(Get|Is|Has|Find|Calc|Count|Num|Compute|Can|Check|Read|Query)[A-Z0-9_]')
CMP = {'==', '!=', '<', '>', '<=', '>='}
TERM = (A.Break, A.Return, A.Continue)


def walk(n):
    """Every node below n (expressions and statements)."""
    stack = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, (list, tuple)):
            stack.extend(x)
        elif isinstance(x, A.Node):
            yield x
            for k in x.__slots__:
                v = getattr(x, k)
                if isinstance(v, (A.Node, list, tuple)):
                    stack.append(v)


def call_name(e):
    if type(e) is A.Native:
        return e.name
    if type(e) is A.Call:
        return e.target[1] if isinstance(e.target, tuple) else e.target
    return None


def key(e):
    return repr(e)


def ends(stmts):
    if not stmts:
        return False
    s = stmts[-1]
    if isinstance(s, TERM):
        return True
    if type(s) is A.If and s.els is not None:
        return ends(s.then) and ends(s.els)
    if type(s) is A.Block:
        return ends(s.body)
    return False


class Scan:
    def __init__(self, S, cls, name, entry, fn, out):
        self.S, self.cls, self.name, self.entry, self.fn, self.out = S, cls, name, entry, fn, out
        self.pc = fn.stmt_pc or {}

    def add(self, kind, stmt, note):
        self.out.append({'kind': kind, 'cls': self.cls, 'method': self.name, 'entry': '%06x' % self.entry,
                         'pc': '%06x' % self.pc.get(id(stmt), self.entry), 'note': note})

    def field_name(self, e):
        if type(e) is A.Mem and self.cls:
            m = self.S.member(self.cls, e.k)
            return m[1].name if m else None
        return None

    def run(self):
        self.block(self.fn.body, None)

    def block(self, stmts, owner):
        for k, s in enumerate(stmts):
            self.stmt(s)
            nxt = stmts[k + 1] if k + 1 < len(stmts) else None
            if nxt is not None:
                self.pair(s, nxt)

    def pair(self, a, b):
        # dead store: x = e1; x = e2 (e2 does not read x, e1 has no call)
        if type(a) is A.ExprStmt and type(b) is A.ExprStmt and type(a.e) is A.Assign and type(b.e) is A.Assign:
            ta, tb = key(a.e.t), key(b.e.t)
            if ta == tb and ta not in key(b.e.v) and not any(call_name(x) for x in walk(a.e.v)):
                self.add('dead-store', a, 'assigned again by the next statement before any read')
        # setter then field read
        if type(a) is A.ExprStmt and call_name(a.e) and call_name(a.e).startswith('Set'):
            want = call_name(a.e)[3:].lower()
            for x in walk(b):
                n = self.field_name(x)
                if n and n.lower() == want:
                    self.add('set-then-read', b, '%s() just before reading field %s' % (call_name(a.e), n))
                    break

    def stmt(self, s):
        t = type(s)
        if t is A.ExprStmt:
            n = call_name(s.e)
            short = n.split('_', 1)[1] if n and re.match(r'[A-Z0-9]+_', n) else n
            if n and GETTER.match(short or ''):
                self.add('discarded', s, 'result of %s() is not used' % n)
            self.expr(s.e, s)
        elif t is A.VarDecl:
            if s.init is not None:
                self.expr(s.init, s)
        elif t is A.Block:
            self.block(s.body, s)
        elif t is A.If:
            self.expr(s.c, s)
            self.block(s.then, s)
            if s.els is not None:
                if s.then and key(s.then) == key(s.els):
                    self.add('same-branches', s, 'then and else are identical')
                conds = [key(s.c)]
                e = s.els
                while e and len(e) == 1 and type(e[0]) is A.If:
                    if key(e[0].c) in conds:
                        self.add('same-branches', e[0], 'else-if repeats an earlier condition')
                    conds.append(key(e[0].c))
                    e = e[0].els
                self.block(s.els, s)
        elif t in (A.While, A.DoWhile):
            self.expr(s.c, s)
            self.block(s.body, s)
        elif t is A.For:
            self.block(s.init, s)
            self.expr(s.c, s)
            self.block(s.step, s)
            self.block(s.body, s)
        elif t is A.Switch:
            self.expr(s.e, s)
            secs = s.sections
            for k, (labels, stmts) in enumerate(secs):
                self.block(stmts, s)
                if stmts and k + 1 < len(secs) and not ends(stmts):
                    lab = ', '.join('default' if type(c) is A.Default else kcprint.esc(c.v.s) if type(c.v) is A.Str
                                    else str(c.v.v) for c in labels)
                    nl = secs[k + 1][0]
                    nxt = ', '.join('default' if type(c) is A.Default else kcprint.esc(c.v.s)
                                    if type(c.v) is A.Str else str(c.v.v) for c in nl)
                    self.add('fallthrough', stmts[0], 'case %s falls through into case %s' % (lab, nxt))
        elif t is A.Return:
            if s.e is not None:
                self.expr(s.e, s)
        elif t is A.Fork:
            self.block(s.body, s)

    def expr(self, e, stmt):
        for x in walk(e):
            t = type(x)
            if t is A.Bin:
                if x.op in ('&', '|', '^') and type(x.a) is A.Un and x.a.op == '!' and type(x.b) is A.Int:
                    self.add('not-bitop', stmt, '`!x %s %d`: (!x) %s %d' % (x.op, x.b.v, x.op, x.b.v))
                if x.op in ('&', '|', '^') and (type(x.a) is A.Bin and x.a.op in CMP and type(x.b) is A.Int
                                                or type(x.b) is A.Bin and x.b.op in CMP and type(x.a) is A.Int):
                    self.add('cmp-bitop', stmt, 'a comparison combined with a constant by %s' % x.op)
                if x.op in CMP | {'-', '^', '/', '%'} and key(x.a) == key(x.b) and type(x.a) is not A.Int:
                    self.add('self-op', stmt, 'both operands of %s are the same' % x.op)
                for side, other, op in ((x.a, x.b, x.op), (x.b, x.a, {'>': '<', '<=': '>='}.get(x.op))):
                    if op in ('>', '<=') and type(other) is A.Native and other.name == 'SE_ArraySize':
                        self.add('size-compare', stmt, 'index compared with `%s SE_ArraySize(...)`' % op)
                        break
            elif t is A.Logic and len(x.items) > 1:
                ks = [key(i) for i in x.items]
                if len(set(ks)) < len(ks):
                    self.add('self-op', stmt, 'the same operand twice in %s' % x.op)
            elif t is A.Assign and key(x.t) == key(x.v):
                self.add('self-op', stmt, 'self-assignment')
            elif t is A.Native and x.name == 'SE_Random' and x.args and type(x.args[0]) is A.Bin \
                    and x.args[0].op == '-' and type(x.args[0].b) is A.Int and x.args[0].b.v == 1:
                self.add('random-minus-1', stmt, 'SE_Random(n - 1)')


def body_key(fn):
    """A method body without its own name, for copy-paste detection."""
    return repr(fn.body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('obj')
    ap.add_argument('--names', help='annotations (JSON), or "auto": the bundled names for this object')
    ap.add_argument('-o', '--out', required=True)
    a = ap.parse_args()
    o = kcobj.load(a.obj)
    S = kcd.build_symbols(o)
    names = kcd.bundled_names(os.path.splitext(os.path.basename(a.obj))[0]) if a.names == 'auto' else a.names
    ann = kcnames.load(names) if names else {'classes': {}, 'globals': {}}
    kcnames.apply(S, annotations.resolve(o, ann), [])
    rows = sorted([(r[0], 0, r) for r in o.symb] + [(r[0], c.id, r) for c in o.classes for r in c.methods])
    by_addr = {e: (cid, o.string(r[1])) for e, cid, r in rows}
    out = []
    bodies = defaultdict(list)
    failed = 0
    for k, (e, cid, r) in enumerate(rows):
        end = rows[k + 1][0] if k + 1 < len(rows) else len(o.code)
        name = o.string(r[1])
        try:
            fn = kcdec.decompile(o.code, e, end, r[3], by_addr, o.string)
        except kcdec.DecompileError:
            failed += 1
            continue
        Scan(S, cid, name, e, fn, out).run()
        if GETTER.match(name) and len(repr(fn.body)) > 60:
            bodies[(cid, body_key(fn))].append((name, e))
    # a discarded result is suspicious only when the callee does nothing else: drop calls of KC methods that
    # change state (assign a field/global/element or call anything but other getters); group the rest by callee
    effects = {}
    for k, (e, cid, r) in enumerate(rows):
        end = rows[k + 1][0] if k + 1 < len(rows) else len(o.code)
        name = o.string(r[1])
        fn = kcdec.decompile(o.code, e, end, r[3], by_addr, o.string)
        side = False
        for x in walk(fn.body):
            if type(x) is A.Assign and type(x.t) in (A.Mem, A.Glob) or type(x) is A.SetIdx:
                side = True
            elif call_name(x) and not GETTER.match(re.sub(r'^[A-Z0-9]+_', '', call_name(x))):
                side = True
        effects[name] = effects.get(name, False) or side
    grouped = defaultdict(list)
    rest = []
    for c in out:
        if c['kind'] == 'discarded':
            callee = c['note'].split()[2][:-2]
            if effects.get(callee):
                continue
            grouped[callee].append(c)
        else:
            rest.append(c)
    for callee, cs in sorted(grouped.items()):
        c = dict(cs[0])
        c['note'] = 'result of %s() is not used (%d places; %s)' % (
            callee, len(cs), 'KC method without side effects' if callee in effects else 'native')
        c['sites'] = ['%s::%s @%s' % (x['cls'], x['method'], x['pc']) for x in cs]
        rest.append(c)
    out = rest
    for (cid, _), ms in bodies.items():
        if len(ms) > 1:
            names = sorted(n for n, _ in ms)
            stems = {re.sub(r'^(Get|Is|Has|Find|Calc|Count|Num|Compute|Can|Check|Read|Query)', '', n) for n in names}
            if len(stems) > 1:
                out.append({'kind': 'same-body', 'cls': cid, 'method': names[0], 'entry': '%06x' % ms[0][1],
                            'pc': '%06x' % ms[0][1], 'note': 'same body as ' + ', '.join(names[1:])})
    for i, c in enumerate(out):
        c['id'] = 'c%04d' % i
        c['clsname'] = S.class_name(c['cls']) if c['cls'] else ''
    with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
        for c in out:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    from collections import Counter
    for kind, n in Counter(c['kind'] for c in out).most_common():
        print('%6d  %s' % (n, kind))
    print('methods not decompiled:', failed)


if __name__ == '__main__':
    main()
