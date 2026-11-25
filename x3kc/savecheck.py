"""Which return sites of a base object a rebuilt source breaks, and why.

    x3kc savecheck <source.kc> <base.obj> [-D NAME[=V] ...] [--target T]

For every WRONG site: the method, the base offset, and the reason — the call after the mapped address differs
(`call`), or the method's frame size changed (`frame old -> new`). Only sites where a task can be suspended are
checked (kcsave.waiting_sites): calls that always come back at once are never in a savegame. --target: the exe whose
natives decide that (ap, fl, x3_src-ap, x3_src-fl; default ap; x3kc/targets.py).
"""
import sys
from collections import defaultdict

from . import kcc
from . import kcisa
from . import kcobj
from . import kcsave


def main():
    from . import kcpre
    from . import targets
    args, defs, target = [], [], None
    it = iter(sys.argv[1:])
    for x in it:
        if x == '--target' or x.startswith('--target='):
            target = x.split('=', 1)[1] if '=' in x else next(it, None)
            if target not in targets.TARGETS:
                print('error: --target is one of %s' % ', '.join(targets.TARGETS))
                return 2
        elif x == '-D':
            n = next(it, None)
            if n is None:
                print('error: -D needs a name (-D NAME or -D NAME=value)')
                print(__doc__)
                return 2
            defs.append(n)
        elif x.startswith('-D') and len(x) > 2:
            defs.append(x[2:])
        else:
            args.append(x)
    if len(args) != 2:
        print(__doc__)
        return 2
    try:
        defines = kcpre.parse_defines(defs)
    except kcpre.PreError as x:
        print('error:', x)
        return 2
    o, _, fns = kcc.compile_source(args[0], defines=defines, target=target)
    base = kcobj.load(args[1])
    foreach = {(f.cls, f.name): f.foreach_lines for f in fns if getattr(f, 'foreach_lines', None)}
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
    live = kcsave.waiting_sites(base, target=target)
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
            fl = foreach.get(tuple(meth))
            out[meth].append('%06x frame %s -> %s%s' % (i.end, fb, fn, (
                ' (the foreach at line %s keeps hidden locals - container, size, index - in the frame: write that '
                'loop with existing locals or move it into a method that does not wait)'
                % ', '.join(map(str, fl))) if fl else ''))
    for m, lines in sorted(out.items()):
        print('%s::%s  %d sites' % (m[0], m[1], len(lines)))
        for l in lines[:4]:
            print('    ' + l)


if __name__ == '__main__':
    main()
