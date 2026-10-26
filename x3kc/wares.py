"""Ware names "main.sub" -> English name from the type files (types/T*.pck), as GetWareName shows them.

    x3kc wares <game dir> <texts.tsv> <out.json>

The name is text 17:<id>; the field that holds <id> differs per file and is found by trying every field and keeping
the one whose values hit page 17 most often.
"""
import glob
import json
import re
import os
import sys

from . import catx
from . import unpck

FILES = {5: 'TDocks', 6: 'TFactories', 7: 'TShips', 8: 'TLaser', 9: 'TShields', 10: 'TMissiles', 11: 'TWareE',
         12: 'TWareB', 13: 'TWareF', 14: 'TWareM', 15: 'TWareN', 16: 'TWareT', 17: 'TSpecial', 18: 'TAsteroids'}


def latest(game, name):
    """Bytes of types/<name>.pck from the last catalogue that has it (addon catalogues win)."""
    found = None
    cats = sorted(glob.glob(os.path.join(game, '[0-9][0-9].cat'))) + sorted(glob.glob(os.path.join(game, 'addon', '[0-9][0-9].cat')))
    for c in cats:
        for p, _, _ in catx.read_index(c):
            if p.lower().endswith('types/%s.pck' % name.lower()):
                found = (c, p)
    if not found:
        return None
    return unpck.unpack(catx.read_member(*found)).decode('latin1')


def rows(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('/'):
            continue
        out.append(line.split(';'))
    return out[1:]          # first line: version;count;


def shown(v, texts, depth=0):
    """A text as the game shows it: {page,id} references expanded, (comments) dropped, escapes resolved."""
    from . import msci
    if depth < 4:
        v = re.sub(r'\{(\d+),\s*(\d+)\}', lambda m: shown(texts.get((int(m.group(1)), int(m.group(2))), ''),
                                                           texts, depth + 1), v)
    return msci.game_text(v) if depth == 0 else v


def main():
    game, textsp, outp = sys.argv[1:4]
    p17, texts = {}, {}
    for line in open(textsp, encoding='utf-8'):
        k, _, v = line.rstrip('\n').partition('\t')
        p, _, i = k.partition(':')
        if p.isdigit() and i.isdigit():
            texts[int(p), int(i)] = v
            if p == '17':
                p17[int(i)] = v
    names = {}
    for main_, fn in FILES.items():
        t = latest(game, fn)
        if t is None:
            continue
        rs = rows(t)
        best, score = None, -1
        for f in range(min(len(r) for r in rs if r) if rs else 0):
            s = sum(1 for r in rs if r[f].lstrip('-').isdigit() and int(r[f]) in p17 and int(r[f]) > 0)
            if s > score:
                best, score = f, s
        for sub, r in enumerate(rs):
            v = r[best]
            if v.lstrip('-').isdigit() and int(v) in p17:
                names['%d.%d' % (main_, sub)] = shown(p17[int(v)], texts)
        print(fn, 'rows', len(rs), 'name field', best, 'hits', score)
    json.dump(names, open(outp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)


if __name__ == '__main__':
    main()
