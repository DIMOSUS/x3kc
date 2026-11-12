"""Check the KC TextMate grammar without an editor.

    python -X utf8 -B editors/tools/check_grammar.py [source.kc] [--lines N] [--oniguruma DLL]

1. The grammar is well-formed JSON with scopeName, fileTypes, patterns, repository, and every #include resolves.
2. Every regex compiles in Python's re and - when the Oniguruma.dll shipped with Visual Studio's TextMate support is
   found - in Oniguruma itself (the engine VS and VS Code run). A deliberately broken pattern is fed first to make
   sure the DLL reports errors.
3. A small TextMate interpreter over Python re tokenises the first N lines (default 2000) of a source and reports
   the share of non-blank characters that got a scope besides the root source.kc, the share that got a specific
   one (not the catch-all variable.other.kc), invalid.* hits and lines no rule touched.

The interpreter follows vscode-textmate's rules that the grammar relies on: at each position the earliest match
wins, ties go to the end pattern of the open rule first and then to the order of the patterns; a line is scanned
with a trailing newline.
"""
import argparse
import ctypes
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GRAMMAR = os.path.join(os.path.dirname(HERE), 'kc-textmate', 'syntaxes', 'kc.tmLanguage.json')
VS_ONIG = 'C:/Program Files/Microsoft Visual Studio/*/*/Common7/IDE/CommonExtensions/Microsoft/TextMate/' \
          'runtimes/win-x64/native/Oniguruma.dll'


def all_regexes(g):
    out = []

    def walk(rule, where):
        for k in ('match', 'begin', 'end'):
            if k in rule:
                out.append((where + '.' + k, rule[k]))
        for i, p in enumerate(rule.get('patterns', [])):
            walk(p, '%s.patterns[%d]' % (where, i))
    walk(g, '$self')
    for name, r in g['repository'].items():
        walk(r, name)
    return out


def check_includes(g):
    bad = []

    def walk(rule):
        for p in rule.get('patterns', []):
            inc = p.get('include')
            if inc and inc.startswith('#') and inc[1:] not in g['repository']:
                bad.append(inc)
            walk(p)
    walk(g)
    for r in g['repository'].values():
        walk(r)
    return bad


class Onig:
    """The two exports of VS's Oniguruma.dll wrapper used here (read from its code):
    CreateRegEx(const wchar_t *pattern, int length, int *error) -> regex_t* (onig_new, UTF-16LE, CAPTURE_GROUP);
    FreeRegEx(regex_t*)."""

    def __init__(self, path):
        self.dll = ctypes.CDLL(path)
        self.dll.CreateRegEx.restype = ctypes.c_void_p
        self.dll.CreateRegEx.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.dll.FreeRegEx.argtypes = [ctypes.c_void_p]

    def compile(self, pat):
        err = ctypes.c_int(0)
        n = len(pat.encode('utf-16-le')) // 2
        r = self.dll.CreateRegEx(pat, n, ctypes.byref(err))
        if r:
            self.dll.FreeRegEx(r)
        return err.value


# ---- a minimal TextMate interpreter ------------------------------------------------------------------------------
class Rule:
    __slots__ = ('name', 'match', 'begin', 'end', 'captures', 'bcap', 'ecap', 'patterns', 'content')


def build(g):
    cache = {}
    comp = {}

    def rx(s):
        if s not in comp:
            comp[s] = re.compile(s)
        return comp[s]

    def make(d):
        key = id(d)
        if key in cache:
            return cache[key]
        r = Rule()
        cache[key] = r
        r.name = d.get('name')
        r.match = rx(d['match']) if 'match' in d else None
        r.begin = rx(d['begin']) if 'begin' in d else None
        r.end = rx(d['end']) if 'end' in d else None
        r.captures = d.get('captures', {})
        r.bcap = d.get('beginCaptures', d.get('captures', {}))
        r.ecap = d.get('endCaptures', d.get('captures', {}))
        r.patterns = []
        r.content = d.get('patterns', [])
        return r

    def expand(lst, seen=None):
        out = []
        for d in lst:
            if 'include' in d:
                inc = d['include']
                sub = g['repository'][inc[1:]] if inc.startswith('#') else g
                if 'match' in sub or 'begin' in sub:
                    out.append(make(sub))
                else:
                    out.extend(expand(sub.get('patterns', [])))
            else:
                out.append(make(d))
        return out

    root = make({'patterns': g['patterns']})
    pending = [root]
    done = set()
    while pending:
        r = pending.pop()
        if id(r) in done:
            continue
        done.add(id(r))
        r.patterns = expand(r.content)
        pending.extend(r.patterns)
    return root


def tokenise(root, lines):
    """Yield per line a list of scope stacks per character (only the innermost names are kept)."""
    stack = [[root, 0]]             # [rule, where its span starts on the current line]
    for text in lines:
        line = text + '\n'
        n = len(text)
        scopes = [[] for _ in range(n)]
        for fr in stack:
            fr[1] = 0
        pos = 0
        while pos <= n:
            top = stack[-1][0]
            best, bestrule, isend = None, None, False
            if top.end is not None:
                m = top.end.search(line, pos)
                if m:
                    best, isend = m, True
            for r in top.patterns:
                m = (r.match or r.begin).search(line, pos)
                if m and (best is None or m.start() < best.start()):
                    best, bestrule, isend = m, r, False
            if best is None or (best.start() >= n and not isend):
                break
            if isend:
                paint(scopes, best, top.ecap, None, n)
                if top.name:
                    for i in range(stack[-1][1], min(best.end(), n)):
                        scopes[i].insert(0, top.name)
                stack.pop()
                pos = best.end()        # an empty end match ($, a lookahead) closes the rule without consuming
                continue
            r = bestrule
            if r.match is not None:
                paint(scopes, best, r.captures, r.name, n)
                pos = best.end() if best.end() > best.start() else best.start() + 1
            else:
                paint(scopes, best, r.bcap, None, n)
                stack.append([r, best.start()])
                pos = best.end()
        for fr in stack:                # rules still open at the end of the line cover the rest of it
            if fr[0].name:
                for i in range(fr[1], n):
                    scopes[i].insert(0, fr[0].name)
        yield scopes


def paint(scopes, m, caps, name, n):
    if name:
        for i in range(m.start(), min(m.end(), n)):
            scopes[i].append(name)
    for k, c in caps.items():
        if 'name' not in c:
            continue
        gi = int(k)
        if gi > (m.re.groups or 0) or m.start(gi) < 0:
            continue
        for i in range(m.start(gi), min(m.end(gi), n)):
            scopes[i].append(c['name'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('source', nargs='?', default='D:/GAMES/x3_src/kc/ap/x3story.kc')
    ap.add_argument('--lines', type=int, default=2000, help='0 = the whole file')
    ap.add_argument('--oniguruma', default=None)
    a = ap.parse_args()
    with open(GRAMMAR, encoding='utf-8') as f:
        g = json.load(f)
    for k in ('scopeName', 'fileTypes', 'patterns', 'repository'):
        assert k in g, 'grammar lacks ' + k
    bad = check_includes(g)
    print('grammar: %s, %d repository rules, unresolved includes: %s' % (g['scopeName'], len(g['repository']),
                                                                            bad or 'none'))
    regs = all_regexes(g)
    perr = []
    for where, s in regs:
        try:
            re.compile(s)
        except re.error as e:
            perr.append((where, str(e)))
    print('regexes: %d, Python re errors: %d' % (len(regs), len(perr)))
    for w, e in perr:
        print('  ', w, e)
    dll = a.oniguruma or next(iter(sorted(glob.glob(VS_ONIG))), None)
    if dll and os.path.isfile(dll):
        o = Onig(dll)
        probe = o.compile('(unclosed')
        if probe == 0:
            print('Oniguruma: %s accepted a broken pattern; check skipped' % dll)
        else:
            oerr = [(w, o.compile(s)) for w, s in regs]
            oerr = [x for x in oerr if x[1] != 0]
            print('Oniguruma (%s): broken probe -> error %d; grammar errors: %d' % (dll, probe, len(oerr)))
            for w, e in oerr:
                print('  ', w, 'onig error', e)
    else:
        print('Oniguruma: no DLL found, only Python re was used')

    root = build(g)
    with open(a.source, encoding='utf-8', errors='replace', newline='') as f:
        lines = f.read().split('\n')
    lines = [l[:-1] if l.endswith('\r') else l for l in lines]
    if a.lines:
        lines = lines[:a.lines]
    t0 = time.perf_counter()
    total = scoped = specific = 0
    untouched, invalid = [], []
    by_scope = {}
    for no, (text, sc) in enumerate(zip(lines, tokenise(root, lines)), 1):
        line_total = line_scoped = 0
        for ch, s in zip(text, sc):
            if ch.isspace():
                continue
            total += 1
            line_total += 1
            if s:
                scoped += 1
                line_scoped += 1
                inner = s[-1]
                if inner != 'variable.other.kc':
                    specific += 1
                top = inner.split('.')[0]
                by_scope[top] = by_scope.get(top, 0) + 1
                if any(x.startswith('invalid.') for x in s):
                    invalid.append(no)
        if line_total and not line_scoped:
            untouched.append(no)
    dt = time.perf_counter() - t0
    invalid = sorted(set(invalid))
    print('tokenised %d lines of %s in %.2f s' % (len(lines), a.source, dt))
    print('non-blank chars: %d; with a scope: %d (%.2f%%); with a specific scope (not variable.other.kc): %d (%.2f%%)'
          % (total, scoped, 100.0 * scoped / max(total, 1), specific, 100.0 * specific / max(total, 1)))
    print('innermost scope families:', ', '.join('%s %.1f%%' % (k, 100.0 * v / max(total, 1))
                                                 for k, v in sorted(by_scope.items(), key=lambda x: -x[1])))
    print('lines no rule touched: %d %s' % (len(untouched), untouched[:20]))
    print('lines with invalid.* scopes: %d %s' % (len(invalid), invalid[:20]))
    for no in invalid[:5]:
        print('   %d: %s' % (no, lines[no - 1].strip()[:120]))
    return 1 if perr or bad else 0


if __name__ == '__main__':
    sys.exit(main())
