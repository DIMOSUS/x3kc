"""Random programs compiled by kcc and run by the emulator agree with a reference evaluator written in Python
(32-bit ints, C division/modulo, division by 0 = 0, comparisons and logic give 0/1, C precedence)."""
import os
import random
import tempfile
import unittest

from x3kc import kcc, kcvm


M = 0xffffffff


def s32(v):
    v &= M
    return v - 0x100000000 if v & 0x80000000 else v


class Crash(Exception):
    pass


def cdiv(a, b):
    if b == 0:
        return 0
    if a == -0x80000000 and b == -1:
        raise Crash()
    q = abs(a) // abs(b)
    return s32(q if (a < 0) == (b < 0) else -q)


def cmod(a, b):
    if b == 0:
        return 0
    if a == -0x80000000 and b == -1:
        raise Crash()
    return s32(a - b * cdiv(a, b))


BIN = {
    '*': (12, lambda a, b: s32(a * b)), '/': (12, cdiv), '%': (12, cmod),
    '+': (11, lambda a, b: s32(a + b)), '-': (11, lambda a, b: s32(a - b)),
    '<<': (10, lambda a, b: s32(a << (b & 31))), '>>': (10, lambda a, b: a >> (b & 31)),
    '<': (9, lambda a, b: int(a < b)), '>': (9, lambda a, b: int(a > b)),
    '<=': (9, lambda a, b: int(a <= b)), '>=': (9, lambda a, b: int(a >= b)),
    '==': (8, lambda a, b: int(a == b)), '!=': (8, lambda a, b: int(a != b)),
    '&': (7, lambda a, b: s32(a & b)), '^': (6, lambda a, b: s32(a ^ b)), '|': (5, lambda a, b: s32(a | b)),
    '&&': (4, lambda a, b: int(bool(a) and bool(b))), '||': (3, lambda a, b: int(bool(a) or bool(b))),
}


CRASH = 'crash'


def apply(op, av, bv):
    """Reference value of `a op b`; && and || do not evaluate b when a decides (the compiler emits jumps)."""
    if av == CRASH:
        return CRASH
    if op == '&&' and not av:
        return 0
    if op == '||' and av:
        return 1
    if bv == CRASH:
        return CRASH
    try:
        return BIN[op][1](av, bv)
    except Crash:
        return CRASH


def unary(op, v):
    if v == CRASH:
        return CRASH
    return s32(-v) if op == '-' else s32(~v) if op == '~' else int(not v)


class Gen:
    def __init__(self, rnd, names):
        self.r = rnd
        self.names = names

    def leaf(self, env):
        r = self.r
        k = r.random()
        if k < 0.4 and env:
            n = r.choice(sorted(env))
            return n, env[n]
        v = r.choice([0, 1, 2, 3, 7, -1, -5, 100, 255, 1000, -32768, 65535, 0x7fffffff, -0x80000000,
                      r.randint(-300, 300)])
        if v < 0:
            return '(%d)' % v, v
        if r.random() < 0.2 and v >= 0:
            return '0x%x' % v, v
        return str(v), v

    def expr(self, env, depth):
        r = self.r
        if depth <= 0 or r.random() < 0.25:
            return self.leaf(env)
        k = r.random()
        if k < 0.12:
            t, v = self.expr(env, depth - 1)
            op = r.choice(['-', '~', '!'])
            return '%s(%s)' % (op, t), unary(op, v)
        if k < 0.2:
            c, cv = self.expr(env, depth - 1)
            a, av = self.expr(env, depth - 1)
            b, bv = self.expr(env, depth - 1)
            return '((%s) ? (%s) : (%s))' % (c, a, b), CRASH if cv == CRASH else av if cv else bv
        op = r.choice(list(BIN))
        a, av = self.expr(env, depth - 1)
        b, bv = self.expr(env, depth - 1)
        if r.random() < 0.5:
            return '(%s %s %s)' % (wrap(a), op, wrap(b)), apply(op, av, bv)
        return '%s %s %s' % (wrap(a), op, wrap(b)), apply(op, av, bv)

    def expr_prec(self, env, depth):
        """An expression without redundant parentheses: the text relies on the precedence table, and the
        reference value is computed from the same tree."""
        r = self.r
        if depth <= 0 or r.random() < 0.3:
            t, v = self.leaf(env)
            return t, v, 99
        op = r.choice(list(BIN))
        p = BIN[op][0]
        a, av, ap = self.expr_prec(env, depth - 1)
        b, bv, bp = self.expr_prec(env, depth - 1)
        if ap < p:
            a = '(%s)' % a
        if bp <= p:                                   # left-associative: equal precedence on the right needs ()
            b = '(%s)' % b
        return '%s %s %s' % (a, op, b), apply(op, av, bv), p


def wrap(t):
    return '(%s)' % t


class Program:
    """Straight-line code with ifs and bounded loops over a few variables; the reference runs alongside."""

    def __init__(self, rnd):
        self.r = rnd
        self.lines = []
        self.env = {}
        self.g = Gen(rnd, None)

    def stmt(self, depth, ind):
        r = self.r
        names = ['a', 'b', 'c', 'd']
        k = r.random()
        pad = '    ' * ind
        if k < 0.45 or depth <= 0:
            n = r.choice(names)
            e, v = self.safe_expr()
            op = r.choice(['=', '+=', '-=', '*=', '^=', '|=', '&='])
            if op == '=':
                self.env[n] = v
            else:
                old = self.env.get(n, 0)
                self.env[n] = BIN[op[0]][1](old, v)
            self.lines.append('%s%s %s %s;' % (pad, n, op, e))
        elif k < 0.6:
            n = r.choice(names)
            op = r.choice(['++', '--'])
            self.env[n] = s32(self.env.get(n, 0) + (1 if op == '++' else -1))
            self.lines.append('%s%s%s;' % (pad, n, op))
        elif k < 0.8:
            c, cv = self.safe_expr()
            self.lines.append('%sif (%s) {' % (pad, c))
            saved = dict(self.env)
            if cv:
                for _ in range(r.randint(1, 3)):
                    self.stmt(depth - 1, ind + 1)
                self.lines.append('%s}' % pad)
            else:
                # the branch is not taken: generate it on a scratch env
                keep = self.env
                self.env = dict(saved)
                for _ in range(r.randint(1, 3)):
                    self.stmt(depth - 1, ind + 1)
                self.env = keep
                self.lines.append('%s}' % pad)
        else:
            n = r.randint(0, 4)
            v = 'i%d' % len(self.lines)
            self.lines.append('%svar %s;' % (pad, v))
            self.lines.append('%sfor (%s = 0; %s < %d; %s++) {' % (pad, v, v, n, v))
            body_start = len(self.lines)
            before = dict(self.env)
            # body: assignments only, replayed n times in the reference
            body = []
            for _ in range(r.randint(1, 2)):
                nm = r.choice(names)
                c = r.randint(-9, 9)
                op = r.choice(['+=', '^=', '-='])
                body.append((nm, op, c))
                self.lines.append('%s    %s %s %d;' % (pad, nm, op, c))
            for _ in range(n):
                for nm, op, c in body:
                    self.env[nm] = BIN[op[0]][1](self.env.get(nm, 0), c)
            self.lines.append('%s}' % pad)

    def safe_expr(self):
        """Programs stay clear of the divide-overflow crash (the expression tests cover it)."""
        while True:
            e, v = self.g.expr(self.env, 2)
            if v != CRASH:
                return e, v

    def build(self, n):
        for _ in range(n):
            self.stmt(2, 1)
        return self.lines


def compile_run(src, fns):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'f.kc')
    with open(p, 'w', encoding='latin1') as f:
        f.write(src)
    o, _, _ = kcc.compile_source(p)
    from x3kc import kcverify
    V = kcverify.Verifier(o, kcverify.load_natives())
    V.run()
    assert not V.errors, dict(V.errors)
    vm = kcvm.VM(o)
    out = []
    for fn in fns:
        try:
            out.append(vm.call(0, fn))
        except kcvm.VMError as x:
            out.append('crash' if 'divide overflow' in str(x) else x)
    return out


class Fuzz(unittest.TestCase):
    def test_expressions(self):
        r = random.Random(1234)
        g = Gen(r, None)
        cases = [g.expr({}, r.randint(1, 5)) for _ in range(400)]
        src = '\n'.join('function F%d() { return %s; }' % (k, t) for k, (t, _) in enumerate(cases))
        got = compile_run(src, ['F%d' % k for k in range(len(cases))])
        bad = [(cases[k][0], cases[k][1], got[k]) for k in range(len(cases)) if got[k] != cases[k][1]]
        self.assertEqual(bad[:5], [])

    def test_precedence(self):
        """Minimal parentheses: the compiler must apply C precedence and left associativity."""
        r = random.Random(77)
        g = Gen(r, None)
        cases = [g.expr_prec({}, r.randint(2, 5))[:2] for _ in range(400)]
        src = '\n'.join('function F%d() { return %s; }' % (k, t) for k, (t, _) in enumerate(cases))
        got = compile_run(src, ['F%d' % k for k in range(len(cases))])
        bad = [(cases[k][0], cases[k][1], got[k]) for k in range(len(cases)) if got[k] != cases[k][1]]
        self.assertEqual(bad[:5], [])

    def test_programs(self):
        r = random.Random(99)
        progs = []
        for k in range(120):
            p = Program(r)
            lines = p.build(r.randint(3, 10))
            body = '\n'.join(lines)
            env = {n: p.env.get(n, 0) for n in 'abcd'}
            progs.append(('function P%d() {\n    var a = 0;\n    var b = 0;\n    var c = 0;\n    var d = 0;\n%s\n'
                          '    return {a, b, c, d};\n}' % (k, body), env))
        got = compile_run('\n'.join(p for p, _ in progs), ['P%d' % k for k in range(len(progs))])
        bad = []
        for k, ((src, env), res) in enumerate(zip(progs, got)):
            vals = dict(zip('abcd', res.items))
            if vals != env:
                bad.append((src, env, vals))
        self.assertEqual(bad[:2], [])


if __name__ == '__main__':
    unittest.main()
