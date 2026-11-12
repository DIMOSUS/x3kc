"""Game directory access: the current KC objects and everything the MSCI tools need, read the way the game reads it.

    x3kc extract <game dir> <out dir> [--addon addon2]          the KC objects the game loads (l/*.obj) -> out dir
    x3kc msci-setup <game dir> <work dir> [--addon addon2]      texts, ware names, command database, scripts

Albion Prelude (a game dir with addon/NN.cat) loads addon catalogues, Terran Conflict the ones in the game dir;
Farnham's Legacy (X3FL.exe in the Terran Conflict dir) loads addon2/ instead of addon/ (`--addon addon2`).
A later catalogue overrides an earlier one, loose files override catalogues.
"""
import argparse
import glob
import os
import sys

from . import catx
from . import unpck


def catalogues(game, addon='addon'):
    cats = sorted(glob.glob(os.path.join(game, addon, '[0-9][0-9].cat')))
    if cats:
        return cats, os.path.join(game, addon)
    return sorted(glob.glob(os.path.join(game, '[0-9][0-9].cat'))), game


def find_objects(game, addon='addon'):
    """{name: (source description, bytes)} of every l/*.obj the game would load."""
    cats, root = catalogues(game, addon)
    found = {}
    for c in cats:
        for p, _, _ in catx.read_index(c):
            n = catx.norm(p)
            if n.startswith(addon.lower() + '/'):
                n = n[len(addon) + 1:]          # FL catalogues name their members addon2/L/x3story.obj
            if n.startswith('l/') and n.endswith('.obj'):
                found[os.path.basename(n)] = (c, p)
    out = {}
    for name, (c, p) in found.items():
        out[name] = ('%s:%s' % (os.path.basename(c), p), catx.read_member(c, p))
    for f in glob.glob(os.path.join(root, 'l', '*.obj')) + glob.glob(os.path.join(root, 'L', '*.obj')):
        out[os.path.basename(f).lower()] = (f, open(f, 'rb').read())
    return out


def extract(game, outdir, addon='addon'):
    objs = find_objects(game, addon)
    if not objs:
        raise SystemExit('no l/*.obj found in %s (is it the game directory?)' % game)
    os.makedirs(outdir, exist_ok=True)
    for name, (src, b) in sorted(objs.items()):
        with open(os.path.join(outdir, name), 'wb') as f:
            f.write(b)
        print('%-16s %8d bytes  from %s' % (name, len(b), src))


def msci_setup(game, work, lang='044', addon='addon'):
    """texts_L<lang>.tsv, wares_L<lang>.json, msci_db.json and scripts/ (unpacked addon/scripts) in work."""
    from . import msci, texts_tsv, wares
    os.makedirs(work, exist_ok=True)
    texts = os.path.join(work, 'texts_L%s.tsv' % lang)
    wjson = os.path.join(work, 'wares_L%s.json' % lang)
    db = os.path.join(work, 'msci_db.json')
    obj = os.path.join(work, 'x3story.obj')
    run(texts_tsv.main, [game, lang, texts, '--addon', addon])
    run(wares.main, [game, texts, wjson, '--addon', addon])
    objs = find_objects(game, addon)
    if 'x3story.obj' not in objs:
        raise SystemExit('x3story.obj not found in the game catalogues')
    with open(obj, 'wb') as f:
        f.write(objs['x3story.obj'][1])
    msci.build_db(obj, texts, db, wjson)
    _, root = catalogues(game, addon)
    sd = os.path.join(root, 'scripts')
    if os.path.isdir(sd):
        run(unpck.main, [sd, os.path.join(work, 'scripts')])
        # editor texts the text files lack (most new commands of FL), from the scripts' saved source text
        from . import msci_learn
        run(msci_learn.main, [db, os.path.join(work, 'scripts')])
    print('database: %s; scripts: %s' % (db, os.path.join(work, 'scripts')))


def run(fn, argv):
    old = sys.argv
    sys.argv = [fn.__module__] + list(argv)
    try:
        return fn()
    finally:
        sys.argv = old


def main_extract():
    ap = argparse.ArgumentParser(prog='x3kc extract', description='the KC objects the game loads')
    ap.add_argument('game')
    ap.add_argument('out')
    ap.add_argument('--addon', default='addon', help="addon dir: addon (Albion Prelude), addon2 (Farnham's Legacy)")
    a = ap.parse_args()
    extract(a.game, a.out, a.addon)
    return 0


def main_msci_setup():
    ap = argparse.ArgumentParser(prog='x3kc msci-setup',
                                 description='texts, ware names, MSCI command database and unpacked scripts')
    ap.add_argument('game')
    ap.add_argument('work')
    ap.add_argument('--lang', default='044', help='language id of the text files (044 = English)')
    ap.add_argument('--addon', default='addon', help="addon dir: addon (Albion Prelude), addon2 (Farnham's Legacy)")
    a = ap.parse_args()
    msci_setup(a.game, a.work, a.lang, a.addon)
    return 0
