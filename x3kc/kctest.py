"""Round-trip test at AST level: for every method, bytecode -> AST (kcdec) -> bytecode (kcgen) must be identical.

    x3kc test <obj> [-v N] [--only 0xADDR]
"""
import argparse
import sys
import traceback
from collections import Counter

from . import kcdec
from . import kcgen
from . import kcobj


def method_rows(o):
    rows = []
    for r in o.symb:
        rows.append((r[0], 0, r))
    for c in o.classes:
        for r in c.methods:
            rows.append((r[0], c.id, r))
    rows.sort()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('obj')
    ap.add_argument('-v', type=int, default=0, help='show N failures')
    ap.add_argument('--only')
    ap.add_argument('--cat', default='', help='show only failures whose message contains this')
    ap.add_argument('--dis', action='store_true', help='disassemble shown failures')
    a = ap.parse_args()
    o = kcobj.load(a.obj)
    rows = method_rows(o)
    by_addr = {e: (cid, o.string(r[1])) for e, cid, r in rows}
    env = kcgen.Env(kcgen.Pool(o.strings), {v: k for k, v in by_addr.items()})
    stats = Counter()
    frames = Counter()
    fails = []
    for k, (e, cid, r) in enumerate(rows):
        if a.only and e != int(a.only, 16):
            continue
        end = rows[k + 1][0] if k + 1 < len(rows) else len(o.code)
        orig = o.code[e:end]
        try:
            fn = kcdec.decompile(o.code, e, end, r[3], by_addr, o.string)
        except kcdec.DecompileError as x:
            stats['dec: ' + str(x).split(' at ')[0]] += 1
            fails.append((e, cid, r, 'dec', str(x)))
            continue
        except Exception as x:
            stats['dec crash ' + type(x).__name__] += 1
            fails.append((e, cid, r, 'crash', traceback.format_exc()))
            continue
        try:
            g = kcgen.FuncGen(fn, e, env).function()
            b = g.resolve()
        except Exception as x:
            stats['gen: ' + str(x)[:60]] += 1
            fails.append((e, cid, r, 'gen', traceback.format_exc()))
            continue
        frames[g.computed_frame() - fn.frame] += 1
        if b == orig:
            stats['ok'] += 1
        else:
            n = next((j for j in range(min(len(b), len(orig))) if b[j] != orig[j]), min(len(b), len(orig)))
            stats['mismatch'] += 1
            fails.append((e, cid, r, 'mismatch', 'first diff at +%x (%06x) len %d vs %d' % (n, e + n, len(b), len(orig))))
    for k, v in stats.most_common():
        print('%6d  %s' % (v, k))
    print('computed frame - original:', sorted(frames.items()))
    shown = [f for f in fails if a.cat in f[4]]
    shown.sort(key=lambda f: len(o.code) if f[0] == rows[-1][0] else next(x[0] for x in rows if x[0] > f[0]) - f[0])
    from . import kcdis
    for f in shown[:a.v]:
        e, cid, r, kind, msg = f
        print('--- %06x %d::%s %s: %s' % (e, cid, o.string(r[1]), kind, msg))
        if a.dis:
            meth = kcdis.method_table(o)
            end = next((x[0] for x in rows if x[0] > e), len(o.code))
            from . import kcisa
            for i in kcisa.walk(o.code, e, end):
                print(kcdis.fmt_insn(o, i, meth))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
