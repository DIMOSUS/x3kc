"""Text pages for the language server: an index of every <t> entry of the project's text files, all languages.

Where the pages are found (first match of each kind; KCLSP_TEXT_DIRS adds folders, separated by os.pathsep):
  <repo>/game/<edition>/**/t/*.xml    the stock pages as the content import stores them (addon/t, addon2/t, t)
  <repo>/kc/<edition>/t/*.xml         the project's own pages (e.g. 9500)
<repo> is the first folder above the source that holds kc/ or game/; <edition> (ap / fl) is the path component
after kc/ (or game/), else the setting.

Page numbers follow the exe's loader (Script::ParsePageXml, x3_src src/script/vm.cpp normalizeXmlPageNumber): a page
id read from XML is normalised before it is stored - AP: 380001..399999 -> -380000, 350001..379999 -> -350000,
300001..349999 -> -300000; FL in addition 390001..399999 -> -390000 first. Scripts ask for the normalised number
(SE_ReadText(35, 951) reads <page id="350035"> or <page id="35">). A later file replaces the entries of an earlier
one with the same page and id (storeTextPageEntries); the order assumed here is: stock folders before the project's
own, file names in sorted order, the language-neutral NNNN.xml (KC's LoadText) after the NNNN-LXXX.xml files.
"""
import bisect
import os
import re

LANGS = {7: 'Russian', 44: 'English', 49: 'German', 33: 'French', 39: 'Italian', 34: 'Spanish', 48: 'Polish',
         42: 'Czech', 81: 'Japanese', 82: 'Korean', 86: 'Chinese (simplified)', 88: 'Chinese (traditional)'}
FIRST = (7, 44)

LANG_RE = re.compile(r'<language\b[^>]*?\bid\s*=\s*"(\d+)"')
PAGE_RE = re.compile(r'<page\b[^>]*?\bid\s*=\s*"(-?\d+)"')
T_RE = re.compile(r'<t\b[^>]*?\bid\s*=\s*"(-?\d+)"[^>]*?(?:/>|>(.*?)</t\s*>)', re.S)
ENT = {'&lt;': '<', '&gt;': '>', '&amp;': '&', '&quot;': '"', '&apos;': "'"}


def normalize_page(raw, edition):
    p = raw
    if edition == 'fl':
        if 390001 <= p <= 399999:
            p -= 390000
        if 380001 <= p <= 389999:
            p -= 380000
        elif 350001 <= p <= 379999:
            p -= 350000
        elif 300001 <= p <= 349999:
            p -= 300000
        return p
    if 380001 <= p <= 399999:
        return p - 380000
    if 350001 <= p <= 379999:
        return p - 350000
    if 300001 <= p <= 349999:
        return p - 300000
    return p


def unxml(s):
    s = re.sub(r'&(lt|gt|amp|quot|apos);', lambda m: ENT[m.group(0)], s)
    return re.sub(r'&#(x[0-9a-fA-F]+|\d+);', lambda m: chr(int(m.group(1)[1:], 16) if m.group(1)[0] == 'x'
                                                           else int(m.group(1))), s)


class Entry:
    __slots__ = ('lang', 'page', 'raw_page', 'id', 'text', 'path', 'line', 'col', 'order')

    def __init__(self, lang, page, raw_page, tid, text, path, line, col, order):
        self.lang, self.page, self.raw_page, self.id, self.text = lang, page, raw_page, tid, text
        self.path, self.line, self.col, self.order = path, line, col, order


def file_order(path):
    """Neutral NNNN.xml after the NNNN-LXXX.xml files of the same folder."""
    b = os.path.basename(path).lower()
    return (os.path.dirname(path).lower(), 1 if re.fullmatch(r'\d+\.xml', b) else 0, b)


class TextIndex:
    def __init__(self, dirs, edition='ap'):
        self.dirs = dirs                # in load order
        self.edition = edition
        self.files = {}                 # path -> (mtime, [Entry])
        self.by_key = {}                # (page, id) -> [Entry] in load order
        self.built = False

    def paths(self):
        out = []
        for k, d in enumerate(self.dirs):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            fs = [os.path.join(d, n) for n in names if n.lower().endswith('.xml')]
            out.extend((k,) + file_order(p) + (p,) for p in fs)
        return [x[-1] for x in sorted(out)]

    def refresh(self):
        """(Re)read the files whose modification time changed."""
        changed = not self.built
        seen = set()
        for order, p in enumerate(self.paths()):
            seen.add(p)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            old = self.files.get(p)
            if old and old[0] == mt and old[2] == order:
                continue
            self.files[p] = (mt, self.read(p, order), order)
            changed = True
        for p in list(self.files):
            if p not in seen:
                del self.files[p]
                changed = True
        if changed:
            by = {}
            for p, (_, es, _) in sorted(self.files.items(), key=lambda x: x[1][2]):
                for e in es:
                    by.setdefault((e.page, e.id), []).append(e)
            self.by_key = by
        self.built = True
        return changed

    def read(self, path, order):
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError:
            return []
        text = raw.decode('utf-8', 'replace')
        if text.startswith('﻿'):
            text = text[1:]
        if '<t' not in text:
            return []
        starts = [0]
        i = text.find('\n')
        while i >= 0:
            starts.append(i + 1)
            i = text.find('\n', i + 1)
        m = LANG_RE.search(text)
        lang = int(m.group(1)) if m else 0
        pages = [(m.start(), int(m.group(1))) for m in PAGE_RE.finditer(text)]
        ppos = [p for p, _ in pages]
        out = []
        for m in T_RE.finditer(text):
            k = bisect.bisect_right(ppos, m.start()) - 1
            if k < 0:
                continue
            raw_page = pages[k][1]
            ln = bisect.bisect_right(starts, m.start()) - 1
            out.append(Entry(lang, normalize_page(raw_page, self.edition), raw_page, int(m.group(1)),
                             unxml(m.group(2) or ''), path, ln + 1, m.start() - starts[ln], order))
        return out

    def lookup(self, page, tid, lang=None):
        """Entries for (page, id): per language the effective one (the last loaded) first, then the ones it
        replaced. -> [(lang, effective Entry, [replaced Entries])], Russian and English first."""
        self.refresh()
        es = self.by_key.get((page, tid), [])
        by_lang = {}
        for e in es:
            by_lang.setdefault(e.lang, []).append(e)
        out = []
        for lg in sorted(by_lang, key=lambda x: (x not in FIRST, FIRST.index(x) if x in FIRST else 0, x)):
            if lang is not None and lg != lang:
                continue
            lst = by_lang[lg]
            out.append((lg, lst[-1], lst[:-1]))
        return out


def lang_name(lg):
    return LANGS.get(lg, 'language %d' % lg)


def find_text_dirs(source_path, edition=None):
    """(dirs in load order, edition) for a source file, from the repository layout described above."""
    p = os.path.abspath(source_path)
    parts = p.replace('\\', '/').split('/')
    repo = None
    for i in range(len(parts) - 1, 0, -1):
        d = '/'.join(parts[:i])
        if os.path.isdir(d + '/kc') or os.path.isdir(d + '/game'):
            repo = d
            if edition is None and i < len(parts) and parts[i] in ('kc', 'game') and i + 1 < len(parts):
                edition = parts[i + 1].lower() if parts[i + 1].lower() in ('ap', 'fl') else None
            break
    if edition is None:
        low = [x.lower() for x in parts]
        edition = 'fl' if 'fl' in low else 'ap'
    dirs = []
    if repo:
        g = os.path.join(repo, 'game', edition)
        if os.path.isdir(g):
            base = os.path.join(g, 't')
            if os.path.isdir(base):
                dirs.append(base)
            for sub in sorted(os.listdir(g)):
                t = os.path.join(g, sub, 't')
                if sub.lower().startswith('addon') and os.path.isdir(t):
                    dirs.append(t)
        own = os.path.join(repo, 'kc', edition, 't')
        if os.path.isdir(own):
            dirs.append(own)
    for d in os.environ.get('KCLSP_TEXT_DIRS', '').split(os.pathsep):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs, edition


_INDEXES = {}


def index_for(source_path):
    dirs, ed = find_text_dirs(source_path)
    key = (tuple(dirs), ed)
    ix = _INDEXES.get(key)
    if ix is None:
        ix = _INDEXES[key] = TextIndex(dirs, ed)
    return ix
