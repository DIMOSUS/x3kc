"""SE_ / TI_ natives for the emulator (kcvm), following docs/natives/spec_*.jsonl (read from the exe's
Script::NativeDispatch and the TI dispatcher). Natives that only touch the engine (SA_, X2_, INS_, sound, 3D, files)
are not here: the VM answers them with int 0 and records the call; a test that needs one sets
vm.natives[name] = f(vm, task, args).

A native is f(vm, task, args) -> value | WAIT; args[0] is the first argument of the script call.
Where the exe would crash (an integer divide overflow in SE_DivFix / SE_MulDivFix) the native raises VMError.
"""
import math
import re

from .kcvm import Hang, KArray, KTable, PStr, WAIT, VMError, s32, truth


def arg(args, k, default=0):
    return args[k] if k < len(args) else default


def ival(v):
    """The raw dword of a cell read as int, the way most natives read their arguments."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    return 0


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def sval(v):
    return str(v) if isinstance(v, str) else None


class Rand:
    """MSVC rand(): SE_Random(n) = rand() * n / 0x8000 (Math::RandScaled 0x004ee1c0)."""

    def __init__(self, seed=1):
        self.seed = seed

    def rand(self):
        self.seed = (self.seed * 214013 + 2531011) & 0xffffffff
        return (self.seed >> 16) & 0x7fff

    def scaled(self, n):
        return (n * self.rand()) // 0x8000 if n >= 1 else 0


def debug_string(v):
    """Script::ValueToDebugString (the %a conversion)."""
    if v is None:
        return '<type 0>'
    if isinstance(v, PStr):
        return "'%s'" % v
    if isinstance(v, str):
        return '"%s"' % v
    if isinstance(v, float):
        return '%g' % v
    if is_int(v):
        return '%d' % v
    if isinstance(v, KArray):
        return '{%s}' % ','.join(debug_string(x) for x in v.items)
    if isinstance(v, KTable):
        return '[%s]' % ','.join('%s:%s' % (debug_string(k), debug_string(x)) for k, x in v.d.items())
    return '<type ?>'


SPEC = re.compile(r'%(!\d)?([-+ #0]*)(\*|\d+)?(?:\.(\*|\d*))?([hlL]?)(.)', re.S)


def format_string(fmt, args):
    """Script::FormatString 0x004a6d80: printf over cells; ints only from int cells, doubles only from doubles,
    %s only from strings ('<invalid>'), %!N picks argument N, %a dumps any cell."""
    out = []
    pos = 0
    k = 0
    n = len(args)
    while True:
        j = fmt.find('%', pos)
        if j < 0:
            out.append(fmt[pos:])
            break
        out.append(fmt[pos:j])
        if j + 1 >= len(fmt):
            out.append('%')
            break
        if fmt[j + 1] == '%':
            out.append('%')
            pos = j + 2
            continue
        m = SPEC.match(fmt, j)
        if not m:
            out.append(fmt[j:j + 2])
            pos = j + 2
            continue
        sel, flags, width, prec, length, conv = m.groups()
        pos = m.end()

        def take():
            nonlocal k
            v = args[k] if k < n else None
            k += 1
            return v, k - 1 < n
        if width == '*':
            w, _ = take()
            width = str(ival(w))
            if width.startswith('-'):
                flags += '-'
                width = width[1:]
        if prec == '*':
            p, _ = take()
            prec = str(max(0, ival(p)))
        if sel:
            idx = int(sel[1:])
            v, present = (args[idx], True) if idx < n else (None, False)
        else:
            v, present = take()
        spec = '%' + flags + (width or '') + ('.' + prec if prec is not None else '')
        if conv in 'di':
            out.append((spec + 'd') % (v if is_int(v) else 0))
        elif conv in 'uxXo':
            x = (v if is_int(v) else 0) & (0xffff if length == 'h' else 0xffffffff)
            out.append((spec + ('d' if conv == 'u' else conv)) % x)
        elif conv in 'eEfFgG':
            out.append((spec + conv) % (v if isinstance(v, float) else 0.0))
        elif conv == 'c':
            out.append(chr((v if is_int(v) else 0) & 0xff))
        elif conv == 's':
            out.append((spec + 's') % (str(v) if isinstance(v, str) and present else '<invalid>'))
        elif conv == 'a':
            out.append(debug_string(v) if present else '[NULL]')
        else:
            out.append('%' + conv)
    return ''.join(out)


def muldiv(a, b, c):
    """64/32-bit IDIV: truncates toward zero; a quotient beyond 32 bits raises #DE in the exe."""
    if c == 0:
        return 0
    q = abs(a * b) // abs(c)
    if (a * b < 0) != (c < 0):
        q = -q
    if not -0x80000000 <= q <= 0x7fffffff:
        raise VMError('integer divide overflow (#DE): the exe would crash')
    return q


def install(vm):
    R = Rand()
    vm.rand = R
    vm.texts = {}                           # (page, id) -> text
    vm.jitter = False                       # TI waits get the exe's small random extra delay when True
    n = vm.natives

    def define(*names):
        def deco(f):
            for name in names:
                n[name] = f
            return f
        return deco

    def arr(a, k=0):
        v = arg(a, k, None)
        return v if isinstance(v, KArray) else None

    def tab(a, k=0):
        v = arg(a, k, None)
        return v if isinstance(v, KTable) else None

    # ---- arrays ---------------------------------------------------------------------------------------------
    @define('SE_ArrayAlloc')
    def _(vm, t, a):
        k = ival(arg(a, 0))
        return KArray([0] * k) if k >= 0 else None

    n['SE_ArraySize'] = lambda vm, t, a: len(arr(a).items) if arr(a) else 0

    @define('SE_ArrayAppend')
    def _(vm, t, a):
        if arr(a):
            arr(a).items.append(arg(a, 1))
        return 0

    @define('SE_ArrayRemove')
    def _(vm, t, a):
        x, i = arr(a), ival(arg(a, 1))
        if x and 0 <= i < len(x.items):
            del x.items[i]
        return 0

    @define('SE_ArrayResize')
    def _(vm, t, a):
        x, m = arr(a), ival(arg(a, 1))
        if x and m >= 0:
            del x.items[m:]
            x.items.extend([0] * (m - len(x.items)))
        return 0

    n['SE_ArrayClone'] = lambda vm, t, a: KArray(arr(a).items) if arr(a) else KArray()

    @define('SE_ArraySubClone')
    def _(vm, t, a):
        x = arr(a)
        size = len(x.items) if x else 0
        s, e = ival(arg(a, 1)), ival(arg(a, 2))
        if e < 0 or e > size:
            e = size
        if s >= e:
            return 0
        return KArray([x.items[i] if 0 <= i < size else 0 for i in range(s, e)])

    # ---- tables ---------------------------------------------------------------------------------------------
    n['SE_TableAlloc'] = lambda vm, t, a: KTable()
    n['SE_TableSize'] = lambda vm, t, a: tab(a).count if tab(a) else 0
    n['SE_TableExists'] = lambda vm, t, a: int(bool(tab(a)) and arg(a, 1) in tab(a))

    @define('SE_TableRemove')
    def _(vm, t, a):
        nd = tab(a).remove(arg(a, 1)) if tab(a) else None
        return nd[1] if nd is not None else 0

    @define('SE_TableNext')
    def _(vm, t, a):
        tb = tab(a)
        if tb is None:
            return 0
        start = len(a) < 2 or not truth(arg(a, 1))
        return tb.next_key(arg(a, 1, 0), start)

    @define('SE_TableKeys')
    def _(vm, t, a):
        tb = tab(a)
        if tb is None:
            return KArray()
        out, prev, start = [], 0, True
        for _ in range(tb.count):
            prev = tb.next_key(prev, start)
            start = False
            out.append(prev)
        return KArray(out)

    @define('SE_TableAdd')
    def _(vm, t, a):
        tb = tab(a)
        if tb is None:
            return 0
        key = arg(a, 1)
        tb.set(key, key if len(a) == 2 else arg(a, 2))
        return 1

    # ---- objects and tasks ----------------------------------------------------------------------------------
    @define('SE_ObjectExists')
    def _(vm, t, a):
        v = arg(a, 0)
        return int(is_int(v) and v != 0 and vm.exists(v))

    @define('SE_IsClass')
    def _(vm, t, a):
        o = arg(a, 0)
        if not truth(o) or not is_int(o):
            return 0
        return int(vm.exists(o) and vm.is_class(o, ival(arg(a, 1))))

    n['SE_GetTaskID'] = lambda vm, t, a: t.id

    @define('SE_KillTask')
    def _(vm, t, a):
        k = vm.tasks.get(ival(arg(a, 0)))
        if k is not None:
            k.state = 'killed'
            vm.tasks.pop(k.id, None)
        return 0

    # ---- numbers --------------------------------------------------------------------------------------------
    n['SE_Random'] = lambda vm, t, a: R.scaled(ival(arg(a, 0)))
    n['SE_MulFix'] = lambda vm, t, a: s32((ival(arg(a, 0)) * ival(arg(a, 1)) + 0x8000) >> 16)
    n['SE_DivFix'] = lambda vm, t, a: muldiv(ival(arg(a, 0)), 0x10000, ival(arg(a, 1)))
    n['SE_MulDivFix'] = lambda vm, t, a: muldiv(ival(arg(a, 0)), ival(arg(a, 1)), ival(arg(a, 2)))

    def tdiv(x, y):
        if y == 0:
            return 0
        q = abs(x) // abs(y)
        return q if (x < 0) == (y < 0) else -q

    @define('SE_LinFunc')
    def _(vm, t, a):
        x0, x1, y0, y1, x = (ival(arg(a, k)) for k in range(5))
        if x0 == x1:
            return tdiv(y0 + y1, 2)
        if x <= x0:
            return y0
        if x >= x1:
            return y1
        mid = tdiv(y0 + y1, 2)
        if x < 0x10000 and s32(x1 - x0) < 0x10000:
            return s32(mid + muldiv(y1 - y0, 2 * x - x0 - x1, 2 * (x1 - x0)))
        return s32(mid + tdiv((y1 - y0) * (x - tdiv(x1 + x0, 2)), x1 - x0))

    @define('SE_DistanceAA')
    def _(vm, t, a):
        p, q = arr(a, 0), arr(a, 1)
        if not p or not q or len(p.items) < 3 or len(q.items) < 3:
            return 0
        d = [s32(ival(p.items[k]) - ival(q.items[k])) for k in range(3)]
        import struct
        f = struct.unpack('<f', struct.pack('<f', float(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])))[0]
        return int(math.sqrt(f))

    n['SE_GetExprType'] = lambda vm, t, a: cell_type(arg(a, 0, None))

    # ---- strings --------------------------------------------------------------------------------------------
    n['SE_StringLen'] = lambda vm, t, a: len(sval(arg(a, 0)).encode('latin1', 'replace')) if sval(arg(a, 0)) is not None else 0

    @define('SE_StringToInt')
    def _(vm, t, a):
        s = sval(arg(a, 0))
        if s is None:
            return 0
        m = re.match(r'\s*([-+]?\d+)', s)
        return s32(int(m.group(1))) if m else 0

    @define('SE_StringSub')
    def _(vm, t, a):
        s = sval(arg(a, 0))
        if s is None:
            return ''
        size = len(s)
        st = ival(arg(a, 1)) & 0xffffffff
        if st >= size:
            return ''
        if len(a) < 3:
            return s[st:]
        ln = ival(arg(a, 2)) & 0xffffffff
        if ln >= size or st + ln >= size:
            return s[st:]
        if ln == 0:
            return ''
        b = s.encode('latin1', 'replace')
        e = st + ln
        while e < len(b) and b[e] & 0xc0 == 0x80:
            e += 1
        return b[st:e].decode('latin1')

    @define('SE_StringSubst')
    def _(vm, t, a):
        s, f, r = sval(arg(a, 0)), sval(arg(a, 1)), sval(arg(a, 2))
        if s is None or f is None or r is None or f not in s:
            return arg(a, 0)
        if f == '':
            raise VMError('SE_StringSubst with an empty search string loops in the exe')
        return s.replace(f, r)

    def find_from(a, finder):
        s = sval(arg(a, 0))
        if s is None:
            return -1
        start = 0
        if len(a) > 2:
            start = ival(arg(a, 2)) & 0xffffffff
            if start >= len(s):
                return -1
        return finder(s, start)

    @define('SE_StringStr')
    def _(vm, t, a):
        needle = sval(arg(a, 1))
        if needle is None:
            return -1
        return find_from(a, lambda s, st: s.find(needle, st))

    @define('SE_StringChr')
    def _(vm, t, a):
        ch = ival(arg(a, 1)) & 0xff
        return find_from(a, lambda s, st: len(s) if ch == 0 else s.find(chr(ch), st))

    n['SE_SPrintf'] = lambda vm, t, a: format_string(sval(arg(a, 0)) or '', list(a[1:]))

    @define('SE_ConvertFormatString')
    def _(vm, t, a):
        s = sval(arg(a, 0))
        if s is None:
            return 0
        if '{' not in s:
            return arg(a, 0)
        return expand_refs(vm, s)

    def text_key(a):
        if len(a) == 1:
            return (0, ival(a[0])), 'ReadText%d' % ival(a[0])
        if len(a) == 2:
            return (ival(a[0]), ival(a[1])), 'ReadText%d-%d' % (ival(a[0]), ival(a[1]))
        return (ival(a[0]), ival(a[2])), 'ReadText%d-%d-%d' % (ival(a[0]), ival(a[1]), ival(a[2]))

    @define('SE_ReadText')
    def _(vm, t, a):
        k, miss = text_key(a)
        return vm.texts.get(k, miss)

    @define('SE_ExistsText')
    def _(vm, t, a):
        k, _ = text_key(a)
        return vm.texts.get(k, 0)

    # ---- time (virtual game clock, ms) -------------------------------------------------------------------------
    def due(vm, delay):
        j = 0
        if vm.jitter and delay >= 0:
            j = R.rand() * min(delay // 16 + 1, 256) // 0x8000
        return vm.clock + max(delay, 0) + j

    @define('TI_Delay')
    def _(vm, t, a):
        d = ival(a[0]) * 1000 + ival(a[1]) if len(a) == 2 else ival(arg(a, 0))
        return WAIT(due(vm, d))

    n['TI_DelayRandom'] = lambda vm, t, a: WAIT(due(vm, ival(arg(a, 0)) + R.scaled(ival(arg(a, 1)))))
    n['TI_GetAbsTime'] = lambda vm, t, a: vm.clock
    n['TI_GetRealTime'] = lambda vm, t, a: vm.clock
    n['TI_CmpTime'] = lambda vm, t, a: s32(ival(arg(a, 0)) - ival(arg(a, 1)))
    n['TI_AddTime'] = lambda vm, t, a: s32(ival(arg(a, 0)) + ival(arg(a, 1)))

    @define('TI_WaitForInterrupt')
    def _(vm, t, a):
        t.interrupt_key = ival(arg(a, 1)) if len(a) > 1 else ival(arg(a, 0))
        d = ival(arg(a, 0)) if len(a) > 1 else -1
        return WAIT(due(vm, d) if d >= 0 else None)

    @define('TI_Interrupt')
    def _(vm, t, a):
        key = ival(arg(a, 0))
        for w in list(vm.tasks.values()):
            if w.state == 'wait' and getattr(w, 'interrupt_key', None) == key:
                w.interrupt_key = None
                left = (w.until - vm.clock) if w.until is not None else 0
                vm.ready.insert(0, w)
                w.state, w.until = 'ready', None
                w.stack.append(left)
        return 0


def install_1(vm):
    """docs/natives/spec_1.jsonl."""
    n = vm.natives
    R = vm.rand

    def s(a, k):
        return sval(arg(a, k))

    @_def(n, 'SE_ArrayCopy')
    def _(vm, t, a):
        dst, ds, src, ss, end = arg(a, 0), ival(arg(a, 1)), arg(a, 2), ival(arg(a, 3)), ival(arg(a, 4))
        ssize = len(src.items) if isinstance(src, KArray) else 0
        dsize = len(dst.items) if isinstance(dst, KArray) else 0
        if end < 0 or end > ssize:
            end = ssize
        while ds < dsize and ss < end:
            if ds >= 0:
                dst.items[ds] = src.items[ss] if 0 <= ss < ssize else 0
            ds += 1
            ss += 1
        return 0

    @_def(n, 'SE_TableClone')
    def _(vm, t, a):
        x = KTable()
        if isinstance(arg(a, 0), KTable):
            for k, v in arg(a, 0).d.items():
                x.set(k, v)
        return x

    @_def(n, 'SE_StringSplit')
    def _(vm, t, a):
        st, sep = s(a, 0), s(a, 1)
        if st is None or sep is None:
            raise VMError('SE_StringSplit of a non-string: access violation in the exe')
        if sep == '':
            raise VMError('SE_StringSplit with an empty separator never ends in the exe')
        return KArray(st.split(sep))

    @_def(n, 'SE_ArrayInsert')
    def _(vm, t, a):
        x, i = arg(a, 0), ival(arg(a, 1))
        if isinstance(x, KArray):
            if i < 0 or i == len(x.items):
                x.items.append(arg(a, 2))
            elif i < len(x.items):
                x.items.insert(i, arg(a, 2))
        return 0

    @_def(n, 'SE_ObjectFunctionExists')
    def _(vm, t, a):
        c = vm.class_of(arg(a, 0)) if vm.exists(arg(a, 0)) else None
        return int(c is not None and s(a, 1) is not None and vm.find_method(c, s(a, 1)) is not None)

    @_def(n, 'SE_TableRandomKey')
    def _(vm, t, a):
        tb = arg(a, 0)
        if not isinstance(tb, KTable) or tb.count <= 0:
            return 0
        return tb.keys()[R.scaled(tb.count)]

    @_def(n, 'SE_QSortByInt')
    def _(vm, t, a):
        x, keys, st, en = arg(a, 0), arg(a, 1), ival(arg(a, 2)), ival(arg(a, 3))
        desc = len(a) >= 5 and ival(a[4]) != 0
        if isinstance(x, KArray) and isinstance(keys, KArray) and 0 <= st < en < len(x.items) and en < len(keys.items):
            rec = sorted(((ival(keys.items[i]), x.items[i]) for i in range(st, en + 1)), key=lambda r: r[0],
                         reverse=desc)
            x.items[st:en + 1] = [v for _, v in rec]
        return 0

    @_def(n, 'SE_QSortByName')
    def _(vm, t, a):
        x, keys, st, en = arg(a, 0), arg(a, 1), ival(arg(a, 2)), ival(arg(a, 3))
        desc = len(a) >= 5 and ival(a[4]) != 0
        if isinstance(x, KArray) and isinstance(keys, KArray) and 0 <= st < en < len(x.items) and en < len(keys.items):
            rec = [((str(keys.items[i]).lower() if isinstance(keys.items[i], str) else ''), x.items[i])
                   for i in range(st, en + 1)]
            rec.sort(key=lambda r: r[0], reverse=desc)
            x.items[st:en + 1] = [v for _, v in rec]
        return 0

    n['SE_IsString'] = lambda vm, t, a: int(isinstance(arg(a, 0), str))

    @_def(n, 'SE_ArrayFind')
    def _(vm, t, a):
        from .kcvm import equals
        x = arg(a, 0)
        if not isinstance(x, KArray):
            return -1
        st = max(0, ival(arg(a, 2))) if len(a) > 2 else 0
        for k in range(st, len(x.items)):
            if equals(x.items[k], arg(a, 1)):
                return k
        return -1

    n['SE_GetClass'] = lambda vm, t, a: (vm.class_of(arg(a, 0)) or 0) if vm.exists(arg(a, 0)) else 0

    @_def(n, 'SE_ToInt')
    def _(vm, t, a):
        v = arg(a, 0)
        if isinstance(v, float):
            return int(v) if -2 ** 31 <= v < 2 ** 31 else -2 ** 31
        if isinstance(v, str):
            m = re.match(r'\s*([-+]?\d+)', v)
            return s32(int(m.group(1))) if m else 0
        return 0                            # an int argument gives 0, not itself

    @_def(n, 'SE_Distance3')
    def _(vm, t, a):
        import struct
        f = [struct.unpack('<f', struct.pack('<f', float(ival(arg(a, k)))))[0] for k in range(3)]
        q = struct.unpack('<f', struct.pack('<f', f[0] * f[0] + f[1] * f[1] + f[2] * f[2]))[0]
        return int(math.sqrt(q))

    @_def(n, 'SE_StringXMLEscape')
    def _(vm, t, a):
        if s(a, 0) is None:
            raise VMError('SE_StringXMLEscape of a non-string: crash in the exe')
        return s(a, 0).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    n['SE_StringRChr'] = lambda vm, t, a: (-1 if s(a, 0) is None else len(s(a, 0)) if ival(arg(a, 1)) & 0xff == 0
                                           else s(a, 0).rfind(chr(ival(arg(a, 1)) & 0xff)))
    n['SE_Sqrt'] = lambda vm, t, a: math.isqrt(ival(arg(a, 0))) if ival(arg(a, 0)) > 0 else 0

    @_def(n, 'SE_RandomLowNorm')
    def _(vm, t, a):
        base, cnt = ival(arg(a, 0)), ival(arg(a, 1))
        if cnt == 0:
            raise VMError('SE_RandomLowNorm(n = 0): division by zero in the exe')
        total = sum(R.scaled(2 * base) for _ in range(cnt)) if cnt > 0 else 0
        avg = total // cnt if total >= 0 else -((-total) // cnt)
        if avg < base:
            avg = 2 * base - avg - 1
        return avg - base

    @_def(n, 'SE_stricmp')
    def _(vm, t, a):
        x, y = s(a, 0), s(a, 1)
        if x is None:
            return -2
        if y is None:
            return 2
        x, y = x.lower().encode('latin1', 'replace') + b'\0', y.lower().encode('latin1', 'replace') + b'\0'
        for c1, c2 in zip(x, y):
            if c1 != c2 or c1 == 0:
                return c1 - c2
        return 0

    @_def(n, 'SE_TableNumberedKey')
    def _(vm, t, a):
        tb, k = arg(a, 0), ival(arg(a, 1))
        return tb.keys()[k] if isinstance(tb, KTable) and 0 <= k < tb.count else 0

    @_def(n, 'SE_TableNew')
    def _(vm, t, a):
        tb = arg(a, 0)
        if not isinstance(tb, KTable):
            return 0
        k = getattr(tb, 'auto', 1)
        while True:
            k = k + 1 if k < 0x7ffffffe else 1
            if k not in tb:
                break
        tb.auto = k
        tb.set(k, arg(a, 1))
        return k

    n['SE_TableData'] = lambda vm, t, a: KArray(list(arg(a, 0).d.values())) if isinstance(arg(a, 0), KTable) else KArray()

    table = _sin_table()

    def sin(x):
        x &= 0xffff
        if x <= 0x4000:
            return table[x]
        if x <= 0x8000:
            return table[0x8000 - x]
        if x <= 0xc000:
            return -table[x - 0x8000]
        return -table[0x10000 - x]
    n['SE_Sin'] = lambda vm, t, a: sin(ival(arg(a, 0)))
    n['SE_Cos'] = lambda vm, t, a: sin(ival(arg(a, 0)) + 0x4000)

    @_def(n, 'TI_WaitRandomForInterrupt')
    def _(vm, t, a):
        t.interrupt_key = ival(arg(a, 2))
        return WAIT(vm.clock + ival(arg(a, 0)) + R.scaled(ival(arg(a, 1))))


def install_2(vm):
    """docs/natives/spec_2.jsonl."""
    n = vm.natives

    def s(a, k):
        return sval(arg(a, k))

    @_def(n, 'SE_ScriptComputeExpression')
    def _(vm, t, a):
        buf, pairs = arg(a, 0), ival(arg(a, 1))
        if not isinstance(buf, KArray):
            raise VMError('SE_ScriptComputeExpression without an array: crash in the exe')
        items = buf.items
        if pairs <= 0:
            if len(items) >= 2:
                items[0], items[1] = 0, 0
            return 0
        if len(items) < 2 * pairs:
            raise VMError('SE_ScriptComputeExpression reads past the buffer: crash in the exe')
        stack = []
        FALSE, TRUE = (0, 0), (4, 1)

        def valid_obj(v):
            return is_int(v) and v != 0 and vm.exists(v)

        def truthy(e):
            tp, v = e
            if tp == 0:
                return False
            if tp == 4:
                return not (is_int(v) and v == 0) and not (isinstance(v, float) and v == 0.0)
            if tp & 0x10000:
                return valid_obj(v)
            return True
        for k in range(pairs):
            tp = ival(items[2 * k])
            v = items[2 * k + 1]
            if tp != 15:
                stack.append((tp, v))
                if len(stack) > 50:
                    raise VMError('expression stack overflow (50 entries) in the exe')
                continue
            op = ival(v)
            if op & 0x10000:
                if not stack:
                    raise VMError('unary operator on an empty expression stack')
                et, ev = stack[-1]
                if op == 0x10012:
                    stack[-1] = (4, s32(~ev) if is_int(ev) else ev) if et == 4 else FALSE
                elif op == 0x10013:
                    stack[-1] = (4, s32(-ev) if is_int(ev) else -ev if isinstance(ev, float) else ev) if et == 4 else FALSE
                elif op == 0x10014:
                    stack[-1] = FALSE if truthy(stack[-1]) else TRUE
                else:
                    stack[-1] = FALSE
                continue
            if not stack:
                continue
            B = stack.pop()
            if not stack:
                raise VMError('binary operator with one operand: the exe reads stale memory')
            A = stack[-1]
            (ta, va), (tb, vb) = A, B
            r = FALSE
            if op <= 5:
                if ta != tb:
                    ok = op == 1
                    r = TRUE if ok else FALSE
                else:
                    cmp, ordered = None, True
                    if ta == 4:
                        cmp = s32(ival(va) - ival(vb)) if is_int(va) and is_int(vb) else \
                            (0 if va == vb else 1 if va > vb else -1)
                    elif ta == 5:
                        if isinstance(va, str) and isinstance(vb, str):
                            x, y = str(va).encode('latin1', 'replace'), str(vb).encode('latin1', 'replace')
                            cmp = (x > y) - (x < y)
                        else:
                            cmp = 1
                    else:
                        cmp = s32(va - vb) if is_int(va) and is_int(vb) else 1
                        ordered = False
                    res = [cmp == 0, cmp != 0, cmp > 0, cmp < 0, cmp >= 0, cmp <= 0][op]
                    if not ordered and op >= 2:
                        res = False
                    r = TRUE if res else FALSE
            elif op in (6, 7, 8):
                if ta == 4 and tb == 4:
                    if is_int(va) and is_int(vb):
                        r = (4, s32(va & vb if op == 6 else va | vb if op == 7 else va ^ vb))
                    else:
                        r = A
            elif op == 9:
                r = TRUE if truthy(A) and truthy(B) else FALSE
            elif op == 10:
                r = TRUE if truthy(A) or truthy(B) else FALSE
            elif op == 0xb:
                if ta == 4 and tb == 4:
                    r = (4, vm.binop(0x46, va, vb))
                else:
                    sv = vm.call(702, '__scriptOpAddString', [ta, va, tb, vb])
                    r = (5, sv) if isinstance(sv, str) else FALSE
            elif op in (0xc, 0xd):
                if ta == 4 and tb == 4:
                    r = (4, vm.binop(0x4b if op == 0xc else 0x50, va, vb))
            elif op in (0xe, 0xf):
                if ta == 4 and tb == 4 and ival(vb) != 0:
                    r = (4, vm.binop(0x51 if op == 0xe else 0x52, va, vb))
            stack[-1] = r
        if len(stack) == 1:
            items[0], items[1] = stack[0][0], stack[0][1]
        else:
            items[0], items[1] = 0, 0
        return 0

    @_def(n, 'SE_ConvertTextString')
    def _(vm, t, a):
        x = s(a, 0)
        if x is None:
            raise VMError('SE_ConvertTextString of a non-string: strlen(NULL) crashes the exe')
        return convert_text_string(x)

    @_def(n, 'SE_TableClear')
    def _(vm, t, a):
        if isinstance(arg(a, 0), KTable):
            arg(a, 0).__init__()
        return 0

    n['SE_TaskExists'] = lambda vm, t, a: int(ival(arg(a, 0)) in vm.tasks)

    @_def(n, 'SE_GetSuperClass')
    def _(vm, t, a):
        o = arg(a, 0)
        c = vm.class_of(o) if vm.exists(o) else None
        if c is None:
            return 0
        p = vm.cls[c].parent if c in vm.cls else -1
        return 0 if p in (-1, 0xffffffff) else p

    @_def(n, 'SE_StringNumChars')
    def _(vm, t, a):
        x = s(a, 0)
        if x is None:
            return 0
        b = x.encode('latin1', 'replace')
        cnt, i = 0, 0
        while i < len(b):
            if b[i] == 0x1b:
                i += 2
                continue
            if b[i] & 0xc0 != 0x80:
                cnt += 1
            i += 1
        return cnt

    @_def(n, 'SE_StringNextCharPos')
    def _(vm, t, a):
        x = s(a, 0)
        if x is None:
            return 0
        b = x.encode('latin1', 'replace')
        p = ival(arg(a, 1)) & 0xffffffff
        if p >= len(b):
            return len(b)
        j = p + 1
        while j < len(b) and b[j] & 0xc0 == 0x80:
            j += 1
        return j

    @_def(n, 'SE_StringPrevCharPos')
    def _(vm, t, a):
        x = s(a, 0)
        if x is None:
            return 0
        b = x.encode('latin1', 'replace')
        p = ival(arg(a, 1)) & 0xffffffff
        if p == 0:
            return 0
        i = min(p, len(b))
        while True:
            i -= 1
            if i <= 0:
                return 0
            if b[i] & 0xc0 != 0x80:
                return i

    n['SE_GetLanguage'] = lambda vm, t, a: getattr(vm, 'language', 44)

    @_def(n, 'SE_KGV')
    def _(vm, t, a):
        x, y = ival(arg(a, 0)), ival(arg(a, 1))
        if x <= 0 or y <= 0:
            raise Hang('SE_KGV with a zero or negative input never returns in the exe')
        g, h = x, y
        while g != h:
            if g > h:
                g -= h
            else:
                h -= g
        return s32((x // h) * y)

    @_def(n, 'SE_TableFind')
    def _(vm, t, a):
        tb = arg(a, 0)
        return tb.get(arg(a, 1), 0) if isinstance(tb, KTable) else 0

    n['SE_Time'] = lambda vm, t, a: getattr(vm, 'wall_time', 1700000000)

    @_def(n, 'SE_DistanceA3')
    def _(vm, t, a):
        p = arg(a, 0)
        if not isinstance(p, KArray) or len(p.items) < 3:
            return 0
        import struct
        f32 = lambda v: struct.unpack('<f', struct.pack('<f', float(v)))[0]
        d = [f32(s32(ival(p.items[k]) - ival(arg(a, k + 1)))) for k in range(3)]
        return int(math.sqrt(f32(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])))

    @_def(n, 'SE_IsPosInRangeA')
    def _(vm, t, a):
        p, sp = arg(a, 0), arg(a, 1)
        if not isinstance(p, KArray) or len(p.items) < 3:
            return 0
        cnt = len(sp.items) if isinstance(sp, KArray) else 0
        mx = ival(arg(a, 2)) if len(a) > 2 else -1
        if mx >= 0 and 2 * mx < cnt:
            cnt = 2 * mx
        if cnt % 2:
            raise VMError('SE_IsPosInRangeA with an odd-length array: NULL dereference in the exe')
        for i in range(0, cnt, 2):
            c = sp.items[i]
            if not isinstance(c, KArray) or len(c.items) < 3:
                continue
            dd = sum((ival(p.items[k]) - ival(c.items[k])) ** 2 for k in range(3))
            import struct
            dist = int(math.sqrt(struct.unpack('<f', struct.pack('<f', float(dd)))[0]))
            if dist <= ival(sp.items[i + 1]):
                return 1
        return 0

    n['SE_CallStackDump'] = lambda vm, t, a: 0


def convert_text_string(x):
    """Script::UnescapeTextPageString on a copy, as SE_ConvertTextString uses it: escapes \\n \\b \\t \\r,
    octal \\NNN, \\c -> c, a raw LF -> space, unescaped (...) dropped. The function writes no terminator, but the
    handler passes the new length on (ResumeTaskWithString -> SetCellStringDup -> StrNDupReplace copies
    min(length, strlen) bytes), so the result has no tail of the input; it is cut at the first NUL (octal 000)."""
    b = x.encode('latin1', 'replace')
    out = bytearray()
    i, skip = 0, False
    while i < len(b):
        c = b[i]
        if c == 0x5c:                       # backslash
            if i + 1 >= len(b):
                ch, i = 0, i + 1
            else:
                d = b[i + 1]
                if d in b'nbtr':
                    ch = {ord('n'): 10, ord('b'): 8, ord('t'): 9, ord('r'): 13}[d]
                    i += 2
                elif 0x30 <= d <= 0x37:
                    v, j = 0, i + 1
                    while j < len(b) and 0x30 <= b[j] <= 0x37:
                        v = (v * 8 + b[j] - 0x30) & 0xff
                        j += 1
                    ch, i = v, j
                else:
                    ch, i = d, i + 2
            if not skip:
                out.append(ch)
            continue
        i += 1
        if c == 0x28:
            skip = True
        elif c == 0x29:
            skip = False
        elif not skip:
            out.append(0x20 if c == 0x0a else c)
    res = bytes(out)
    z = res.find(b'\0')
    return (res[:z] if z >= 0 else res).decode('latin1')


def _def(table, name):
    def deco(f):
        table[name] = f
        return f
    return deco


def _sin_table():
    import struct
    step = struct.unpack('<f', struct.pack('<f', 2 * math.pi / 65536))[0]
    return [int(math.sin(k * step) * 65536.0) for k in range(0x4001)]


def install_pending(vm):
    """Natives whose spec is not read yet (docs/natives/spec_1, spec_2): plausible behaviour, to be replaced."""
    n = vm.natives
    R = vm.rand

    def setdef(name, f):
        n.setdefault(name, f)

    def s(a, k):
        return sval(arg(a, k)) or ''
    setdef('SE_ConvertTextString', lambda vm, t, a: re.sub(r'\\(.)', r'\1', s(a, 0)) if isinstance(arg(a, 0), str) else 0)
    setdef('SE_StringSplit',lambda vm, t, a: KArray(s(a, 0).split(s(a, 1)) if s(a, 1) else [s(a, 0)]))
    setdef('SE_IsString', lambda vm, t, a: int(isinstance(arg(a, 0), str)))
    setdef('SE_ToInt', lambda vm, t, a: ival(arg(a, 0)) if not isinstance(arg(a, 0), float) else s32(int(arg(a, 0))))
    setdef('SE_GetClass', lambda vm, t, a: vm.class_of(arg(a, 0)) or 0)
    setdef('SE_GetSuperClass', lambda vm, t, a: vm.cls[ival(arg(a, 0))].parent if ival(arg(a, 0)) in vm.cls else 0)
    setdef('SE_TaskExists', lambda vm, t, a: int(ival(arg(a, 0)) in vm.tasks))
    setdef('SE_TableNew', lambda vm, t, a: KTable())
    setdef('SE_TableClone', lambda vm, t, a: KTable(arg(a, 0).d) if isinstance(arg(a, 0), KTable) else 0)
    setdef('SE_StringRChr', lambda vm, t, a: s(a, 0).rfind(chr(ival(arg(a, 1)) & 0xff)))
    setdef('SE_stricmp', lambda vm, t, a: (s(a, 0).lower() > s(a, 1).lower()) - (s(a, 0).lower() < s(a, 1).lower()))
    setdef('SE_Sqrt', lambda vm, t, a: int(math.sqrt(max(0, ival(arg(a, 0))))))

    def clear(vm, t, a):
        if isinstance(arg(a, 0), KTable):
            arg(a, 0).__init__()
        return 0
    setdef('SE_TableClear', clear)

    def rkey(vm, t, a):
        tb = arg(a, 0)
        if not isinstance(tb, KTable) or not tb.count:
            return 0
        return tb.keys()[R.scaled(tb.count)]
    setdef('SE_TableRandomKey', rkey)

    def insert(vm, t, a):
        x, i = arg(a, 0), ival(arg(a, 1))
        if isinstance(x, KArray) and 0 <= i <= len(x.items):
            x.items.insert(i, arg(a, 2))
        return 0
    setdef('SE_ArrayInsert', insert)

    def find(vm, t, a):
        from .kcvm import equals
        x = arg(a, 0)
        if isinstance(x, KArray):
            for k, v in enumerate(x.items):
                if equals(v, arg(a, 1)):
                    return k
        return -1
    setdef('SE_ArrayFind', find)

    def copy(vm, t, a):
        dst, dp, src, sp, c = arg(a, 0), ival(arg(a, 1)), arg(a, 2), ival(arg(a, 3)), ival(arg(a, 4))
        if isinstance(dst, KArray) and isinstance(src, KArray):
            for k in range(c):
                if 0 <= sp + k < len(src.items) and 0 <= dp + k < len(dst.items):
                    dst.items[dp + k] = src.items[sp + k]
        return 0
    setdef('SE_ArrayCopy', copy)

    def fexists(vm, t, a):
        c = vm.class_of(arg(a, 0))
        return int(c is not None and isinstance(arg(a, 1), str) and vm.find_method(c, str(arg(a, 1))) is not None)
    setdef('SE_ObjectFunctionExists', fexists)


def cell_type(v):
    """The exe's cell type byte (SE_GetExprType)."""
    if v is None:
        return 0
    if isinstance(v, bool) or isinstance(v, int):
        return 1
    if isinstance(v, PStr):
        return 2
    if isinstance(v, str):
        return 0xb
    if isinstance(v, float):
        return 0xe
    if isinstance(v, KArray):
        return 8
    if isinstance(v, KTable):
        return 9
    return 0xf


def expand_refs(vm, s):
    """Gui::ExpandTextReferences: {page,id} -> the text, recursively; other braces stay."""
    def repl(m):
        page, tid = int(m.group(1)), int(m.group(2))
        if page == 0:
            return m.group(0)
        txt = vm.texts.get((page, tid))
        return expand_refs(vm, txt) if txt is not None else m.group(0)
    return re.sub(r'\{(\d+),\s*(\d+)\}', repl, s)
