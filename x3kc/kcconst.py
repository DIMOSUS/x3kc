"""Compile-time named constants: `const NAME = <expr>;` and `enum Name { A = 1, B, C = 10 }`.

Pass 1 of the parser (kcparse.Parser) records every declaration with the tokens of its value; `resolve` evaluates
them once the whole project is read (any order: a constant may use one declared later or in another part) and puts
the tables on the Symbols. A use in a body becomes the plain literal (kcast.Int / kcast.Str), so the code is exactly
what the typed number gives. Nothing of a constant reaches the object.

Scopes: a declaration at the top level is project-global; one in a class body belongs to that class - bare NAME in
its methods and in those of its subclasses, `Class.NAME` anywhere (also through a subclass). Enum members are
constants of the enum's scope (bare `A`); `Name.A` qualifies one where the enum name is visible.
"""
from .kcsym import NO_PARENT


class ConstDef:
    """One constant: a `const` or an enum member. toks: the value's tokens (None: an enum member without `=`,
    the previous member + 1, or 0 for the first). value: set by resolve."""
    __slots__ = ('name', 'tok', 'cid', 'toks', 'enum', 'prev', 'value', 'loc', 'state')

    def __init__(self, name, tok, cid, toks, enum=None, prev=None):
        self.name, self.tok, self.cid, self.toks, self.enum, self.prev = name, tok, cid, toks, enum, prev
        self.value = None
        self.loc = None         # 'part:line' of the name (set by the parser)
        self.state = 0          # 0 new, 1 being evaluated, 2 done


class EnumDef:
    __slots__ = ('name', 'tok', 'cid', 'members', 'loc')

    def __init__(self, name, tok, cid):
        self.name, self.tok, self.cid = name, tok, cid
        self.members = []       # [ConstDef]
        self.loc = None


class ConstError(Exception):
    """tok: the token the message is about; the text already starts with its location."""

    def __init__(self, msg, tok=None):
        Exception.__init__(self, msg)
        self.tok = tok


def wrap32(v):
    return (v + 0x80000000) % 0x100000000 - 0x80000000


class Table:
    """The constants of a project: consts[cid][name] -> ConstDef (cid 0: global; enum members included),
    enums[cid][name] -> EnumDef."""

    def __init__(self, syms):
        self.syms = syms
        self.consts = {}
        self.enums = {}

    # ---- lookup ----------------------------------------------------------------------------------------------
    def scopes(self, cid):
        """The scopes a body of class cid sees, nearest first: its class chain, then the globals."""
        return (self.syms.chain(cid) if cid else []) + [0]

    def find(self, cid, name):
        for c in self.scopes(cid):
            d = self.consts.get(c, {}).get(name)
            if d is not None:
                return d
        return None

    def find_enum(self, cid, name):
        for c in self.scopes(cid):
            e = self.enums.get(c, {}).get(name)
            if e is not None:
                return e
        return None

    def qualified(self, cid, q, name):
        """`q.name` seen from class cid: a member of the enum q, or a constant of class q (or of its parents).
        -> ConstDef | None, or the string 'noenum' / 'noclass' when q is an enum / class without that name."""
        e = self.find_enum(cid, q)
        if e is not None:
            for m in e.members:
                if m.name == name:
                    return m
            return 'noenum'
        qid = self.syms.by_name.get(q)
        if qid is None and q[:1] == 'C' and q[1:].isdigit() and int(q[1:]) in self.syms.classes:
            qid = int(q[1:])
        if qid is None:
            return None
        for c in self.syms.chain(qid):
            d = self.consts.get(c, {}).get(name)
            if d is not None:
                return d
        return 'noclass'

    def signature(self):
        """For the compile cache: every constant's scope, name and value."""
        return sorted((c, n, repr(d.value)) for c, t in self.consts.items() for n, d in t.items()) + \
            sorted((c, n) for c, t in self.enums.items() for n in t)


def descendants(syms, cid):
    out = set()
    for c in syms.classes:
        if c != cid and c != 0 and cid in syms.chain(c):
            out.add(c)
    return out


def resolve(syms, consts, enums, loc, fields=()):
    """Evaluate every constant, check names, set syms.consts (a Table). consts: [ConstDef] (enum members included),
    enums: [EnumDef], fields: [(Field, cid, toks)] - field initialisers that are constant expressions.
    loc(tok) -> 'part:line'. Raises ConstError."""
    T = Table(syms)
    syms.consts = T

    def err(msg, tok):
        return ConstError('%s: %s' % (loc(tok), msg), tok)

    gvars = {f.name for f in syms.classes[0].fields} if 0 in syms.classes else set()
    gfuncs = {m.name for m in syms.classes[0].methods} if 0 in syms.classes else set()

    members = {}
    for c, k in syms.classes.items():
        if c:
            members[c] = {m.name: 'function' for m in k.methods}
            members[c].update({f.name: 'field' for f in k.fields})

    def member_names(c):
        return members[c]

    def clash(name, tok, cid, what):
        """A name a constant / enum takes must not be a global, a class, or a member visible where the constant is."""
        if name in syms.by_name:
            raise err('%s %s has the name of a class' % (what, name), tok)
        if name in gvars:
            raise err('%s %s has the name of a global variable' % (what, name), tok)
        if name in gfuncs:
            raise err('%s %s has the name of a global function' % (what, name), tok)
        if cid:
            near = set(syms.chain(cid)) | descendants(syms, cid)
        else:
            near = set(c for c in syms.classes if c)
        for c in sorted(near):
            kind = member_names(c).get(name)
            if kind:
                raise err('%s %s has the name of the %s %s.%s' % (what, name, kind, syms.class_name(c), name), tok)
        # the same name in a scope that sees this one, or that this one sees
        for c in sorted(set(near) | {0}):
            if c == cid:
                continue
            if cid and c and c not in syms.chain(cid) and cid not in syms.chain(c):
                continue
            d = T.consts.get(c, {}).get(name) or T.enums.get(c, {}).get(name)
            if d is not None:
                raise err('%s %s is also defined at %s' % (what, name, d.loc), tok)

    for e in enums:
        tab = T.enums.setdefault(e.cid, {})
        if e.name in tab or e.name in T.consts.get(e.cid, {}):
            first = tab.get(e.name) or T.consts[e.cid][e.name]
            raise err('enum %s defined twice (first at %s)' % (e.name, first.loc), e.tok)
        tab[e.name] = e
    for d in consts:
        tab = T.consts.setdefault(d.cid, {})
        first = tab.get(d.name) or T.enums.get(d.cid, {}).get(d.name)
        if first is not None:
            raise err('constant %s defined twice (first at %s)' % (d.name, first.loc), d.tok)
        tab[d.name] = d
    for e in enums:
        clash(e.name, e.tok, e.cid, 'enum')
    for d in consts:
        clash(d.name, d.tok, d.cid, 'constant')

    for d in consts:
        value_of(T, d, err)
    for f, cid, toks in fields:
        v = Eval(T, cid, toks, err, None).run()
        if isinstance(v, str):
            f.type, f.value = 2, v
        else:
            f.type, f.value = 1, v
    return T


def value_of(T, d, err):
    if d.state == 2:
        return d.value
    d.state = 1
    if d.toks is None:
        if d.prev is None:
            v = 0
        else:
            p = value_of(T, d.prev, err)
            if not isinstance(p, int):
                raise err('enum member %s follows a string: give it a value' % d.name, d.tok)
            v = wrap32(p + 1)
    else:
        v = Eval(T, d.cid, d.toks, err, d).run()
    d.value = v
    d.state = 2
    return v


class Eval:
    """A constant expression over tokens: integers (dec / hex), strings (alone), constants, unary - ~ +,
    * / % + - << >> & ^ |, parentheses. Integers are 32-bit cells, as in the game."""
    LEVELS = [('|',), ('^',), ('&',), ('<<', '>>'), ('+', '-'), ('*', '/', '%')]

    def __init__(self, T, cid, toks, err, owner):
        self.T, self.cid, self.toks, self.err, self.owner = T, cid, toks, err, owner
        self.p = 0

    def fail(self, msg, t):
        return self.err(msg, t or (self.owner.tok if self.owner else self.toks[0]))

    def peek(self, k=0):
        i = self.p + k
        return self.toks[i] if i < len(self.toks) else None

    def what(self):
        return 'the value of %s' % self.owner.name if self.owner else 'the initialiser'

    def run(self):
        if not self.toks:
            raise self.fail('%s is empty' % self.what(), None)
        v = self.binary(0)
        t = self.peek()
        if t is not None:
            raise self.fail('not a constant expression: unexpected %r in %s' % (t.text, self.what()), t)
        return v

    def binary(self, lv):
        if lv == len(self.LEVELS):
            return self.unary()
        a = self.binary(lv + 1)
        while True:
            t = self.peek()
            if t is None or t.kind != 'op' or t.text not in self.LEVELS[lv]:
                return a
            self.p += 1
            b = self.binary(lv + 1)
            a = self.apply(t, a, b)

    def ints(self, t, *xs):
        for x in xs:
            if not isinstance(x, int):
                raise self.fail('operator %s on a string in a constant expression' % t.text, t)

    def apply(self, t, a, b):
        self.ints(t, a, b)
        op = t.text
        if op in ('/', '%'):
            if b == 0:
                raise self.fail('division by zero in a constant expression', t)
            if a == -0x80000000 and b == -1:
                raise self.fail('-2147483648 %s -1 overflows (the game would crash)' % op, t)
            q = abs(a) // abs(b)
            q = q if (a < 0) == (b < 0) else -q          # C: towards zero, the remainder has the sign of a
            return wrap32(q) if op == '/' else wrap32(a - q * b)
        if op in ('<<', '>>'):
            if not 0 <= b <= 31:
                raise self.fail('shift by %d in a constant expression (0..31)' % b, t)
            return wrap32(a << b) if op == '<<' else a >> b
        return wrap32({'+': a + b, '-': a - b, '*': a * b, '&': a & b, '|': a | b, '^': a ^ b}[op])

    def unary(self):
        t = self.peek()
        if t is None:
            raise self.fail('%s ends too early' % self.what(), None)
        if t.kind == 'op' and t.text in ('-', '~', '+'):
            self.p += 1
            v = self.unary()
            self.ints(t, v)
            return wrap32(-v) if t.text == '-' else (wrap32(~v) if t.text == '~' else v)
        return self.atom()

    def atom(self):
        from .kcparse import parse_number, unescape, ParseError
        t = self.peek()
        self.p += 1
        if t.kind == 'num':
            try:
                v = parse_number(t.text)
            except ParseError as x:
                raise self.fail(str(x), t)
            if isinstance(v, float):
                raise self.fail('constants are integers or strings, not %s' % t.text, t)
            return v
        if t.kind == 'str':
            try:
                return unescape(t.text)
            except ParseError as x:
                raise self.fail(str(x), t)
        if t.kind == 'op' and t.text == '(':
            v = self.binary(0)
            c = self.peek()
            if c is None or c.text != ')':
                raise self.fail("expected ')' in a constant expression", c or t)
            self.p += 1
            return v
        if t.kind == 'id':
            T = self.T
            n = self.peek()
            if n is not None and n.text == '.' and self.peek(1) is not None and self.peek(1).kind == 'id':
                m = self.peek(1)
                d = T.qualified(self.cid, t.text, m.text)
                if isinstance(d, ConstDef):
                    self.p += 2
                    return self.ref(d, m)
                if d == 'noenum':
                    raise self.fail('enum %s has no member %s' % (t.text, m.text), m)
                if d == 'noclass':
                    raise self.fail('class %s has no constant %s' % (t.text, m.text), m)
            d = T.find(self.cid, t.text)
            if d is not None:
                return self.ref(d, t)
            S = T.syms
            if t.text in S.by_name:             # a class name is its id, as in a body
                return S.by_name[t.text]
            if t.text[:1] == 'C' and t.text[1:].isdigit():
                return int(t.text[1:])
            raise self.fail('not a constant expression: %s is %s' % (t.text, self.kind_of(t.text)), t)
        raise self.fail('not a constant expression: unexpected %r' % t.text, t)

    def ref(self, d, t):
        if d.state == 1:
            raise self.fail('cyclic constant definition: %s depends on itself (through %s)'
                            % (d.name, self.owner.name if self.owner else d.name), t)
        return value_of(self.T, d, self.err)

    def kind_of(self, name):
        S = self.T.syms
        if S.glob_by_name(name) is not None:
            return 'a global variable'
        if S.global_function(name):
            return 'a function'
        if self.cid and S.member_by_name(self.cid, name) is not None:
            return 'a field'
        return 'not a known constant'
