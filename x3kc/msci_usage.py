"""Which MSCI commands scripts use, statically and inside loops (backward gotos).

    x3kc msci usage <msci_db.json> <dir of *.xml> [-n N] [--json out.json]

Loops are the ranges of backward `goto` (100 / hidden 112, which `while` and `end` compile to); a command inside k
nested loops weighs 10^k (capped at 3). Also lists loops with no command that can suspend the task (wait, an
interruptible command, call script): such a loop runs to the end within one frame.
"""
import argparse
import glob
import json
import os
from collections import Counter

from . import msci

WAITS = {105, 107}


def scan(db, files):
    flags = {int(k): v[1] for k, v in db['cmds'].items()}
    static, inloop = Counter(), Counter()
    nloops, busy, nscripts = 0, [], 0
    for p in files:
        code, _ = msci.read_script(p)
        if not code or not isinstance(code[6], list):
            continue
        nscripts += 1
        lines = code[6]
        ids = [ln[0] if isinstance(ln, list) and ln else 0 for ln in lines]
        static.update(ids)
        depth = [0] * len(lines)
        for k, ln in enumerate(lines):
            if ids[k] in (100, 112) and isinstance(ln[1], int) and ln[1] <= k:
                nloops += 1
                body = range(ln[1], k + 1)
                for j in body:
                    depth[j] += 1
                if not any(ids[j] in WAITS or flags.get(ids[j], 0) & 0x10000 or ids[j] == 102 for j in body):
                    busy.append((os.path.basename(p), ln[1], k))
        for k, c in enumerate(ids):
            if depth[k]:
                inloop[c] += 10 ** min(depth[k], 3)
    return nscripts, static, inloop, nloops, busy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('dir')
    ap.add_argument('-n', type=int, default=30)
    ap.add_argument('--json')
    a = ap.parse_args()
    db = json.load(open(a.db, encoding='utf-8'))
    text = {int(k): v for k, v in db['text'].get('2003', {}).items()}
    ns, static, inloop, nloops, busy = scan(db, sorted(glob.glob(os.path.join(a.dir, '*.xml'))))
    print('%d scripts, %d lines, %d loops, %d of them with no command that can suspend' % (
        ns, sum(static.values()), nloops, len(busy)))
    for title, c in (('by static count', static), ('inside loops (weight 10^depth)', inloop)):
        print('\n%s:' % title)
        for cid, n in c.most_common(a.n):
            print('%9d  %5d  %s' % (n, cid, (text.get(cid) or '?')[:70]))
    if a.json:
        with open(a.json, 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'static': static, 'inloop': inloop, 'busy_loops': busy}, f)


if __name__ == '__main__':
    main()
