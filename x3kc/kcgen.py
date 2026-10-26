"""Code generator: AST function -> KC bytecode (file format), mirroring Egosoft's one-pass compiler.

Emission rules (all inferred from x3story.obj and checked by the round-trip test):
  locals live on the stack; `ldl n` / `stl n` count from the current top (n = 1 is the top cell);
  entry frame (bottom -> top): argN .. arg1, argc, return pc, context;
  args of calls, array items and table pairs are pushed last-first; argc is pushed as a constant before the object;
  `a && b` -> a jf F; b jf F; push1; jmp E; F: push0; E:      (`||` the same with jt / push0 / push1)
  `c ? a : b` -> c jf F; a; jmp E; F: b; E:
  if: c jf L1; then; [jmp L2;] L1: [else; L2:]       while: S: c jf X; body; jmp S; X:
  for: init; S: c jf X; jmp B; I: step; jmp S; B: body; jmp I; X:          do: S: body; c; jt S
  switch: e; jmp D; body; jmp End; D: switch/sswitch; End:                  fork: fork; jt L; body; exit; L:
  return e: e; [stl nloc+1; pop/popn nloc]; ret       block end: pop / popn n       function: frame N; body; push0; ret
"""
import struct

from . import kcast as A
from . import kcisa

PUSHN = {0: 0x01, 1: 0x02, 2: 0x03, 3: 0x04}


class GenError(Exception):
    pass


class Label:
    __slots__ = ('pos', 'name')

    def __init__(self, name=None):
        self.pos, self.name = None, name


class Env:
    """What codegen needs from the outside: string pool and method addresses."""

    def __init__(self, pool, methods=None):
        self.pool = pool                # Pool
        self.methods = methods or {}    # (cls, name) -> entry address (filled before fixups are resolved)


class Pool:
    def __init__(self, data=b'\0'):
        self.data = bytearray(data)
        self.index = {}
        p = 0
        while p < len(self.data):
            e = self.data.find(b'\0', p)
            if e < 0:
                e = len(self.data)
            s = self.data[p:e].decode('latin1')
            if s not in self.index or (s == '' and self.index[s] == 0):
                self.index.setdefault(s, p)
                if s == '' and p != 0:
                    self.index[s] = p       # "" literals use the second empty string, not offset 0
            p = e + 1
        self.added = []

    def offset(self, s):
        o = self.index.get(s)
        if o is None or o == 0:             # offset 0 reads as "SB_String(0)" in the VM, not as ""
            if self.data and self.data[-1] != 0:
                self.data += b'\0'
            o = len(self.data)
            self.data += s.encode('latin1') + b'\0'
            self.index[s] = o
            self.added.append(s)
        return o

    def text(self, off):
        e = self.data.find(b'\0', off)
        return self.data[off:e].decode('latin1')


class FuncGen:
    def __init__(self, fn, base, env):
        self.fn, self.base, self.env = fn, base, env
        self.out = bytearray()
        self.depth = 0
        self.maxd = 0
        self.jfix = []      # (pos of operand, Label)
        self.cfix = []      # (pos of operand, (cls, name))
        self.ctx = []       # loop/switch contexts: dict(brk, cont, depth)
        self.nparams = len(fn.params)

    # ---- low level ---------------------------------------------------------------------------
    def emit(self, op, arg=None, delta=0, cases=None):
        self.out += kcisa.encode(op, arg, cases)
        self.depth += delta
        if self.depth > self.maxd:
            self.maxd = self.depth

    def here(self):
        return self.base + len(self.out)

    def place(self, lab):
        lab.pos = self.here()

    def jump(self, op, lab, delta=0):
        self.out.append(op)
        self.jfix.append((len(self.out), lab))
        self.out += b'\0\0\0\0'
        self.depth += delta

    def call_ref(self, op, ref, delta):
        if op == 0x88:
            self.maxd = max(self.maxd, self.depth + 1)  # self calls count an implicit object cell
        self.out.append(op)
        self.cfix.append((len(self.out), ref))
        self.out += b'\0\0\0\0'
        self.depth += delta
        self.maxd = max(self.maxd, self.depth)

    def name_ref(self, op, name, delta):
        if op == 0x87:
            self.maxd = max(self.maxd, self.depth + 1)
        self.emit(op, self.env.pool.offset(name), delta)

    def push_int(self, v):
        v = (v + 0x80000000) % 0x100000000 - 0x80000000     # folded constants may leave 32 bits
        if v in PUSHN:
            self.emit(PUSHN[v], delta=1)
        elif -128 <= v <= 127:
            self.emit(0x05, v, 1)
        elif -32768 <= v <= 32767:
            self.emit(0x06, v, 1)
        else:
            self.emit(0x07, v, 1)

    def pops(self, n):
        if n == 1:
            self.emit(0x24, delta=-1)
        elif n > 1:
            self.emit(0x23, n, -n)

    def slot_n(self, var):
        if var.slot is None:
            raise GenError('variable %s used outside its scope' % var.name)
        return self.depth - var.slot

    # ---- function ----------------------------------------------------------------------------
    def function(self):
        fn = self.fn
        n = self.nparams
        for i, p in enumerate(fn.params):
            p.slot = n - 1 - i
        for k, v in (fn.frame_vars or {}).items():
            v.slot = n + {'argc': 0, 'retpc': 1, 'ctx': 2}[v.index] if isinstance(v.index, str) else n - v.index
        self.depth = self.maxd = n + 3
        self.frame0 = n + 3
        self.emit(0x6e, 0)                    # patched below
        if fn.asm is not None:
            if fn.frame is None:
                raise GenError('an asm function needs "frame N": the stack depth of asm is not tracked')
            self.asm(fn.asm)
        else:
            self.block(fn.body)
            m = self.maxd
            self.push_int(0)                # the epilogue is not counted in the frame size
            self.emit(0x83, delta=-1)
            self.maxd = m
        frame = fn.frame if fn.frame is not None else self.computed_frame()
        struct.pack_into('>h', self.out, 1, frame)
        self.frame = frame
        return self

    def computed_frame(self):
        return self.maxd - self.frame0

    def resolve(self):
        for pos, lab in self.jfix:
            if lab.pos is None:
                raise GenError('unplaced label %s' % lab.name)
            struct.pack_into('>I', self.out, pos, lab.pos)
        for pos, ref in self.cfix:
            a = self.env.methods.get(ref)
            if a is None:
                raise GenError('unknown method %s::%s' % ref)
            struct.pack_into('>I', self.out, pos, a)
        return bytes(self.out)

    # ---- statements --------------------------------------------------------------------------
    def block(self, stmts):
        start = self.depth
        for s in stmts:
            self.stmt(s)
        self.pops(self.depth - start)

    def stmt(self, s):
        t = type(s)
        if t is A.ExprStmt:
            self.expr(s.e)
            self.emit(0x24, delta=-1)
        elif t is A.VarDecl:
            if s.init is None:
                self.push_int(0)
            else:
                self.expr(s.init)
            s.var.slot = self.depth - 1
        elif t is A.Block:
            self.block(s.body)
        elif t is A.If:
            l1 = Label()
            self.expr(s.c)
            self.jump(0x34, l1, -1)
            self.block(s.then)
            if s.els is not None:
                l2 = Label()
                self.jump(0x32, l2)
                self.place(l1)
                self.block(s.els)
                self.place(l2)
            else:
                self.place(l1)
        elif t is A.While:
            S, X = Label(), Label()
            self.place(S)
            self.expr(s.c)
            self.jump(0x34, X, -1)
            self.loop_body(s.body, X, S)
            self.jump(0x32, S)
            self.place(X)
        elif t is A.For:
            for st in s.init:
                self.stmt(st)
            S, X, B, I = Label(), Label(), Label(), Label()
            self.place(S)
            self.expr(s.c)
            self.jump(0x34, X, -1)
            self.jump(0x32, B)
            self.place(I)
            self.block(s.step)
            self.jump(0x32, S)
            self.place(B)
            self.loop_body(s.body, X, I)
            self.jump(0x32, I)
            self.place(X)
        elif t is A.DoWhile:
            S, X, C = Label(), Label(), Label()
            self.place(S)
            self.loop_body(s.body, X, C)
            self.place(C)
            self.expr(s.c)
            self.jump(0x33, S, -1)
            self.place(X)
        elif t is A.Switch:
            self.switch(s)
        elif t is A.Return and s.e is None:
            nloc = self.depth - self.frame0
            self.pops(nloc)
            m = self.maxd
            self.push_int(0)                # like the epilogue, not counted in the frame
            self.emit(0x83, delta=-1)
            self.maxd = m
            self.depth += nloc
        elif t is A.Return:
            nloc = self.depth - self.frame0
            self.expr(s.e)
            if nloc:
                self.emit(0x14, nloc + 1)
                self.pops(nloc)
                self.depth += nloc
            self.emit(0x83)
            self.depth -= 1
        elif t is A.Break or t is A.Continue:
            c = self.find_ctx(t is A.Continue)
            k = self.depth - c['depth']
            self.pops(k)
            self.jump(0x32, c['cont'] if t is A.Continue else c['brk'])
            self.depth += k
        elif t is A.Fork:
            L = Label()
            self.emit(0x6f, delta=1)
            self.jump(0x33, L, -1)
            self.ctx.append({'fork': True})
            self.block(s.body)
            self.ctx.pop()
            self.emit(0x70)
            self.place(L)
        elif t is A.Asm:
            self.asm(s.items)
        else:
            raise GenError('statement %s' % t.__name__)

    def loop_body(self, body, brk, cont):
        self.ctx.append({'brk': brk, 'cont': cont, 'depth': self.depth, 'loop': True})
        self.block(body)
        self.ctx.pop()

    def find_ctx(self, cont):
        for c in reversed(self.ctx):
            if c.get('fork'):
                raise GenError('break/continue cannot leave a fork body (the child task would run on)')
            if c['loop'] or not cont:
                return c
        raise GenError('break/continue outside a loop')

    def switch(self, s):
        D, End = Label(), Label()
        self.expr(s.e)
        self.jump(0x32, D)
        self.depth -= 1
        saved = getattr(self, 'cases', None)
        self.cases = []
        self.ctx.append({'brk': End, 'cont': None, 'depth': self.depth, 'loop': False})
        for labels, stmts in s.sections:
            for lab in labels:
                self.cases.append((lab, self.here()))
            self.block(stmts)
        self.ctx.pop()
        self.jump(0x32, End)
        self.place(D)
        cases, self.cases = self.cases, saved
        default = End
        ints, strs = [], []
        seen = set()
        for c, pos in cases:
            if type(c) is A.Default:
                key = 'default'
            elif type(c.v) is A.Int:
                key = c.v.v
            else:
                key = ('str', c.v.s if c.v.off is None else c.v.off)
            if key in seen:
                raise GenError('switch: case %s appears twice' % (key[1] if isinstance(key, tuple) else key))
            seen.add(key)
            if type(c) is A.Default:
                default = pos
            elif type(c.v) is A.Int:
                ints.append((c.v.v, pos))
            else:
                strs.append((c.v, pos))
        dpos = default if isinstance(default, int) else None
        start = len(self.out)
        if s.dense:
            if strs:
                raise GenError('dense switch with string cases')
            lo = min(v for v, _ in ints) if ints else 0
            hi = max(v for v, _ in ints) if ints else -1
            if s.span is not None:
                lo, hi = s.span
                out = [v for v, _ in ints if not lo <= v <= hi]
                if out:
                    raise GenError('switch: case %d is outside dense(%d, %d)' % (out[0], lo, hi))
            tab = {v: p for v, p in ints}
            n = hi - lo + 1
            if not 0 <= n <= 0xffff:
                raise GenError('switch: dense table of %d entries (0..65535 allowed)' % n)
            self.out += struct.pack('>BHii', 0x78, n, lo, 0)
            dfix = len(self.out) - 4
            for v in range(lo, hi + 1):
                self.out += struct.pack('>I', tab.get(v, dpos or 0))
                if v not in tab and dpos is None:
                    self.jfix.append((len(self.out) - 4, End))
        else:
            ints.sort(key=lambda x: x[0])
            strs = sorted(((self.env.pool.offset(sv.s) if sv.off is None else sv.off, p) for sv, p in strs),
                          key=lambda x: self.env.pool.text(x[0]))
            self.out += struct.pack('>BHHI', 0x79, len(ints), len(strs), 0)
            dfix = len(self.out) - 4
            for v, p in ints:
                self.out += struct.pack('>iI', v, p)
            for o, p in strs:
                self.out += struct.pack('>II', o, p)
        if dpos is None:
            self.jfix.append((dfix, End))
        else:
            struct.pack_into('>I', self.out, dfix, dpos)
        self.place(End)

    # ---- expressions -------------------------------------------------------------------------
    def expr(self, e):
        t = type(e)
        if t is A.Int:
            self.push_int(e.v)
        elif t is A.Ref:
            self.emit(0x0d, self.slot_n(e.var), 1)
        elif t is A.Mem:
            self.emit(0x0f, e.k, 1)
        elif t is A.Glob:
            self.emit(0x0e, e.k, 1)
        elif t is A.Str:
            self.emit(0x0b, self.env.pool.offset(e.s) if e.off is None else e.off, 1)
        elif t is A.Dbl:
            self.emit(0x0a, e.raw, 1)
        elif t is A.This:
            self.emit(0x2e, delta=1)
        elif t is A.Bin:
            self.expr(e.a)
            self.expr(e.b)
            self.emit(A.BINOP_CODE[e.op], delta=-1)
        elif t is A.Un:
            self.expr(e.a)
            self.emit(A.UNOP_CODE[e.op])
        elif t is A.Assign:
            self.expr(e.v)
            tt = type(e.t)
            if tt is A.Ref:
                self.emit(0x14, self.slot_n(e.t.var))
            elif tt is A.Mem:
                self.emit(0x16, e.t.k)
            elif tt is A.Glob:
                self.emit(0x15, e.t.k)
            else:
                raise GenError('assignment target')
        elif t is A.Index:
            self.expr(e.i)
            self.expr(e.c)
            self.emit(0x10, delta=-1)
        elif t is A.SetIdx:
            self.expr(e.v)
            self.expr(e.k)
            self.expr(e.c)
            self.emit(0x17, delta=-2)
        elif t is A.Call:
            for a in reversed(e.args):
                self.expr(a)
            self.push_int(len(e.args))
            n = len(e.args)
            if e.kind in 'oc':
                self.expr(e.obj)
                if e.kind == 'o':
                    self.name_ref(0x85, e.target, -(n + 1))
                else:
                    self.call_ref(0x86, e.target, -(n + 1))
            elif e.kind == 'n':
                self.name_ref(0x87, e.target, -n)
            else:
                self.call_ref(0x84 if e.kind == 'g' else 0x88, e.target, -n)
        elif t is A.Native:
            for a in reversed(e.args):
                self.expr(a)
            self.push_int(len(e.args))
            self.name_ref(0x82, e.name, -len(e.args))
        elif t is A.MkArr:
            for a in reversed(e.items):
                self.expr(a)
            self.emit(0x28, len(e.items), 1 - len(e.items))
        elif t is A.MkTab:
            for k, v in reversed(e.pairs):
                self.expr(v)
                self.expr(k)
            self.emit(0x29, len(e.pairs), 1 - 2 * len(e.pairs))
        elif t is A.Logic:
            F, E = Label(), Label()
            j = 0x34 if e.op == '&&' else 0x33
            for a in e.items:
                self.expr(a)
                self.jump(j, F, -1)
            self.push_int(1 if e.op == '&&' else 0)
            self.jump(0x32, E)
            self.depth -= 1
            self.place(F)
            self.push_int(0 if e.op == '&&' else 1)
            self.place(E)
        elif t is A.Cond:
            F, E = Label(), Label()
            self.expr(e.c)
            self.jump(0x34, F, -1)
            self.expr(e.a)
            self.jump(0x32, E)
            self.depth -= 1
            self.place(F)
            self.expr(e.b)
            self.place(E)
        else:
            raise GenError('expression %s' % t.__name__)

    # ---- raw instructions --------------------------------------------------------------------
    def asm(self, items):
        labels = {}

        def lab(name):
            return labels.setdefault(name, Label(name))
        for it in items:
            if it[0] == 'label':
                self.place(lab(it[1]))
                continue
            _, op, arg, cases = it
            f = kcisa.FMT.get(op)
            if f == 't':
                self.jump(op, lab(arg))
            elif f == 'c':
                self.call_ref(op, arg, 0)
            elif f == 'n':
                self.emit(op, self.env.pool.offset(arg))
            elif f == 's':
                self.emit(op, self.env.pool.offset(arg) if isinstance(arg, str) else arg)
            elif op == 0x78:
                base, d, tg = cases
                self.out += struct.pack('>BHii', op, len(tg), base, 0)
                self.jfix.append((len(self.out) - 4, lab(d)))
                for x in tg:
                    self.out += b'\0\0\0\0'
                    self.jfix.append((len(self.out) - 4, lab(x)))
            elif op == 0x79:
                d, ints, strs = cases
                self.out += struct.pack('>BHHI', op, len(ints), len(strs), 0)
                self.jfix.append((len(self.out) - 4, lab(d)))
                for k, x in ints:
                    self.out += struct.pack('>iI', k, 0)
                    self.jfix.append((len(self.out) - 4, lab(x)))
                for k, x in strs:
                    self.out += struct.pack('>II', self.env.pool.offset(k) if isinstance(k, str) else k, 0)
                    self.jfix.append((len(self.out) - 4, lab(x)))
            else:
                self.emit(op, arg)


class _NullPool:
    def offset(self, s):
        return 0

    def text(self, off):
        return ''


class _NullMethods(dict):
    def get(self, k, d=None):
        return 0


def frame_of(fn):
    """Frame size the compiler would compute for fn (addresses and strings do not matter)."""
    keep = fn.frame
    fn.frame = None
    try:
        g = FuncGen(fn, 0, Env(_NullPool(), _NullMethods())).function()
        return g.computed_frame()
    finally:
        fn.frame = keep
