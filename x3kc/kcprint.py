"""Printer: AST -> KC source text (the language kcparse reads back)."""
import math
import re
import struct

from . import kcast as A
from . import kcisa
from .kcsym import FIELD_TYPES

PREC_BIN = {'*': 12, '/': 12, '%': 12, '+': 11, '-': 11, '<<': 10, '>>': 10, '<': 9, '>': 9, '<=': 9, '>=': 9,
            '==': 8, '!=': 8, '&': 7, '^': 6, '|': 5}
P_ASSIGN, P_COND, P_OR, P_AND, P_UNARY, P_POSTFIX, P_PRIMARY = 1, 2, 3, 4, 13, 14, 15


class PrintError(Exception):
    pass


def esc(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif 0x20 <= o < 0x7f:
            out.append(ch)
        else:
            out.append('\\x%02x' % o)
    return '"' + ''.join(out) + '"'


def fmt_double(raw):
    v = struct.unpack('>d', raw)[0]
    if math.isfinite(v):
        r = repr(v)
        if struct.pack('>d', float(r)) == raw:
            return r
    return '__f64(0x%s)' % raw.hex()


def same_target(t, x):
    if type(t) is not type(x):
        return False
    if type(t) is A.Ref:
        return t.var is x.var
    if type(t) in (A.Mem, A.Glob):
        return t.k == x.k
    return False


def dense_rule(ints, strs):
    """Switch table kind the compiler picks by itself (78 dense / 79 sparse)."""
    if strs or not ints:
        return False
    rng = max(ints) - min(ints) + 1
    return rng <= 20 * len(ints)


class Printer:
    def __init__(self, syms, natives=(), pcs=False):
        self.syms = syms
        self.natives = set(natives)
        self.lines = []
        self.pcs = pcs              # append `// @offset` to every statement (input for annotators)
        self.comments = {}
        self.stmt_pc = {}
        self.local_names = {}
        self.used = set()
        self.unplaced = []           # comments whose offset starts no statement: "cid::name @pc"

    # ---- expressions -------------------------------------------------------------------------
    def ex(self, e, prec=0):
        s, p = self.ex_p(e)
        return '(%s)' % s if p < prec else s

    def ex_p(self, e):
        t = type(e)
        if t is A.Int:
            return str(e.v), P_PRIMARY if e.v >= 0 else P_UNARY
        if t is A.Str:
            return esc(e.s), P_PRIMARY
        if t is A.Dbl:
            r = fmt_double(e.raw)
            return r, P_UNARY if r.startswith('-') else P_PRIMARY
        if t is A.Ref:
            return self.var_name(e.var), P_PRIMARY
        if t is A.Mem:
            return self.mem_name(e.k), P_PRIMARY
        if t is A.Glob:
            return self.glob_name(e.k), P_PRIMARY
        if t is A.This:
            return 'this', P_PRIMARY
        if t is A.Index:
            return '%s[%s]' % (self.ex(e.c, P_POSTFIX), self.ex(e.i)), P_POSTFIX
        if t is A.Assign and type(e.v) is A.Bin and e.v.op in ('+', '-') and same_target(e.t, e.v.a) \
                and type(e.v.b) is A.Int and e.v.b.v == 1:
            return e.v.op * 2 + self.ex(e.t, P_PRIMARY), P_UNARY
        if (t is A.Bin and e.op in ('+', '-') and type(e.b) is A.Int and e.b.v == 1 and type(e.a) is A.Assign
                and type(e.a.v) is A.Bin and e.a.v.op == ('-' if e.op == '+' else '+') and same_target(e.a.t, e.a.v.a)
                and type(e.a.v.b) is A.Int and e.a.v.b.v == 1):
            return self.ex(e.a.t, P_PRIMARY) + e.a.v.op * 2, P_POSTFIX
        if t is A.Assign:
            return '%s = %s' % (self.ex(e.t, P_PRIMARY), self.ex(e.v, P_ASSIGN)), P_ASSIGN
        if t is A.SetIdx:
            return '%s[%s] = %s' % (self.ex(e.c, P_POSTFIX), self.ex(e.k), self.ex(e.v, P_ASSIGN)), P_ASSIGN
        if t is A.Bin:
            if e.op not in PREC_BIN:
                return '__op%s(%s, %s)' % ({'&&&': 'land', '|||': 'lor'}[e.op], self.ex(e.a), self.ex(e.b)), P_POSTFIX
            p = PREC_BIN[e.op]
            return '%s %s %s' % (self.ex(e.a, p), e.op, self.ex(e.b, p + 1)), p
        if t is A.Un:
            if e.op in ('new', 'delete'):
                return '%s %s' % (e.op, self.ex(e.a, P_UNARY)), P_UNARY
            a = self.ex(e.a, P_UNARY)
            if e.op == '-' and (a[:1].isdigit() or a[:1] == '.' or a.startswith('-')):
                # keep a negated literal (also `-(5[x])`) apart from a negative literal: the parser folds `-5`
                a = '(%s)' % a
            return e.op + a, P_UNARY
        if t is A.Logic:
            if len(e.items) == 1:
                return '%s(%s)' % ('__and' if e.op == '&&' else '__or', self.ex(e.items[0])), P_POSTFIX
            p = P_AND if e.op == '&&' else P_OR
            return (' %s ' % e.op).join(self.ex(x, p + 1) for x in e.items), p
        if t is A.Cond:
            return '%s ? %s : %s' % (self.ex(e.c, P_OR), self.ex(e.a, P_ASSIGN), self.ex(e.b, P_COND)), P_COND
        if t is A.MkArr:
            return '{%s}' % ', '.join(self.ex(x, P_ASSIGN) for x in e.items), P_PRIMARY
        if t is A.MkTab:
            if not e.pairs:
                return '{:}', P_PRIMARY
            return '{%s}' % ', '.join('%s: %s' % (self.ex(k, P_ASSIGN), self.ex(v, P_ASSIGN)) for k, v in e.pairs), P_PRIMARY
        if t is A.Native:
            shadow = self.syms.find_method(self.cls, e.name) is not None or self.syms.global_function(e.name)
            return '%s%s(%s)' % ('native::' if shadow else '', e.name, self.args(e.args)), P_POSTFIX
        if t is A.Call:
            return self.call(e), P_POSTFIX
        raise PrintError('expression %s' % t.__name__)

    def ex_stmt(self, e):
        """An expression statement; `x = x op y` prints as `x op= y`, `x = x + 1` as `x++`."""
        if (type(e) is A.Assign and type(e.v) is A.Bin and same_target(e.t, e.v.a) and e.v.op in PREC_BIN
                and e.v.op not in ('==', '!=', '<', '>', '<=', '>=')):
            b = e.v.b
            return '%s %s= %s' % (self.ex(e.t, P_PRIMARY), e.v.op, self.ex(b, P_ASSIGN))
        r = self.ex(e)
        if r.startswith('{'):
            r = '(%s)' % r              # a statement starting with `{` would parse as a block
        return r

    def args(self, xs):
        return ', '.join(self.ex(x, P_ASSIGN) for x in xs)

    def is_class_const(self, e):
        return type(e) is A.Int and e.v in self.syms.classes and e.v != 0

    def call(self, e):
        S = self.syms
        k = e.kind
        if k == 'g':
            cid, name = e.target
            if cid != 0:
                raise PrintError('callg to a class method')
            plain = S.find_method(self.cls, name) is None
            return '%s%s(%s)' % ('' if plain else '::', name, self.args(e.args))
        if k == 's':
            cid, name = e.target
            if cid != 0 and S.find_method(self.cls, name) == cid:
                return '%s(%s)' % (name, self.args(e.args))
            if cid == 0:
                return '%s::%s(%s)' % ('__global', name, self.args(e.args))
            return '%s::%s(%s)' % (S.class_name(cid), name, self.args(e.args))
        if k == 'n':
            return 'this->%s(%s)' % (e.target, self.args(e.args))
        if k == 'o':
            o = e.obj
            if (e.target == 'Create' and type(o) is A.Un and o.op == 'new' and self.is_class_const(o.a)):
                return 'new %s(%s)' % (S.class_name(o.a.v), self.args(e.args))
            obj = self.ex(o, P_POSTFIX)
            if type(o) in (A.Int, A.Dbl) and not obj.startswith('('):
                obj = '(%s)' % obj       # a number object: also keeps it apart from the static C.f() form
            return '%s.%s(%s)' % (obj, e.target, self.args(e.args))
        if k == 'c':
            cid, name = e.target
            o = e.obj
            if self.is_class_const(o) and S.find_method(o.v, name) == cid:
                return '%s.%s(%s)' % (S.class_name(o.v), name, self.args(e.args))
            obj = self.ex(o, P_POSTFIX)
            if type(o) in (A.Int, A.Dbl) and not obj.startswith('('):
                obj = '(%s)' % obj
            return '%s.%s::%s(%s)' % (obj, 'C%d' % cid if cid not in S.classes else S.class_name(cid), name,
                                      self.args(e.args))
        raise PrintError('call kind %s' % k)

    def var_name(self, v):
        if v.kind == 'local':
            if v not in self.names:
                raise PrintError('local used before its declaration')
            return self.names[v]
        return v.name

    def mem_name(self, k):
        m = self.syms.member(self.cls, k) if self.cls else None
        return m[1].name if m else '__m(%d)' % k

    def glob_name(self, k):
        g = self.syms.glob(k)
        return g.name if g else '__g(%d)' % k

    # ---- statements --------------------------------------------------------------------------
    def out(self, depth, s):
        self.lines.append('    ' * depth + s)

    def comment(self, d, text):
        import textwrap
        width = max(40, 116 - 4 * d)
        for line in text.splitlines() or ['']:
            for part in textwrap.wrap(line, width) or ['']:
                self.out(d, ('// ' + part).rstrip())

    def annotated(self, s, d, emit):
        """Print a statement with its comment (by code offset) above it and, in pcs mode, its offset after it."""
        pc = self.stmt_pc.get(id(s))
        if pc is not None and pc in self.comments:
            self.comment(d, self.comments[pc])
            self.used.add(pc)
        start = len(self.lines)
        emit()
        if self.pcs and pc is not None and len(self.lines) > start:
            self.lines[start] += '  // @%06x' % pc

    def body(self, stmts, depth):
        k = 0
        while k < len(stmts):
            s = stmts[k]
            if (type(s) is A.ExprStmt and k + 1 < len(stmts) and type(stmts[k + 1]) is A.For
                    and not stmts[k + 1].init and self.loop_init(s, stmts[k + 1])):
                f = stmts[k + 1]
                self.annotated(f, depth, lambda: self.for_(f, depth, s.e))
                k += 2
                continue
            self.annotated(s, depth, lambda: self.stmt(s, depth))
            k += 1

    def loop_init(self, s, f):
        """Print an expression statement as the init of the following for only when it sets a variable of the
        loop condition and carries no comment of its own (else it stays a statement above the loop)."""
        pc = self.stmt_pc.get(id(s))
        if pc is not None and pc in self.comments:
            return False
        if type(s.e) is not A.Assign:
            return False
        try:
            target, cond = self.ex(s.e.t, P_PRIMARY), self.ex(f.c)
        except PrintError:
            return False
        return re.search(r'(?<![\w.])%s(?!\w)' % re.escape(target), cond) is not None

    def stmt(self, s, d):
        t = type(s)
        if t is A.ExprStmt:
            self.out(d, self.ex_stmt(s.e) + ';')
        elif t is A.VarDecl:
            self.nloc += 1
            name = self.local_names.get('v%d' % self.nloc, 'v%d' % self.nloc)
            init = self.ex(s.init, P_ASSIGN) if s.init is not None else None
            self.names[s.var] = name
            self.out(d, 'var %s%s;' % (name, '' if init is None else ' = ' + init))
        elif t is A.Block:
            self.out(d, '{')
            self.body(s.body, d + 1)
            self.out(d, '}')
        elif t is A.If:
            self.if_(s, d, 'if')
        elif t is A.While:
            self.out(d, 'while (%s) {' % self.ex(s.c))
            self.body(s.body, d + 1)
            self.out(d, '}')
        elif t is A.For:
            self.for_(s, d, None)
        elif t is A.DoWhile:
            self.out(d, 'do {')
            self.body(s.body, d + 1)
            self.out(d, '} while (%s);' % self.ex(s.c))
        elif t is A.Switch:
            self.switch(s, d)
        elif t is A.Return:
            self.out(d, 'return;' if s.e is None else 'return %s;' % self.ex(s.e))
        elif t is A.Break:
            self.out(d, 'break;')
        elif t is A.Continue:
            self.out(d, 'continue;')
        elif t is A.Fork:
            self.out(d, 'fork {')
            self.body(s.body, d + 1)
            self.out(d, '}')
        else:
            raise PrintError('statement %s' % t.__name__)

    def if_(self, s, d, kw):
        self.out(d, '%s (%s) {' % (kw, self.ex(s.c)))
        self.body(s.then, d + 1)
        els = s.els
        while els is not None:
            if len(els) == 1 and type(els[0]) is A.If:
                s = els[0]
                self.out(d, '} else if (%s) {' % self.ex(s.c))
                pc = self.stmt_pc.get(id(s))
                if pc is not None:
                    if self.pcs:
                        self.lines[-1] += '  // @%06x' % pc
                    if pc in self.comments:
                        self.comment(d + 1, self.comments[pc])
                        self.used.add(pc)
                self.body(s.then, d + 1)
                els = s.els
            else:
                self.out(d, '} else {')
                self.body(els, d + 1)
                break
        self.out(d, '}')

    def for_(self, s, d, init):
        steps = []
        for st in s.step:
            if type(st) is not A.ExprStmt:
                raise PrintError('for step is not an expression')
            steps.append(self.ex_stmt(st.e))
        for st in s.init:
            raise PrintError('for init list')
        self.out(d, 'for (%s; %s; %s) {' % ('' if init is None else self.ex(init), self.ex(s.c), ', '.join(steps)))
        self.body(s.body, d + 1)
        self.out(d, '}')

    def switch(self, s, d):
        ints = [c.v.v for labels, _ in s.sections for c in labels if type(c) is A.Case and type(c.v) is A.Int]
        strs = [c for labels, _ in s.sections for c in labels if type(c) is A.Case and type(c.v) is A.Str]
        attr = ''
        if s.span is not None:
            attr = ' : dense(%d, %d)' % s.span
        elif s.dense != dense_rule(ints, strs):
            attr = ' : dense' if s.dense else ' : sparse'
        self.out(d, 'switch (%s)%s {' % (self.ex(s.e), attr))
        for labels, stmts in s.sections:
            if not labels:
                raise PrintError('switch section without a label')
            for c in labels:
                self.out(d, 'default:' if type(c) is A.Default else 'case %s:' % self.ex(c.v))
            self.body(stmts, d + 1)
        self.out(d, '}')

    # ---- functions ---------------------------------------------------------------------------
    def function(self, fn, cid, name, entry, d, computed_frame=None, ann=None):
        """ann: {'doc', 'params': [names], 'locals': {'v1': name}, 'comments': {pc: text}} (all optional)."""
        ann = ann or {}
        self.cls = cid
        self.names = {}
        self.nloc = 0
        self.local_names = ann.get('locals') or {}
        self.comments = ann.get('comments') or {}
        self.used = set()
        self.stmt_pc = fn.stmt_pc or {}
        for p, n in zip(fn.params, ann.get('params') or []):
            if n:
                p.name = n
        params = ', '.join(p.name for p in fn.params)
        frame = ''
        if fn.frame is not None and computed_frame is not None and fn.frame != computed_frame:
            frame = ' frame %d' % fn.frame
        start = len(self.lines)
        try:
            if ann.get('doc'):
                self.comment(d, ann['doc'])
            self.out(d, 'function %s(%s)%s {  // %06x' % (name, params, frame, entry))
            self.body(fn.body, d + 1)
            self.out(d, '}')
        except Exception:           # nothing half-printed stays behind
            del self.lines[start:]
            raise
        for pc in sorted(set(self.comments) - self.used):
            self.unplaced.append('%d::%s @%06x' % (cid, name, pc))

    def asm_function(self, name, params, frame, entry, items, d):
        self.out(d, 'function %s(%s) frame %d asm {  // %06x' % (name, ', '.join(params), frame, entry))
        for it in items:
            if it[0] == 'label':
                self.out(0, '%s:' % it[1])
            else:
                self.out(d + 1, it[1])
        self.out(d, '}')
