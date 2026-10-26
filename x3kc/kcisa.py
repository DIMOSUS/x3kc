"""KC bytecode instruction set as stored in the object file (before LoadSwapAndResolve / PeepholePass).

Operands are big-endian. Jump/call operands are absolute CODE offsets. See vm/scheduler_bytecode.md for semantics.
Opcodes made only by the loader's peephole pass (7d, 8c-99, a0-b1) never occur in files.
"""
import struct

# operand kinds: b = s8, h = s16, H = u16, i = s32, s = u32 string offset, t = u32 code target, c = u32 code entry,
# n = u32 name (string offset), d = f64 (raw 8 bytes)
FMT = {}
for op in (0x05, 0x47, 0x4c):
    FMT[op] = 'b'
for op in (0x06, 0x0d, 0x0e, 0x0f, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1f,
           0x20, 0x21, 0x28, 0x29, 0x2c, 0x30, 0x48, 0x4d, 0x6e):
    FMT[op] = 'h'
FMT[0x23] = 'H'
for op in (0x07, 0x49, 0x4e):
    FMT[op] = 'i'
FMT[0x0b] = 's'
for op in range(0x32, 0x3b):
    FMT[op] = 't'
for op in (0x84, 0x86, 0x88):
    FMT[op] = 'c'
for op in (0x82, 0x85, 0x87):
    FMT[op] = 'n'
FMT[0x0a] = 'd'

# opcodes the interpreter handles (file level); everything else kills the task
NOARG = {0x01, 0x02, 0x03, 0x04, 0x08, 0x09, 0x0c, 0x10, 0x17, 0x1e, 0x22, 0x24, 0x2a, 0x2b, 0x2d, 0x2e, 0x2f,
         0x46, 0x4a, 0x4b, 0x4f, 0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x5b, 0x5c, 0x5d,
         0x5e, 0x5f, 0x64, 0x65, 0x66, 0x6f, 0x70, 0x83}

MN = {
    0x01: 'push0', 0x02: 'push1', 0x03: 'push2', 0x04: 'push3', 0x05: 'pushb', 0x06: 'pushw', 0x07: 'pushd',
    0x08: 'pushf0', 0x09: 'pushf1', 0x0a: 'pushf', 0x0b: 'pushs', 0x0c: 'nop',
    0x0d: 'ldl', 0x0e: 'ldg', 0x0f: 'ldm', 0x10: 'index', 0x11: 'idxl', 0x12: 'idxg', 0x13: 'idxm',
    0x14: 'stl', 0x15: 'stg', 0x16: 'stm', 0x17: 'setidx', 0x18: 'setidxl', 0x19: 'setidxg', 0x1a: 'setidxm',
    0x1b: 'mvl', 0x1c: 'mvg', 0x1d: 'mvm', 0x1e: 'mvidx', 0x1f: 'mvidxl', 0x20: 'mvidxg', 0x21: 'mvidxm',
    0x22: 'dup', 0x23: 'popn', 0x24: 'pop', 0x28: 'mkarr', 0x29: 'mktab', 0x2a: 'len', 0x2b: 'new', 0x2c: 'newc',
    0x2d: 'delete', 0x2e: 'this', 0x2f: 'instof', 0x30: 'instofc',
    0x32: 'jmp', 0x33: 'jt', 0x34: 'jf', 0x35: 'jne', 0x36: 'jeq', 0x37: 'jlt', 0x38: 'jgt', 0x39: 'jle', 0x3a: 'jge',
    0x46: 'add', 0x47: 'addb', 0x48: 'addw', 0x49: 'addd', 0x4a: 'inc', 0x4b: 'sub', 0x4c: 'subb', 0x4d: 'subw',
    0x4e: 'subd', 0x4f: 'dec', 0x50: 'mul', 0x51: 'div', 0x52: 'mod', 0x53: 'and', 0x54: 'or', 0x55: 'xor',
    0x56: 'land', 0x57: 'lor', 0x58: 'shl', 0x59: 'shr', 0x5a: 'ne', 0x5b: 'eq', 0x5c: 'lt', 0x5d: 'gt', 0x5e: 'le',
    0x5f: 'ge', 0x64: 'lnot', 0x65: 'neg', 0x66: 'not', 0x6e: 'frame', 0x6f: 'fork', 0x70: 'exit',
    0x78: 'switch', 0x79: 'sswitch', 0x82: 'native', 0x83: 'ret', 0x84: 'callg', 0x85: 'callo', 0x86: 'callc',
    0x87: 'calln', 0x88: 'calls',
}
OP = {v: k for k, v in MN.items()}


class Insn:
    __slots__ = ('pc', 'op', 'arg', 'size', 'cases')

    def __init__(self, pc, op, arg=None, size=1, cases=None):
        self.pc, self.op, self.arg, self.size, self.cases = pc, op, arg, size, cases

    @property
    def end(self):
        return self.pc + self.size

    def targets(self):
        """Code offsets this instruction may jump to (not calls)."""
        if 0x32 <= self.op <= 0x3a:
            return [self.arg]
        if self.op == 0x78:
            base, default, tgts = self.cases
            return [default] + list(tgts)
        if self.op == 0x79:
            default, ints, strs = self.cases
            return [default] + [t for _, t in ints] + [t for _, t in strs]
        return []

    def __repr__(self):
        return '%06x %s %r' % (self.pc, MN.get(self.op, 'op%02x' % self.op), self.arg if self.cases is None else self.cases)


def decode(code, pc):
    op = code[pc]
    f = FMT.get(op)
    if f is None:
        if op == 0x78:
            n, base, default = struct.unpack_from('>Hii', code, pc + 1)
            tg = struct.unpack_from('>%dI' % n, code, pc + 11)
            return Insn(pc, op, n, 11 + 4 * n, (base, default, list(tg)))
        if op == 0x79:
            ni, ns, default = struct.unpack_from('>HHI', code, pc + 1)
            w = struct.unpack_from('>%dI' % (2 * (ni + ns)), code, pc + 9)
            ints = [(struct.unpack('>i', struct.pack('>I', w[2 * k]))[0], w[2 * k + 1]) for k in range(ni)]
            strs = [(w[2 * k], w[2 * k + 1]) for k in range(ni, ni + ns)]
            return Insn(pc, op, (ni, ns), 9 + 8 * (ni + ns), (default, ints, strs))
        return Insn(pc, op)
    if f == 'b':
        return Insn(pc, op, struct.unpack_from('>b', code, pc + 1)[0], 2)
    if f == 'h':
        return Insn(pc, op, struct.unpack_from('>h', code, pc + 1)[0], 3)
    if f == 'H':
        return Insn(pc, op, struct.unpack_from('>H', code, pc + 1)[0], 3)
    if f == 'i':
        return Insn(pc, op, struct.unpack_from('>i', code, pc + 1)[0], 5)
    if f in 'stcn':
        return Insn(pc, op, struct.unpack_from('>I', code, pc + 1)[0], 5)
    if f == 'd':
        return Insn(pc, op, bytes(code[pc + 1:pc + 9]), 9)
    raise AssertionError(f)


def encode(op, arg=None, cases=None):
    f = FMT.get(op)
    if f is None:
        if op == 0x78:
            base, default, tg = cases
            return struct.pack('>BHii', op, len(tg), base, default) + b''.join(struct.pack('>I', t) for t in tg)
        if op == 0x79:
            default, ints, strs = cases
            return (struct.pack('>BHHI', op, len(ints), len(strs), default)
                    + b''.join(struct.pack('>iI', k, t) for k, t in ints)
                    + b''.join(struct.pack('>II', k, t) for k, t in strs))
        return bytes([op])
    if f == 'b':
        return struct.pack('>Bb', op, arg)
    if f == 'h':
        return struct.pack('>Bh', op, arg)
    if f == 'H':
        return struct.pack('>BH', op, arg)
    if f == 'i':
        return struct.pack('>Bi', op, arg)
    if f in 'stcn':
        return struct.pack('>BI', op, arg)
    if f == 'd':
        assert len(arg) == 8
        return bytes([op]) + arg
    raise AssertionError(f)


def walk(code, start=0, end=None):
    end = len(code) if end is None else end
    pc = start
    while pc < end:
        i = decode(code, pc)
        yield i
        pc += i.size
