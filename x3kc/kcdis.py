"""Plain disassembly listing of a KC object (for study; the round-trip form is `kcd --asm-all`).

    x3kc dis <obj> [method-substring | 0xOFFSET] [-n N]
"""
import argparse
import struct
import sys

from . import kcisa
from . import kcobj


def method_table(o):
    """{entry: (class id, row)}, class 0 = SYMB."""
    m = {}
    for r in o.symb:
        m[r[0]] = (0, r)
    for c in o.classes:
        for r in c.methods:
            m[r[0]] = (c.id, r)
    return m


def fmt_insn(o, i, meth):
    mn = kcisa.MN.get(i.op, 'op%02x' % i.op)
    f = kcisa.FMT.get(i.op)
    if f in ('b', 'h', 'H', 'i'):
        a = str(i.arg)
    elif f == 's':
        a = repr(o.string(i.arg))
    elif f == 'n':
        a = o.string(i.arg) or '?%x' % i.arg
    elif f == 't':
        a = 'L%06x' % i.arg
    elif f == 'c':
        cid, r = meth.get(i.arg, (None, None))
        a = '%s::%s' % (cid, o.string(r[1])) if r else '%06x' % i.arg
    elif f == 'd':
        a = repr(struct.unpack('>d', i.arg)[0])
    elif i.op == 0x78:
        base, d, tg = i.cases
        a = 'base=%d default=L%06x [%s]' % (base, d, ', '.join('L%06x' % t for t in tg))
    elif i.op == 0x79:
        d, ints, strs = i.cases
        a = 'default=L%06x ints[%s] strs[%s]' % (d, ', '.join('%d:L%06x' % kt for kt in ints),
                                                 ', '.join('%r:L%06x' % (o.string(k), t) for k, t in strs))
    else:
        a = ''
    return '%06x  %-8s %s' % (i.pc, mn, a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('obj')
    ap.add_argument('what', nargs='?')
    ap.add_argument('-n', type=int, default=1)
    a = ap.parse_args()
    o = kcobj.load(a.obj)
    meth = method_table(o)
    ents = sorted(meth)
    sel = []
    if a.what and a.what.startswith('0x'):
        pc = int(a.what, 16)
        first = [e for e in ents if e <= pc]
        if not first:
            raise SystemExit('%06x lies before the first method (%06x)' % (pc, ents[0]))
        k = ents.index(max(first))
        sel = ents[k:k + a.n]
    elif a.what:
        sel = [e for e in ents if a.what in '%d::%s' % (meth[e][0], o.string(meth[e][1][1]))][:a.n]
    else:
        sel = ents
    for e in sel:
        cid, r = meth[e]
        k = ents.index(e)
        end = ents[k + 1] if k + 1 < len(ents) else len(o.code)
        print('; %d::%s  locals=%d argc=%d  [%06x..%06x)' % (cid, o.string(r[1]), r[2], r[3], e, end))
        for i in kcisa.walk(o.code, e, end):
            print(fmt_insn(o, i, meth))
        print()


if __name__ == '__main__':
    sys.exit(main())
