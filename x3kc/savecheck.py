"""Which return sites of a base object a rebuilt source breaks, and why.

    x3kc savecheck <source.kc> <base.obj>

For every WRONG site: the method, the base offset, and the reason — the call after the mapped address differs
(`call`), or the method's frame size changed (`frame old -> new`). Only sites where a task can be suspended are
checked (kcsave.waiting_sites): calls that always come back at once are never in a savegame.
"""
import sys
from collections import defaultdict

from . import kcc
from . import kcisa
from . import kcobj
from . import kcsave


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    o, _, _ = kcc.compile_source(sys.argv[1])
    base = kcobj.load(sys.argv[2])
    segs = kcsave.code_map(base, o)
    bn = {e: k for k, (e, _) in kcsave.method_spans(base).items()}
    nn = {e: k for k, (e, _) in kcsave.method_spans(o).items()}
    bfr, nfr = kcsave.frames(base), kcsave.frames(o)
    nkeys = {}
    for i in kcisa.walk(o.code, 1):
        nkeys[i.end] = (i.op, o.string(i.arg) if i.op in (0x82, 0x85, 0x87) else
                        nn.get(i.arg, i.arg) if kcisa.FMT.get(i.op) == 'c' else None)
    spans = sorted((e, k) for k, (e, _) in kcsave.method_spans(base).items())
    import bisect
    starts = [e for e, _ in spans]
    out = defaultdict(list)
    live = kcsave.waiting_sites(base)
    for i in kcisa.walk(base.code, 1):
        if i.op not in kcsave.CALLS or i.end not in live:
            continue
        d = kcsave.lookup(segs, i.end)
        if d is None:
            meth = spans[bisect.bisect_right(starts, i.pc) - 1][1]
            out[meth].append('%06x gone (%s %s)' % (i.end, kcisa.MN[i.op], base.string(i.arg) if i.op in (
                0x82, 0x85, 0x87) else bn.get(i.arg, '')))
            continue
        want = (i.op, base.string(i.arg) if i.op in (0x82, 0x85, 0x87) else
                bn.get(i.arg, i.arg) if kcisa.FMT.get(i.op) == 'c' else None)
        got = nkeys.get(i.end + d)
        fb, fn = kcsave.frame_at(bfr, i.end - 1), kcsave.frame_at(nfr, i.end + d - 1)
        meth = spans[bisect.bisect_right(starts, i.pc) - 1][1]
        if got != want:
            out[meth].append('%06x call %s -> %s' % (i.end, want, got))
        elif fb != fn:
            out[meth].append('%06x frame %s -> %s' % (i.end, fb, fn))
    for m, lines in sorted(out.items()):
        print('%s::%s  %d sites' % (m[0], m[1], len(lines)))
        for l in lines[:4]:
            print('    ' + l)


if __name__ == '__main__':
    main()
