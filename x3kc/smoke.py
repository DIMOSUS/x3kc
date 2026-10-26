"""Run every method of two builds of an object in the emulator and compare what they do.

    x3kc smoke <old.obj> <new.obj> [--budget N] [-v]

Each method runs on a fresh VM state: an instance of its class (class 0: a global function), all arguments 0,
until it returns or waits (no virtual time passes). The outcome is the result, the VM error if any (a hang, an
unknown opcode, a stack fault, a crash the exe would have), and the sequence of natives and methods it called.
Methods whose outcome differs are listed; methods whose code differs (methoddiff) are expected among them.
"""
import argparse
from collections import Counter

from . import kcobj
from . import kcsave
from . import kcvm


def describe(v, depth=0):
    if isinstance(v, kcvm.KArray):
        return 'A%d' % len(v.items) if depth else 'A[%s]' % ','.join(describe(x, 1) for x in v.items[:6])
    if isinstance(v, kcvm.KTable):
        return 'T%d' % v.count
    if isinstance(v, int) and v < -1:
        return 'obj'                        # object ids depend on allocation order
    return repr(v)


def outcome(vm, cid, name, nparams):
    vm.reset()
    obj = vm.new(cid) if cid else 0
    err = None
    res = None
    try:
        r = vm.call(obj, name, [0] * nparams)
        res = 'wait' if isinstance(r, kcvm.Task) else describe(r)
    except kcvm.Hang:
        err = 'hang'
    except kcvm.VMError as x:
        err = 'vm: ' + str(x).split(' at ')[0][:80]
    except Exception as x:
        err = 'emulator: %s' % type(x).__name__
    calls = tuple(n for n, _ in vm.calls[:40])
    methods = tuple(n for _, n, _, _ in vm.method_log[:40])
    return res, err, calls, methods


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('old')
    ap.add_argument('new')
    ap.add_argument('--budget', type=int, default=20000)
    ap.add_argument('-v', action='store_true')
    a = ap.parse_args()
    oa, ob = kcobj.load(a.old), kcobj.load(a.new)
    va, vb = kcvm.VM(oa), kcvm.VM(ob)
    va.budget = vb.budget = a.budget
    rows = [(0, oa.string(r[1]), r[3]) for r in oa.symb] + [(c.id, oa.string(r[1]), r[3]) for c in oa.classes
                                                              for r in c.methods]
    changed = set()
    sa, sb = kcsave.method_spans(oa), kcsave.method_spans(ob)
    na = {e: k for k, (e, _) in sa.items()}
    nb = {e: k for k, (e, _) in sb.items()}
    for k in sa:
        if k in sb and [x for _, x in kcsave.insn_keys(oa, *sa[k], na)] != [x for _, x in kcsave.insn_keys(ob, *sb[k], nb)]:
            changed.add(k)
    stats = Counter()
    diffs = []
    errors = Counter()
    for cid, name, np in rows:
        x = outcome(va, cid, name, np)
        y = outcome(vb, cid, name, np)
        stats['same' if x == y else 'different'] += 1
        if x[1]:
            errors[x[1].split(':')[0] if x[1].startswith('emulator') else x[1]] += 1
        if x != y:
            diffs.append(((cid, name), (cid, name) in changed, x, y))
    print('methods run: %d (same %d, different %d)' % (len(rows), stats['same'], stats['different']))
    print('outcomes of the old build: %d returned or waited, %d stopped with an error' %
          (len(rows) - sum(errors.values()), sum(errors.values())))
    for e, n in errors.most_common(12):
        print('   %5d  %s' % (n, e))
    unexpected = [d for d in diffs if not d[1]]
    print('different outcome in a changed method: %d; in an unchanged method: %d' %
          (len(diffs) - len(unexpected), len(unexpected)))
    for (cid, name), ch, x, y in diffs if a.v else unexpected[:30]:
        print('  %s %d::%s\n     old %s\n     new %s' % ('changed' if ch else 'UNCHANGED', cid, name, x[:2], y[:2]))
        if x[2] != y[2] or x[3] != y[3]:
            print('     calls differ: old %s\n                   new %s' % (x[3][:12], y[3][:12]))


if __name__ == '__main__':
    main()
