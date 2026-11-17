"""MSCI sugar (.xms): a source layer over the editor text form that lowers to plain MSCI text, then to script XML
through `msci new` (msci_asm.new_script_text), unchanged. Stock editor text is valid .xms and builds the same.

    x3kc msci build <db.json> <src.xms> [-o out.xml] [--text] [-D NAME[=V]]... [--scripts DIR] [--engine N]
                    [--no-types] [--addon ap|fl]

Layers, in order (docs/msci_manual.md, "MSCI sugar"):
  1. preprocessor: #define (object- and function-like, `\\` continues a line), #undef, #ifdef / #ifndef / #if /
     #elseif / #else / #endif (optionally `NAME op value`), #include "path", #error, #warning; -D on the command
     line. Every line keeps the file and line it came from.
  2. lowering: function name($a, $b) { } (private variables $fn.name.x, called by gosub, result in $fn.name),
     sub name() { } (label + body + endsub, shared variables), foreach [$i,] $x in <value> { } (a while with a
     hidden index), $x += e, -=, *=, /=, $x++ / $x--, $a = $b = e. Functions and subs are placed after the main body.
  3. the assembler on the lowered text; its error is mapped back to the source line.
  4. type warnings: variable types inferred from command results (data/msci_semantics_<ap|fl>.json `returns`),
     header argument types, literals and expressions; a known type passed to a parameter of another type warns.
Messages are `file:line:col: message` (`warning: ...` for warnings).
"""
import json
import os
import re
import sys

from . import msci
from . import msci_asm

NAME = r'[A-Za-z_]\w*(?:\.\w+)*'
VAR = r'\$[\w.]+'
KEYWORDS = {'if', 'else', 'end', 'while', 'break', 'continue', 'skip', 'do', 'gosub', 'goto', 'endsub', 'return',
            'function', 'sub', 'foreach', 'in', 'not', 'null', 'START'}


class Diag:
    def __init__(self, file, line, col, msg, severity='error'):
        self.file, self.line, self.col, self.msg, self.severity = file, line, col, msg, severity

    def __str__(self):
        return '%s:%d:%d: %s%s' % (self.file, self.line, self.col, '' if self.severity == 'error' else 'warning: ',
                                   self.msg)

    __repr__ = __str__


class SugarError(Exception):
    def __init__(self, diags):
        Exception.__init__(self, '\n'.join(str(d) for d in diags))
        self.diags = diags


class Line:
    """A source line after preprocessing: its text and where it came from (1-based line and column)."""
    __slots__ = ('text', 'file', 'line', 'col')

    def __init__(self, text, file, line, col=None):
        self.text, self.file, self.line = text, file, line
        self.col = col if col is not None else len(text) - len(text.lstrip()) + 1

    def at(self, col=None):
        return self.file, self.line, col if col is not None else self.col


def outside_quotes(text, fn):
    """Apply fn to the parts of text that are not inside '...' strings."""
    parts = re.split(r"('[^']*')", text)
    return ''.join(p if k % 2 else fn(p) for k, p in enumerate(parts))


def unquoted(s, fill=' '):
    """s with the inside of every '...' string blanked (same length)."""
    return re.sub(r"'[^']*'", lambda m: "'" + fill * (len(m.group()) - 2) + "'", s)


def read_file(p, errors='strict'):
    with open(p, encoding='utf-8-sig', errors=errors) as f:
        return f.read()


def indent(ln):
    return ln.text[:len(ln.text) - len(ln.text.lstrip())]


def one_group(e):
    """e is one parenthesised group: its first `(` closes at its last character."""
    e = unquoted(e, 'x')
    if not (e.startswith('(') and e.endswith(')')):
        return False
    depth = 0
    for k, c in enumerate(e):
        depth += (c == '(') - (c == ')')
        if depth == 0:
            return k == len(e) - 1
    return False


def split_args(s):
    """Top-level comma-separated arguments (quotes and parentheses respected); None when unbalanced."""
    out, depth, cur, q = [], 0, '', False
    for c in s:
        if q:
            cur += c
            q = c != "'"
            continue
        if c == "'":
            q = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth < 0:
                return None
        elif c == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
            continue
        cur += c
    if q or depth:
        return None
    if cur.strip() or out:
        out.append(cur.strip())
    return out


# ---- preprocessor ------------------------------------------------------------------------------------------------
DIRECTIVE_RX = re.compile(r'^\s*#\s*([A-Za-z]+)\b\s*(.*?)\s*$')
DEFINE_RX = re.compile(r'^([A-Za-z_]\w*)(\(([^)]*)\))?(?:\s+(.*))?$', re.S)
COND_RX = re.compile(r'^([A-Za-z_]\w*)(?:\s*(==|!=|<=|>=|<|>)\s*(.*))?$')
IDENT_RX = re.compile(r'(?<![\w.$\[])[A-Za-z_]\w*(?![\w.])')


class Macro:
    def __init__(self, name, params, body, loc):
        self.name, self.params, self.body, self.loc = name, params, body, loc


class Preprocessor:
    """Lines of a file with its directives done and macros expanded; reader(path) -> text (the LSP passes the
    open buffer)."""

    def __init__(self, defines=(), reader=None):
        self.macros = {}
        self.diags = []
        self.reader = reader or read_file
        for d in defines:
            k, _, v = d.partition('=')
            self.macros[k.strip()] = Macro(k.strip(), None, v if '=' in d else '1', ('<command line>', 0, 0))

    def error(self, loc, msg, severity='error'):
        self.diags.append(Diag(loc[0], loc[1], loc[2], msg, severity))

    def run(self, path, text=None):
        out = []
        self._file(os.path.abspath(path), path, text, [], out)
        return out

    def _file(self, apath, shown, text, stack, out):
        if text is None:
            text = self.reader(apath)
        lines = text.replace('\r', '').split('\n')
        if lines and lines[-1] == '':        # the line break of the last line
            lines.pop()
        stack = stack + [apath]
        conds = []          # [active, taken, parent active, else seen, loc]
        i = 0
        while i < len(lines):
            raw, no = lines[i], i + 1
            i += 1
            m = DIRECTIVE_RX.match(raw)
            active = all(c[0] for c in conds)
            if not m:
                if active:
                    s = raw.strip()
                    if s.startswith('*') or s.startswith('//') or not self.macros:
                        out.append(Line(raw, shown, no))
                    else:
                        exp = self.expand(raw, (shown, no, len(raw) - len(raw.lstrip()) + 1))
                        if exp == raw:
                            out.append(Line(raw, shown, no))
                        else:
                            ind = raw[:len(raw) - len(raw.lstrip())]
                            for part in exp.split('\n'):
                                out.append(Line(ind + part.strip(), shown, no, len(ind) + 1))
                continue
            word, rest = m.group(1), m.group(2)
            col = raw.index('#') + 1
            loc = (shown, no, col)
            while rest.endswith('\\'):        # continued directive: each continued part is its own line of the body
                rest = rest[:-1].rstrip()
                if i >= len(lines):
                    break
                rest += '\n' + lines[i].strip()
                i += 1
            if word in ('ifdef', 'ifndef', 'if'):
                ok = active and self.cond(rest, loc, value=word == 'if')
                if word == 'ifndef':
                    ok = active and not self.cond(rest, loc)
                conds.append([ok, ok, active, False, loc])
                continue
            if word in ('elseif', 'elif', 'elifdef', 'elseifdef', 'elifndef', 'elseifndef'):
                if not conds:
                    self.error(loc, '#%s without #ifdef' % word)
                    continue
                c = conds[-1]
                if c[3]:
                    self.error(loc, '#%s after #else' % word)
                ok = c[2] and not c[1] and (not self.cond(rest, loc) if word.endswith('ndef') else
                                            self.cond(rest, loc, value=word in ('elseif', 'elif')))
                c[0] = ok
                c[1] = c[1] or ok
                continue
            if word == 'else':
                if not conds:
                    self.error(loc, '#else without #ifdef')
                    continue
                c = conds[-1]
                if c[3]:
                    self.error(loc, '#else after #else')
                c[0] = c[2] and not c[1]
                c[1] = True
                c[3] = True
                continue
            if word == 'endif':
                if not conds:
                    self.error(loc, '#endif without #ifdef')
                else:
                    conds.pop()
                continue
            if not active:
                continue
            if word == 'define':
                d = DEFINE_RX.match(rest)
                if not d:
                    self.error(loc, '#define needs a name')
                    continue
                params = None
                if d.group(2):
                    params = [p.strip() for p in d.group(3).split(',')] if d.group(3).strip() else []
                    if any(not re.fullmatch(r'[A-Za-z_]\w*', p) for p in params) or len(set(params)) < len(params):
                        self.error(loc, 'bad parameter list of macro %s' % d.group(1))
                        continue
                body = (d.group(4) or '').strip()
                old = self.macros.get(d.group(1))
                if old and (old.body != body or old.params != params):
                    self.error(loc, 'macro %s redefined (first at %s:%d)' % (d.group(1), old.loc[0], old.loc[1]),
                               'warning')
                self.macros[d.group(1)] = Macro(d.group(1), params, body, loc)
            elif word == 'undef':
                self.macros.pop(rest.strip(), None)
            elif word == 'include':
                im = re.fullmatch(r'"([^"]+)"', rest.strip())
                if not im:
                    self.error(loc, '#include needs a "path"')
                    continue
                p = os.path.abspath(os.path.join(os.path.dirname(apath), im.group(1)))
                if p in stack:
                    self.error(loc, '#include cycle: %s' % ' -> '.join(os.path.basename(x) for x in stack + [p]))
                    continue
                try:
                    t = self.reader(p)
                except OSError as x:
                    self.error(loc, 'cannot read %s: %s' % (im.group(1), x.strerror or x))
                    continue
                self._file(p, os.path.normpath(os.path.join(os.path.dirname(shown), im.group(1))).replace(os.sep, '/'), t, stack, out)
            elif word == 'error':
                self.error(loc, '#error %s' % rest)
            elif word == 'warning':
                self.error(loc, '#warning %s' % rest, 'warning')
            else:
                self.error(loc, 'unknown directive #%s' % word)
        for c in conds:
            self.error(c[4], '#if without #endif')

    def cond(self, s, loc, value=False):
        """NAME: defined (#ifdef) or, with value (#if, #elseif), defined with a value other than 0 or empty, as in C;
        NAME <op> value: defined and the comparison holds."""
        m = COND_RX.match(s.strip())
        if not m:
            self.error(loc, 'condition: NAME or NAME <op> value expected')
            return False
        mac = self.macros.get(m.group(1))
        if not m.group(2):
            if mac is None or not value:
                return mac is not None
            return self.expand(mac.body, loc).strip() not in ('', '0')
        if mac is None:
            return False
        a = self.expand(mac.body, loc).strip()
        b = m.group(3).strip()
        unq = lambda v: v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in '\'"' else v
        a, b = unq(a), unq(b)
        if re.fullmatch(r'-?\d+', a) and re.fullmatch(r'-?\d+', b):
            a, b = int(a), int(b)
        op = m.group(2)
        try:
            return {'==': a == b, '!=': a != b, '<': a < b, '>': a > b, '<=': a <= b, '>=': a >= b}[op]
        except TypeError:
            self.error(loc, 'cannot compare %r with %r' % (a, b))
            return False

    def expand(self, text, loc, active=frozenset()):
        out, i = [], 0
        while i < len(text):
            c = text[i]
            if c == "'":
                j = text.find("'", i + 1)
                if j < 0 or (0 < i < len(text) - 1 and text[i - 1].isalnum() and text[i + 1].isalnum()):
                    out.append(c)       # an apostrophe (Duke's Haven, Kha'ak) or an unclosed quote: not a string
                    i += 1
                    continue
                out.append(text[i:j + 1])
                i = j + 1
                continue
            m = IDENT_RX.match(text, i) if (c.isalpha() or c == '_') else None
            if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in '_.$[')):
                name = m.group()
                mac = self.macros.get(name)
                if mac is None or name in active:
                    out.append(name)
                    i = m.end()
                    continue
                if mac.params is None:
                    out.append(self.expand(mac.body, loc, active | {name}))
                    i = m.end()
                    continue
                j = m.end()
                while j < len(text) and text[j] == ' ':
                    j += 1
                if j >= len(text) or text[j] != '(':
                    out.append(name)        # a function-like macro without arguments is not expanded
                    i = m.end()
                    continue
                depth, k, q = 0, j, False
                while k < len(text):
                    ch = text[k]
                    if q:
                        q = ch != "'"
                    elif ch == "'":
                        q = True
                    elif ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                if k >= len(text):
                    self.error(loc, 'macro %s: missing )' % name)
                    out.append(text[i:])
                    break
                args = split_args(text[j + 1:k]) or []
                if len(args) != len(mac.params):
                    self.error(loc, 'macro %s takes %d argument(s), %d given' % (name, len(mac.params), len(args)))
                    out.append(text[i:k + 1])
                    i = k + 1
                    continue
                sub = dict(zip(mac.params, args))
                body = outside_quotes(mac.body, lambda p: re.sub(r'(?<![\w.$\[])[A-Za-z_]\w*(?![\w.])',
                                                                lambda t: sub.get(t.group(), t.group()), p))
                out.append(self.expand(body, loc, active | {name}))
                i = k + 1
                continue
            if m:
                out.append(m.group())
                i = m.end()
                continue
            out.append(c)
            i += 1
        return ''.join(out)


# ---- lowering ----------------------------------------------------------------------------------------------------
FUNC_RX = re.compile(r'^function\s+(' + NAME + r')\s*\(([^)]*)\)\s*(\{)?\s*$')
SUB_RX = re.compile(r'^sub\s+(' + NAME + r')\s*(?:\(\s*\))?\s*(\{)?\s*$')
FOREACH_RX = re.compile(r'^foreach\s+(' + VAR + r')(?:\s*,\s*(' + VAR + r'))?\s+in\s+(.+?)\s*\{\s*$')
HEADER_START_RX = re.compile(r'^(function|sub|foreach)\b')
CALL_RX = re.compile(r'^(' + NAME + r')\s*\((.*)\)$')
ASSIGN_RX = re.compile(r'^(' + VAR + r')\s*=(?!=)\s*(.*)$')
COMPOUND_RX = re.compile(r'^(' + VAR + r')\s*([-+*/])=(?!=)\s*(.+)$')
INCDEC_RX = re.compile(r'^(' + VAR + r')\s*(\+\+|--)$')
LABEL_RX = re.compile(r'^([\w.]+):$')
JUMP_RX = re.compile(r'\b(goto label|gosub)\s+([\w.]+)')
RETURN_RX = re.compile(r'^return(?:\s+(.*))?$')
GUARD_RX = re.compile(r'^(skip if not|skip if|do if)\s+(.*)$')


class Block:
    def __init__(self, kind, name, params, line):
        self.kind, self.name, self.params, self.line = kind, name, params, line
        self.body, self.pre = [], []
        self.exit_used = self.has_value = False


class Scope:
    def __init__(self, fn=None, labels=()):
        self.fn, self.labels = fn, set(labels)

    def var(self, name):
        return '$fn.%s.%s' % (self.fn, name) if self.fn else '$' + name

    def label(self, name):
        return 'fn.%s.%s' % (self.fn, name) if self.fn and name in self.labels else name

    def rename(self, text):
        if not self.fn:
            return text

        def part(p):
            p = re.sub(VAR, lambda m: self.var(m.group()[1:]), p)
            return JUMP_RX.sub(lambda m: '%s %s' % (m.group(1), self.label(m.group(2))), p)
        s = text.strip()
        m = LABEL_RX.match(s)
        if m:
            return text.replace(m.group(1), self.label(m.group(1)), 1)
        return outside_quotes(text, part)


class Lowerer:
    def __init__(self, A=None):
        self.A = A
        self.diags = []
        self.fe = 0
        self.calls = {}         # caller function (None = main/sub) -> {callee}

    def error(self, ln, msg, col=None, severity='error'):
        f, l, c = ln.at(col)
        self.diags.append(Diag(f, l, c, msg, severity))

    def run(self, lines):
        """Preprocessed lines -> (header lines, body [(text, Line)])."""
        lines = self.join_braces(lines)
        header, main, blocks = [], [], []
        i = 0
        while i < len(lines):
            ln = lines[i]
            s = ln.text.strip()
            i += 1
            if s.startswith('//'):
                header.append(ln)
                continue
            fm, sm = FUNC_RX.match(s), SUB_RX.match(s)
            if sm and not sm.group(2):
                sm = None           # `sub x` without `{`: an ordinary line (no command starts so, but be safe)
            if fm or sm:
                if fm:
                    params = [p.strip() for p in fm.group(2).split(',')] if fm.group(2).strip() else []
                    b = Block('function', fm.group(1), [], ln)
                    for p in params:
                        if not re.fullmatch(VAR, p):
                            self.error(ln, 'function %s: parameter %r is not a $variable' % (b.name, p))
                        elif p[1:] in b.params:
                            self.error(ln, 'function %s: parameter %s given twice' % (b.name, p))
                        else:
                            b.params.append(p[1:])
                    brace = fm.group(3)
                else:
                    b = Block('sub', sm.group(1), [], ln)
                    brace = sm.group(2)
                if not brace:
                    self.error(ln, '%s %s: `{` expected at the end of the line' % (b.kind, b.name))
                    continue
                while main and (main[-1].text.strip().startswith('*') or not main[-1].text.strip()):
                    b.pre.insert(0, main.pop())         # comments right above belong to the block
                depth = 1
                while i < len(lines):
                    l2 = lines[i]
                    s2 = l2.text.strip()
                    i += 1
                    if s2.startswith('//'):
                        header.append(l2)
                        continue
                    if FUNC_RX.match(s2) or SUB_RX.match(s2):
                        self.error(l2, '%s inside %s %s: define it at the top level' % (s2.split()[0], b.kind, b.name))
                        continue
                    if s2 == '}':
                        depth -= 1
                        if depth == 0:
                            break
                    elif HEADER_START_RX.match(s2) and s2.endswith('{'):
                        depth += 1
                    b.body.append(l2)
                else:
                    self.error(ln, '%s %s without a closing }' % (b.kind, b.name))
                blocks.append(b)
                continue
            main.append(ln)             # a `}` here closes a foreach of the main body (block() checks)
        # names
        self.funcs = {}
        self.subs = {}
        for b in blocks:
            if b.name in self.funcs or b.name in self.subs:
                self.error(b.line, '%s %s defined twice' % (b.kind, b.name))
                continue
            if b.name in KEYWORDS:
                self.error(b.line, '%s name %s is a keyword' % (b.kind, b.name))
            if self.A is not None and b.name in self.A.by_word:
                cid = self.A.by_word[b.name][0][0]
                self.error(b.line, '%s name %s is also the first word of command %d (%s)' % (
                    b.kind, b.name, cid, self.A.p.cmdtext(cid).replace('%', '')))
            (self.funcs if b.kind == 'function' else self.subs)[b.name] = b
        for n, b in self.funcs.items():
            for o in self.funcs:
                if n != o and n.startswith(o + '.'):
                    # $fn.<o>.<x> are o's variables: $fn.vec.len would be both a local of vec and vec.len's result
                    self.error(b.line, 'function %s: the name extends function %s (line %d), whose variables are '
                                       '$fn.%s.*; rename one of them' % (n, o, self.funcs[o].line.line, o))
        top_labels = {}
        for ln in main + [l for b in blocks if b.kind == 'sub' for l in b.body]:
            m = LABEL_RX.match(ln.text.strip())
            if m:
                top_labels.setdefault(m.group(1), ln)
        for n, b in list(self.funcs.items()) + list(self.subs.items()):
            if n in top_labels:
                self.error(top_labels[n], 'label %s has the name of %s %s (line %d)' % (n, b.kind, n, b.line.line))
            if 'fn.' + n in top_labels:
                self.error(top_labels['fn.' + n], 'label fn.%s is reserved for function %s' % (n, n))
        # bodies
        body = []
        self.owner = None           # who makes the calls block() lowers: None (main), a sub's or a function's name
        self.block(main, Scope(), body, None)
        if blocks:
            code = [t.strip().lstrip('@').strip() for t, _ in body if t.strip() and not t.strip().startswith('*')]
            last = code[-1] if code else ''
            guarded = len(code) > 1 and GUARD_RX.match(code[-2])
            if not (last.startswith('return') or last.startswith('goto label')) or guarded:
                body.append(('return null', main[-1] if main else blocks[0].line))   # never run into the blocks
        for b in blocks:
            for ln in b.pre:
                body.append((ln.text, ln))
            self.owner = b.name
            if b.kind == 'sub':
                body.append(('%s:' % b.name, b.line))
                self.block(b.body, Scope(), body, None)
                body.append(('endsub', b.line))
                continue
            labels = [LABEL_RX.match(l.text.strip()).group(1) for l in b.body if LABEL_RX.match(l.text.strip())]
            if 'exit' in labels:
                self.error(b.line, 'function %s: the label exit is reserved inside a function' % b.name)
            sc = Scope(b.name, labels)
            inner = []
            self.block(b.body, sc, inner, b)
            body.append(('fn.%s:' % b.name, b.line))
            if b.has_value:
                body.append(('$fn.%s = null' % b.name, b.line))
            body += inner
            if b.exit_used:
                body.append(('fn.%s.exit:' % b.name, b.line))
            body.append(('endsub', b.line))
        self.recursion()
        self.check_labels(body)
        return header, body

    def join_braces(self, lines):
        """`function f()` / `sub s` / `foreach ...` with `{` alone on the next line: one line."""
        out = []
        for ln in lines:
            if ln.text.strip() == '{' and out:
                k = len(out) - 1
                while k >= 0 and not out[k].text.strip():
                    k -= 1
                if k >= 0 and HEADER_START_RX.match(out[k].text.strip()) and not out[k].text.rstrip().endswith('{'):
                    p = out[k]
                    out[k] = Line(p.text.rstrip() + ' {', p.file, p.line, p.col)
                    continue
            out.append(ln)
        return out

    def recursion(self):
        def reach(f, seen):
            for g in self.calls.get(f, ()):
                if g not in seen:
                    seen.add(g)
                    reach(g, seen)
            return seen
        for f in self.funcs:
            if f in reach(f, set()):
                self.error(self.funcs[f].line, 'function %s is recursive: its variables are shared by every call '
                                               '(there is no stack), so a nested call overwrites them' % f,
                           severity='warning')

    def check_labels(self, body):
        defs = {}
        for t, ln in body:
            s = t.strip().lstrip('@').strip()
            m = LABEL_RX.match(s)
            if m and not s.startswith('*'):
                if m.group(1) in defs:
                    self.error(ln, 'label %s defined twice (first at line %d)' % (m.group(1), defs[m.group(1)].line))
                else:
                    defs[m.group(1)] = ln
        for t, ln in body:
            s = t.strip().lstrip('@').strip()
            if s.startswith('*'):
                continue
            for m in JUMP_RX.finditer(unquoted(s)):
                if m.group(2) not in defs:
                    shown = m.group(2)
                    self.error(ln, 'undefined label %s' % shown)

    # ---- statements ------------------------------------------------------------------------------------------
    def block(self, lines, sc, out, fn):
        stack = []          # (kind, Line): 'if', 'while', 'foreach'
        code_at = [k for k, l in enumerate(lines) if l.text.strip() and not l.text.strip().startswith('*')]
        last = code_at[-1] if code_at else -1

        def bare(ln):
            s = ln.text.strip()
            return s[1:].strip() if s.startswith('@') else s
        k = 0
        while k < len(lines):
            ln = lines[k]
            s = bare(ln)
            g = GUARD_RX.match(s)
            nxt = next((j for j in code_at if j > k), None) if g else None
            if g and nxt is not None and not self.opens_or_closes(bare(lines[nxt])):
                # `skip if` / `do if` guard the next code line only: when the guarded statement lowers to more (or
                # fewer) than one line, the guard becomes `if not c` / `if c` ... `end`
                tmp = []
                self.statement(nxt, lines[nxt], sc, tmp, fn, stack, last)
                between = [(l.text, l) for l in lines[k + 1:nxt]]
                n = sum(1 for t, _ in tmp if t.strip() and not t.strip().startswith('*'))
                if n == 1:
                    self.statement(k, ln, sc, out, fn, stack, last)
                    out += between + tmp
                else:
                    self.no_call_in(s, ln)
                    neg = g.group(1) == 'skip if'
                    ind = indent(ln)
                    out.append((ind + ('if not ' if neg else 'if ') + sc.rename(g.group(2)), ln))
                    out += between + tmp
                    out.append((ind + 'end', ln))
                k = nxt + 1
                continue
            self.statement(k, ln, sc, out, fn, stack, last)
            k += 1
        for kind, ln in stack:
            self.error(ln, '%s without %s' % (kind, '}' if kind == 'foreach' else 'end'))

    @staticmethod
    def opens_or_closes(s):
        """A line that opens or closes a block, or is a guard itself: a guard before it stays as written."""
        w = s.split(' ')[0]
        return w in ('if', 'while', 'else', 'end', 'foreach', '}') or s == '}' or bool(GUARD_RX.match(s))

    def statement(self, k, ln, sc, out, fn, stack, last):
        """One source line (k: its index in the block, last: the index of the block's last code line)."""
        raw = ln.text
        s = raw.strip()
        if not s or s.startswith('*'):
            out.append((raw, ln))
            return
        at = ''
        if s.startswith('@'):
            at, s = '@ ', s[1:].strip()
        ind = raw[:len(raw) - len(raw.lstrip())]

        def emit(t):
            out.append((ind + t, ln))

        def same():
            """The line as written (main and subs: unchanged), in a function with its variables renamed."""
            return raw if sc.fn is None else ind + at + sc.rename(s)
        if s == '}':
            if not stack or stack[-1][0] != 'foreach':
                if stack:
                    self.error(ln, '} closes nothing: %s at line %d is still open (close it with end)'
                               % (stack[-1][0], stack[-1][1].line))
                else:
                    self.error(ln, '} without an open block')
                return
            stack.pop()
            emit('end')
            return
        m = FOREACH_RX.match(s)
        if m or s.startswith('foreach ') or s == 'foreach':
            if not m:
                self.error(ln, 'foreach: `foreach $item in <array> {` or `foreach $index, $item in <array> {`')
                return
            self.fe += 1
            h = '_fe%d' % self.fe
            a, n, i = sc.var(h + '.a'), sc.var(h + '.n'), sc.var(h + '.i')
            item, index = (m.group(2), m.group(1)) if m.group(2) else (m.group(1), None)
            self.assign(a, m.group(3), sc, ln, out, fn, raw_target=True)
            emit('%s = size of array %s' % (n, a))
            emit('%s = 0' % i)
            emit('while %s < %s' % (i, n))
            emit('  %s = %s[%s]' % (sc.rename(item), a, i))
            if index:
                emit('  %s = %s' % (sc.rename(index), i))
            emit('  inc %s =' % i)
            stack.append(('foreach', ln))
            return
        w = s.split(' ')[0]
        if w in ('if', 'while'):
            self.no_call_in(s, ln)
            stack.append((w, ln))
            out.append((same(), ln))
            return
        if w == 'else':
            if not stack or stack[-1][0] != 'if':
                self.error(ln, 'else without if' + (' (%s at line %d is open)' % (stack[-1][0], stack[-1][1].line)
                                                     if stack else ''))
                return
            self.no_call_in(s, ln)
            out.append((same(), ln))
            return
        if s == 'end':
            if not stack:
                self.error(ln, 'end without if/while')
                return
            if stack[-1][0] == 'foreach':
                self.error(ln, 'end inside foreach (line %d): close the foreach with }' % stack[-1][1].line)
                return
            stack.pop()
            out.append((same(), ln))
            return
        if GUARD_RX.match(s):
            self.no_call_in(s, ln)
        if fn is not None:
            m = RETURN_RX.match(s)
            if m:
                if m.group(1) and m.group(1).strip() != 'null':
                    fn.has_value = True
                    self.assign('$fn.%s' % fn.name, m.group(1), sc, ln, out, fn, raw_target=True)
                if not (k == last and not stack):
                    fn.exit_used = True
                    emit('goto label fn.%s.exit' % fn.name)
                return
        m = INCDEC_RX.match(s)
        if m:
            emit(at + '%s %s =' % ('inc' if m.group(2) == '++' else 'dec', sc.rename(m.group(1))))
            return
        m = COMPOUND_RX.match(s)
        if m:
            e = self.value(m.group(3), sc, ln, out, fn)
            if e is None:
                return
            if re.search(r'\s', unquoted(e, 'x').strip()) and not one_group(e):
                e = '( %s )' % e
            t = sc.rename(m.group(1))
            emit(at + '%s = %s %s %s' % (t, t, m.group(2), e))
            return
        m = ASSIGN_RX.match(s)
        if m and (ASSIGN_RX.match(m.group(2)) or self.call_of(m.group(2))):
            self.assign(m.group(1), m.group(2), sc, ln, out, fn, at=at)
            return
        if s.startswith('=') and self.call_of(s[1:].strip()):
            self.call(self.call_of(s[1:].strip()), None, sc, ln, out, fn)
            return
        c = self.call_of(s)
        if c:
            self.call(c, None, sc, ln, out, fn)
            return
        m = re.match(r'gosub\s+([\w.]+)$', s)
        if m and m.group(1) in self.subs:
            self.calls.setdefault(self.owner, set()).add(m.group(1))
        out.append((same(), ln))

    def no_call_in(self, s, ln):
        for m in re.finditer(r'(?<![\w.$\[])(' + NAME + r')\s*\(', unquoted(s)):
            if m.group(1) in self.funcs:
                self.error(ln, 'a function call cannot stand in a condition: assign it to a variable first',
                           ln.col + m.start(1))

    def call_of(self, s):
        m = CALL_RX.match(s.strip())
        if m and (m.group(1) in self.funcs or m.group(1) in self.subs):
            return m.group(1), m.group(2)
        return None

    def value(self, rhs, sc, ln, out, fn):
        """An operand / expression text in the caller's scope; a function call is made first, its result used."""
        c = self.call_of(rhs)
        if c:
            if c[0] in self.subs:
                self.error(ln, 'sub %s has no result' % c[0])
                return None
            self.call(c, None, sc, ln, out, fn)
            return '$fn.%s' % c[0]
        return sc.rename(rhs.strip())

    def assign(self, target, rhs, sc, ln, out, fn, raw_target=False, at=''):
        """target = rhs, rhs maybe a chain ($b = e) or a function call."""
        t = target if raw_target else sc.rename(target)
        m = ASSIGN_RX.match(rhs.strip())
        if m:
            self.assign(m.group(1), m.group(2), sc, ln, out, fn, at=at)
            out.append((indent(ln) + '%s = %s' % (t, sc.rename(m.group(1))), ln))
            return
        c = self.call_of(rhs)
        if c:
            if c[0] in self.subs:
                self.error(ln, 'sub %s has no result' % c[0])
                return
            self.call(c, t, sc, ln, out, fn)
            return
        out.append((indent(ln) + at + '%s = %s' % (t, sc.rename(rhs.strip())), ln))

    def call(self, c, target, sc, ln, out, fn):
        name, argtext = c
        self.calls.setdefault(self.owner, set()).add(name)
        args = split_args(argtext)
        if args is None:
            self.error(ln, 'call of %s: unbalanced parentheses or quotes' % name)
            return
        if name in self.subs:
            if args:
                self.error(ln, 'sub %s takes no arguments (it shares the variables of the script)' % name)
            out.append((indent(ln) + 'gosub %s' % name, ln))
            return
        f = self.funcs[name]
        if len(args) > len(f.params):
            self.error(ln, 'function %s takes %d argument(s), %d given' % (name, len(f.params), len(args)))
            return
        for k, p in enumerate(f.params):
            a = args[k] if k < len(args) else 'null'
            if not a:
                self.error(ln, 'call of %s: argument %d is empty' % (name, k + 1))
                return
            for m in re.finditer(r'(?<![\w.$\[])(' + NAME + r')\s*\(', unquoted(a)):
                if m.group(1) in self.funcs:
                    self.error(ln, 'call of %s: a function call as an argument is not supported; assign it to a '
                                   'variable first' % name)
                    return
            dst = '$fn.%s.%s' % (name, p)
            v = sc.rename(a)
            if v != dst:
                out.append((indent(ln) + '%s = %s' % (dst, v), ln))
        out.append((indent(ln) + 'gosub fn.%s' % name, ln))
        if target:
            out.append((indent(ln) + '%s = $fn.%s' % (target, name), ln))


# ---- type warnings -----------------------------------------------------------------------------------------------
KIND_OF_DTYPE = {4: 'number', 5: 'string', 65542: 'ship', 65543: 'station', 65544: 'sector', 9: 'ware', 10: 'race',
                 12: 'class', 21: 'array', 29: 'table'}
ARG_KIND = {'Var/Number': 'number', 'Var/String': 'string', 'Var/Ship': 'ship', 'Var/Station': 'station',
            'Var/Sector': 'sector', 'Var/Ware': 'ware', 'Var/Race': 'race', 'Var/Class': 'class', 'Var/Array': 'array'}
SHIP, STATION = {'ship'}, {'station'}
ACCEPT = {
    # Var/String is not checked: the stock scripts pass arrays (menu columns) and objects (printed by name)
    'Var/Number': {'number'},
    'Var/Ship': SHIP, 'Var/Ship owned by Player': SHIP,
    'Var/Station': STATION, 'Var/Station owned by Player': STATION,
    'Var/Ship/Station': SHIP | STATION, 'Var/Ship/Station owned by Player': SHIP | STATION,
    'Var/Station/Carrier': SHIP | STATION, 'Var/Station/Carrier owned by Player': SHIP | STATION,
    'Var/Sector': {'sector'}, 'Var/Array': {'array', 'table'}, 'Var/Ware': {'ware', 'number'}, 'Var/Race': {'race'},
    'Var/Class': {'class'},
    # an object: FL also calls table and string commands on `$x ->`; only plainly non-object values warn
    'RefObj': {'ship', 'station', 'sector', 'array', 'table', 'string'},
}
CONST_KIND = {'TRUE': 'number', 'FALSE': 'number', 'PLAYERSHIP': 'ship', 'SECTOR': 'sector', 'MAX': 'number'}
CMP_OPS = {0, 1, 2, 3, 4, 5, 9, 10, 65556}
NUM_OPS = {6, 7, 8, 12, 13, 14, 15, 65554, 65555}
_SEM = {}


def article(k):
    return ('an ' if k[0] in 'aeiou' else 'a ') + k


def returns_kind(s):
    """The result kind from a `returns` description ("int (type 4)", "Station (type 65543) or null if none found");
    None when it is not one clear kind ("ship or station", "element value, any type")."""
    s0 = (s or '').lower().strip()
    m = re.match(r'\W*([\w/]+)', s0)
    if not m:
        return None
    w = m.group(1)
    if '/' in w and not re.fullmatch(r'[01]/[01]', w):
        return None         # "ship/station"
    kinds = {'int': 'number', 'integer': 'number', 'number': 'number', '1': 'number', '0': 'number', '1/0': 'number',
             '0/1': 'number', 'bool': 'number', 'string': 'string', 'ship': 'ship', 'station': 'station',
             'sector': 'sector', 'ware': 'ware', 'race': 'race', 'class': 'class', 'array': 'array', 'table': 'table'}
    k = kinds.get(w)
    if k not in ('array', 'table') and re.search(r'\b(array|table)s?\b', s0):
        return None         # "ship (65542), array of ships (type 21) or null": find ... maxnum
    for alt in re.findall(r'\bor\s+([\w-]+)', s0):
        if alt not in ('null', '0', '1', '-1', 'none', 'nothing', 'zero'):
            return None     # "ship or station", "int or string"
    return k


def semantics(addon):
    if addon not in _SEM:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
        sem = {}
        for a in (['ap', 'fl'] if addon == 'fl' else ['ap']):        # FL keeps only its new or changed commands
            try:
                with open(os.path.join(d, 'msci_semantics_%s.json' % a), encoding='utf-8') as f:
                    sem.update(json.load(f))
            except OSError:
                pass
        _SEM[addon] = {int(k): returns_kind(v.get('returns')) for k, v in sem.items()}
    return _SEM[addon]


def operands(A, cid, line):
    """[(param type id, 'pair'|'ret'|'expr'|'args', value)] of a code line."""
    out, k = [], 1
    line = list(line) + [0] * 64
    for p in A.p.pars(cid):
        if cid in (102, 104) and p in (5, 30):
            n = line[k] if isinstance(line[k], int) else 0
            k += 1
            pairs = [(line[k + 2 * j], line[k + 2 * j + 1]) for j in range(n)]
            k += 2 * n
            if cid == 104:
                m = line[k] if isinstance(line[k], int) else 0
                k += 1 + m
            out.append((p, 'expr' if cid == 104 else 'args', pairs))
            continue
        f = A.pf.get(p + 1, 0)
        if f & 65536 or p == 4:
            out.append((p, 'pair', (line[k], line[k + 1])))
            k += 2
        elif f & 131072:
            out.append((p, 'ret', line[k]))
            k += 1
        else:
            out.append((p, 'cell', line[k]))
            k += 1
    return out


def typecheck(A, names, code, arg_types, addon, where):
    """Warnings [(code line index, message)] for typed parameters given a value of a known other type.
    where(code index) -> text for 'set at ...'."""
    ret = semantics(addon)
    T = A.p
    assigns = []        # (var, source, code index); source: kind | ('var', j) | ('expr', pairs) | None (unknown)
    for v, t in enumerate(arg_types):
        assigns.append((v, ARG_KIND.get(t), None))
    decoded = []
    for idx, ln in enumerate(code):
        cid = ln[0]
        if cid in (3, 112, 100, 101, 1167, 1168):
            decoded.append(None)
            continue
        ops = operands(A, cid, ln)
        decoded.append(ops)
        rv = next((v for p, kind, v in ops if kind == 'ret'), None)
        if not isinstance(rv, int) or rv < 0:
            continue
        if cid == 104:
            pairs = next(v for p, kind, v in ops if kind == 'expr')
            assigns.append((rv, ('expr', pairs), idx))
        elif cid == 102:
            assigns.append((rv, None, idx))
        else:
            assigns.append((rv, ret.get(cid), idx))

    def pair_kind(pr, kinds):
        t, v = pr
        if t == 131074:
            return kinds.get(v, (None,))[0] if isinstance(v, int) else None
        if t == 131075:
            return CONST_KIND.get(T.t(2002, v, ''))
        if t == 0:
            return 'null'
        if t >= 65536 and t not in KIND_OF_DTYPE:
            return None
        return KIND_OF_DTYPE.get(t)

    def source_kind(src, kinds):
        if src is None or isinstance(src, str):
            return src
        pairs = src[1]
        if len(pairs) == 1:
            return pair_kind(pairs[0], kinds)
        if not pairs or pairs[-1][0] != 15:
            return None
        root = pairs[-1][1]
        if root in CMP_OPS or root in NUM_OPS:
            return 'number'
        if root == 11:
            ks = [pair_kind(p, kinds) for p in pairs if p[0] != 15]
            if 'string' in ks:
                return 'string'
            if all(k == 'number' for k in ks):
                return 'number'
        return None
    kinds = {}
    for _ in range(20):
        new = {}
        for v, src, idx in assigns:
            k = source_kind(src, kinds)
            if k == 'null':
                continue
            cur = new.get(v)
            if cur is None:
                new[v] = (k, idx)
            elif cur[0] != k:
                new[v] = (None, idx)
        if new == kinds:
            break
        kinds = new
    out = []

    def shown(v):
        n = names[v] if 0 <= v < len(names) else str(v)
        m = re.match(r'fn\.(' + NAME + r')$', n)
        if m and m.group(1) in where.funcs:
            return 'the result of %s()' % m.group(1)
        for f in where.funcs:
            if n.startswith('fn.%s.' % f):
                return '$' + n[len(f) + 4:]
        return '$' + n
    for idx, ops in enumerate(decoded):
        if not ops:
            continue
        for p, kind, v in ops:
            if kind != 'pair':
                continue
            pt = T.t(2000, p, '')
            acc = ACCEPT.get(pt)
            if acc is None:
                continue
            k = pair_kind(v, kinds)
            if k is None or k == 'null' or k in acc:
                continue
            if v[0] == 131074:
                info = kinds.get(v[1])
                what = '%s, which holds %s%s' % (shown(v[1]), article(k), (' (set at %s)' % where(info[1]))
                                                   if info and info[1] is not None else ' (its declared argument type)')
            else:
                what = article(k)
            cmd = re.sub(r'%\d', ' ', T.cmdtext(code[idx][0]))
            cmd = ' '.join(cmd.replace('->', ' ').split())
            out.append((idx, '%s parameter of `%s` gets %s' % (pt, cmd, what)))
    return out


# ---- build -------------------------------------------------------------------------------------------------------
class Result:
    def __init__(self):
        self.diags, self.text, self.xml, self.code = [], None, None, None

    @property
    def errors(self):
        return [d for d in self.diags if d.severity == 'error']


def addon_of(db):
    return 'fl' if len(db.get('cmds', {})) > 1200 else 'ap'


def build(path, db=None, A=None, defines=(), text=None, reader=None, engine=40, types=True, addon=None,
          assemble=True):
    """Source file -> Result (diags, lowered text, xml, codearray). Without db / A (or assemble=False) only the
    preprocessor and the lowering run."""
    r = Result()
    pp = Preprocessor(defines, reader)
    lines = pp.run(path, text)
    r.diags += pp.diags
    if A is None and db is not None:
        A = msci_asm.Assembler(db)
    lo = Lowerer(A)
    header, body = lo.run(lines)
    r.diags += lo.diags
    r.text = '\n'.join([h.text for h in header] + [t for t, _ in body]) + '\n'
    if r.errors or A is None or not assemble:
        return r
    try:
        r.xml = msci_asm.new_script_text(A, db, r.text, engine)
    except msci_asm.AsmError as x:
        k = getattr(x, 'lineno', None)
        if k is not None and k < len(body):
            ln = body[k][1]
            r.diags.append(Diag(ln.file, ln.line, ln.col, getattr(x, 'msg', str(x)) +
                                ('' if body[k][0].strip() == ln.text.strip() else '  [lowered: %s]' % body[k][0].strip())))
        else:
            ln = body[-1][1] if body else Line('', path, 1)
            r.diags.append(Diag(ln.file, ln.line, 1, str(x)))
        return r
    m = re.search(r'<codearray>(.*?)</codearray>', r.xml, re.S)
    r.code = msci.sval(msci.ET.fromstring(m.group(1).strip()))
    if types:
        code_src = {}
        for o in A.origin:
            if o[1] is not None:
                code_src[o[1]] = body[o[0]][1]
        names = r.code[5] or []
        argtypes = [a.group(2) for a in (msci_asm.ARG_RX.match(h.text.strip()) for h in header) if a]

        def where(idx):
            ln = code_src.get(idx)
            return '%s:%d' % (ln.file, ln.line) if ln else '?'
        where.funcs = lo.funcs
        for idx, msg in typecheck(A, names, r.code[6], argtypes, addon or addon_of(db), where):
            ln = code_src.get(idx)
            if ln is not None:
                r.diags.append(Diag(ln.file, ln.line, ln.col, msg, 'warning'))
    return r


def load_scriptargs(A, dirs):
    import glob
    for d in dirs:
        for f in glob.glob(os.path.join(d, '*.xml')):
            c, _ = msci.read_script(f)
            if c:
                raw = read_file(f, 'replace')
                A.scriptargs[c[0]] = [msci.html.unescape(x) for x in re.findall(r'<argument [^>]*desc="([^"]*)"', raw)]


def main():
    import argparse
    ap = argparse.ArgumentParser(prog='x3kc msci build', description='MSCI sugar (.xms) -> script XML')
    ap.add_argument('db')
    ap.add_argument('src')
    ap.add_argument('-o', dest='out')
    ap.add_argument('--text', action='store_true', help='write the lowered MSCI text instead of the XML')
    ap.add_argument('-D', dest='defines', action='append', default=[], metavar='NAME[=V]')
    ap.add_argument('--scripts', action='append', default=[], help='dir of *.xml whose argument names `call script` uses')
    ap.add_argument('--engine', type=int, default=40)
    ap.add_argument('--no-types', action='store_true', help='no type warnings')
    ap.add_argument('--addon', choices=['ap', 'fl'], help='command semantics for type warnings (default: by the db)')
    a = ap.parse_args(sys.argv[1:])
    with open(a.db, encoding='utf-8') as f:
        db = json.load(f)
    A = msci_asm.Assembler(db)
    load_scriptargs(A, a.scripts + [os.path.dirname(os.path.abspath(a.out)) if a.out else '.'])
    try:
        r = build(a.src, db, A, a.defines, engine=a.engine, types=not a.no_types, addon=a.addon,
                  assemble=not a.text)
    except OSError as x:
        print('%s: %s' % (a.src, x), file=sys.stderr)
        return 1
    for d in r.diags:
        print(d, file=sys.stderr)
    if r.errors:
        return 1
    data = r.text.encode('utf-8') if a.text else r.xml.replace('\n', '\r\n').encode('utf-8')
    if a.out:
        with open(a.out, 'wb') as f:
            f.write(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == '__main__':
    sys.exit(main())
