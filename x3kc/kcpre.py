"""Conditional compilation of KC sources (compiler only; the decompiler never writes it).

    #ifdef NAME / #ifndef NAME / #if <expr> / #elif <expr> / #else / #endif

A directive is a line whose first non-blank character is `#` followed by one of these words. Lines of an inactive
branch and the directive lines themselves become empty lines, so every other line keeps its number and messages
point at the right line of the right part. Each part of a split project must close its own `#if`.

Names: `-D NAME[=V]` on the command line (`-D NAME` is 1; V is an integer, or a string compared with `==`/`!=`),
then top-level `const NAME = ...;` declarations of the project that stand outside every `#if` block (integer
literals, strings, other such constants and the operators of `#if`). An unknown name in `#if` is an error, not 0
as in C: `#ifdef NAME` / `defined(NAME)` test whether a -D name or such a constant exists.

`#if` expressions: integers, strings, names, `defined(NAME)` / `defined NAME`, parentheses, unary `! - ~ +`, and
`* / % + - << >> < > <= >= == != & ^ | && ||` with C precedence; 32-bit arithmetic as the VM does it.
"""
import re

from . import kcparse

DIRECTIVE = re.compile(r'^[ \t]*#[ \t]*(ifdef|ifndef|if|elif|else|endif)\b(.*)$')
QUICK = re.compile(r'^[ \t]*#[ \t]*(?:if|el|endif)', re.M)


class PreError(Exception):
    pass


def parse_defines(items):
    """['NAME', 'NAME=5', 'NAME=text'] -> {NAME: value}. -D NAME is 1."""
    out = {}
    for it in items or ():
        name, eq, v = it.partition('=')
        name = name.strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise PreError('-D %s: a name is expected (-D NAME or -D NAME=value)' % it)
        if not eq:
            out[name] = 1
            continue
        v = v.strip()
        try:
            out[name] = kcparse.wrap32(int(v, 0))
        except ValueError:
            out[name] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v
    return out


def has_directives(text):
    return QUICK.search(text) is not None


class Consts:
    """The top-level constants outside any #if block of the given texts, evaluated on demand."""

    def __init__(self, texts):
        self.texts = texts
        self.toks = None
        self.values = {}
        self.busy = set()

    def scan(self):
        self.toks = {}
        for text in self.texts:
            try:
                toks = kcparse.lex(text)
            except kcparse.ParseError:
                continue                    # the compile reports it
            depth = cond = 0
            i = 0
            while i < len(toks):
                t = toks[i]
                if t.kind == 'dir':
                    m = DIRECTIVE.match(t.text)
                    if m and m.group(1) in ('if', 'ifdef', 'ifndef'):
                        cond += 1
                    elif m and m.group(1) == 'endif':
                        cond -= 1
                elif t.kind == 'op' and t.text == '{':
                    depth += 1
                elif t.kind == 'op' and t.text == '}':
                    depth -= 1
                elif depth == 0 and cond == 0 and t.kind == 'id' and t.text == 'const' and \
                        toks[i + 1].kind == 'id' and toks[i + 2].text == '=':
                    j = i + 3
                    while toks[j].kind != 'eof' and toks[j].text != ';':
                        j += 1
                    self.toks.setdefault(toks[i + 1].text, toks[i + 3:j])
                    i = j
                i += 1

    def has(self, name):
        if self.toks is None:
            self.scan()
        return name in self.toks

    def get(self, name, ev):
        if name in self.values:
            return self.values[name]
        if self.toks is None:
            self.scan()
        if name not in self.toks:
            return None
        if name in self.busy:
            raise PreError('constant %s refers to itself' % name)
        self.busy.add(name)
        try:
            v = ev.sub(self.toks[name] + [kcparse.Tok('eof', '', 0)])
        finally:
            self.busy.discard(name)
        self.values[name] = v
        return v


class Eval:
    LEVELS = [('||',), ('&&',), ('|',), ('^',), ('&',), ('==', '!='), ('<', '>', '<=', '>='), ('<<', '>>'),
              ('+', '-'), ('*', '/', '%')]

    def __init__(self, defines, consts):
        self.defines, self.consts = defines, consts
        self.skip = 0           # > 0 while parsing the side && / || has already decided

    def run(self, text):
        try:
            toks = kcparse.lex(text)
        except kcparse.ParseError as x:
            raise PreError(str(x).split(': ', 1)[-1])
        return self.sub(toks)

    def sub(self, toks):
        saved = getattr(self, 't', None), getattr(self, 'p', 0)
        self.t, self.p = toks, 0
        try:
            if self.t[0].kind == 'eof':
                raise PreError('an expression is expected')
            v = self.binary(0)
            if self.t[self.p].kind != 'eof':
                raise PreError('unexpected %r' % self.t[self.p].text)
            return v
        finally:
            self.t, self.p = saved

    def peek(self):
        return self.t[self.p]

    def accept(self, s):
        t = self.peek()
        if t.kind in ('op', 'id') and t.text == s:
            self.p += 1
            return True
        return False

    def ints(self, op, *xs):
        for x in xs:
            if not isinstance(x, int):
                raise PreError('%s needs integers' % op)

    def binary(self, lv):
        if lv == len(self.LEVELS):
            return self.unary()
        a = self.binary(lv + 1)
        while self.peek().kind == 'op' and self.peek().text in self.LEVELS[lv]:
            op = self.t[self.p].text
            self.p += 1
            # && and || short-circuit as in C: `defined(X) && X > 1` does not resolve X when it is not defined;
            # the decided right side is only parsed (skip: no name lookups, no arithmetic errors)
            decided = (op == '&&' and not a) or (op == '||' and a)
            if decided:
                self.skip += 1
            try:
                b = self.binary(lv + 1)
            finally:
                if decided:
                    self.skip -= 1
            a = int(bool(a)) if decided else self.apply(op, a, b)
        return a

    def apply(self, op, a, b):
        if self.skip:
            return 0
        if op == '||':
            return int(bool(a) or bool(b))
        if op == '&&':
            return int(bool(a) and bool(b))
        if op == '==':
            return int(a == b)
        if op == '!=':
            return int(a != b)
        self.ints(op, a, b)
        w = kcparse.wrap32
        if op in ('/', '%'):
            if b == 0:
                raise PreError('division by zero')
            q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
            return w(q if op == '/' else a - q * b)
        if op in ('<<', '>>'):
            if not 0 <= b <= 31:
                raise PreError('shift by %d' % b)
            return w(a << b) if op == '<<' else a >> b
        return {'+': lambda: w(a + b), '-': lambda: w(a - b), '*': lambda: w(a * b), '&': lambda: a & b,
                '|': lambda: a | b, '^': lambda: a ^ b, '<': lambda: int(a < b), '>': lambda: int(a > b),
                '<=': lambda: int(a <= b), '>=': lambda: int(a >= b)}[op]()

    def unary(self):
        t = self.peek()
        if t.kind == 'op' and t.text in ('!', '-', '~', '+'):
            self.p += 1
            v = self.unary()
            if t.text == '!':
                return int(not v)
            if self.skip:
                return 0
            self.ints(t.text, v)
            return kcparse.wrap32(-v if t.text == '-' else ~v if t.text == '~' else v)
        return self.atom()

    def atom(self):
        t = self.peek()
        self.p += 1
        if t.kind == 'num':
            v = kcparse.parse_number(t.text)
            if isinstance(v, float):
                raise PreError('%s: #if takes integers' % t.text)
            return v
        if t.kind == 'str':
            return kcparse.unescape(t.text)
        if t.kind == 'op' and t.text == '(':
            v = self.binary(0)
            if not self.accept(')'):
                raise PreError("')' expected")
            return v
        if t.kind == 'id' and t.text == 'defined':
            par = self.accept('(')
            n = self.peek()
            if n.kind != 'id':
                raise PreError('defined: a name is expected')
            self.p += 1
            if par and not self.accept(')'):
                raise PreError("')' expected")
            return int(n.text in self.defines or (self.consts is not None and self.consts.has(n.text)))
        if t.kind == 'id':
            if t.text in self.defines:
                return self.defines[t.text]
            if self.skip:
                return 0
            v = self.consts.get(t.text, self) if self.consts is not None else None
            if v is None:
                raise PreError('unknown name %s in #if (not a -D name nor a top-level const outside #if blocks; '
                               '#ifdef %s or defined(%s) tests whether it is defined)' % (t.text, t.text, t.text))
            return v
        raise PreError('unexpected %r' % t.text if t.kind != 'eof' else 'the expression ends too early')


def process(text, name, defines, consts=None):
    """Text with the inactive branches and the directive lines blanked (line numbers kept). name: the file name
    for messages (`part.kc:line: ...`). consts: a Consts over the whole project (for #if NAME of a const)."""
    if not has_directives(text):
        return text
    ev = Eval(defines, consts)
    lines = text.split('\n')        # the lexer counts '\n' only (latin1 \x85, \x0b, \x0c ... are no line breaks)
    out = []
    stack = []          # [active of the enclosing level, this branch active, some branch taken, has else, line]
    active = True
    in_comment = False  # inside a /* */ comment at the start of the line: a '#' line there is no directive
    for n, line in enumerate(lines, 1):
        m = None if in_comment else DIRECTIVE.match(line.rstrip('\r'))
        in_comment = comment_after(line, in_comment)
        if not m:
            out.append(line if active else blank(line))
            continue
        word, rest = m.group(1), strip_comments(m.group(2))
        where = '%s:%d: ' % (name, n)
        if in_comment:      # the directive line is blanked: a comment it opens would lose its start
            raise PreError(where + 'a /* comment after a directive must close on its line')

        def cond():
            if word in ('ifdef', 'ifndef'):
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', rest):
                    raise PreError(where + '#%s takes one name' % word)
                d = rest in defines or (consts is not None and consts.has(rest))
                return d if word == 'ifdef' else not d
            if not rest:
                raise PreError(where + '#%s needs an expression' % word)
            try:
                return bool(ev.run(rest))
            except PreError as x:
                raise PreError(where + '#%s: %s' % (word, x))
        if word in ('if', 'ifdef', 'ifndef'):
            c = cond() if active else False
            stack.append([active, c, c, False, n])
            active = active and c
        elif word == 'elif':
            if not stack or stack[-1][3]:
                raise PreError(where + ('#elif after #else' if stack else '#elif without #if'))
            top = stack[-1]
            c = (not top[2]) and top[0] and cond()
            top[1] = c
            top[2] = top[2] or c
            active = c
        elif word == 'else':
            if not stack or stack[-1][3]:
                raise PreError(where + ('#else twice' if stack else '#else without #if'))
            top = stack[-1]
            if rest:
                raise PreError(where + '#else takes nothing after it')
            top[3] = True
            top[1] = top[0] and not top[2]
            top[2] = True
            active = top[1]
        else:
            if not stack:
                raise PreError(where + '#endif without #if')
            if rest:
                raise PreError(where + '#endif takes nothing after it')
            active = stack.pop()[0]
        out.append(blank(line))
    if stack:
        raise PreError('%s:%d: #if without #endif' % (name, stack[-1][4]))
    return '\n'.join(out)


def blank(line):
    return '\r' if line.endswith('\r') else ''


def comment_after(line, in_comment):
    """Whether a /* */ comment is open at the end of the line (strings and // comments respected)."""
    i, n = 0, len(line)
    while i < n:
        if in_comment:
            j = line.find('*/', i)
            if j < 0:
                return True
            in_comment, i = False, j + 2
            continue
        c = line[i]
        if c == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == '\\' else 1
            i += 1
        elif line.startswith('//', i):
            return False
        elif line.startswith('/*', i):
            in_comment, i = True, i + 2
        else:
            i += 1
    return in_comment


def strip_comments(rest):
    """The text after a directive word without its /* */ and // comments (an unclosed /* runs to the end)."""
    rest = re.sub(r'/\*.*?(\*/|$)', ' ', rest)
    return rest.split('//')[0].strip()
