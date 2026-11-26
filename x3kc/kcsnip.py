"""KC code compiled against a loaded object's sidecar for the live debugger (stage 2b, docs/debugging.md "Debugger
code"): an expression or a statement block (x3kc dbg eval, break / watch --if, --log expressions) becomes a method of
the frame's class, compiled at code offset 0 with the relocations the game applies when it installs the block in its
code tail (x3_src src/script/debugger.cpp installCode): `jumps` (the offsets of 4-byte code targets: the block's offset
is added), `strings` ([offset, text]: the game writes the pool offset of text there, adding the text to its pool's tail
when the object lacks it). Calls of the object's methods are their absolute entries (the sidecar's).

The names a snippet sees: the frame's live variables (params, locals) - read from copies of their cells, written back
when the eval asks -, `this` and its fields, globals, constants, the object's methods and functions, natives. A string
literal becomes a heap string (`"text" + ""`): no pool string of the game's tail ends up in the game's state (a save
stores pool strings by offset)."""
import struct

from . import kcast as A
from . import kcconst
from . import kcgen
from . import kcisa
from . import kcparse
from .kcsym import Field, Klass, Method, NO_PARENT, Symbols


class SnipError(Exception):
    pass


def symbols(info):
    """the compiler's symbol table of a loaded object, from its sidecar (kcdbg.DebugInfo)"""
    S = Symbols()
    g = Klass(0, '', NO_PARENT, None)
    g.fields = [Field(n, 0, 0, None) for n in info.globals]
    S.add(g)
    for cid, k in sorted(info.classes.items()):
        parent = k.get('parent')
        inherited = len(info.classes[parent]['fields']) if parent in info.classes else 0
        kl = Klass(cid, k['name'], NO_PARENT if parent is None else parent, None)
        kl.fields = [Field(n, 0, 0, None) for n in k['fields'][inherited:]]
        S.add(kl)
    for m in info.methods:
        owner = S.classes.get(m.cls or 0)
        if owner is not None and owner.method(m.name) is None:
            owner.methods.append(Method(m.name, len(m.params), m.frame, None, m.origin))
    table = kcconst.Table(S)
    for cid, vals in info.constants.items():
        for name, value in vals.items():
            d = kcconst.ConstDef(name, None, cid, None)
            d.value, d.loc = value, 'the sidecar'
            table.consts.setdefault(cid, {})[name] = d
    S.consts = table
    return S


class _Parser(kcparse.BodyParser):
    """a snippet's body: the frame's variables are in its outermost scope (Var kind 'outer', the slot set once the
    cells it uses are known)"""

    def __init__(self, toks, syms, decl, outer):
        super().__init__(toks, syms, decl, '<eval>')
        self.outer = outer
        self.used = set()
        self._first = True

    def block(self):
        if self._first:
            self._first = False
            for name, var in self.outer.items():
                self.scopes[0].setdefault(name, var)
        return super().block()

    def name_value(self, w, t):
        e = super().name_value(w, t)
        if isinstance(e, A.Ref) and e.var.kind == 'outer':
            self.used.add(e.var.name)
        return e


def _heap_strings(node, inside_switch=False):
    """every string literal of an expression -> `literal + ""` (a heap string at run time), switch labels left"""
    if isinstance(node, list):
        for i, x in enumerate(node):
            if isinstance(x, A.Str) and x.s != '':
                node[i] = A.Bin('+', x, A.Str(''))
            else:
                _heap_strings(x)
        return
    if isinstance(node, tuple):
        for x in node:
            _heap_strings(x)
        return
    if not isinstance(node, A.Node):
        return
    if isinstance(node, A.Switch):
        _heap_strings(node.e)
        for _, stmts in node.sections:
            _heap_strings(stmts)
        return
    for k in node.__slots__:
        if k == 'at' or not hasattr(node, k):
            continue
        v = getattr(node, k)
        if isinstance(v, A.Str) and v.s != '':
            setattr(node, k, A.Bin('+', v, A.Str('')))
        elif isinstance(v, (A.Node, list, tuple)):
            _heap_strings(v)


def relocations(code, pool):
    """the relocations of code compiled at offset 0: (jumps, strings) - the offsets of its 4-byte code targets, and
    [offset, text] of its string operands (pool: the kcgen.Pool the code was compiled with)"""
    jumps, strings = [], []
    for i in kcisa.walk(code, 0):
        f = kcisa.FMT.get(i.op)
        if f == 't':
            jumps.append(i.pc + 1)
        elif f in ('s', 'n'):
            strings.append([i.pc + 1, pool.text(i.arg)])
        elif i.op == 0x78:
            jumps.append(i.pc + 7)                          # default
            jumps += [i.pc + 11 + 4 * k for k in range(i.arg)]
        elif i.op == 0x79:
            ni, ns = i.arg
            jumps.append(i.pc + 5)
            for k in range(ni + ns):
                p = i.pc + 9 + 8 * k
                if k >= ni:
                    strings.append([p, pool.text(struct.unpack_from('>I', code, p)[0])])
                jumps.append(p + 4)
    return jumps, strings


def compile_snippet(info, text, block=False, cls=0, outer=None, heap_strings=True, syms=None):
    """text (an expression, or a statement block when block) as the body of a method of class cls (0: a global
    function) -> {code (hex), jumps, strings, low, high (the outer cells it reads, relative to the frame's R), cells
    (the cells of the outer variables it uses), lines [(pc, snippet line)], natives}. outer: {name: cell} - the frame's
    variables (kcdbg frame_vars). Errors: SnipError with the snippet's own line."""
    S = syms or symbols(info)
    src = '{\n%s\n}' % (text if block else 'return (%s);' % text)
    try:
        toks = kcparse.lex(src, lambda line: '<eval>:%d' % (line - 1))
    except kcparse.ParseError as x:
        raise SnipError(str(x))
    decl = kcparse.FuncDecl(cls, '__eval', [], None, False, 0, len(toks) - 1, 1)
    vars_ = {}
    for name, cell in (outer or {}).items():
        v = A.Var('outer', name)
        v.index = cell
        vars_[name] = v
    P = _Parser(toks, S, decl, vars_)
    P.loc = lambda line: '<eval>:%d' % (line - 1)
    try:
        fn = P.function()
    except kcparse.ParseError as x:
        raise SnipError(str(x))
    used = [vars_[n] for n in sorted(P.used)]
    low = min((v.index for v in used), default=0)
    high = max((v.index for v in used), default=-1)
    for v in used:
        v.slot = -(1 + v.index - low)                       # the run's scratch stack: the cells R+low.. below its frame
    for v in vars_.values():
        if v.slot is None:
            v.slot = 0
    if heap_strings:
        _heap_strings(fn.body)
    _no_fork(fn.body)
    pool = kcgen.Pool(b'\0')
    methods = {(m.cls or 0, m.name): m.origin for m in info.methods}     # (a reloaded one: its loaded entry)
    env = kcgen.Env(pool, methods)
    g = kcgen.FuncGen(fn, 0, env)
    try:
        g.function()
        code = g.resolve()
    except kcgen.GenError as x:
        raise SnipError('<eval>: %s' % x)
    jumps, strings = relocations(code, pool)
    lines = [(pc, toks[at].line - 1) for pc, at in g.lines]
    from .kcc import natives_of
    return {'code': code.hex(), 'jumps': jumps, 'strings': strings, 'low': low, 'high': high,
            'cells': sorted(v.index for v in used), 'written': sorted(_assigned(fn.body)), 'lines': lines,
            'natives': sorted(set(natives_of(fn.body))), 'calls': _calls(fn.body)}


def _assigned(node):
    """the cells of the outer variables the snippet assigns (=, op=, ++, --: all A.Assign of a Ref)"""
    out, stack = set(), [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (list, tuple)):
            stack.extend(n)
        elif isinstance(n, A.Node):
            if isinstance(n, A.Assign) and isinstance(n.t, A.Ref) and n.t.var.kind == 'outer':
                out.add(n.t.var.index)
            stack.extend(getattr(n, k) for k in n.__slots__ if k != 'at' and hasattr(n, k))
    return out


def _calls(node):
    """the snippet's calls: [(kind, target)] - kind 's' / 'g' a method by (class, name), 'n' / 'o' by name"""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (list, tuple)):
            stack.extend(n)
        elif isinstance(n, A.Node):
            if isinstance(n, A.Call):
                out.append((n.kind, n.target))
            stack.extend(getattr(n, k) for k in n.__slots__ if k != 'at' and hasattr(n, k))
    return out


def _no_fork(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (list, tuple)):
            stack.extend(n)
        elif isinstance(n, (A.Fork, A.ForkVal)):
            raise SnipError('<eval>: fork in debugger code (its child would run the debugger\'s block)')
        elif isinstance(n, A.Node):
            stack.extend(getattr(n, k) for k in n.__slots__ if k != 'at' and hasattr(n, k))


class CallGraph:
    """What the methods of a loaded object reach (its .obj): per method entry the natives it calls, whether it forks,
    the methods it calls by entry and by name. A snippet is checked before it is sent: every method it can reach
    (calls by name: every method of that name) must fork nothing and call only natives that answer at once - a run the
    game ends inside a game method would leave that method's work half done."""

    def __init__(self, info, objpath):
        from . import kcobj
        o = kcobj.load(objpath)
        self.names = {}             # entry -> 'Class::Name'
        self.by_name = {}           # name -> [entry]
        self.by_key = {}            # (class, name) -> entry
        self.body = {}              # entry -> (natives, fork, entries, names)
        ms = sorted(info.methods, key=lambda m: m.origin)
        for k, m in enumerate(ms):
            entry = m.origin
            end = ms[k + 1].origin if k + 1 < len(ms) else len(o.code)
            self.names[entry] = '%s::%s' % (m.cls_name, m.name) if m.cls else m.name
            self.by_name.setdefault(m.name, []).append(entry)
            self.by_key[(m.cls or 0, m.name)] = entry
            natives, fork, entries, names = set(), False, set(), set()
            for i in kcisa.walk(o.code, entry, min(end, len(o.code))):
                if i.op == 0x82:
                    natives.add(o.string(i.arg))
                elif i.op == 0x6f:
                    fork = True
                elif i.op in (0x84, 0x86, 0x88):
                    entries.add(i.arg)
                elif i.op in (0x85, 0x87):
                    names.add(o.string(i.arg))
            self.body[entry] = (natives, fork, entries, names)

    def overlay(self, bodies):
        """the bodies of reloaded methods ({entry: [natives, fork, entries, names]}, x3kc dbg reload's file beside its
        sidecar) in place of the loaded object's"""
        for e, (natives, fork, entries, names) in bodies.items():
            self.body[int(e)] = (set(natives), bool(fork), set(entries), set(names))

    def refusal(self, snip, sync, known):
        """None, or why the snippet must not run: the first method it can reach that forks or calls a native that
        may wait (with the call path)"""
        start = []
        for kind, target in snip.get('calls', []):
            if kind in ('g', 's', 'c'):
                e = self.by_key.get(tuple(target))
                if e is not None:
                    start.append(e)
            else:
                start += self.by_name.get(target, [])
        parent = {e: None for e in start}
        queue = list(parent)
        while queue:
            e = queue.pop(0)
            natives, fork, entries, names = self.body.get(e, (set(), False, set(), set()))
            bad = 'a fork' if fork else next((n for n in sorted(natives) if n in known and n not in sync), None)
            if bad:
                path, x = [], e
                while x is not None:
                    path.append(self.names.get(x, '0x%x' % x))
                    x = parent.get(x)
                return '%s (reached through %s) has %s%s' % (
                    path[0], ' <- '.join(path[1:]) or 'the snippet', bad,
                    '' if bad == 'a fork' else ', which may wait')
            for x in list(entries) + [y for n in names for y in self.by_name.get(n, [])]:
                if x not in parent:
                    parent[x] = e
                    queue.append(x)
        return None


def line_of(snip, pc):
    """the snippet line of a pc of its block (a run's failure)"""
    best = None
    for p, line in snip['lines']:
        if p <= pc:
            best = line
    return best
