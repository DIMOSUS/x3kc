"""All game texts of one language as `page:id<TAB>text`, with page ids as the game sees them.

    x3kc texts <game dir> <lang 044> <out.tsv>

Sources, later ones override earlier ones: base catalogues (13.cat ... t/0001), addon catalogues (addon/NN.cat
addon/t/*), loose addon/t/*.pck|xml. Pages 300001-349999, 350000-379999, 380000-399999 are renumbered to their
range start like SE_XMLImportText does (301903 -> 1903); the raw id is kept too.
"""
import glob
import html
import os
import re
import sys

from . import catx
from . import unpck


def renum(p):
    for lo, hi in ((380000, 399999), (350000, 379999), (300001, 349999)):
        if lo <= p <= hi:
            return p - (lo if lo != 300001 else 300000)
    return None


def wanted(name, data, lang):
    """A text file of this language: named NNNN-L044, or named without a language (0002.pck, 440001.xml)
    and declaring <language id="44"> inside."""
    base = os.path.basename(name).lower()
    m = re.search(r'-l(\d+)\.', base)
    if m:
        return m.group(1) == lang
    if data is None:
        return True                     # decide after reading
    lid = re.search(rb'<language id="(\d+)"', data[:400])
    return bool(lid) and int(lid.group(1)) == int(lang)


def main():
    game, lang, out = sys.argv[1], sys.argv[2], sys.argv[3]
    docs = []
    for c in sorted(glob.glob(os.path.join(game, '[0-9][0-9].cat'))) + sorted(glob.glob(os.path.join(game, 'addon', '[0-9][0-9].cat'))):
        for p, _, _ in catx.read_index(c):
            if '/t/' in '/' + p and p.lower().endswith(('.pck', '.xml')) and wanted(p, None, lang):
                b = catx.read_member(c, p)
                b = unpck.unpack(b) if p.lower().endswith('.pck') else b
                if wanted(p, b, lang):
                    docs.append((c + ':' + p, b))
    for f in sorted(glob.glob(os.path.join(game, 'addon', 't', '*.*'))):
        if not f.lower().endswith(('.pck', '.xml')) or not wanted(f, None, lang):
            continue
        b = open(f, 'rb').read()
        b = unpck.unpack(b) if f.lower().endswith('.pck') else b
        if wanted(f, b, lang):
            docs.append((f, b))
    texts, titles = {}, {}
    for name, b in docs:
        t = b.decode('utf-8', 'replace')
        for pid, title, body in re.findall(r'<page id="(\d+)"(?: title="([^"]*)")?[^>]*>(.*?)</page>', t, re.S):
            pids = [int(pid)]
            r = renum(int(pid))
            if r is not None:
                pids.append(r)
            for i, x in re.findall(r'<t id="(\d+)">(.*?)</t>', body, re.S):
                # keep spaces (editor texts like " = " depend on them); one line per text
                x = html.unescape(x).replace('\r', '').replace('\n', ' ').replace('\t', ' ')
                for p in pids:
                    texts[p, int(i)] = x
            for p in pids:
                if title:
                    titles[p] = title
    lines = ['%d:title\t%s' % (p, t) for p, t in sorted(titles.items())]
    lines += ['%d:%d\t%s' % (p, i, x) for (p, i), x in sorted(texts.items())]
    open(out, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines) + '\n')
    print(len(docs), 'files,', len(texts), 'texts,', len(titles), 'pages')


if __name__ == '__main__':
    main()
