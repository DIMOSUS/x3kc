"""Static verifier of KC bytecode: what would make the game's VM misbehave, checked for every method of an object.

    x3kc verify <obj> [-v]

Per method (abstract interpretation of the stack, every path):
  * the first instruction is `frame N` with N = the method row's frame (the loader sizes the task from the row,
    0x6e reserves N + 4 cells), and the stack never grows more than N + 4 cells above the frame;
  * every instruction decodes inside the method, no path runs past its end, jump and switch targets are
    instruction starts inside the method, unknown opcodes (they kill the task) do not occur;
  * the stack depth is the same on every path into an instruction and never goes below the frame;
  * `ret` is reached with exactly the return value above the frame;
  * locals (`ldl n` ...) stay inside the frame, writes never hit the argc/return-pc/context cells;
  * member fields exist in the method's class (inherited + own), globals exist;
  * direct calls go to method entries and pass as many arguments as the callee declares; calls by name and natives
    name something that exists (a missing name is not a crash — the VM answers 0 — so these are warnings);
  * argc of every call is a constant known at the call.
Per object: every class comes after its parent in CLAS, no class has parent 0, class ids are unique (the loader
sizes a class's fields from the parent records read before it; otherwise the heap overflows at load time).
Errors would break or crash the task in the game; warnings are suspicious but legal.
"""
import argparse
import os
from collections import defaultdict

from . import kcisa
from . import kcobj

# fixed stack effect of opcodes without an argc
EFFECT = {}
for op in list(range(0x01, 0x0c)) + [0x0d, 0x0e, 0x0f, 0x22, 0x2c, 0x2e, 0x6f]:
    EFFECT[op] = 1
for op in (0x0c, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x2a, 0x2b, 0x2d, 0x30, 0x32, 0x47, 0x48, 0x49, 0x4a, 0x4c,
           0x4d, 0x4e, 0x4f, 0x64, 0x65, 0x66, 0x6e):
    EFFECT[op] = 0
for op in (0x10, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x24, 0x2f, 0x33, 0x34, 0x46, 0x4b, 0x78, 0x79) + tuple(
        range(0x50, 0x60)):
    EFFECT[op] = -1
for op in (0x17, 0x1f, 0x20, 0x21) + tuple(range(0x35, 0x3b)):
    EFFECT[op] = -2
EFFECT[0x1e] = -3
LOCAL_READ = {0x0d, 0x11}
LOCAL_WRITE = {0x14, 0x18, 0x1b, 0x1f}       # 18/1f use the local as a container: its cell is read, an element set
MEMBER = {0x0f, 0x13, 0x16, 0x1a, 0x1d, 0x21}
GLOBAL = {0x0e, 0x12, 0x15, 0x19, 0x1c, 0x20}
HANDLED = set(EFFECT) | {0x23, 0x28, 0x29, 0x70, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88}


class Verifier:
    def __init__(self, o, natives=None):
        self.o = o
        self.natives = natives
        rows = [(r[0], 0, r) for r in o.symb] + [(r[0], c.id, r) for c in o.classes for r in c.methods]
        rows.sort()
        self.rows = rows
        self.entry = {e: (cid, r) for e, cid, r in rows}
        self.ends = {e: (rows[k + 1][0] if k + 1 < len(rows) else len(o.code)) for k, (e, _, _) in enumerate(rows)}
        self.method_names = {o.string(r[1]) for _, _, r in rows}
        cls = {c.id: c for c in o.classes}
        self.nfields = {0: len(o.vars)}

        def total(cid):
            n, c = 0, cls.get(cid)
            seen = set()
            while c is not None and c.id not in seen:
                seen.add(c.id)
                n += len(c.fields)
                c = cls.get(c.parent)
            return n
        for c in o.classes:
            self.nfields[c.id] = total(c.id)
        self.errors = defaultdict(list)
        self.warnings = defaultdict(list)

    def where(self, e):
        cid, r = self.entry[e]
        return '%d::%s' % (cid, self.o.string(r[1]))

    def err(self, e, pc, msg):
        self.errors[self.where(e)].append('%06x %s' % (pc, msg))

    def warn(self, e, pc, msg):
        self.warnings[self.where(e)].append('%06x %s' % (pc, msg))

    def method(self, e):
        o = self.o
        cid, r = self.entry[e]
        end = self.ends[e]
        nparams, frame = r[3], r[2]
        code = o.code
        ins = {}
        try:
            for i in kcisa.walk(code, e, end):
                ins[i.pc] = i
                if i.end > end:
                    self.err(e, i.pc, 'instruction runs past the method end')
        except Exception as x:
            self.err(e, e, 'undecodable code: %s' % x)
            return
        first = ins.get(e)
        if first is None or first.op != 0x6e:
            self.err(e, e, 'does not start with frame')
        elif first.arg != frame:
            self.err(e, e, 'frame %d but the method row says %d' % (first.arg, frame))
        for i in ins.values():
            if i.op not in HANDLED:
                self.err(e, i.pc, 'opcode %02x kills the task' % i.op)
            for t in i.targets():
                if t not in ins:
                    self.err(e, i.pc, 'jump to %06x: not an instruction of the method' % t)
        # abstract interpretation: state = list of known constants (None = unknown) above the frame
        state = {e: []}
        work = [e]
        maxd = 0
        while work:
            pc = work.pop()
            st = state[pc]
            while True:
                i = ins.get(pc)
                if i is None:
                    self.err(e, pc, 'control runs past the method end')
                    break
                st = list(st)
                d = len(st)
                op = i.op
                nxt = [i.end]
                if op in LOCAL_READ or op in LOCAL_WRITE:
                    n = i.arg
                    limit = d + 3 + nparams
                    if n < 1 or n > limit:
                        self.err(e, pc, '%s %d outside the frame (%d cells)' % (kcisa.MN[op], n, limit))
                    elif op in (0x14, 0x1b) and d < n <= d + 3:
                        self.err(e, pc, '%s %d overwrites the %s cell' % (
                            kcisa.MN[op], n, ('context', 'return pc', 'argc')[n - d - 1]))
                if op in MEMBER and not 0 <= i.arg < self.nfields.get(cid, 0):
                    self.err(e, pc, '%s %d: class %d has %d fields' % (kcisa.MN[op], i.arg, cid, self.nfields.get(cid, 0)))
                if op in GLOBAL and not 0 <= i.arg < self.nfields[0]:
                    self.err(e, pc, '%s %d: %d globals' % (kcisa.MN[op], i.arg, self.nfields[0]))
                if op == 0x82 and self.natives is not None and o.string(i.arg) not in self.natives:
                    self.warn(e, pc, 'native %s is unknown to the exe (answers int 0)' % o.string(i.arg))
                if op in (0x85, 0x87) and o.string(i.arg) not in self.method_names:
                    self.warn(e, pc, 'no class has a method %s (the call answers int 0)' % o.string(i.arg))
                # stack effect
                if op in (0x01, 0x02, 0x03, 0x04):
                    st.append(op - 1)
                elif op in (0x05, 0x06, 0x07):
                    st.append(i.arg)
                elif op in EFFECT:
                    k = EFFECT[op]
                    if k < 0:
                        if d + k < 0:
                            self.err(e, pc, '%s pops below the frame' % kcisa.MN[op])
                            break
                        del st[len(st) + k:]
                    else:
                        st.extend([None] * k)
                    if op == 0x6f:
                        pass
                elif op == 0x23:
                    if i.arg > d:
                        self.err(e, pc, 'popn %d below the frame' % i.arg)
                        break
                    del st[d - i.arg:]
                elif op in (0x28, 0x29):
                    k = i.arg * (2 if op == 0x29 else 1)
                    if k > d:
                        self.err(e, pc, '%s %d takes more cells than the stack holds' % (kcisa.MN[op], i.arg))
                        break
                    del st[d - k:]
                    st.append(None)
                elif op in (0x82, 0x84, 0x87, 0x88, 0x85, 0x86):
                    obj = 1 if op in (0x85, 0x86) else 0
                    if d < 1 + obj:
                        self.err(e, pc, '%s without argc on the stack' % kcisa.MN[op])
                        break
                    argc = st[d - 1 - obj]
                    if argc is None:
                        self.err(e, pc, '%s: argc is not a known constant' % kcisa.MN[op])
                        break
                    if argc < 0 or argc + 1 + obj > d:
                        self.err(e, pc, '%s: argc %d but only %d cells' % (kcisa.MN[op], argc, d))
                        break
                    if op in (0x84, 0x86, 0x88):
                        tgt = self.entry.get(i.arg)
                        if tgt is None:
                            self.err(e, pc, '%s to %06x: not a method entry' % (kcisa.MN[op], i.arg))
                        elif tgt[1][3] != argc:
                            self.warn(e, pc, '%s %s with %d args, it declares %d' % (
                                kcisa.MN[op], self.where(i.arg), argc, tgt[1][3]))
                    del st[d - 1 - obj - argc:]
                    st.append(None)
                elif op == 0x83:
                    if d != 1:
                        self.err(e, pc, 'ret with %d cells above the frame (1 expected)' % d)
                    break
                elif op == 0x70:
                    break
                maxd = max(maxd, len(st))
                if op == 0x32:
                    nxt = [i.arg]
                elif 0x33 <= op <= 0x3a:
                    nxt = [i.end, i.arg]
                elif op in (0x78, 0x79):
                    nxt = i.targets()
                cont = None
                for t in nxt:
                    if t in state:
                        if len(state[t]) != len(st):
                            self.err(e, pc, 'stack depth %d at %06x, %d on another path' % (
                                len(st), t, len(state[t])))
                        else:
                            merged = [a if a == b else None for a, b in zip(state[t], st)]
                            if merged != state[t]:
                                state[t] = merged
                                work.append(t)
                    else:
                        state[t] = st
                        if cont is None and t == i.end:
                            cont = t
                        else:
                            work.append(t)
                if cont is None:
                    break
                pc = cont
        if first is not None and first.op == 0x6e and maxd > frame + 4:
            self.err(e, e, 'stack reaches %d cells above the frame, frame %d reserves %d' % (maxd, frame, frame + 4))

    def classes(self):
        """CLAS order rules of Script::LoadObject (0x0049d030): while reading CLAS the loader looks a class's parent up
        only among the records already read, and slot 0 (class 0) is still empty then; the inherited field count comes
        from that lookup, while the static cells are filled later along the whole parent chain (resolved again by id
        after the sort). A parent that comes later, or parent 0, leaves the class with a cell array too small for its
        inherited fields: a heap overflow at load time. Class ids must be unique, as parents are resolved by id."""
        seen = {}
        for k, c in enumerate(self.o.classes):
            where = '(classes)'
            if c.id in seen:
                self.errors[where].append('class %d appears twice in CLAS (records %d and %d)' % (c.id, seen[c.id], k))
            seen.setdefault(c.id, k)
            if c.parent == 0xffffffff:
                continue
            if c.parent == 0:
                self.errors[where].append('class %d has parent 0 (the globals): the loader does not count its fields'
                                          % c.id)
            elif c.parent not in seen:
                later = any(x.id == c.parent for x in self.o.classes[k + 1:])
                self.errors[where].append(
                    'class %d comes before its parent %d in CLAS: the loader then does not count the inherited fields '
                    '(heap overflow at load); put the class after its parent in the source' % (c.id, c.parent)
                    if later else 'class %d has an unknown parent %d' % (c.id, c.parent))

    def run(self):
        self.classes()
        for e, _, _ in self.rows:
            self.method(e)


def load_natives():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'natives.txt')
    return {l.strip() for l in open(p) if l.strip() and not l.startswith('#')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('obj')
    ap.add_argument('-v', action='store_true', help='print every finding, not only counts per method')
    a = ap.parse_args()
    V = Verifier(kcobj.load(a.obj), load_natives())
    V.run()
    for kind, table in (('ERROR', V.errors), ('warning', V.warnings)):
        n = sum(len(v) for v in table.values())
        print('%s: %d in %d methods' % (kind, n, len(table)))
        for m, lines in sorted(table.items()):
            if a.v or kind == 'ERROR':
                for l in lines[:6 if not a.v else None]:
                    print('  %s  %s' % (m, l))
    return 1 if V.errors else 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
