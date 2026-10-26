"""AST of the KC source language (decompiler output, compiler input)."""


class Node:
    __slots__ = ()

    def __repr__(self):
        return '%s(%s)' % (type(self).__name__, ', '.join('%s=%r' % (k, getattr(self, k)) for k in self.__slots__))


# ---- variables -------------------------------------------------------------------------------
class Var:
    """A stack cell with a name: parameter, local, or frame cell (argc / retpc / ctx)."""
    __slots__ = ('kind', 'name', 'index', 'slot', 'decl')

    def __init__(self, kind, name, index=None):
        self.kind = kind        # 'param' | 'local' | 'frame'
        self.name = name
        self.index = index      # param number (1-based) or frame cell name
        self.slot = None        # absolute stack index during codegen
        self.decl = None

    def __repr__(self):
        return 'Var(%s)' % self.name


# ---- expressions -----------------------------------------------------------------------------
class Int(Node):
    __slots__ = ('v',)

    def __init__(self, v):
        self.v = v


class Dbl(Node):
    __slots__ = ('raw',)       # 8 big-endian bytes, kept exactly

    def __init__(self, raw):
        self.raw = raw


class Str(Node):
    __slots__ = ('s', 'off')   # text; off = pool offset if pinned (duplicate texts)

    def __init__(self, s, off=None):
        self.s, self.off = s, off


class Ref(Node):
    __slots__ = ('var',)

    def __init__(self, var):
        self.var = var


class Mem(Node):
    __slots__ = ('k',)

    def __init__(self, k):
        self.k = k


class Glob(Node):
    __slots__ = ('k',)

    def __init__(self, k):
        self.k = k


class This(Node):
    __slots__ = ()


class Index(Node):
    __slots__ = ('c', 'i')

    def __init__(self, c, i):
        self.c, self.i = c, i


class Assign(Node):
    __slots__ = ('t', 'v')     # t: Ref | Mem | Glob

    def __init__(self, t, v):
        self.t, self.v = t, v


class SetIdx(Node):
    __slots__ = ('c', 'k', 'v')

    def __init__(self, c, k, v):
        self.c, self.k, self.v = c, k, v


class Bin(Node):
    __slots__ = ('op', 'a', 'b')

    def __init__(self, op, a, b):
        self.op, self.a, self.b = op, a, b


class Un(Node):
    __slots__ = ('op', 'a')    # '!' '-' '~' 'new' 'delete'

    def __init__(self, op, a):
        self.op, self.a = op, a


class Call(Node):
    """kind: 'g' callg (global by address), 's' calls (self, by address), 'n' calln (self, by name),
    'o' callo (object, by name), 'c' callc (object, by address).
    target: (class id, method name) for g/s/c; method name for n/o."""
    __slots__ = ('kind', 'target', 'args', 'obj')

    def __init__(self, kind, target, args, obj=None):
        self.kind, self.target, self.args, self.obj = kind, target, args, obj


class Native(Node):
    __slots__ = ('name', 'args')

    def __init__(self, name, args):
        self.name, self.args = name, args


class MkArr(Node):
    __slots__ = ('items',)     # element 0 first

    def __init__(self, items):
        self.items = items


class MkTab(Node):
    __slots__ = ('pairs',)     # [(key, value)], pair 0 first

    def __init__(self, pairs):
        self.pairs = pairs


class Logic(Node):
    __slots__ = ('op', 'items')    # '&&' | '||'; one item allowed (value 0/1 of a truth test)

    def __init__(self, op, items):
        self.op, self.items = op, items


class Cond(Node):
    __slots__ = ('c', 'a', 'b')

    def __init__(self, c, a, b):
        self.c, self.a, self.b = c, a, b


class ForkVal(Node):
    __slots__ = ()


# ---- statements ------------------------------------------------------------------------------
class ExprStmt(Node):
    __slots__ = ('e',)

    def __init__(self, e):
        self.e = e


class VarDecl(Node):
    __slots__ = ('var', 'init')

    def __init__(self, var, init):
        self.var, self.init = var, init


class Block(Node):
    __slots__ = ('body',)

    def __init__(self, body):
        self.body = body


class If(Node):
    __slots__ = ('c', 'then', 'els')   # then/els: lists of statements (each its own scope); els None = no else

    def __init__(self, c, then, els=None):
        self.c, self.then, self.els = c, then, els


class While(Node):
    __slots__ = ('c', 'body')

    def __init__(self, c, body):
        self.c, self.body = c, body


class For(Node):
    __slots__ = ('init', 'c', 'step', 'body')   # init: list of ExprStmt (emitted before the loop); step: list of stmts

    def __init__(self, init, c, step, body):
        self.init, self.c, self.step, self.body = init, c, step, body


class DoWhile(Node):
    __slots__ = ('body', 'c')

    def __init__(self, body, c):
        self.body, self.c = body, c


class Switch(Node):
    """sections: [(labels, stmts)], labels = [Case | Default]; every section is its own scope.
    dense: True -> opcode 78, False -> 79. span: (lo, hi) of a dense table when not implied by the cases."""
    __slots__ = ('e', 'sections', 'dense', 'span')

    def __init__(self, e, sections, dense, span=None):
        self.e, self.sections, self.dense, self.span = e, sections, dense, span


class Case(Node):
    __slots__ = ('v',)          # Int or Str

    def __init__(self, v):
        self.v = v


class Default(Node):
    __slots__ = ()


class Return(Node):
    __slots__ = ('e',)

    def __init__(self, e):
        self.e = e


class Break(Node):
    __slots__ = ()


class Continue(Node):
    __slots__ = ()


class Fork(Node):
    __slots__ = ('body',)

    def __init__(self, body):
        self.body = body


class Asm(Node):
    """Raw instructions (fallback). items: list of ('label', name) | ('insn', op, arg, cases)."""
    __slots__ = ('items',)

    def __init__(self, items):
        self.items = items


# ---- top level -------------------------------------------------------------------------------
class Function(Node):
    __slots__ = ('cls', 'name', 'params', 'body', 'frame', 'asm', 'frame_vars', 'stmt_pc')

    def __init__(self, cls, name, params, body, frame=None, asm=None):
        self.cls, self.name, self.params, self.body = cls, name, params, body
        self.frame = frame      # explicit frame size (None = computed)
        self.asm = asm          # list of asm items when the body is raw
        self.frame_vars = None  # dict name -> Var for frame cells
        self.stmt_pc = {}       # id(statement) -> code offset (decompiler only)


BINOPS = {0x46: '+', 0x4b: '-', 0x50: '*', 0x51: '/', 0x52: '%', 0x53: '&', 0x54: '|', 0x55: '^', 0x56: '&&&',
          0x57: '|||', 0x58: '<<', 0x59: '>>', 0x5a: '!=', 0x5b: '==', 0x5c: '<', 0x5d: '>', 0x5e: '<=', 0x5f: '>='}
BINOP_CODE = {v: k for k, v in BINOPS.items()}
UNOPS = {0x64: '!', 0x65: '-', 0x66: '~', 0x2b: 'new', 0x2d: 'delete'}
UNOP_CODE = {v: k for k, v in UNOPS.items()}
