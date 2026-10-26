"""Portable annotations: names and comments that apply to any build of an object, not only the one they were made on.

The working form (what kcd reads, what annotators write) keys comments by code offset, so it fits one build only:
{"classes": {"150": {"name", "doc", "fields": {"3": {"name", "doc"}},
                     "methods": {"Init": {"doc", "params", "locals": {"v1": ..}, "comments": {"0x12fb3": ..}}}}},
 "globals": {"fields": {..}, "methods": {..}}}

The portable form (`pack`) adds what is needed to re-attach everything to another build (`resolve`):
- per class and for the globals, the field layout (type and initial value of every own field; strings by text):
  a field name applies to field j only when fields 0..j of the target have the same layout;
- per method, the parameter count and a hash of its instructions: params need the same count, locals need the
  same body (local numbering follows the code);
- per comment, the index of its instruction and a window of hashes of the instructions around it: with the same
  body the index is used, else the window is searched in the target method (a unique match, or the match nearest
  to the old relative position).
Class names and docs, method docs follow class ids and method names, which are stable between builds.
"""
import hashlib

from . import kcobj
from . import kcsave

FORMAT = 'x3kc-names/2'
WINDOW = (2, 3)         # instructions before and from the commented one


def _h(key):
    return hashlib.md5(repr(key).encode('latin1', 'replace')).hexdigest()[:8]


class _Code:
    """Instruction key hashes of every method of an object."""

    def __init__(self, o):
        self.o = o
        self.spans = kcsave.method_spans(o)
        names = {e: k for k, (e, _) in self.spans.items()}
        self.keys = {}
        for k, (e, end) in self.spans.items():
            ks = kcsave.insn_keys(o, e, end, names)
            self.keys[k] = ([pc for pc, _ in ks], [_h(x) for _, x in ks])
        rows = [(0, r) for r in o.symb] + [(c.id, r) for c in o.classes for r in c.methods]
        self.nparams = {(cid, o.string(r[1])): r[3] for cid, r in rows}

    def body(self, k):
        return hashlib.md5(''.join(self.keys[k][1]).encode()).hexdigest()[:16]

    def layout(self, cid):
        rows = self.o.vars if cid == 0 else next(c.fields for c in self.o.classes if c.id == cid)
        return [[r[2], self.o.string(r[1]) if r[2] == 2 else r[1]] for r in rows]


def pack(o, ann):
    """Working annotations made on object o -> portable form."""
    C = _Code(o)
    out = {'format': FORMAT, 'build': o.build, 'classes': {}, 'globals': {}}
    lost = []
    parts = [('0', ann.get('globals') or {})] + sorted((ann.get('classes') or {}).items(), key=lambda x: int(x[0]))
    cids = {c.id for c in o.classes} | {0}
    for scid, a in parts:
        cid = int(scid)
        if cid not in cids:
            lost.append('class %d: not in the object' % cid)
            continue
        e = {k: a[k] for k in ('name', 'doc') if a.get(k)}
        if a.get('fields'):
            e['layout'] = C.layout(cid)
            e['fields'] = a['fields']
        ms = {}
        for mn, m in (a.get('methods') or {}).items():
            k = (cid, mn)
            if k not in C.spans:
                lost.append('%d::%s: no such method' % k)
                continue
            pm = {x: m[x] for x in ('doc', 'params', 'locals') if m.get(x)}
            pm['nparams'] = C.nparams[k]
            pm['body'] = C.body(k)
            pcs, hs = C.keys[k]
            index = {pc: i for i, pc in enumerate(pcs)}
            cl = []
            for sk, text in sorted((m.get('comments') or {}).items(), key=lambda x: int(x[0], 16)):
                i = index.get(int(sk, 16))
                if i is None:
                    lost.append('%d::%s: comment at %s is not at an instruction of the method' % (cid, mn, sk))
                    continue
                lo = max(0, i - WINDOW[0])
                cl.append({'at': i, 'w': hs[lo:i + WINDOW[1]], 'o': i - lo, 'text': text})
            if cl:
                pm['comments'] = cl
            ms[mn] = pm
        if ms:
            e['methods'] = ms
        if cid == 0:
            out['globals'] = e
        else:
            out['classes'][scid] = e
    return out, lost


def resolve(o, port, report=None):
    """Portable annotations -> working form for object o (any build). report collects counts of what did not fit."""
    if port.get('format') != FORMAT:
        return port                     # already the working form
    C = _Code(o)
    stats = {'fields': 0, 'fields skipped': 0, 'params skipped': 0, 'locals skipped': 0, 'comments': 0,
             'comments moved': 0, 'comments dropped': 0, 'methods missing': 0, 'classes missing': 0}
    out = {'classes': {}, 'globals': {}}
    cids = {c.id for c in o.classes} | {0}
    parts = [('0', port.get('globals') or {})] + list((port.get('classes') or {}).items())
    for scid, a in parts:
        cid = int(scid)
        if cid not in cids:
            stats['classes missing'] += 1
            continue
        e = {k: a[k] for k in ('name', 'doc') if a.get(k)}
        if a.get('fields'):
            mine, theirs = C.layout(cid), a.get('layout') or []
            same = 0
            while same < min(len(mine), len(theirs)) and mine[same] == list(theirs[same]):
                same += 1
            e['fields'] = {}
            for j, f in a['fields'].items():
                if int(j) < same:
                    e['fields'][j] = f
                    stats['fields'] += 1
                else:
                    stats['fields skipped'] += 1
        ms = {}
        for mn, m in (a.get('methods') or {}).items():
            k = (cid, mn)
            if k not in C.spans:
                stats['methods missing'] += 1
                continue
            r = {x: m[x] for x in ('doc',) if m.get(x)}
            if m.get('params'):
                if m.get('nparams') == C.nparams[k]:
                    r['params'] = m['params']
                else:
                    stats['params skipped'] += 1
            same_body = m.get('body') == C.body(k)
            if m.get('locals'):
                if same_body:
                    r['locals'] = m['locals']
                else:
                    stats['locals skipped'] += 1
            pcs, hs = C.keys[k]
            cm = {}
            for c in m.get('comments') or []:
                i = c['at'] if same_body else _find(hs, c, len(pcs))
                if i is None:
                    stats['comments dropped'] += 1
                    continue
                stats['comments' if same_body else 'comments moved'] += 1
                key = '0x%06x' % pcs[i]
                cm[key] = cm[key] + ' ' + c['text'] if key in cm else c['text']
            if cm:
                r['comments'] = cm
            if r:
                ms[mn] = r
        if ms:
            e['methods'] = ms
        if cid == 0:
            out['globals'] = e
        else:
            out['classes'][scid] = e
    if report is not None:
        report.append('portable names for build %s applied to build %d: %s'
                      % (port.get('build'), o.build, ', '.join('%s %d' % kv for kv in stats.items() if kv[1])))
    return out


def _find(hs, c, n):
    """The commented instruction in a changed method: the whole window, else the instruction with the ones after it,
    else with the ones before it (code inserted or changed on one side); among several hits the one nearest to
    the old position."""
    w, off = c['w'], c['o']
    for part, poff in ((w, off), (w[off:], 0), (w[:off + 1], off)):
        if len(part) < 2 and len(w) >= 2:
            continue
        hits = [p + poff for p in range(len(hs) - len(part) + 1) if hs[p:p + len(part)] == part]
        if hits:
            return min(hits, key=lambda i: abs(i - c['at']))
    return None


def main():
    import argparse
    import json
    ap = argparse.ArgumentParser(prog='x3kc names', description='portable annotations')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pack', help='working annotations made on <obj> -> portable form')
    p.add_argument('obj')
    p.add_argument('src')
    p.add_argument('out')
    p = sub.add_parser('unpack', help='portable annotations -> working form (offset-keyed) for <obj>')
    p.add_argument('obj')
    p.add_argument('src')
    p.add_argument('out')
    a = ap.parse_args()
    o = kcobj.load(a.obj)
    src = json.load(open(a.src, encoding='utf-8'))
    if a.cmd == 'pack':
        res, lost = pack(o, src)
        for line in lost:
            print('not packed:', line)
    else:
        rep = []
        res = resolve(o, src, rep)
        for line in rep:
            print(line)
    with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(res, f, indent=1, ensure_ascii=False, sort_keys=True)
    return 0
