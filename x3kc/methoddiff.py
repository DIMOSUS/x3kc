"""Methods whose code differs between two builds of an object (instruction keys, so pure moves do not count).

    x3kc methoddiff <old.obj> <new.obj>
"""
import sys

from . import kcobj
from . import kcsave


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = kcobj.load(sys.argv[1]), kcobj.load(sys.argv[2])
    sa, sb = kcsave.method_spans(a), kcsave.method_spans(b)
    na = {e: k for k, (e, _) in sa.items()}
    nb = {e: k for k, (e, _) in sb.items()}
    changed = []
    for k in sorted(set(sa) | set(sb), key=str):
        if k not in sa or k not in sb:
            changed.append((k, 'only in %s' % ('new' if k in sb else 'old')))
            continue
        ka = [x for _, x in kcsave.insn_keys(a, *sa[k], na)]
        kb = [x for _, x in kcsave.insn_keys(b, *sb[k], nb)]
        if ka != kb:
            changed.append((k, '%d -> %d instructions' % (len(ka), len(kb))))
    for (cid, name), why in changed:
        print('%5d::%-40s %s' % (cid, name, why))
    print(len(changed), 'methods differ')


if __name__ == '__main__':
    main()
