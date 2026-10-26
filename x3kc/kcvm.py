"""Emulator of the KC script VM of X3: Albion Prelude (Script::Interpret 0x004a26a0), for testing objects without
the game.

    vm = VM(kcobj.load('x3story.obj'))
    obj = vm.new(2016)                          # an instance of class 2016, fields from the class chain's init records
    vm.call(obj, 'Hit', [100, 50, 0, 0])        # runs the method (and every task it forks) until all tasks wait
    vm.run(ms=5000)                             # advances the virtual clock, wakes timer waits

Semantics follow vm/scheduler_bytecode.md and the cell helpers of the exe (checked against their code):
  * cells: int (32-bit), double, string (pool string: false only at offset 0; heap string: always true), array
    and table (by reference), object (an int id: instances < 0, class objects = class id), empty (true!);
  * a missing table key or an array index out of range reads as int 0; writing outside an array does nothing;
  * `<`/`>` of non-numbers are false, so `<=`/`>=` of them are true; `==` of strings compares text, of
    containers identity, mixed others "both false";
  * by-name calls (callo/calln) pad missing arguments with int 0; a missing object or method answers int 0;
  * a native the emulator does not know answers int 0 and is recorded in vm.unknown_natives (like the exe for
    names it lacks); natives the tests care about are given as Python functions (vm.natives[name] = f).
Problems the game would not survive are raised as VMError (unknown opcode, stack underflow, a jump or a return
the frame does not allow); a task that runs more than vm.budget instructions without waiting raises Hang.
"""
import math
import struct

from . import kcisa

M32 = 0xffffffff


def s32(v):
    v &= M32
    return v - 0x100000000 if v & 0x80000000 else v


class VMError(Exception):
    pass


class Hang(VMError):
    pass


class PStr(str):
    """A string cell that points into the pool (type 2): its truth is its offset."""
    off = 0

    def __new__(cls, s, off):
        x = str.__new__(cls, s)
        x.off = off
        return x


class KArray:
    __slots__ = ('items',)

    def __init__(self, items=None):
        self.items = list(items or [])

    def __repr__(self):
        return 'KArray(%r)' % self.items


class KTable:
    """Script table exactly as the exe keeps it (Script::TableSet 0x004a7630, TableRehash 0x004a77e0,
    TableHashKey 0x004aabe0, TableNextEntry 0x004aace0): buckets (4, doubled when count reaches 4 x buckets, a
    rehash pushes every node at the head of its new chain), int key -> key & (n-1), string key -> h = c + 2h over
    its bytes; only int and string keys can be found again (others go to bucket 0 as unfindable duplicates).
    Iteration: buckets ascending, chain order."""
    __slots__ = ('buckets', 'count', 'auto')

    def __init__(self, d=None):
        self.buckets = [[] for _ in range(4)]
        self.count = 0
        self.auto = 1                       # SE_TableNew's key counter (table+0x10)
        for k, v in (d or {}).items():
            self.set(k, v)

    @staticmethod
    def findable(k):
        return (isinstance(k, int) and not isinstance(k, bool)) or isinstance(k, str)

    def hash(self, k, n=None):
        n = n or len(self.buckets)
        if isinstance(k, bool):
            k = int(k)
        if isinstance(k, int):
            return k & (n - 1)
        if isinstance(k, str):
            h = 0
            for c in k.encode('latin1', 'replace'):
                h = (c + 2 * h) & 0xffffffff
            return h & (n - 1)
        return 0

    def node(self, k):
        if not self.findable(k):
            return None
        for nd in self.buckets[self.hash(k)]:
            if type(nd[0]) is not bool and self.findable(nd[0]) and isinstance(nd[0], str) == isinstance(k, str) \
                    and nd[0] == k:
                return nd
        return None

    def get(self, k, default=0):
        nd = self.node(k)
        return nd[1] if nd is not None else default

    def set(self, k, v):
        if isinstance(k, bool):
            k = int(k)
        if isinstance(k, str):
            k = str(k)
        nd = self.node(k)
        if nd is not None:
            nd[1] = v
            return
        self.buckets[self.hash(k)].append([k, v])
        self.count += 1
        n = len(self.buckets)
        if self.count >= 4 * n:
            new = [[] for _ in range(2 * n)]
            for chain in self.buckets:
                for nd in chain:
                    new[self.hash(nd[0], 2 * n)].insert(0, nd)
            self.buckets = new

    def remove(self, k):
        nd = self.node(k)
        if nd is None:
            return None
        self.buckets[self.hash(k)].remove(nd)
        self.count -= 1
        return nd

    def __contains__(self, k):
        return self.node(k) is not None

    def keys(self):
        return [nd[0] for chain in self.buckets for nd in chain]

    def next_key(self, prev, start):
        """TableNextEntry: the key after prev; a prev that is not found (removed, unfindable) restarts."""
        if not self.count:
            return 0
        if not start:
            nd = self.node(prev)
            if nd is not None:
                b = self.hash(prev)
                chain = self.buckets[b]
                j = next(i for i, x in enumerate(chain) if x is nd)
                if j + 1 < len(chain):
                    return chain[j + 1][0]
                for chain in self.buckets[b + 1:]:
                    if chain:
                        return chain[0][0]
                return 0
        for chain in self.buckets:
            if chain:
                return chain[0][0]
        return 0

    @property
    def d(self):
        """{key: value} in iteration order (findable keys), for tests."""
        return {nd[0]: nd[1] for chain in self.buckets for nd in chain}

    def __repr__(self):
        return 'KTable(%r)' % self.d


EMPTY = None


class RetPC:
    __slots__ = ('pc',)

    def __init__(self, pc):
        self.pc = pc


class Ctx:
    __slots__ = ('obj',)

    def __init__(self, obj):
        self.obj = obj


class WAIT:
    """Returned by a native to suspend the task: until=virtual time (ms) or None (until vm.wake(task, value))."""

    def __init__(self, until=None, result=0):
        self.until, self.result = until, result


def key_of(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, str)):
        return str(v) if isinstance(v, str) else v
    return None


def truth(v):
    if v is None:
        return True                         # CellIsTrue: type 0 falls into the default "true"
    if isinstance(v, PStr):
        return v.off != 0
    if isinstance(v, str):
        return True
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, (KArray, KTable)):
        return True
    return True


def equals(a, b):
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return str(a) == str(b)
    if type(a) is type(b) and isinstance(a, (KArray, KTable)):
        return a is b
    return not truth(a) and not truth(b)


def less(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a < b
    return False


def fmt_num(v):
    if isinstance(v, float):
        return '%g' % v
    return '%d' % v


class Instance:
    __slots__ = ('id', 'cls', 'fields', 'alive')

    def __init__(self, oid, cls, fields):
        self.id, self.cls, self.fields, self.alive = oid, cls, fields, True


class Task:
    _next = 1

    def __init__(self, vm, ctx, pc, stack):
        self.id = Task._next
        Task._next += 1
        self.vm, self.ctx, self.pc, self.stack = vm, ctx, pc, stack
        self.state = 'ready'                # ready | wait | done | killed
        self.until = None
        self.result = None
        self.error = None


class VM:
    def __init__(self, o, names=None):
        self.o = o
        self.code = o.code
        self.cls = {c.id: c for c in o.classes}
        self.methods = {}                   # cid -> {name: row}
        for c in o.classes:
            self.methods[c.id] = {o.string(r[1]): r for r in c.methods}
        self.methods[0] = {o.string(r[1]): r for r in o.symb}
        self.entries = {}
        for cid, ms in self.methods.items():
            for n, r in ms.items():
                self.entries[r[0]] = (cid, n, r)
        self.objects = {}
        self.statics = {}
        self.next_id = 1
        self.tasks = {}
        self.ready = []
        self.clock = 0
        self.budget = 5_000_000
        self.steps = 0
        self.natives = {}
        self.overrides = {}                 # (cid, name) or name -> f(vm, task, obj, args): replaces a KC method
        self.method_log = []                # (cid, name, obj, args) of every method entered
        self.unknown_natives = []
        self.calls = []                     # (native name, args) of every native call, for tests
        self.trace = None
        from . import kcnatives
        kcnatives.install(self)
        kcnatives.install_1(self)
        kcnatives.install_2(self)
        kcnatives.install_pending(self)

    def migrate(self, new_obj):
        """A savegame made now, loaded with another build of the object (Script::LoadVmState): the new VM gets
        every object, static field and task; the pc of each task and every return address on its stack are moved
        through the new object's PTCH record for this build. Raises VMError where the game would refuse or break."""
        from . import kcsave
        rec = next((p for b, p in new_obj.ptch if b == self.o.build), None)
        same = new_obj.build == self.o.build
        if rec is None and not same:
            raise VMError('no PTCH record for build %d: the game refuses the savegame' % self.o.build)
        segs = kcsave.pairs_to_segs(rec) if rec is not None else [(0, 0)]

        def move(pc, what):
            d = kcsave.lookup(segs, pc)
            if d is None:
                raise VMError('%s %06x is in removed code: the task cannot be restored' % (what, pc))
            return pc + d
        nv = VM(new_obj)
        nv.field_names = self.field_names
        nv.objects, nv.statics, nv.next_id, nv.clock = self.objects, self.statics, self.next_id, self.clock
        nv.rand.seed = self.rand.seed
        nv.natives.update({k: v for k, v in self.natives.items() if k not in nv.natives})
        nv.overrides = self.overrides
        nv.msci_ready = getattr(self, 'msci_ready', False)
        for t in self.tasks.values():
            if t.state not in ('ready', 'wait'):
                continue
            t.vm = nv
            t.pc = move(t.pc, 'task pc')
            t.stack = [RetPC(move(x.pc, 'return address')) if isinstance(x, RetPC) and x.pc else x for x in t.stack]
            nv.tasks[t.id] = t
        return nv

    def reset(self, seed=1):
        """Forget every object, static field, task and log (the loaded object stays)."""
        self.objects, self.statics, self.tasks, self.ready = {}, {}, {}, []
        self.next_id, self.clock = 1, 0
        self.calls, self.method_log, self.unknown_natives = [], [], []
        self.rand.seed = seed

    # ---- classes and objects -----------------------------------------------------------------------------------
    def chain(self, cid):
        out = []
        c = self.cls.get(cid)
        while c is not None and c.id not in out:
            out.append(c.id)
            c = self.cls.get(c.parent)
        return out

    def init_fields(self, cid):
        rows = list(self.o.vars) if cid == 0 else [r for c in reversed(self.chain(cid)) for r in self.cls[c].fields]
        return [self.const(r[2], r[1]) for r in rows]

    def const(self, typ, val):
        if typ == 1:
            return s32(val)
        if typ == 2:
            return PStr(self.o.string(val) or '', val)
        if typ == 8:
            return KArray()
        if typ == 9:
            return KTable()
        return EMPTY

    def static(self, cid):
        if cid not in self.statics:
            self.statics[cid] = self.init_fields(cid)
        return self.statics[cid]

    def new(self, cid):
        if cid not in self.cls:
            return 0
        oid = -1 - self.next_id
        self.next_id += 1
        self.objects[oid] = Instance(oid, cid, self.init_fields(cid))
        return oid

    def delete(self, oid):
        inst = self.objects.pop(oid, None)
        if inst is not None:
            inst.alive = False
            for t in list(self.tasks.values()):
                if t.ctx == oid and t.state in ('ready', 'wait'):
                    t.state = 'killed'

    def class_of(self, obj):
        if isinstance(obj, int) and obj < 0:
            inst = self.objects.get(obj)
            return inst.cls if inst else None
        if isinstance(obj, int) and obj in self.cls:
            return obj
        if obj == 0:
            return None
        return None

    def is_class(self, obj, cid):
        c = self.class_of(obj)
        return c is not None and cid in self.chain(c)

    def fields_of(self, obj):
        if isinstance(obj, int) and obj < 0:
            return self.objects[obj].fields
        return self.static(obj)

    def find_method(self, cid, name):
        for c in self.chain(cid) if cid else [0]:
            r = self.methods.get(c, {}).get(name)
            if r is not None:
                return r
        return None

    # ---- names for tests -----------------------------------------------------------------------------------
    def field_index(self, cid, name):
        """Absolute field index of a named field (annotations in vm.field_names: {(cid, own index): name});
        `m12` is the absolute index 12, as the unnamed source prints it."""
        import re
        m = re.fullmatch(r'[mg](\d+)', name)
        if m:
            return int(m.group(1))
        if cid == 0:
            for (c, j), n in self.field_names.items():
                if c == 0 and n == name:
                    return j
            raise KeyError('no global %s' % name)
        base = 0
        for c in reversed(self.chain(cid)):
            for j in range(len(self.cls[c].fields)):
                if self.field_names.get((c, j)) == name:
                    return base + j
            base += len(self.cls[c].fields)
        raise KeyError('%d has no field %s' % (cid, name))

    field_names = {}

    def load_names(self, path):
        """Field names from an annotation file (working or portable form; "auto:x3story": the bundled names of
        that object), for get()/set()."""
        from . import annotations, kcd, kcnames
        if path.startswith('auto:'):
            path = kcd.bundled_names(path[5:])
        a = annotations.resolve(self.o, kcnames.load(path))
        fn = {}
        for scid, c in (a.get('classes') or {}).items():
            for j, f in (c.get('fields') or {}).items():
                if f.get('name'):
                    fn[int(scid), int(j)] = f['name']
        for j, f in ((a.get('globals') or {}).get('fields') or {}).items():
            if f.get('name'):
                fn[0, int(j)] = f['name']
        self.field_names = fn

    def get(self, obj, name):
        return self.fields_of(obj)[self.field_index(self.class_of(obj) if obj else 0, name)]

    def set(self, obj, name, value):
        self.fields_of(obj)[self.field_index(self.class_of(obj) if obj else 0, name)] = value

    def called(self, name):
        """Argument lists of every entry into a method `name` so far."""
        return [args for _, n, _, args in self.method_log if n == name]

    # ---- running ----------------------------------------------------------------------------------------------
    def call(self, obj, name, args=(), run=True):
        """Start method `name` on obj (an object id, a class id, or 0 for a global function); returns its result
        when it ends without waiting, else the task (finish it with vm.run)."""
        cid = self.class_of(obj) if obj else 0
        r = self.find_method(cid, name) if (cid or obj == 0) else None
        if r is None:
            raise VMError('no method %s on %r' % (name, obj))
        args = list(args) + [0] * (r[3] - len(args))
        stack = list(reversed(args)) + [len(args), RetPC(0), Ctx(obj)]
        t = Task(self, obj, r[0], stack)
        self.tasks[t.id] = t
        if run:
            self.execute(t)
            self.run_ready()
            if t.error:
                raise t.error
            return t.result if t.state == 'done' else t
        return t

    def run_ready(self):
        while self.ready:
            t = self.ready.pop(0)
            if t.state == 'ready':
                self.execute(t)

    def run(self, ms=0):
        """Advance the virtual clock by up to ms, waking timer waits in order."""
        end = self.clock + ms
        while True:
            self.run_ready()
            due = [t for t in self.tasks.values() if t.state == 'wait' and t.until is not None and t.until <= end]
            if not due:
                break
            t = min(due, key=lambda x: (x.until, x.id))
            self.clock = max(self.clock, t.until)
            self.resume(t, t.wait_result)
        self.clock = end

    def resume(self, t, value):
        t.stack.append(value)
        t.state, t.until = 'ready', None
        self.execute(t)

    def wake(self, t, value=0):
        if t.state == 'wait':
            self.resume(t, value)
            self.run_ready()

    # ---- the interpreter -----------------------------------------------------------------------------------
    def execute(self, t):
        try:
            self._execute(t)
        except VMError as x:
            t.state, t.error = 'killed', x
            x.task = t
        except Exception as x:                  # an emulator fault, not the script's
            t.state = 'killed'
            t.error = VMError('emulator: %s: %s at %06x' % (type(x).__name__, x, t.pc))
            raise

    def _execute(self, t):
        code, o = self.code, self.o
        st = t.stack
        steps = 0
        while True:
            if t.state != 'ready':
                return
            steps += 1
            if steps > self.budget:
                raise Hang('task %d ran %d instructions without waiting (at %06x)' % (t.id, steps, t.pc))
            i = kcisa.decode(code, t.pc)
            op = i.op
            if self.trace:
                self.trace(t, i)
            t.pc = i.end
            if op in (0x01, 0x02, 0x03, 0x04):
                st.append(op - 1)
            elif op in (0x05, 0x06, 0x07):
                st.append(i.arg)
            elif op == 0x08:
                st.append(0.0)
            elif op == 0x09:
                st.append(1.0)
            elif op == 0x0a:
                st.append(struct.unpack('>d', i.arg)[0])
            elif op == 0x0b:
                st.append(PStr(self.pool_string(i.arg), i.arg))
            elif op == 0x0c:
                pass
            elif op == 0x0d:
                st.append(self.slot(st, i.arg))
            elif op == 0x0e:
                st.append(self.static(0)[i.arg])
            elif op == 0x0f:
                st.append(self.member(t)[i.arg])
            elif op == 0x10:
                c = st.pop()
                st[-1] = self.index_get(c, st[-1])
            elif op in (0x11, 0x12, 0x13):
                st[-1] = self.index_get(self.var(t, op - 0x11, i.arg), st[-1])
            elif op in (0x14, 0x15, 0x16):
                self.set_var(t, op - 0x14, i.arg, st[-1])
            elif op == 0x17:
                c, k = st.pop(), st.pop()
                self.index_set(c, k, st[-1])
            elif op in (0x18, 0x19, 0x1a):
                c = self.var(t, op - 0x18, i.arg)
                k = st.pop()
                self.index_set(c, k, st[-1])
            elif op in (0x1b, 0x1c, 0x1d):
                v = st[-1]
                self.set_var(t, op - 0x1b, i.arg, v)
                st.pop()
            elif op == 0x1e:
                c, k, v = st.pop(), st.pop(), st.pop()
                self.index_set(c, k, v)
            elif op in (0x1f, 0x20, 0x21):
                c = self.var(t, op - 0x1f, i.arg)
                k, v = st.pop(), st.pop()
                self.index_set(c, k, v)
            elif op == 0x22:
                st.append(st[-1])
            elif op == 0x23:
                self.need(st, i.arg)
                del st[len(st) - i.arg:]
            elif op == 0x24:
                self.need(st, 1)
                st.pop()
            elif op == 0x28:
                n = i.arg
                items = [st.pop() for _ in range(n)] if n else []
                st.append(KArray(items))
            elif op == 0x29:
                tb = KTable()
                for _ in range(i.arg):
                    k = st.pop()
                    v = st.pop()
                    tb.set(k, v)
                st.append(tb)
            elif op == 0x2a:
                st[-1] = self.length(st[-1])
            elif op == 0x2b:
                st[-1] = self.new(st[-1]) if isinstance(st[-1], int) and st[-1] > 0 else 0
            elif op == 0x2c:
                st.append(self.new(i.arg))
            elif op == 0x2d:
                obj = st[-1]
                st[-1] = EMPTY
                if isinstance(obj, int) and obj in self.objects:
                    self.delete(obj)
                    if obj == t.ctx:
                        t.state = 'killed'
                        return
            elif op == 0x2e:
                st.append(t.ctx if isinstance(t.ctx, int) else 0)
            elif op == 0x2f:
                c = st.pop()
                st[-1] = int(self.is_class(st[-1], c)) if isinstance(c, int) else 0
            elif op == 0x30:
                pass
            elif op == 0x32:
                t.pc = i.arg
            elif op in (0x33, 0x34):
                v = st.pop()
                if truth(v) == (op == 0x33):
                    t.pc = i.arg
            elif 0x35 <= op <= 0x3a:
                b, a = st.pop(), st.pop()
                if self.compare(op - 0x35, a, b):
                    t.pc = i.arg
            elif op in (0x46, 0x4b, 0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59):
                b = st.pop()
                st[-1] = self.binop(op, st[-1], b)
            elif op in (0x47, 0x48, 0x49, 0x4a):
                st[-1] = self.binop(0x46, st[-1], 1 if op == 0x4a else i.arg)
            elif op in (0x4c, 0x4d, 0x4e, 0x4f):
                st[-1] = self.binop(0x4b, st[-1], 1 if op == 0x4f else i.arg)
            elif 0x5a <= op <= 0x5f:
                b = st.pop()
                st[-1] = int(self.compare({0x5a: 0, 0x5b: 1, 0x5c: 2, 0x5d: 3, 0x5e: 4, 0x5f: 5}[op], st[-1], b))
            elif op == 0x64:
                st[-1] = int(not truth(st[-1]))
            elif op == 0x65:
                v = st[-1]
                st[-1] = s32(-v) if isinstance(v, int) else -v if isinstance(v, float) else v
            elif op == 0x66:
                v = st[-1]
                st[-1] = s32(~v) if isinstance(v, int) else v
            elif op == 0x6e:
                pass
            elif op == 0x6f:
                self.fork(t)
            elif op == 0x70:
                t.state = 'killed'
                return
            elif op == 0x78:
                v = st.pop()
                base, default, tg = i.cases
                t.pc = tg[v - base] if isinstance(v, int) and 0 <= v - base < len(tg) else default
            elif op == 0x79:
                v = st.pop()
                default, ints, strs = i.cases
                t.pc = default
                if isinstance(v, int):
                    t.pc = next((p for k, p in ints if k == v), default)
                elif isinstance(v, str):
                    t.pc = next((p for k, p in strs if self.pool_string(k) == str(v)), default)
            elif op == 0x82:
                self.native(t, o.string(i.arg))
                if t.state != 'ready':
                    return
            elif op == 0x83:
                if self.ret(t):
                    return
            elif op in (0x84, 0x88):
                self.enter(t, i.arg, 0 if op == 0x84 else t.ctx, None)
            elif op == 0x86:
                obj = st.pop()
                if not self.exists(obj):
                    self.answer_zero(t)
                else:
                    self.enter(t, i.arg, obj, None)
            elif op == 0x85:
                obj = st.pop()
                self.by_name(t, obj, o.string(i.arg))
            elif op == 0x87:
                self.by_name(t, t.ctx, o.string(i.arg))
            else:
                raise VMError('opcode %02x kills the task at %06x' % (op, i.pc))

    # ---- helpers --------------------------------------------------------------------------------------------
    def pool_string(self, off):
        data = self.o.strings
        e = data.find(b'\0', off)
        return data[off:e if e >= 0 else len(data)].decode('latin1')

    def need(self, st, n):
        if n > len(st):
            raise VMError('stack underflow')

    def slot(self, st, n):
        if n < 1 or n > len(st):
            raise VMError('local %d outside the stack' % n)
        v = st[-n]
        if isinstance(v, (RetPC, Ctx)):
            return 0
        return v

    def member(self, t):
        return self.fields_of(t.ctx)

    def var(self, t, kind, n):
        if kind == 0:
            return self.slot(t.stack, n)
        if kind == 1:
            return self.static(0)[n]
        return self.member(t)[n]

    def set_var(self, t, kind, n, v):
        if kind == 0:
            if n < 1 or n > len(t.stack) or isinstance(t.stack[-n], (RetPC, Ctx)):
                raise VMError('write to local %d hits the frame' % n)
            t.stack[-n] = v
        elif kind == 1:
            self.static(0)[n] = v
        else:
            self.member(t)[n] = v

    def exists(self, obj):
        return isinstance(obj, int) and ((obj < 0 and obj in self.objects) or obj in self.cls or obj == 0 and False)

    def index_get(self, c, k):
        if isinstance(c, KArray):
            if isinstance(k, int) and 0 <= k < len(c.items):
                return c.items[k]
            return 0
        if isinstance(c, str):
            if isinstance(k, int) and 0 <= k < len(c):
                return ord(c[k]) & 0xff
            return 0
        if isinstance(c, KTable):
            return c.get(k, 0)
        return 0

    def index_set(self, c, k, v):
        if isinstance(c, KArray):
            if isinstance(k, int) and 0 <= k < len(c.items):
                c.items[k] = v
        elif isinstance(c, KTable):
            if KTable.findable(k):          # ArrayOrTableSetKey: int and string keys only
                c.set(k, v)

    def length(self, v):
        if isinstance(v, KArray):
            return len(v.items)
        if isinstance(v, str):
            return len(v)
        if isinstance(v, KTable):
            return v.count
        return 0

    def compare(self, k, a, b):
        if k == 0:
            return not equals(a, b)
        if k == 1:
            return equals(a, b)
        if k == 2:
            return less(a, b)
        if k == 3:
            return less(b, a)
        if k == 4:
            return not less(b, a)
        return not less(a, b)

    def binop(self, op, a, b):
        if op == 0x46:
            if isinstance(a, str) or isinstance(b, str):
                if isinstance(a, str) and isinstance(b, str):
                    return str(a) + str(b)
                if isinstance(a, str) and isinstance(b, (int, float)):
                    return str(a) + fmt_num(b)
                if isinstance(b, str) and isinstance(a, (int, float)):
                    return fmt_num(a) + str(b)
                return a
            return self.arith(a, b, lambda x, y: x + y)
        if op == 0x4b:
            return self.arith(a, b, lambda x, y: x - y)
        if op == 0x50:
            return self.arith(a, b, lambda x, y: x * y)
        if op == 0x51:
            return self.arith(a, b, self.div)
        if op == 0x52:
            return self.arith(a, b, self.mod)
        if op in (0x53, 0x54, 0x55, 0x58, 0x59):
            if not (isinstance(a, int) and isinstance(b, int)):
                return a
            if op == 0x53:
                return s32(a & b)
            if op == 0x54:
                return s32(a | b)
            if op == 0x55:
                return s32(a ^ b)
            if op == 0x58:
                return s32(a << (b & 31))
            return a >> (b & 31)
        if op == 0x56:
            return int(truth(a) and truth(b))
        if op == 0x57:
            return int(truth(a) or truth(b))
        raise VMError('binop %02x' % op)

    @staticmethod
    def div(x, y):
        if y == 0:
            return 0
        if isinstance(x, int) and isinstance(y, int):
            if x == -0x80000000 and y == -1:
                # CellDivide 0x004a9170 guards only y == 0: IDIV raises #DE and the game crashes
                raise VMError('-2147483648 / -1: integer divide overflow, the game crashes')
            q = abs(x) // abs(y)
            return q if (x < 0) == (y < 0) else -q
        return x / y

    @staticmethod
    def mod(x, y):
        if y == 0:
            return 0
        if isinstance(x, int) and isinstance(y, int):
            if x == -0x80000000 and y == -1:
                # CellModulo 0x004a92c0: the same unguarded IDIV
                raise VMError('-2147483648 %% -1: integer divide overflow, the game crashes')
            return int(math.fmod(x, y))
        return math.fmod(x, y)

    def arith(self, a, b, f):
        if isinstance(a, bool):
            a = int(a)
        if isinstance(a, int) and isinstance(b, int):
            return s32(f(a, b))
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(f(float(a), float(b)))
        return a

    def enter(self, t, entry, ctx, pad_to):
        if entry not in self.entries:
            raise VMError('call to %06x, which is no method entry' % entry)
        st = t.stack
        cid, name, r = self.entries[entry]
        argc = st[-1]
        args = [st[-2 - k] for k in range(argc)]
        self.method_log.append((cid, name, ctx, args))
        f = self.overrides.get((cid, name), self.overrides.get(name))
        if f is not None:
            st.pop()
            del st[len(st) - argc:]
            r = f(self, t, ctx, args)
            if isinstance(r, WAIT):
                t.state, t.until, t.wait_result = 'wait', r.until, r.result
                return
            st.append(0 if r is None else r)
            return
        st.append(RetPC(t.pc))
        st.append(Ctx(t.ctx))
        t.ctx = ctx
        t.pc = entry

    def answer_zero(self, t):
        st = t.stack
        argc = st.pop()
        del st[len(st) - argc:]
        st.append(0)

    def by_name(self, t, obj, name):
        cid = self.class_of(obj) if self.exists(obj) else None
        r = self.find_method(cid, name) if cid is not None else None
        if r is None:
            self.answer_zero(t)
            return
        st = t.stack
        argc = st[-1]
        if argc < r[3]:
            st.pop()
            args = [st.pop() for _ in range(argc)]
            st.extend([0] * (r[3] - argc))
            st.extend(reversed(args))
            st.append(r[3])
        self.enter(t, r[0], obj, None)

    def ret(self, t):
        st = t.stack
        v = st.pop()
        ctx, pc, argc = st.pop(), st.pop(), st.pop()
        if not isinstance(ctx, Ctx) or not isinstance(pc, RetPC) or not isinstance(argc, int):
            raise VMError('ret on a broken frame at %06x' % t.pc)
        del st[len(st) - argc:]
        t.ctx = ctx.obj
        if pc.pc == 0:
            t.state, t.result = 'done', v
            del self.tasks[t.id]
            return True
        st.append(v)
        t.pc = pc.pc
        return False

    def fork(self, t):
        st = t.stack
        k = len(st) - 1
        while k >= 0 and not isinstance(st[k], RetPC):
            k -= 1
        if k < 1 or not isinstance(st[k - 1], int):
            raise VMError('fork outside a call frame')
        argc = st[k - 1]
        # the child gets the whole frame (args, argc, return cell, context, locals); its return ends it
        frame = st[k - 1 - argc:]
        frame = [RetPC(0) if isinstance(x, RetPC) and j == argc + 1 else x for j, x in enumerate(frame)]
        child = Task(self, t.ctx, t.pc, frame + [0])
        child.forked = True
        self.tasks[child.id] = child
        st.append(child.id)
        self.execute(child)
        if child.state == 'done':
            self.tasks.pop(child.id, None)

    def native(self, t, name):
        st = t.stack
        argc = st.pop()
        args = [st.pop() for _ in range(argc)]
        self.calls.append((name, args))
        f = self.natives.get(name)
        if f is None:
            self.unknown_natives.append(name)
            st.append(0)
            return
        r = f(self, t, args)
        if isinstance(r, WAIT):
            t.state, t.until, t.wait_result = 'wait', r.until, r.result
            return
        st.append(0 if r is None else r)
