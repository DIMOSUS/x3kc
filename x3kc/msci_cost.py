"""Cost of MSCI script lines in KC instructions, measured on the emulator through the game's own __runScript.

    x3kc msci cost <x3story.obj> <msci_db.json> [line ...] [--pre LINE ...] [--reps N]

Every line runs N times inside a counted MSCI loop; the empty loop is measured too and subtracted, so the number is
what the line adds: the interpreter's per-line overhead (fetch, checks, result store) plus the command body.
Natives called per repetition are listed (their own cost is in the exe, not counted here). Without lines, a set
of common commands is measured. Variables $a = 2, $b = 3 and $arr (50 elements) exist before the loop.
"""
import argparse
from collections import Counter

from . import kcmsci
from . import kcobj
from . import kcvm

PRE = ['$a = 2', '$b = 3', '$arr = array alloc: size=50']
COMMON = [
    '$c = $a',
    '$c = $a + 1',
    '$c = $a + $b * 2',
    '$c = ( $a + 1 ) * ( $b - 2 ) / 3',
    '$c = $a == 2 AND $b < 5',
    "$c = 'abc' + $a",
    '$c = $arr[3]',
    '$arr[3] = $a',
    '$n = size of array $arr',
    '$f = find $a in array: $arr',
    '$x = [THIS] -> exists',
    "$t = sprintf: fmt='%s %s', $a, $b, null, null, null",
    '$t = read text: page=17 id=1',
    '$t = convert number $a to string',
    '$r = random value from 0 to 10 - 1',
    'inc $c =',
]


class Meter:
    def __init__(self, obj, db):
        self.vm = kcvm.VM(kcobj.load(obj))
        self.vm.budget = 50_000_000
        self.m = kcmsci.Msci(self.vm, db)
        self.ops = 0
        self.natives = Counter()

    def trace(self, t, i):
        self.ops += 1
        if i.op == 0x82:
            self.natives[self.vm.pool_string(i.arg)] += 1

    def run(self, pre, body, reps):
        src = pre + ['$i = 0', 'while $i < %d' % reps] + [' ' + l for l in body] + [' inc $i =', 'end',
                                                                                     'return $i']
        self.ops, self.natives = 0, Counter()
        self.vm.trace = self.trace
        try:
            self.m.run_text(src)
        finally:
            self.vm.trace = None
        return self.ops, self.natives

    def cost(self, line, pre=PRE, reps=100):
        """(KC instructions added per repetition, {native: calls per repetition}, empty loop per iteration)."""
        b, bn = self.run(pre, [], reps)
        t, tn = self.run(pre, [line], reps)
        nat = {k: (v - bn.get(k, 0)) / reps for k, v in tn.items() if v != bn.get(k, 0)}
        return (t - b) / reps, nat, b / reps


def main():
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument('obj')
    ap.add_argument('db')
    ap.add_argument('lines', nargs='*')
    ap.add_argument('--pre', nargs='*', default=PRE)
    ap.add_argument('--reps', type=int, default=100)
    a = ap.parse_args()
    M = Meter(a.obj, json.load(open(a.db, encoding='utf-8')))
    first = True
    for line in a.lines or COMMON:
        try:
            c, nat, loop = M.cost(line, a.pre, a.reps)
        except Exception as x:
            print('%-55s FAILED %s' % (line, str(x)[:120]))
            continue
        if first:
            print('empty loop iteration (while + inc + end): %.1f instructions' % loop)
            first = False
        print('%-55s %7.1f  %s' % (line, c, ' '.join('%s x%g' % kv for kv in sorted(nat.items()))))


if __name__ == '__main__':
    main()
