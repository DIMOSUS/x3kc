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
    """tok: the token the message is about (when known); line / col: 1-based line, 0-based column of the error."""
    tok = None
    line = None
    col = None


class Tok:
    __slots__ = ('kind', 'text', 'line', 'col')

    def __init__(self, kind, text, line, col=0):
        self.kind, self.text, self.line, self.col = kind, text, line, col     # col: 0-based, in characters

    def __repr__(self):
        return '%s:%r@%d' % (self.kind, self.text, self.line)


def lex(src, loc=None):
    toks = []
    pos, line, ls = 0, 1, 0         # ls: offset of the current line's start
    n = len(src)
    while pos < n:
        m = TOKEN.match(src, pos)
        if not m:
            e = ParseError('%s: bad character %r' % (loc(line) if loc else 'line %d' % line, src[pos]))
            e.line, e.col = line, pos - ls
            raise e
        k = m.lastgroup
        t = m.group()
        if k != 'ws':
            toks.append(Tok(k, t, line, pos - ls))
        c = t.count('\n')
        if c:
            line += c
            ls = pos + t.rfind('\n') + 1
        pos = m.end()
    toks.append(Tok('eof', '', line, pos - ls))
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
        self.ptypes = [None] * len(params)  # type hints of the parameters: (class name, token) | None
        self.rtype = None                   # return type hint: (class name, token) | None
        self.varargs = False                # the body reads __argc or an argN past its parameters


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
        self.consts = []            # [kcconst.ConstDef] in source order (enum members included)
        self.enums = []             # [kcconst.EnumDef]
        self.const_fields = []      # [(Field, class id, value tokens)]: initialisers that are constant expressions


class Parser:
    def __init__(self, src, name='<src>', loc=None):
        """loc(line) -> 'file:line' for messages (a split project maps its joined lines back to the part files)."""
        self.name = name
        self.loc = loc or self.default_loc
        self.toks = lex(src, self.loc)
        self.p = 0

    def default_loc(self, line):
        return '%s:%d' % (self.name, line)

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
        e = ParseError('%s: %s' % (self.loc(t.line), msg))
        e.tok, e.line, e.col = t, t.line, t.col
        return e

    # ---- pass 1 ------------------------------------------------------------------------------
    def unit(self, resolve=True):
        """Pass 1. resolve=False (one part of a split project read alone): parent class names stay unresolved in
        Unit.class_parent_names, as the parent may be declared in another part."""
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
            elif self.at('const'):
                self.const_decl(0)
            elif self.at('enum'):
                self.enum_decl(0)
            else:
                raise self.err('declaration expected, found %r' % t.text)
        if not resolve:
            return u
        for cid, (pname, line) in u.class_parent_names.items():
            k = u.syms.classes[cid]
            if pname is None:
                k.parent = NO_PARENT
            else:
                if pname not in u.syms.by_name:
                    raise ParseError('%s: unknown parent class %s' % (self.loc(line), pname))
                k.parent = u.syms.by_name[pname]
        self.check_hints(u)
        self.resolve_consts(u)
        return u

    def resolve_consts(self, u):
        """Evaluate the constants (any order: declarations may come later or in another part)."""
        from . import kcconst
        try:
            kcconst.resolve(u.syms, u.consts, u.enums, lambda t: self.loc(t.line), u.const_fields)
        except kcconst.ConstError as x:
            e = ParseError(str(x))
            if x.tok is not None:
                e.tok, e.line, e.col = x.tok, x.tok.line, x.tok.col
            raise e

    # ---- constants -------------------------------------------------------------------------------------------
    def value_tokens(self, stops, what):
        """The tokens of a constant expression up to one of `stops` (an operator outside parentheses)."""
        out = []
        depth = 0
        while True:
            t = self.peek()
            if t.kind == 'eof' or (t.kind == 'op' and t.text in ('{', '}', ';') and t.text not in stops):
                raise self.err('%s: expected %s' % (what, ' or '.join(repr(s) for s in stops)), t)
            if depth == 0 and t.kind == 'op' and t.text in stops:
                break
            if t.kind == 'op' and t.text == '(':
                depth += 1
            elif t.kind == 'op' and t.text == ')':
                depth -= 1
            out.append(self.next())
        if not out:
            raise self.err('%s: value expected' % what)
        return out

    def const_decl(self, cid):
        """const NAME = <constant expression>;"""
        from . import kcconst
        self.expect('const')
        t = self.peek()
        name = self.ident()
        self.expect('=')
        d = kcconst.ConstDef(name, t, cid, self.value_tokens((';',), 'constant %s' % name))
        d.loc = self.loc(t.line)
        self.expect(';')
        self.u.consts.append(d)
        return d

    def enum_decl(self, cid):
        """enum Name { A = <constant expression>, B, ... } - B is A + 1; the first one without a value is 0."""
        from . import kcconst
        self.expect('enum')
        t = self.peek()
        e = kcconst.EnumDef(self.ident(), t, cid)
        e.loc = self.loc(t.line)
        self.expect('{')
        prev = None
        while not self.accept('}'):
            mt = self.peek()
            name = self.ident()
            toks = self.value_tokens((',', '}'), 'enum member %s' % name) if self.accept('=') else None
            d = kcconst.ConstDef(name, mt, cid, toks, e.name, prev)
            d.loc = self.loc(mt.line)
            e.members.append(d)
            self.u.consts.append(d)
            prev = d
            if not self.accept(','):
                self.expect('}')
                break
        self.accept(';')
        self.u.enums.append(e)
        return e

    def stamp(self):
        if self.accept('@'):
            return self.number()
        return None

    def hint_opt(self):
        """`: ClassName` after a variable, parameter or function header: a type hint, (name, token) or None.
        Hints never change the code; they only let the compiler check calls (BodyParser.check_call)."""
        if not self.accept(':'):
            return None
        t = self.peek()
        self.ident()
        return (t.text, t)

    def resolve_hint(self, h, syms):
        """(name, token) -> class id; an unknown class is an error at the hint."""
        name, t = h
        if name in syms.by_name:
            return syms.by_name[name]
        m = re.fullmatch(r'C(\d+)', name)
        if m and int(m.group(1)) in syms.classes:
            return int(m.group(1))
        raise self.err('unknown class %s in a type hint' % name, t)

    def check_hints(self, u):
        """Every hint of the headers and fields names a class of the project (pass 1, after all classes)."""
        for k in u.syms.classes.values():
            for h in k.hints.values():
                self.resolve_hint(h, u.syms)
        for f in u.funcs:
            for h in list(f.ptypes) + [f.rtype]:
                if h is not None:
                    self.resolve_hint(h, u.syms)

    def field(self, cid=0):
        self.expect('var')
        name = self.ident()
        h = self.hint_opt()
        if h is not None:
            k = self.u.syms.classes.get(cid)
            if k is not None:
                k.hints[name] = h
        typ, val = 0, 0
        deferred = None
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
            elif self.peek().kind == 'str' and (self.at(';', 1) or self.at('@', 1)):
                typ, val = 2, unescape(self.next().text)
            elif (self.peek().kind == 'num' and (self.at(';', 1) or self.at('@', 1))) or \
                    (self.at('-') and self.peek(1).kind == 'num' and (self.at(';', 2) or self.at('@', 2))):
                typ, val = 1, self.number()
                if isinstance(val, float):
                    raise self.err('float field initialiser')
            else:       # a constant expression: evaluated with the project's constants (resolve_consts)
                deferred = self.value_tokens(('@', ';'), 'initialiser of %s' % name)
                typ = 1
        st = self.stamp()
        self.expect(';')
        f = Field(name, typ, val, st)
        if deferred is not None:
            self.u.const_fields.append((f, cid, deferred))
        return f

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
                k.fields.append(self.unique(k.fields, self.field(cid), 'field'))
            elif self.at('function'):
                f = self.func_header(cid)
                k.methods.append(self.unique(k.methods, Method(f.name, len(f.params), f.frame, f), 'function'))
            elif self.at('const'):
                self.const_decl(cid)
            elif self.at('enum'):
                self.enum_decl(cid)
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
        ptypes = []
        if not self.at(')'):
            while True:
                params.append(self.ident())
                ptypes.append(self.hint_opt())
                if not self.accept(','):
                    break
        self.expect(')')
        rtype = self.hint_opt()
        frame = None
        if self.accept('frame'):
            frame = self.number()
        asm = self.accept('asm')
        if not self.at('{'):
            raise self.err('function body expected')
        start = self.p
        depth = 0
        varargs = False
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
            elif t.kind == 'id' and t.text.startswith(('__argc', 'arg')) and \
                    (t.text == '__argc' or re.fullmatch(r'arg(\d+)', t.text) and int(t.text[3:]) > len(params)):
                varargs = True
        f = FuncDecl(cid, name, params, frame, asm, start, self.p, line)
        f.ptypes, f.rtype, f.varargs = ptypes, rtype, varargs
        self.u.funcs.append(f)
        return f


class BodyParser(Parser):
    """Pass 2: one function body with symbols known."""

    def __init__(self, toks, syms, decl, name, natives=(), loc=None):
        self.toks = toks
        self.syms = syms
        self.decl = decl
        self.name = name
        self.loc = loc or self.default_loc
        self.cls = decl.cls
        self.p = decl.start
        from . import kcconst
        self.ctab = syms.consts if syms.consts is not None else kcconst.Table(syms)
        self.const_nodes = {}       # id(node) -> (constant name, node): literals that came from a constant

    # ---- constants -------------------------------------------------------------------------------------------
    def const_node(self, d):
        """A use of constant d: the literal its value is (the code is the typed number's / string's)."""
        e = A.Str(d.value) if isinstance(d.value, str) else A.Int(d.value)
        self.const_nodes[id(e)] = (d.name, e)
        return e

    def is_local(self, w):
        return any(w in sc for sc in self.scopes)

    def no_const_clash(self, name, what, t):
        d = self.ctab.find(self.cls, name)
        if d is not None:
            raise self.err('%s %s has the name of the constant defined at %s' % (what, name, d.loc), t)
        e = self.ctab.find_enum(self.cls, name)
        if e is not None:
            raise self.err('%s %s has the name of the enum defined at %s' % (what, name, e.loc), t)

    def not_const(self, e, op):
        c = self.const_nodes.get(id(e))
        if c is not None and c[1] is e:
            raise self.err(('assignment to the constant %s' if op == '=' else op + ' on the constant %s') % c[0])

    def function(self):
        d = self.decl
        for n in d.params:
            self.no_const_clash(n, 'parameter', Tok('id', n, d.line))
        self.params = [A.Var('param', n, i + 1) for i, n in enumerate(d.params)]
        self.notes = []
        self.hint_ids = {}
        for p, h in zip(self.params, getattr(d, 'ptypes', ()) or ()):
            if h is not None:
                p.type = self.resolve_hint(h, self.syms)
        self.rtype = self.resolve_hint(d.rtype, self.syms) if getattr(d, 'rtype', None) else None
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
        fn.notes = self.notes
        fn.end_at = d.end - 1 - d.start     # the closing brace: the epilogue's line in the debug info (kcdbg)
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
        """One statement; its first token (relative to the body's) is kept as node.at for the debug info (kcdbg)."""
        at = self.p - self.decl.start
        s = self.statement()
        s.at = at
        return s

    def statement(self):
        t = self.peek()
        if t.kind == 'id':
            w = t.text
            if w == 'var':
                self.next()
                nt = self.peek()
                name = self.ident()
                self.no_const_clash(name, 'local variable', nt)
                h = self.hint_opt()
                init = None
                if self.accept('='):
                    it = self.p
                    init = self.expr()
                self.expect(';')
                v = A.Var('local', name)
                if h is not None:
                    v.type = self.resolve_hint(h, self.syms)
                    if init is not None:
                        self.check_value(v.type, init, it, 'the value of %s' % name)
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
                if self.at('var'):
                    return self.foreach(self.p - 2)
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
                it = self.p
                e = self.expr()
                if self.rtype is not None:
                    self.check_value(self.rtype, e, it, 'the returned value')
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

    # ---- foreach ---------------------------------------------------------------------------------------------
    def table_marker(self):
        """`in table <expr>`: `table` is the marker when an expression follows it (else it is a variable)."""
        if not self.at('table'):
            return False
        n = self.peek(1)
        return n.kind in ('id', 'num', 'str') or (n.kind == 'op' and n.text in ('(', '{', '::', '!', '~'))

    def foreach(self, for_i):
        """for (var x in a) / for (var i, x in a)   - an array: element / index and element
           for (var k in table t) / for (var k, v in table t) - a table: key / key and value (a snapshot of the keys)
        Lowered to the ordinary for loop over hidden locals, in a block of its own:
           { var $c = SE_ArrayClone(a);  [tables: var $t = t; var $c = SE_TableKeys($t);]
             var $n = SE_ArraySize($c);  var $i = 0;
             for (; $i < $n; $i = $i + 1) { var x = $c[$i]; ...body } }
        The loop variables are fresh locals of each iteration: assigning them does not move the loop; `continue`
        goes to the step. It walks a snapshot (the elements / the keys at the start): changes in the body do not
        move it, removed elements are still visited."""
        self.expect('var')
        names = []
        name_toks = []
        while True:
            nt = self.peek()
            name_toks.append(self.p)
            name = self.ident()
            self.no_const_clash(name, 'local variable', nt)
            h = self.hint_opt()
            names.append((name, h))
            if not self.accept(','):
                break
        if len(names) > 2:
            raise self.err('foreach takes one or two variables: for (var x in a), for (var i, x in a), '
                           'for (var k, v in table t)')
        if not self.at('in'):
            raise self.err("expected 'in' (a foreach: for (var x in array) or for (var k, v in table t))")
        self.next()
        is_table = self.table_marker()
        if is_table:
            self.next()
        src = self.expr()
        self.expect(')')
        self.notes.append(('foreach', for_i - self.decl.start, ''))
        hid = lambda n: A.Var('local', '(foreach %s)' % n)
        c, n, i = hid('array'), hid('size'), hid('index')
        decls = []
        if is_table:
            t = hid('table')
            decls.append(A.VarDecl(t, src))
            decls.append(A.VarDecl(c, A.Native('SE_TableKeys', [A.Ref(t)])))
        else:
            # a snapshot (SE_ArrayClone, a shallow copy): removing elements in the body (dead ships ...) neither
            # skips the next one nor reads past the end; a non-array clones to an empty array
            decls.append(A.VarDecl(c, A.Native('SE_ArrayClone', [src])))
        decls.append(A.VarDecl(n, A.Native('SE_ArraySize', [A.Ref(c)])))
        decls.append(A.VarDecl(i, A.Int(0)))
        for v in decls:
            v.var.decl = v
        uvars = []
        for (name, h), ti in zip(names, name_toks):
            v = A.Var('local', name)
            if h is not None:
                v.type = self.resolve_hint(h, self.syms)
            uvars.append(v)
            self.declared(v, ti)
        item = A.Index(A.Ref(c), A.Ref(i))
        if len(uvars) == 1:
            inits = [item]
        elif is_table:
            inits = [item, A.Index(A.Ref(t), A.Ref(uvars[0]))]
        else:
            inits = [A.Ref(i), item]
        per = []
        for v, e in zip(uvars, inits):
            d = A.VarDecl(v, e)
            v.decl = d
            per.append(d)
        self.scopes.append({v.name: v for v in uvars})
        body = self.loop_block()
        self.scopes.pop()
        step = [A.ExprStmt(A.Assign(A.Ref(i), A.Bin('+', A.Ref(i), A.Int(1))))]
        loop = A.For([], A.Bin('<', A.Ref(i), A.Ref(n)), step, per + body)
        loop.at = for_i - self.decl.start      # the loop's condition, step and back jump: the line of the `for`
        return A.Block(decls + [loop])

    def declared(self, var, tok_i):
        """A local declared outside a `var` statement (foreach variables) at token tok_i: a hook for the language
        server (go to definition)."""
        pass

    # ---- type hints ------------------------------------------------------------------------------------------
    def note(self, tok_i, msg):
        self.notes.append(('warn', tok_i - self.decl.start, msg))

    def hint_of(self, h):
        """A hint of another declaration (header, field): class id, or None when it does not resolve (its own
        declaration reports that)."""
        if h is None:
            return None
        cid = self.hint_ids.get(h[0], False)
        if cid is False:
            try:
                cid = self.resolve_hint(h, self.syms)
            except ParseError:
                cid = None
            self.hint_ids[h[0]] = cid
        return cid

    def callee(self, e):
        """The Method a call goes to when the compiler can tell (direct calls; calls by name on a hinted object or
        on this), with the class the lookup started from."""
        k = e.kind
        if k in 'sgc':
            cid, name = e.target
            kl = self.syms.classes.get(cid)
            return (kl.method(name) if kl else None), cid
        T = self.type_of(e.obj) if k == 'o' else (self.cls or None)
        if T is None:
            return None, None
        owner = self.syms.find_method(T, e.target)
        return (self.syms.classes[owner].method(e.target) if owner is not None else None), T

    def type_of(self, e):
        """The class a value is known to be an object of (from hints), or None."""
        t = type(e)
        if t is A.Ref:
            return e.var.type
        if t is A.This:
            return self.cls or None
        if t is A.Assign:
            return self.type_of(e.t)
        if t is A.Mem and self.cls:
            m = self.syms.member(self.cls, e.k)
            return self.hint_of(self.syms.classes[m[0]].hints.get(m[1].name)) if m else None
        if t is A.Glob:
            f = self.syms.glob(e.k)
            return self.hint_of(self.syms.classes[0].hints.get(f.name)) if f is not None else None
        if t is A.Call:
            m, _ = self.callee(e)
            fd = m.fn if m is not None else None
            return self.hint_of(getattr(fd, 'rtype', None)) if fd is not None else None
        return None

    def is_a(self, sub, cls):
        return cls in self.syms.chain(sub)

    def check_value(self, want, e, tok_i, what):
        got = self.type_of(e)
        if got is not None and want is not None and not self.is_a(got, want):
            self.note(tok_i, '%s is a %s, not a %s' % (what, self.syms.class_name(got), self.syms.class_name(want)))

    def check_call(self, e, tok_i):
        """Calls on hinted objects: the method exists in the class chain, the argument count fits, hinted
        parameters get objects of their class; x.C::F on a hinted x: x is a C."""
        S = self.syms
        if e.kind in 'on':
            # calls by name on this (`this.F()`, `this->F()`) are left alone: a base class calling a method its
            # subclasses define is the usual virtual call
            T = self.type_of(e.obj) if e.kind == 'o' and type(e.obj) is not A.This else None
            if T is None:
                return
            owner = S.find_method(T, e.target)
            if owner is None:
                self.note(tok_i, '%s has no method %s (a call by name answers 0)' % (S.class_name(T), e.target))
                return
            m = S.classes[owner].method(e.target)
        elif e.kind == 'c':
            m = S.classes[e.target[0]].method(e.target[1]) if e.target[0] in S.classes else None
            if e.obj is not None and type(e.obj) is not A.Int:
                T = self.type_of(e.obj)
                if T is not None and not self.is_a(T, e.target[0]):
                    self.note(tok_i, '%s::%s is called on a %s, which is not a %s'
                              % (S.class_name(e.target[0]), e.target[1], S.class_name(T), S.class_name(e.target[0])))
        else:
            m = S.classes[e.target[0]].method(e.target[1]) if e.target[0] in S.classes else None
        if m is None:
            return
        fd = m.fn
        if e.kind in 'on':
            owner_name = '%s::%s' % (S.class_name(owner), m.name) if owner else m.name
            n = len(e.args)
            if n > m.nparams:
                self.note(tok_i, '%d arguments to %s, which takes %d' % (n, owner_name, m.nparams))
            elif n < m.nparams and not getattr(fd, 'varargs', False):
                self.note(tok_i, '%d argument%s to %s, which takes %d (a call by name passes 0 for the rest)'
                          % (n, '' if n == 1 else 's', owner_name, m.nparams))
        for j, (a, h) in enumerate(zip(e.args, getattr(fd, 'ptypes', None) or ())):
            want = self.hint_of(h)
            if want is not None:
                self.check_value(want, a, tok_i, 'argument %d of %s' % (j + 1, m.name))

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
            self.not_const(e, op)
            it = self.p
            v = self.expr()
            if op == '=' and type(e) in (A.Ref, A.Mem, A.Glob):
                want = self.type_of(e)
                if want is not None:
                    self.check_value(want, v, it, 'the value assigned')
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
        self.not_const(e, op)
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
            if t.text == '-' and self.peek().kind == 'id' and self.peek().text not in ('new', 'delete'):
                e = self.primary()
                c = self.const_nodes.get(id(e))
                if c is not None and c[1] is e and type(e) is A.Int:
                    # -NAME is the negative literal, as -5 is (not a negation of 5)
                    e = A.Int(wrap32(-e.v))
                    self.const_nodes[id(e)] = (c[0], e)
                    return self.postfix(e)
                if type(e) is ClassConst and not self.at('.'):
                    return self.postfix(A.Int(wrap32(-e.id)))        # -Ship is the literal -2004 as well
                return A.Un('-', self.postfix(e))
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
                ni = self.p
                name = self.ident()
                if self.accept('::'):
                    ni = self.p
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
                self.check_call(e, ni)
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
                ni = self.p
                name = self.ident()
                e = A.Call('n', name, self.call_args())
                self.check_call(e, ni)
                return e
            return A.This()
        if w in ('__m', '__g', '__f64', '__and', '__or', '__opland', '__oplor') and self.at('('):
            return self.builtin(w)
        if self.at('::'):
            self.next()
            ni = self.p
            name = self.ident()
            args = self.call_args()
            if w == 'native':
                return A.Native(name, args)
            if w == '__global':
                return A.Call('s', (0, name), args)
            cid = self.class_id(w)
            if not (self.syms.classes.get(cid) and self.syms.classes[cid].method(name)):
                raise self.err('unknown method %s::%s' % (w, name))
            e = A.Call('s', (cid, name), args)
            self.check_call(e, ni)
            return e
        if self.at('('):
            ni = self.p - 1
            args = self.call_args()
            if self.cls:
                owner = self.syms.find_method(self.cls, w)
                if owner is not None:
                    e = A.Call('s', (owner, w), args)
                    self.check_call(e, ni)
                    return e
            if self.syms.global_function(w):
                e = A.Call('g', (0, w), args)
                self.check_call(e, ni)
                return e
            return A.Native(w, args)
        if self.at('.') and self.peek(1).kind == 'id' and not (self.at('(', 2) or self.at('::', 2)) \
                and not self.is_local(w):
            from .kcconst import ConstDef
            mt = self.peek(1)
            d = self.ctab.qualified(self.cls, w, mt.text)
            if isinstance(d, ConstDef):
                self.p += 2
                return self.const_node(d)
            if d == 'noenum':
                raise self.err('enum %s has no member %s' % (w, mt.text), mt)
            if d == 'noclass':
                raise self.err('class %s has no constant %s (a method call needs its arguments)' % (w, mt.text), mt)
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
        d = self.ctab.find(self.cls, w)
        if d is not None:
            return self.const_node(d)
        if self.ctab.find_enum(self.cls, w) is not None:
            raise self.err('enum %s is not a value: name a member (%s.<member> or the member alone)' % (w, w), t)
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
