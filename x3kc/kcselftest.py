"""Full round trip: obj -> source project (kcd) -> obj (kcc), byte for byte, for every object given.

    x3kc selftest <obj>... [--work DIR]
"""
import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the directory holding the x3kc package


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('objs', nargs='+')
    ap.add_argument('--work')
    a = ap.parse_args()
    work = a.work or tempfile.mkdtemp(prefix='kcrt')
    bad = 0
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get('PYTHONPATH', ''))
    for k, obj in enumerate(a.objs):
        name = os.path.splitext(os.path.basename(obj))[0]
        d = os.path.join(work, '%02d_%s' % (k, name))
        r1 = subprocess.run([sys.executable, '-m', 'x3kc', 'decompile', obj, d], capture_output=True, text=True,
                            env=env)
        r2 = subprocess.run([sys.executable, '-m', 'x3kc', 'compile', os.path.join(d, name + '.kc'),
                             '-o', os.path.join(d, name + '.out.obj'), '--check', obj], capture_output=True, text=True, env=env)
        ok = r2.returncode == 0
        bad += not ok
        stats = ' '.join(r1.stdout.split())
        print('%-4s %s  [%s]' % ('OK' if ok else 'FAIL', obj, stats))
        if not ok:
            print(r1.stderr[-2000:] + r2.stdout[-2000:] + r2.stderr[-2000:])
    print('work dir:', work)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
