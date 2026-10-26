"""Annotations of a KC object: names and comments kept apart from the generated source, applied by kcd.

File (JSON), all parts optional:
{
  "classes": {
    "150": {"name": "Client", "doc": "...",
            "fields": {"3": {"name": "fastForward", "doc": "..."}},          <- own field index
            "methods": {"Init": {"doc": "...", "params": ["sectorName"],
                                 "locals": {"v1": "ship"},                    <- default local names as kcd prints them
                                 "comments": {"0x12fb3": "..."}}}}             <- code offset of a statement (kcd --pcs)
  },
  "globals": {"fields": {"33": {"name": "gPatchTable", "doc": "..."}},
              "methods": {"InitPatchTable": {...}}}
}
Names only change the text: kcc builds the same bytes. apply() fixes names that would not resolve back
(keywords, duplicates, shadowing) by appending `_` and reports each fix.
"""
import json
import re

IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
KEYWORDS = {'var', 'if', 'else', 'while', 'for', 'do', 'switch', 'case', 'default', 'return', 'break', 'continue',
            'fork', 'function', 'class', 'new', 'delete', 'this', 'native', 'asm', 'frame', 'dense', 'sparse'}
DEFAULT = re.compile(r'(C|m|g|v|arg)\d+\Z')


ASCII = {'—': '--', '–': '-', '→': '->', '←': '<-', '…': '...', '“': '"', '”': '"',
         '‘': "'", '’': "'", '≤': '<=', '≥': '>=', '≠': '!='}


def latin1(s, bad):
    """The source is written in latin1: other characters of a doc or comment get an ASCII stand-in or '?'."""
    out = []
    for ch in s:
        if ord(ch) > 0xff:
            bad.add(ch)
            ch = ASCII.get(ch, '?')
        out.append(ch)
    return ''.join(out)


def load(path):
    bad = set()

    def fix(x):
        if isinstance(x, str):
            return latin1(x, bad)
        if isinstance(x, list):
            return [fix(v) for v in x]
        if isinstance(x, dict):
            return {k: fix(v) for k, v in x.items()}
        return x
    a = fix(json.load(open(path, encoding='utf-8')))
    if bad:
        print('names: characters outside latin1 replaced: %s' % ' '.join(sorted(bad)))
    a.setdefault('classes', {})
    a.setdefault('globals', {})
    return a


def method_ann(ann, cid, name):
    part = ann['globals'] if cid == 0 else ann['classes'].get(str(cid), {})
    m = (part.get('methods') or {}).get(name) or {}
    out = dict(m)
    out['comments'] = {}
    for k, v in (m.get('comments') or {}).items():
        try:
            out['comments'][int(k, 16) if isinstance(k, str) else k] = v      # offsets are hex, as kcd --pcs prints
        except ValueError:
            raise ValueError('class %s, method %s: comment key %r is not a code offset' % (cid, name, k))
    if out.get('params') is not None and not isinstance(out['params'], list):
        raise ValueError('class %s, method %s: "params" must be a list of names' % (cid, name))
    return out


def ok_name(n):
    return isinstance(n, str) and IDENT.match(n) and n not in KEYWORDS and not n.startswith('__')


def unkw(n):
    """A keyword proposed as a name becomes name_ (frame -> frame_)."""
    return n + '_' if isinstance(n, str) and n in KEYWORDS else n


def apply(S, ann, report):
    """Rename classes, fields and globals in Symbols S; returns {(cid, field index): doc} and class docs."""
    taken_cls = set()
    cdoc, fdoc = {}, {}
    for cid, k in sorted(S.classes.items()):
        if cid == 0:
            continue
        a = ann['classes'].get(str(cid), {})
        n = unkw(a.get('name'))
        if a.get('doc'):
            cdoc[cid] = a['doc']
        if n:
            if not ok_name(n) or (DEFAULT.match(n) and n != 'C%d' % cid):
                report.append('class %d: name %r refused' % (cid, n))
                n = None
            elif n in taken_cls or S.by_name.get(n, cid) != cid:
                m = '%s%d' % (n, cid)
                # the suffixed name must be free too, and must not look like a default name (v12, C12)
                while m in taken_cls or S.by_name.get(m, cid) != cid or DEFAULT.match(m):
                    m += '_'
                report.append('class %d: %s -> %s (taken)' % (cid, n, m))
                n = m
        if n:
            del S.by_name[k.name]
            k.name = n
            S.by_name[n] = cid
        taken_cls.add(k.name)
    # globals
    g = S.classes[0]
    gnames = set()
    for j, f in enumerate(g.fields):
        a = (ann['globals'].get('fields') or {}).get(str(j), {})
        if a.get('doc'):
            fdoc[0, j] = a['doc']
        n = unkw(a.get('name'))
        if n and (not ok_name(n) or DEFAULT.match(n) or n in taken_cls or n in gnames):
            report.append('global %d: name %r refused' % (j, n))
            n = None
        f.name = n or f.name
        gnames.add(f.name)
    # fields, ancestors first, unique along every chain
    order = sorted((c for c in S.classes if c), key=lambda c: len(S.chain(c)))
    for cid in order:
        k = S.classes[cid]
        a = (ann['classes'].get(str(cid), {}).get('fields')) or {}
        above = {f.name for c in S.chain(cid)[1:] for f in S.classes[c].fields}
        own = set()
        for j, f in enumerate(k.fields):
            fa = a.get(str(j), {})
            if fa.get('doc'):
                fdoc[cid, j] = fa['doc']
            n = unkw(fa.get('name'))
            if n:
                if not ok_name(n) or DEFAULT.match(n):
                    report.append('field %d.%d: name %r refused' % (cid, j, n))
                    n = None
                else:
                    while n in above or n in own or n in taken_cls or n in gnames:
                        n += '_'
                    if n != unkw(fa['name']):
                        report.append('field %d.%d: %s -> %s (taken)' % (cid, j, fa['name'], n))
            f.name = n or f.name
            own.add(f.name)
    return cdoc, fdoc


def function_names(S, cid, nparams, ma, report, where):
    """Checked params/locals names for one function (ma: method annotation)."""
    used = set(S.by_name) | {f.name for f in S.classes[0].fields}
    if cid:
        used |= {f.name for c in S.chain(cid) for f in S.classes[c].fields}
    mine = set()
    params = []
    for i in range(nparams):
        n = unkw((ma.get('params') or [None] * nparams)[i] if i < len(ma.get('params') or []) else None)
        if n and (not ok_name(n) or DEFAULT.match(n)):
            report.append('%s: param %r refused' % (where, n))
            n = None
        if n:
            while n in used or n in mine:
                n += '_'
        params.append(n)
        mine.add(n or 'arg%d' % (i + 1))
    locs = {}
    for k, n in (ma.get('locals') or {}).items():
        n = unkw(n)
        if not (re.fullmatch(r'v\d+', k) and ok_name(n)) or DEFAULT.match(n):
            report.append('%s: local %s=%r refused' % (where, k, n))
            continue
        while n in used or n in mine:
            n += '_'
        locs[k] = n
        mine.add(n)
    return params, locs
