"""A KC source project as the language server sees it: parts, per-part analysis, symbols and definitions.

A split project (kcsplit: <name>.kcs index + part files) is analysed part by part: each part is lexed and read by
pass 1 on its own (kcsplit.parse_part does the same), the declarations of all parts are merged into one Symbols in
index order, and pass 2 (bodies) of a part runs against that merged table. So an edit re-reads one part, not the
project. A single-file .kc is a project of one part (the slow path: every edit re-reads the whole file).

Positions are part-local: line 1-based (as kcparse counts), column 0-based in characters.
"""
import bisect
import json
import os
import re

from x3kc import kcast as A
from x3kc import kcc, kcgen, kcparse, kcsplit
from x3kc.kcsym import Klass, Symbols, NO_PARENT

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # the x3kc checkout


def norm(path):
    return os.path.normcase(os.path.abspath(path))


class Diag:
    __slots__ = ('line', 'col', 'end', 'msg', 'severity')    # end: end column on the same line

    def __init__(self, line, col, end, msg, severity=1):
        self.line, self.col, self.end, self.msg, self.severity = line, col, end, msg, severity

    def __repr__(self):
        return 'Diag(%d:%d-%d %r)' % (self.line, self.col, self.end, self.msg)


def tok_diag(t, msg, severity=1):
    return Diag(t.line, t.col, t.col + max(1, len(t.text)), msg, severity)


def strip_loc(msg):
    """'Ship.kc:12: unknown name x' -> 'unknown name x' (the range already says where)."""
    m = re.match(r'^[^:\n]*:\d+: ', msg)
    return msg[m.end():] if m else msg


# ---- recording parsers -------------------------------------------------------------------------------------------
class DeclParser(kcparse.Parser):
    """Pass 1 that also remembers the token of every declaration (by id() of the Klass / Field / FuncDecl)."""

    def __init__(self, src, name):
        super().__init__(src, name)
        self.decl_tok = {}          # id(obj) -> name token
        self.decl_end = {}          # id(obj) -> last token (closing brace or ';')
        self.param_toks = {}        # id(FuncDecl) -> [param name tokens]

    def field(self):
        t = self.toks[self.p + 1]
        f = super().field()
        self.decl_tok[id(f)] = t
        self.decl_end[id(f)] = self.toks[self.p - 1]
        return f

    def klass(self):
        t = self.toks[self.p + 1]
        k = super().klass()
        self.decl_tok[id(k)] = t
        self.decl_end[id(k)] = self.toks[self.p - 1]
        return k

    def func_header(self, cid):
        p0 = self.p
        f = super().func_header(cid)
        self.decl_tok[id(f)] = self.toks[p0 + 1]
        self.decl_end[id(f)] = self.toks[self.p - 1]
        ps = []
        i = p0 + 3                  # function NAME ( ...
        while i < f.start and self.toks[i].text != ')':
            if self.toks[i].kind == 'id':
                ps.append(self.toks[i])
            i += 1
        self.param_toks[id(f)] = ps[:len(f.params)]
        return f


class RefParser(kcparse.BodyParser):
    """Pass 2 of one body that records what names resolve to: self.refs[token index] = target.

    Targets: ('class', cid) ('method', cid, name) ('func', name) ('byname', name, cls) ('native', name)
    ('field', cid, Field) ('global', Field) ('local', Var) ('param', FuncDecl, index)."""

    def __init__(self, toks, syms, decl, name, refs, var_tok, calls):
        super().__init__(toks, syms, decl, name)
        self.refs, self.var_tok, self.calls = refs, var_tok, calls

    def stmt(self):
        t = self.peek()
        name_i = self.p + 1 if (t.kind == 'id' and t.text == 'var') else None
        s = super().stmt()
        if name_i is not None and type(s) is A.VarDecl:
            self.var_tok[id(s.var)] = name_i
            self.refs[name_i] = ('local', s.var)
        return s

    def name_value(self, w, t):
        i = self.p - 1
        r = super().name_value(w, t)
        tr = type(r)
        if tr is A.Ref:
            v = r.var
            if v.kind == 'param' and isinstance(v.index, int) and v.index <= len(self.decl.params):
                self.refs[i] = ('param', self.decl, v.index - 1)
            elif v.kind == 'local':
                self.refs[i] = ('local', v)
        elif tr is A.Mem:
            m = self.syms.member(self.cls, r.k)
            if m:
                self.refs[i] = ('field', m[0], m[1])
        elif tr is A.Glob:
            f = self.syms.glob(r.k)
            if f is not None:
                self.refs[i] = ('global', f)
        elif tr is kcparse.ClassConst:
            self.refs[i] = ('class', r.id)
        return r

    def primary(self):
        p0 = self.p
        toks = self.toks
        t = toks[p0]
        r = super().primary()
        if t.kind == 'op' and t.text == '::':
            self.refs[p0 + 1] = ('func', toks[p0 + 1].text)
            self.calls.append((p0 + 1, r))
        elif t.kind == 'id':
            nx = toks[p0 + 1]
            if t.text == 'this' and nx.text == '->':
                self.refs[p0 + 2] = ('byname', toks[p0 + 2].text, self.cls)
            elif nx.kind == 'op' and nx.text == '::':
                nt = p0 + 2
                if t.text == 'native':
                    self.refs[nt] = ('native', toks[nt].text)
                    self.calls.append((nt, r))
                elif t.text == '__global':
                    self.refs[nt] = ('func', toks[nt].text)
                elif type(r) is A.Call:
                    self.refs[p0] = ('class', r.target[0])
                    self.refs[nt] = ('method', r.target[0], toks[nt].text)
                    self.calls.append((nt, r))
            elif nx.kind == 'op' and nx.text == '(':
                tr = type(r)
                if tr is A.Native:
                    self.refs[p0] = ('native', t.text)
                    self.calls.append((p0, r))
                elif tr is A.Call and r.kind == 's':
                    self.refs[p0] = ('method', r.target[0], t.text)
                    self.calls.append((p0, r))
                elif tr is A.Call and r.kind == 'g':
                    self.refs[p0] = ('func', t.text)
                    self.calls.append((p0, r))
        return r


# ---- one part ----------------------------------------------------------------------------------------------------
class Part:
    def __init__(self, path, project):
        self.path = path
        self.key = norm(path)
        self.project = project
        self.text = None            # editor text when open, else read from disk on demand
        self.open = False
        self.version = 0            # bumped on every change
        self.done1 = -1             # version pass 1 ran on
        self.done2 = -1             # (version, symbols generation) pass 2 ran on
        self.toks = []
        self.unit = None            # last good pass-1 unit (kept when the current text does not parse)
        self.dp = None              # its DeclParser (declaration tokens)
        self.diag1 = []             # lexer / pass-1 diagnostics of the current text
        self.diag2 = []             # pass-2 / codegen diagnostics
        self.refs = {}              # token index -> target
        self.var_tok = {}           # id(Var) -> token index
        self.sig = None             # declarations signature, to know when other parts must be re-checked
        self._keys = None           # (toks, [(line, col)]) for tok_at

    def name(self):
        return os.path.basename(self.path)

    def source(self):
        if self.text is None:
            with open(self.path, encoding='latin1') as f:
                self.text = f.read()
        return self.text

    def set_text(self, text):
        self.text = text
        self.version += 1

    def pass1(self):
        if self.done1 == self.version:
            return False
        self.done1 = self.version
        self.diag1 = []
        src = self.source()
        try:
            dp = DeclParser(src, self.name())
        except kcparse.ParseError as e:
            self.diag1 = [Diag(e.line or 1, e.col or 0, (e.col or 0) + 1, strip_loc(str(e)))]
            self.toks = []
            return False
        self.toks = dp.toks
        try:
            u = dp.unit(resolve=False)
        except kcparse.ParseError as e:
            t = e.tok or dp.toks[min(dp.p, len(dp.toks) - 1)]
            self.diag1 = [tok_diag(t, strip_loc(str(e)))]
            return False
        old = self.sig
        self.unit, self.dp = u, dp
        self.sig = signature(u)
        return self.sig != old

    def tok_at(self, line, col):
        """Index of the token under (line, col), or None."""
        toks = self.toks
        if not toks:
            return None
        if self._keys is None or self._keys[0] is not toks:
            self._keys = (toks, [(t.line, t.col) for t in toks])
        i = bisect.bisect_right(self._keys[1], (line, col)) - 1
        if i >= 0:
            t = toks[i]
            if t.line == line and t.col <= col <= t.col + len(t.text) and t.kind != 'eof':
                return i
        return None


def signature(u):
    """What other parts see of a part: classes (id, name, parent, fields, methods with arity), globals."""
    out = []
    for kind, x in u.items:
        if kind == 'class':
            out.append(('c', x.id, x.name, u.class_parent_names.get(x.id, (None,))[0],
                        tuple(f.name for f in x.fields), tuple((m.name, m.nparams) for m in x.methods)))
    out.append(('g', tuple(f.name for f in u.globals.fields), tuple((m.name, m.nparams) for m in u.globals.methods)))
    return tuple(out)


# ---- the project -------------------------------------------------------------------------------------------------
class Project:
    def __init__(self, index=None, single=None):
        self.index = index
        self.parts = []
        if index:
            base = os.path.dirname(os.path.abspath(index))
            for f, _ in kcsplit.read_index(index):
                self.parts.append(Part(os.path.join(base, f), self))
        else:
            self.parts.append(Part(single, self))
        self.by_key = {p.key: p for p in self.parts}
        self.gen = 0                # symbols generation
        self.syms = None
        self.owner = {}             # class id / ('f', name) / ('g', name) -> (Part, object)
        self.merge_diags = {}       # part key -> [Diag]
        self.known = kcc.known_natives() or set()
        self.natives_doc = native_docs()
        self._built = False

    def name(self):
        return os.path.basename(self.index or self.parts[0].path)

    # -- symbols --
    def refresh(self):
        """Pass 1 of every part whose text changed; rebuild the merged symbols when a signature changed."""
        changed = not self._built
        for p in self.parts:
            if p.pass1():
                changed = True
        if changed:
            self.merge()
            self._built = True
        return changed

    def merge(self):
        self.gen += 1
        S = Symbols()
        g = Klass(0, '', NO_PARENT, None)
        S.add(g)
        owner = {}
        md = {}
        for p in self.parts:
            u = p.unit
            if u is None:
                continue
            for kind, x in u.items:
                if kind != 'class':
                    continue
                t = p.dp.decl_tok.get(id(x))
                if x.id in S.classes:
                    md.setdefault(p.key, []).append(tok_diag(t, 'class %d declared twice (also in %s)'
                                                             % (x.id, owner[x.id][0].name())))
                    continue
                if x.name in S.by_name:
                    md.setdefault(p.key, []).append(tok_diag(t, 'class name %s used twice (also in %s)'
                                                             % (x.name, owner[S.by_name[x.name]][0].name())))
                    continue
                S.add(x)
                owner[x.id] = (p, x)
            for f in u.globals.fields:
                if ('g', f.name) in owner:
                    md.setdefault(p.key, []).append(tok_diag(p.dp.decl_tok[id(f)], 'global variable %s defined '
                                                             'twice (also in %s)' % (f.name, owner[('g', f.name)][0].name())))
                    continue
                g.fields.append(f)
                owner[('g', f.name)] = (p, f)
            for m in u.globals.methods:
                if ('f', m.name) in owner:
                    md.setdefault(p.key, []).append(tok_diag(p.dp.decl_tok[id(m.fn)], 'function %s defined twice '
                                                             '(also in %s)' % (m.name, owner[('f', m.name)][0].name())))
                    continue
                g.methods.append(m)
                owner[('f', m.name)] = (p, m)
        for p in self.parts:
            u = p.unit
            if u is None:
                continue
            for cid, (pname, line) in u.class_parent_names.items():
                if owner.get(cid, (None,))[0] is not p:
                    continue
                k = S.classes[cid]
                if pname is None:
                    k.parent = NO_PARENT
                elif pname in S.by_name:
                    k.parent = S.by_name[pname]
                else:
                    k.parent = NO_PARENT
                    t = p.dp.decl_tok.get(id(k))
                    md.setdefault(p.key, []).append(Diag(line, 0, 200, 'unknown parent class %s' % pname)
                                                    if t is None else tok_diag(t, 'unknown parent class %s' % pname))
        self.syms, self.owner, self.merge_diags = S, owner, md

    # -- pass 2 --
    def check(self, part):
        """Pass 2 + codegen of one part's bodies (after refresh)."""
        state = (part.version, self.gen)
        if part.done2 == state:
            return
        part.done2 = state
        part.diag2 = []
        part.refs = {}
        part.var_tok = {}
        u = part.unit
        if u is None or part.done1 != part.version or part.diag1:
            return                  # pass 1 failed on the current text: its error is the diagnostic
        env = kcgen.Env(kcgen.Pool(b'\0'), {})
        name = part.name()
        for f in u.funcs:
            calls = []
            bp = RefParser(part.toks, self.syms, f, name, part.refs, part.var_tok, calls)
            try:
                f.ast = bp.function()
            except kcparse.ParseError as e:
                t = e.tok or part.toks[max(0, min(bp.p, len(part.toks)) - 1)]
                part.diag2.append(tok_diag(t, strip_loc(str(e))))
                continue
            except (IndexError, RecursionError) as e:
                part.diag2.append(tok_diag(part.dp.decl_tok[id(f)], 'cannot parse the body (%s)' % type(e).__name__))
                continue
            try:
                kcgen.FuncGen(f.ast, 0, env).function()
            except kcgen.GenError as e:
                part.diag2.append(tok_diag(part.dp.decl_tok[id(f)], '%s: %s' % (f.name, e)))
            for i, r in calls:
                t = part.toks[i]
                if type(r) is A.Native:
                    if self.known and r.name not in self.known:
                        part.diag2.append(tok_diag(t, 'native %s is unknown to X3AP.exe (it would return 0)'
                                                   % r.name, 2))
                elif type(r) is A.Call and r.kind in ('s', 'g'):
                    k = self.syms.classes.get(r.target[0])
                    m = k.method(r.target[1]) if k else None
                    if m is not None and len(r.args) != m.nparams:
                        part.diag2.append(tok_diag(t, '%s takes %d argument%s, called with %d'
                                                   % (r.target[1], m.nparams, '' if m.nparams == 1 else 's',
                                                      len(r.args)), 2))
        self._post_scan(part)

    def _post_scan(self, part):
        """`x.F(`, `C.F(`, `x.C::F(`, `new C(`: forms resolved inside postfix / unary, found on the tokens."""
        toks = part.toks
        refs = part.refs
        S = self.syms
        for i in range(1, len(toks) - 1):
            t = toks[i]
            if t.kind != 'op':
                continue
            if t.text == '.' and toks[i + 1].kind == 'id':
                j = i + 1
                if toks[j + 1].text == '::' and toks[j + 2].kind == 'id':
                    cid = class_id(S, toks[j].text)
                    if cid is not None:
                        refs[j] = ('class', cid)
                        refs[j + 2] = ('method', cid, toks[j + 2].text)
                    continue
                prev = refs.get(i - 1)
                if prev and prev[0] == 'class':
                    owner = S.find_method(prev[1], toks[j].text)
                    if owner is not None:
                        refs[j] = ('method', owner, toks[j].text)
                        continue
                refs.setdefault(j, ('byname', toks[j].text, None))
        for i in range(len(toks) - 2):
            if toks[i].kind == 'id' and toks[i].text == 'new' and toks[i + 1].kind == 'id' and toks[i + 2].text == '(':
                cid = class_id(S, toks[i + 1].text)
                if cid is not None:
                    refs[i + 1] = ('class', cid)

    def diagnostics(self, part):
        return part.diag1 + self.merge_diags.get(part.key, []) + part.diag2

    # -- definitions --
    def definition(self, part, line, col):
        """[(path, line, col, length)] for the name under (line, col)."""
        i = part.tok_at(line, col)
        if i is None:
            return []
        t = part.toks[i]
        tgt = part.refs.get(i)
        if tgt is None:
            if part.dp and any(dt is t for dt in part.dp.decl_tok.values()):
                return [(part.path, t.line, t.col, len(t.text))]
            if t.kind == 'id':                  # outside a body (field initialiser, parent name, asm operand)
                cid = class_id(self.syms, t.text)
                if cid is not None:
                    tgt = ('class', cid)
                elif t.text in self.known or t.text in self.natives_doc:
                    tgt = ('native', t.text)
            if tgt is None:
                return []
        return self.locate(part, tgt)

    def decl_loc(self, part, obj):
        t = part.dp.decl_tok.get(id(obj)) if part.dp else None
        return [(part.path, t.line, t.col, len(t.text))] if t else []

    def locate(self, part, tgt):
        k = tgt[0]
        S = self.syms
        if k == 'class':
            o = self.owner.get(tgt[1])
            return self.decl_loc(*o) if o else []
        if k == 'method':
            o = self.owner.get(tgt[1])
            if not o:
                return []
            m = o[1].method(tgt[2])
            return self.decl_loc(o[0], m.fn) if m else []
        if k == 'func':
            o = self.owner.get(('f', tgt[1]))
            return self.decl_loc(o[0], o[1].fn) if o else []
        if k == 'global':
            o = self.owner.get(('g', tgt[1].name))
            return self.decl_loc(o[0], o[1]) if o else []
        if k == 'field':
            o = self.owner.get(tgt[1])
            return self.decl_loc(o[0], tgt[2]) if o else []
        if k == 'local':
            j = part.var_tok.get(id(tgt[1]))
            if j is None:
                return []
            t = part.toks[j]
            return [(part.path, t.line, t.col, len(t.text))]
        if k == 'param':
            ps = part.dp.param_toks.get(id(tgt[1]), []) if part.dp else []
            if tgt[2] < len(ps):
                t = ps[tgt[2]]
                return [(part.path, t.line, t.col, len(t.text))]
            return []
        if k == 'byname':
            name, cls = tgt[1], tgt[2]
            if cls:
                owner = S.find_method(cls, name)
                if owner is not None:
                    return self.locate(part, ('method', owner, name))
            out = []
            for cid, kl in S.classes.items():
                if cid and kl.method(name):
                    out.extend(self.locate(part, ('method', cid, name)))
            return out
        if k == 'native':
            d = self.natives_doc.get(tgt[1])
            return [d] if d else []
        return []

    # -- outline --
    def symbols(self, part):
        """[(name, kind, (line, col, endline, endcol), (selection), children)] with LSP SymbolKind numbers."""
        if part.unit is None or part.dp is None:
            return []
        dp = part.dp

        def rng(obj):
            t = dp.decl_tok[id(obj)]
            e = dp.decl_end.get(id(obj), t)
            start = t
            return (start.line, 0, e.line, e.col + len(e.text)), (t.line, t.col, t.line, t.col + len(t.text))

        out = []
        u = part.unit
        for f in u.globals.fields:
            r, s = rng(f)
            out.append((f.name, 13, r, s, []))                          # Variable
        for kind, x in u.items:
            if kind == 'func':
                r, s = rng(x)
                out.append((x.name + '(%s)' % ', '.join(x.params), 12, r, s, []))      # Function
            else:
                kids = []
                for f in x.fields:
                    r, s = rng(f)
                    kids.append((f.name, 8, r, s, []))                  # Field
                for m in x.methods:
                    r, s = rng(m.fn)
                    kids.append((m.name + '(%s)' % ', '.join(m.fn.params), 6, r, s, []))     # Method
                r, s = rng(x)
                out.append(('%s = %d' % (x.name, x.id), 5, r, s, kids))  # Class
        return out


def class_id(S, name):
    if name in S.by_name:
        return S.by_name[name]
    m = re.fullmatch(r'C(\d+)', name)
    return int(m.group(1)) if m else None


_NATIVE_DOCS = None


def native_docs():
    """native name -> (path, line, col, length) of its entry: docs/natives/spec_*.jsonl, else x3kc/natives.txt."""
    global _NATIVE_DOCS
    if _NATIVE_DOCS is not None:
        return _NATIVE_DOCS
    out = {}
    p = os.path.join(ROOT, 'x3kc', 'natives.txt')
    if os.path.isfile(p):
        with open(p, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                s = line.strip()
                if s and not s.startswith('#'):
                    out[s] = (p, n, 0, len(s))
    d = os.path.join(ROOT, 'docs', 'natives')
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.startswith('spec_') and fn.endswith('.jsonl'):
                path = os.path.join(d, fn)
                with open(path, encoding='utf-8') as f:
                    for n, line in enumerate(f, 1):
                        try:
                            name = json.loads(line).get('name')
                        except ValueError:
                            continue
                        if name:
                            out[name] = (path, n, 0, len(line.rstrip('\n')))
    _NATIVE_DOCS = out
    return out


def find_project(path):
    """The project a file belongs to: a .kcs index in its directory or above that lists it, else the file alone.
    -> (index or None, file)."""
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    for _ in range(4):
        try:
            idx = [f for f in os.listdir(d) if f.lower().endswith(kcsplit.INDEX_EXT)]
        except OSError:
            idx = []
        for f in idx:
            ip = os.path.join(d, f)
            try:
                parts = kcsplit.read_index(ip)
            except (OSError, UnicodeDecodeError):
                continue
            if any(norm(os.path.join(d, x)) == norm(path) for x, _ in parts):
                return ip, path
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None, path
