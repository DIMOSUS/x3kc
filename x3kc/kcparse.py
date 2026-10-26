"""Parser: KC source text -> top-level declarations and AST (kcast).

Pass 1 reads directives, global variables, classes (fields, method headers) and skips function bodies;
pass 2 parses every body with all symbols known, so names and calls resolve forward.
"""
import re
import struct

from . import kcast as A
from .kcsym import Field, Klass, Method, Symbols, NO_PARENT

TOKEN = re.compile(r'''
  (?P<ws>[ \t\r\n]+|//[^\n]*|/\*.*?\*/)
| (?P<dir>\#[^\n]*)
| (?P<num>0[xX][0-9a-fA-F]+|\d+\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|\d+)
| (?P<str>"(?:[^"\\\n]|\\.)*")
| (?P<id>[A-Za-z_][A-Za-z0-9_]*)
| (?P<op>::|->|<<=|>>=|\+\+|--|[-+*/%&|^]=|==|!=|<=|>=|<<|>>|&&|\|\||[-+*/%&|^!~<>=?:;,.(){}\[\]@])
''', re.S | re.X)


class ParseError(Exception):
    pass


class Tok:
    __slots__ = ('kind', 'text', 'line')

    def __init__(self, kind, text, line):
        self.kind, self.text, self.line = kind, text, line

    def __repr__(self):
        return '%s:%r@%d' % (self.kind, self.text, self.line)


def lex(src):
    toks = []
    pos, line = 0, 1
    n = len(src)
    while pos < n:
        m = TOKEN.match(src, pos)
        if not m:
            raise ParseError('line %d: bad character %r' % (line, src[pos]))
        k = m.lastgroup
        t = m.group()
        if k != 'ws':
            toks.append(Tok(k, t, line))
        line += t.count('\n')
        pos = m.end()
    toks.append(Tok('eof', '', line))
    return toks


ESCAPES = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"'}


def unescape(s):
    """String literal -> text. Pool strings end with NUL, so a NUL inside one would cut it: refused."""
    body = s[1:-1]
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == '\\':
            d = body[i + 1]
            if d == 'x':
                h = body[i + 2:i + 4]
                if not re.fullmatch(r'[0-9a-fA-F]{2}', h):
                    raise ParseError('bad escape \\x%s in %s (two hex digits expected)' % (h, s))
                c = chr(int(h, 16))
                i += 4
            elif d in ESCAPES:
                c = ESCAPES[d]
                i += 2
            else:
                raise ParseError('unknown escape \\%s in %s' % (d, s))
            if c == '\0':
                raise ParseError('NUL in string literal %s: the game would cut the string there' % s)
            out.append(c)
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def wrap32(v):
    """Integers are 32-bit cells: 0x80000000..0xFFFFFFFF are the negative values."""
    return (v + 0x80000000) % 0x100000000 - 0x80000000 if isinstance(v, int) else v


def parse_number(t):
    tl = t.lower()
    if tl.startswith('0x'):
        v = int(t, 16)
    elif any(c in tl for c in '.e'):
        return float(t)
    else:
        v = int(t)
    if v > 0xFFFFFFFF:
        raise ParseError('number %s does not fit in 32 bits' % t)
    return wrap32(v)


class FuncDecl:
    """A function header with the token range of its body."""

    def __init__(self, cls, name, params, frame, asm, start, end, line):
        self.cls, self.name, self.params, self.frame, self.asm = cls, name, params, frame, asm
        self.start, self.end, self.line = start, end, line
        self.ast = None


class Unit:
    """Everything pass 1 finds in a source file."""

    def __init__(self):
        self.directives = []        # (name, rest of line)
        self.items = []             # ('func', FuncDecl) | ('class', Klass) in source order
        self.syms = Symbols()
        self.globals = Klass(0, '', NO_PARENT, None)
        self.syms.add(self.globals)
        self.class_parent_names = {}
        self.funcs = []


class Parser:
    def __init__(self, src, name='<src>'):
        self.toks = lex(src)
        self.p = 0
        self.name = name

    # ---- token helpers -----------------------------------------------------------------------
    def peek(self, k=0):
        return self.toks[self.p + k]

    def next(self):
        t = self.toks[self.p]
        self.p += 1
        return t

    def at(self, text, k=0):
        t = self.toks[self.p + k]
        return t.text == text and t.kind in ('op', 'id')

    def accept(self, text):
        if self.at(text):
            self.p += 1
            return True
        return False

    def expect(self, text):
        t = self.next()
        if t.text != text or t.kind not in ('op', 'id'):
            raise self.err('expected %r, found %r' % (text, t.text), t)
        return t

    def ident(self):
        t = self.next()
        if t.kind != 'id':
            raise self.err('identifier expected, found %r' % t.text, t)
        return t.text

    def number(self):
        neg = self.accept('-')
        t = self.next()
        if t.kind != 'num':
            raise self.err('number expected', t)
        v = parse_number(t.text)
        return wrap32(-v) if neg else v

    def err(self, msg, t=None):
        t = t or self.peek()
        return ParseError('%s:%d: %s' % (self.name, t.line, msg))

    # ---- pass 1 ------------------------------------------------------------------------------
    def unit(self):
        u = Unit()
        self.u = u
        while self.peek().kind != 'eof':
            t = self.peek()
            if t.kind == 'dir':
                self.next()
                parts = t.text[1:].split('//')[0].split(None, 1)
                u.directives.append((parts[0], parts[1].strip() if len(parts) > 1 else ''))
            elif self.at('var'):
                u.globals.fields.append(self.unique(u.globals.fields, self.field(), 'global variable'))
            elif self.at('function'):
                f = self.func_header(0)
                u.globals.methods.append(self.unique(u.globals.methods, Method(f.name, len(f.params), f.frame, f),
                                                     'function'))
                u.items.append(('func', f))
            elif self.at('class'):
                u.items.append(('class', self.klass()))
            else:
                raise self.err('declaration expected, found %r' % t.text)
        for cid, (pname, line) in u.class_parent_names.items():
            k = u.syms.classes[cid]
            if pname is None:
                k.parent = NO_PARENT
            else:
                if pname not in u.syms.by_name:
                    raise ParseError('%s:%d: unknown parent class %s' % (self.name, line, pname))
                k.parent = u.syms.by_name[pname]
        return u

    def stamp(self):
        if self.accept('@'):
            return self.number()
        return None

    def field(self):
        self.expect('var')
        name = self.ident()
        typ, val = 0, 0
        if self.accept('='):
            if self.at('{'):
                self.next()
                if self.accept(':'):
                    typ = 9
                else:
                    typ = 8
                self.expect('}')
            elif self.at('__init'):
                self.next()
                self.expect('(')
                typ = self.number()
                self.expect(',')
                val = self.number()
                self.expect(')')
            elif self.peek().kind == 'str':
                typ, val = 2, unescape(self.next().text)
            else:
                typ, val = 1, self.number()
                if isinstance(val, float):
                    raise self.err('float field initialiser')
        st = self.stamp()
        self.expect(';')
        return Field(name, typ, val, st)

    def klass(self):
        line = self.expect('class').line
        name = self.ident()
        if self.accept('='):
            cid = self.number()
        else:
            m = re.fullmatch(r'C(\d+)', name)
            if not m:
                raise self.err('class %s needs an id: class %s = <id>' % (name, name))
            cid = int(m.group(1))
        parent = None
        if self.accept(':'):
            parent = self.ident()
        st = self.stamp()
        k = Klass(cid, name, NO_PARENT, st)
        if cid in self.u.syms.classes:
            raise self.err('class %d declared twice' % cid)
        if name in self.u.syms.by_name:
            raise self.err('class name %s used twice' % name)
        self.u.syms.add(k)
        self.u.class_parent_names[cid] = (parent, line)
        self.expect('{')
        while not self.accept('}'):
            if self.at('var'):
                k.fields.append(self.unique(k.fields, self.field(), 'field'))
            elif self.at('function'):
                f = self.func_header(cid)
                k.methods.append(self.unique(k.methods, Method(f.name, len(f.params), f.frame, f), 'function'))
            else:
                raise self.err('field or function expected in class %s' % name)
        return k

    def unique(self, items, x, what):
        """Two members with one name: calls and lookups would silently bind to only one of them."""
        if any(i.name == x.name for i in items):
            raise self.err('%s %s defined twice' % (what, x.name), self.toks[self.p - 1])
        return x

    def func_header(self, cid):
        line = self.expect('function').line
        name = self.ident()
        self.expect('(')
        params = []
        if not self.at(')'):
            while True:
                params.append(self.ident())
                if not self.accept(','):
                    break
        self.expect(')')
        frame = None
        if self.accept('frame'):
            frame = self.number()
        asm = self.accept('asm')
        if not self.at('{'):
            raise self.err('function body expected')
        start = self.p
        depth = 0
        while True:
            t = self.next()
            if t.kind == 'eof':
                raise self.err('unterminated function %s' % name)
            if t.kind == 'op' and t.text == '{':
                depth += 1
            elif t.kind == 'op' and t.text == '}':
                depth -= 1
                if depth == 0:
                    break
        f = FuncDecl(cid, name, params, frame, asm, start, self.p, line)
        self.u.funcs.append(f)
        return f


class BodyParser(Parser):
    """Pass 2: one function body with symbols known."""

    def __init__(self, toks, syms, decl, name, natives=()):
        self.toks = toks
        self.syms = syms
        self.decl = decl
        self.name = name
        self.cls = decl.cls
        self.p = decl.start

    def function(self):
        d = self.decl
        self.params = [A.Var('param', n, i + 1) for i, n in enumerate(d.params)]
        self.extra = {}
        self.scopes = [{p.name: p for p in self.params}]
        self.loops = 0
        if d.asm:
            items = self.asm_body()
            fn = A.Function(d.cls, d.name, self.params, None, d.frame, items)
        else:
            body = self.block()
            fn = A.Function(d.cls, d.name, self.params, body, d.frame)
        fn.frame_vars = self.extra
        if self.p != d.end:
            raise self.err('junk after function body')
        return fn

    # ---- statements --------------------------------------------------------------------------
    def block(self):
        """'{' stmts '}' as a statement list (its own scope); a single statement is accepted too."""
        self.scopes.append({})
        out = []
        if self.accept('{'):
            while not self.accept('}'):
                out.append(self.stmt())
        else:
            out.append(self.stmt())
        self.scopes.pop()
        return out

    def stmt(self):
        t = self.peek()
        if t.kind == 'id':
            w = t.text
            if w == 'var':
                self.next()
                name = self.ident()
                init = None
                if self.accept('='):
                    init = self.expr()
                self.expect(';')
                v = A.Var('local', name)
                d = A.VarDecl(v, init)
                v.decl = d
                self.scopes[-1][name] = v
                return d
            if w == 'if':
                self.next()
                self.expect('(')
                c = self.expr()
                self.expect(')')
                then = self.block()
                els = None
                if self.accept('else'):
                    if self.at('if'):
                        self.scopes.append({})
                        els = [self.stmt()]
                        self.scopes.pop()
                    else:
                        els = self.block()
                return A.If(c, then, els)
            if w == 'while':
                self.next()
                self.expect('(')
                c = self.expr()
                self.expect(')')
                return A.While(c, self.loop_block())
            if w == 'for':
                self.next()
                self.expect('(')
                init = [] if self.at(';') else [A.ExprStmt(self.expr())]
                self.expect(';')
                c = self.expr()
                self.expect(';')
                step = []
                if not self.at(')'):
                    while True:
                        step.append(A.ExprStmt(self.expr_stmt()))
                        if not self.accept(','):
                            break
                self.expect(')')
                return A.For(init, c, step, self.loop_block())
            if w == 'do':
                self.next()
                body = self.loop_block()
                self.expect('while')
                self.expect('(')
                c = self.expr()
                self.expect(')')
                self.expect(';')
                return A.DoWhile(body, c)
            if w == 'switch':
                return self.switch()
            if w == 'return':
                self.next()
                if self.accept(';'):
                    return A.Return(None)
                e = self.expr()
                self.expect(';')
                return A.Return(e)
            if w in ('break', 'continue'):
                self.next()
                self.expect(';')
                return A.Break() if w == 'break' else A.Continue()
            if w == 'fork' and self.at('{', 1):
                self.next()
                return A.Fork(self.block())
        if t.kind == 'op' and t.text == '{':
            return A.Block(self.block())
        e = self.expr_stmt()
        self.expect(';')
        return A.ExprStmt(e)

    def loop_block(self):
        return self.block()

    def switch(self):
        self.expect('switch')
        self.expect('(')
        e = self.expr()
        self.expect(')')
        dense, span = None, None
        if self.accept(':'):
            if self.accept('dense'):
                dense = True
                if self.accept('('):
                    lo = self.number()
                    self.expect(',')
                    hi = self.number()
                    self.expect(')')
                    span = (lo, hi)
            else:
                self.expect('sparse')
                dense = False
        self.expect('{')
        sections = []
        while not self.accept('}'):
            labels = []
            while self.at('case') or self.at('default'):
                if self.accept('default'):
                    labels.append(A.Default())
                else:
                    self.next()
                    v = self.expr()
                    if type(v) not in (A.Int, A.Str):
                        raise self.err('case value must be an integer or a string')
                    labels.append(A.Case(v))
                self.expect(':')
            if not labels:
                raise self.err('case or default expected')
            self.scopes.append({})
            stmts = []
            while not (self.at('case') or self.at('default') or self.at('}')):
                stmts.append(self.stmt())
            self.scopes.pop()
            sections.append((labels, stmts))
        if dense is None:
            from . import kcprint
            ints = [c.v.v for ls, _ in sections for c in ls if type(c) is A.Case and type(c.v) is A.Int]
            strs = [c for ls, _ in sections for c in ls if type(c) is A.Case and type(c.v) is A.Str]
            dense = kcprint.dense_rule(ints, strs)
        return A.Switch(e, sections, dense, span)

    # ---- expressions -------------------------------------------------------------------------
    COMPOUND = ('+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>=')

    def expr(self):
        e = self.cond()
        if self.at('=') or (self.peek().kind == 'op' and self.peek().text in self.COMPOUND):
            op = self.next().text
            v = self.expr()
            if op != '=':
                if type(e) not in (A.Ref, A.Mem, A.Glob):
                    raise self.err('%s needs a variable, a field or a global' % op)
                v = A.Bin(op[:-1], e, v)        # x op= v  is  x = x op v
            t = type(e)
            if t in (A.Ref, A.Mem, A.Glob):
                return A.Assign(e, v)
            if t is A.Index:
                return A.SetIdx(e.c, e.i, v)
            raise self.err('cannot assign to this expression')
        return e

    def expr_stmt(self):
        return self.expr()

    def incdec(self, op, e, post):
        """++x is x = x + 1 (the new value); x++ is (x = x + 1) - 1 (the old value), as the compiler emitted."""
        if type(e) not in (A.Ref, A.Mem, A.Glob):
            raise self.err('%s needs a variable, a field or a global' % op)
        a = A.Assign(e, A.Bin(op[0], e, A.Int(1)))
        return A.Bin('-' if op == '++' else '+', a, A.Int(1)) if post else a

    def cond(self):
        c = self.logic('||')
        if self.accept('?'):
            a = self.expr()
            self.expect(':')
            b = self.cond()
            return A.Cond(c, a, b)
        return c

    def logic(self, op):
        sub = (lambda: self.logic('&&')) if op == '||' else (lambda: self.binary(0))
        items = [sub()]
        while self.at(op):
            self.next()
            items.append(sub())
        return items[0] if len(items) == 1 else A.Logic(op, items)

    LEVELS = [('|',), ('^',), ('&',), ('==', '!='), ('<', '>', '<=', '>='), ('<<', '>>'), ('+', '-'), ('*', '/', '%')]

    def binary(self, lv):
        if lv == len(self.LEVELS):
            return self.unary()
        a = self.binary(lv + 1)
        while self.peek().kind == 'op' and self.peek().text in self.LEVELS[lv]:
            op = self.next().text
            b = self.binary(lv + 1)
            a = A.Bin(op, a, b)
        return a

    def unary(self):
        t = self.peek()
        if t.kind == 'op' and t.text in ('++', '--'):
            self.next()
            return self.incdec(t.text, self.unary(), False)
        if t.kind == 'op' and t.text in ('!', '-', '~'):
            self.next()
            if t.text == '-' and self.peek().kind == 'num':
                v = parse_number(self.next().text)
                return self.postfix(A.Dbl(struct.pack('>d', -v)) if isinstance(v, float) else A.Int(wrap32(-v)))
            return A.Un(t.text, self.unary())
        if t.kind == 'id' and t.text == 'new':
            self.next()
            nt = self.peek()
            if nt.kind == 'id' and (nt.text in self.syms.by_name or re.fullmatch(r'C\d+', nt.text)) \
                    and self.at('(', 1):
                self.next()
                args = self.call_args()
                return self.postfix(A.Call('o', 'Create', args, A.Un('new', A.Int(self.class_id(nt.text)))))
            return A.Un('new', self.unary())
        if t.kind == 'id' and t.text == 'delete':
            self.next()
            return A.Un('delete', self.unary())
        return self.postfix(self.primary())

    def call_args(self):
        self.expect('(')
        args = []
        if not self.at(')'):
            while True:
                args.append(self.expr())
                if not self.accept(','):
                    break
        self.expect(')')
        return args

    def postfix(self, e):
        while True:
            if self.at('++') or self.at('--'):
                e = self.incdec(self.next().text, self.value(e), True)
                continue
            if self.accept('['):
                i = self.expr()
                self.expect(']')
                e = A.Index(self.value(e), i)
            elif self.at('.'):
                self.next()
                name = self.ident()
                if self.accept('::'):
                    mname = self.ident()
                    cid = self.class_id(name)
                    args = self.call_args()
                    if self.syms.classes.get(cid) is None or not self.syms.classes[cid].method(mname):
                        raise self.err('unknown method %s::%s' % (name, mname))
                    e = A.Call('c', (cid, mname), args, self.value(e))
                else:
                    args = self.call_args()
                    if type(e) is ClassConst:
                        owner = self.syms.find_method(e.id, name)
                        if owner is None:
                            raise self.err('class %s has no method %s' % (self.syms.class_name(e.id), name))
                        e = A.Call('c', (owner, name), args, A.Int(e.id))
                    else:
                        e = A.Call('o', name, args, e)
            else:
                return self.value(e) if type(e) is ClassConst and not self.at('.') else e

    def value(self, e):
        return A.Int(e.id) if type(e) is ClassConst else e

    def class_id(self, name):
        if name in self.syms.by_name:
            return self.syms.by_name[name]
        m = re.fullmatch(r'C(\d+)', name)
        if m:
            return int(m.group(1))
        raise self.err('unknown class %s' % name)

    def primary(self):
        t = self.next()
        k = t.kind
        if k == 'num':
            v = parse_number(t.text)
            return A.Dbl(struct.pack('>d', v)) if isinstance(v, float) else A.Int(v)
        if k == 'str':
            return A.Str(unescape(t.text))
        if k == 'op':
            if t.text == '(':
                e = self.expr()
                self.expect(')')
                return Paren(e)
            if t.text == '{':
                return self.literal()
            if t.text == '::':
                name = self.ident()
                return A.Call('g', (0, name), self.call_args())
            raise self.err('unexpected %r' % t.text, t)
        if k != 'id':
            raise self.err('unexpected %r' % t.text, t)
        w = t.text
        if w == 'this':
            if self.accept('->'):
                name = self.ident()
                return A.Call('n', name, self.call_args())
            return A.This()
        if w in ('__m', '__g', '__f64', '__and', '__or', '__opland', '__oplor') and self.at('('):
            return self.builtin(w)
        if self.at('::'):
            self.next()
            name = self.ident()
            args = self.call_args()
            if w == 'native':
                return A.Native(name, args)
            if w == '__global':
                return A.Call('s', (0, name), args)
            cid = self.class_id(w)
            if not (self.syms.classes.get(cid) and self.syms.classes[cid].method(name)):
                raise self.err('unknown method %s::%s' % (w, name))
            return A.Call('s', (cid, name), args)
        if self.at('('):
            args = self.call_args()
            if self.cls:
                owner = self.syms.find_method(self.cls, w)
                if owner is not None:
                    return A.Call('s', (owner, w), args)
            if self.syms.global_function(w):
                return A.Call('g', (0, w), args)
            return A.Native(w, args)
        return self.name_value(w, t)

    def builtin(self, w):
        self.expect('(')
        if w in ('__m', '__g'):
            k = self.number()
            self.expect(')')
            return A.Mem(k) if w == '__m' else A.Glob(k)
        if w == '__f64':
            t = self.next()
            self.expect(')')
            return A.Dbl(bytes.fromhex(t.text[2:].rjust(16, '0')))
        if w in ('__and', '__or'):
            e = self.expr()
            self.expect(')')
            return A.Logic('&&' if w == '__and' else '||', [e])
        a = self.expr()
        self.expect(',')
        b = self.expr()
        self.expect(')')
        return A.Bin('&&&' if w == '__opland' else '|||', a, b)

    def literal(self):
        if self.accept('}'):
            return A.MkArr([])
        if self.accept(':'):
            self.expect('}')
            return A.MkTab([])
        first = self.expr()
        if self.accept(':'):
            pairs = [(first, self.expr())]
            while self.accept(','):
                k = self.expr()
                self.expect(':')
                pairs.append((k, self.expr()))
            self.expect('}')
            return A.MkTab(pairs)
        items = [first]
        while self.accept(','):
            items.append(self.expr())
        self.expect('}')
        return A.MkArr(items)

    def name_value(self, w, t):
        for sc in reversed(self.scopes):
            if w in sc:
                return A.Ref(sc[w])
        m = re.fullmatch(r'arg(\d+)', w)
        if m and int(m.group(1)) > len(self.params):
            i = int(m.group(1))
            v = self.extra.get(i)
            if v is None:
                v = self.extra[i] = A.Var('param', w, i)
            return A.Ref(v)
        if w in ('__argc', '__retpc', '__ctx'):
            v = self.extra.get(w[2:])
            if v is None:
                v = self.extra[w[2:]] = A.Var('frame', w, w[2:])
            return A.Ref(v)
        if self.cls:
            k = self.syms.member_by_name(self.cls, w)
            if k is not None:
                return A.Mem(k)
        k = self.syms.glob_by_name(w)
        if k is not None:
            return A.Glob(k)
        if w in self.syms.by_name or re.fullmatch(r'C\d+', w):
            return ClassConst(self.class_id(w))
        raise self.err('unknown name %s' % w, t)

    # ---- asm ---------------------------------------------------------------------------------
    def asm_body(self):
        from . import kcisa
        self.expect('{')
        items = []
        while not self.accept('}'):
            t = self.next()
            if t.kind != 'id':
                raise self.err('instruction expected', t)
            if self.accept(':'):
                if ('label', t.text) in items:
                    raise self.err('label %s defined twice' % t.text, t)
                items.append(('label', t.text))
                continue
            op = kcisa.OP.get(t.text)
            if op is None:
                raise self.err('unknown instruction %s' % t.text, t)
            f = kcisa.FMT.get(op)
            arg, cases = None, None
            if f in ('b', 'h', 'H', 'i'):
                at = self.peek()
                arg = self.number()
                lo, hi = {'b': (-128, 127), 'h': (-32768, 32767), 'H': (0, 65535), 'i': (-2 ** 31, 2 ** 31 - 1)}[f]
                if isinstance(arg, float) or not lo <= arg <= hi:
                    raise self.err('operand %s out of range %d..%d' % (arg, lo, hi), at)
            elif f == 's':
                st = self.next()
                if st.kind == 'num':
                    arg = parse_number(st.text)     # a raw pool offset
                elif st.kind == 'str':
                    arg = unescape(st.text)
                else:
                    raise self.err('string or pool offset expected', st)
            elif f == 'n':
                arg = self.ident()
            elif f == 't':
                arg = self.ident()
            elif f == 'c':
                if self.accept('::'):
                    arg = (0, self.ident())
                else:
                    c = self.ident()
                    self.expect('::')
                    arg = (self.class_id(c), self.ident())
            elif f == 'd':
                dt = self.next()
                if not re.fullmatch(r'0[xX][0-9a-fA-F]{1,16}', dt.text):
                    raise self.err('pushf takes the raw double in hex, e.g. 0x3ff0000000000000', dt)
                arg = bytes.fromhex(dt.text[2:].rjust(16, '0'))
            elif op == 0x78:
                base = self.number()
                d = self.ident()
                self.expect('[')
                tg = []
                if not self.at(']'):
                    while True:
                        tg.append(self.ident())
                        if not self.accept(','):
                            break
                self.expect(']')
                cases = (base, d, tg)
            elif op == 0x79:
                d = self.ident()
                ints, strs = [], []
                self.expect('{')
                while not self.accept('}'):
                    k = self.number()
                    self.expect(':')
                    ints.append((k, self.ident()))
                    self.accept(',')
                self.expect('{')
                while not self.accept('}'):
                    kt = self.next()
                    k = parse_number(kt.text) if kt.kind == 'num' else unescape(kt.text)   # raw pool offset
                    self.expect(':')
                    strs.append((k, self.ident()))
                    self.accept(',')
                cases = (d, ints, strs)
            items.append(('insn', op, arg, cases))
        return items


class ClassConst:
    __slots__ = ('id',)

    def __init__(self, cid):
        self.id = cid


def Paren(e):
    return e
