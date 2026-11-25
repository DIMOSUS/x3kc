"""The exe a KC object is built for: which natives it has and which of them always answer at once.

A target is the edition plus the engine, one word:
    ap          the original Albion Prelude exe (X3AP.exe) - the default
    fl          the original Farnham's Legacy exe (X3FL.exe)
    x3_src-ap   the x3_src engine built as Albion Prelude (AP's natives + the engine's own)
    x3_src-fl   the x3_src engine built as Farnham's Legacy (FL's natives + the engine's own)

Lists (one name per line, '#' comments), next to this module:
    natives_ap.txt, natives_fl.txt          every native name of the exe (strings of X3AP.exe / X3FL.exe)
    natives_x3src.txt                       the natives the x3_src engine adds (in both editions' builds)
    sync_natives_ap.txt, sync_natives_fl.txt, sync_natives_x3src.txt
                                            those that always answer at once (not savegame return sites)
A call to a name the target does not have answers int 0 at once (the exe resolves it to group 0).
"""
import os

TARGETS = ('ap', 'fl', 'x3_src-ap', 'x3_src-fl')
DEFAULT = 'ap'
EXE = {'ap': 'X3AP.exe', 'fl': 'X3FL.exe', 'x3_src-ap': 'the x3_src engine (AP)', 'x3_src-fl': 'the x3_src engine (FL)'}
HERE = os.path.dirname(os.path.abspath(__file__))
_cache = {}


def check(target):
    """target or DEFAULT; ValueError for a name that is not a target"""
    t = target or DEFAULT
    if t not in TARGETS:
        raise ValueError('unknown target %r (one of %s)' % (t, ', '.join(TARGETS)))
    return t


def edition(target):
    return check(target).rsplit('-', 1)[-1]


def engine(target):
    return 'x3_src' if check(target).startswith('x3_src') else 'original'


def read(name):
    """the names of one list file (cached)"""
    if name not in _cache:
        with open(os.path.join(HERE, name), encoding='utf-8') as f:
            _cache[name] = frozenset(l.strip() for l in f if l.strip() and not l.startswith('#'))
    return _cache[name]


def known(target=None):
    """every native name the target's exe has"""
    t = check(target)
    s = read('natives_%s.txt' % edition(t))
    return s | read('natives_x3src.txt') if engine(t) == 'x3_src' else s


def sync(target=None):
    """the natives of the target that always answer at once"""
    t = check(target)
    s = read('sync_natives_%s.txt' % edition(t))
    return s | read('sync_natives_x3src.txt') if engine(t) == 'x3_src' else s


def where(name):
    """which exes have the native: a subset of {'ap', 'fl', 'x3_src'}"""
    out = set()
    for e in ('ap', 'fl'):
        if name in read('natives_%s.txt' % e):
            out.add(e)
    if name in read('natives_x3src.txt'):
        out.add('x3_src')
    return out


def unknown_message(name, target=None):
    """the warning for a call of a native the target does not have (None when it has it)"""
    t = check(target)
    if name in known(t):
        return None
    w = where(name)
    ed = edition(t)
    if w == {'x3_src'}:
        return 'native %s exists only in the x3_src engine (the original exe answers 0)' % name
    if ed == 'ap' and 'fl' in w:
        return "native %s exists only in Farnham's Legacy (the AP exe answers 0)" % name
    if ed == 'fl' and 'ap' in w:
        return 'native %s exists only in Albion Prelude (the FL exe answers 0)' % name
    return 'native %s is unknown to %s (it would return 0)' % (name, EXE[t])


def where_word(name):
    """'AP+FL', 'AP only', 'FL only', 'x3_src only', or the mix ('FL+x3_src' ...); '' for an unknown name"""
    w = where(name)
    if not w:
        return ''
    if w == {'ap', 'fl'}:
        return 'AP+FL'
    if len(w) == 1:
        return {'ap': 'AP', 'fl': 'FL', 'x3_src': 'x3_src'}[next(iter(w))] + ' only'
    return '+'.join(x for x, k in (('AP', 'ap'), ('FL', 'fl'), ('x3_src', 'x3_src')) if k in w)


def where_text(name):
    """a line for a hover: which exes have the native"""
    w = where_word(name)
    return {'': '', 'AP+FL': 'in X3AP.exe and X3FL.exe (and the x3_src engine)',
            'AP only': 'only in Albion Prelude (X3AP.exe and the x3_src engine built as AP)',
            'FL only': "only in Farnham's Legacy (X3FL.exe and the x3_src engine built as FL)",
            'x3_src only': 'only in the x3_src engine (the original exes answer 0)'}.get(w, 'in ' + w)


SPEC_DIR = os.path.join(os.path.dirname(HERE), 'docs', 'natives')


def spec_files():
    """the spec files of the x3kc checkout: docs/natives/spec_<GROUP>.jsonl (one per native group) and
    spec_x3src.jsonl (the x3_src engine's own natives); [] without the docs"""
    if not os.path.isdir(SPEC_DIR):
        return []
    return [os.path.join(SPEC_DIR, fn) for fn in sorted(os.listdir(SPEC_DIR))
            if fn.startswith('spec_') and fn.endswith('.jsonl')]


def spec_lines():
    """(path, line number, line text, entry) of every entry of the spec files"""
    import json
    out = []
    for path in spec_files():
        with open(path, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if isinstance(s, dict) and s.get('name'):
                    out.append((path, n, line.rstrip('\n'), s))
    return out


def specs():
    """native name -> its entry of the spec files ({} without the docs); the fields: docs/natives/README.md"""
    if 'specs' not in _cache:
        _cache['specs'] = {s['name']: s for _, _, _, s in spec_lines()}
    return _cache['specs']


def summary(spec):
    """a spec entry's one-line description: its summary, else the first sentence of its semantics"""
    if spec.get('summary'):
        return spec['summary']
    s = ' '.join((spec.get('semantics') or '').split())
    for i in range(len(s)):
        if s[i] == '.' and (i + 1 == len(s) or s[i + 1] == ' '):
            return s[:i + 1]
    return s


FULL_FIELDS = (('args', 'call'), ('returns', 'returns'), ('waits', 'waits'), ('index', 'index AP'),
               ('index_fl', 'index FL'), ('handler', 'handler AP'), ('handler_fl', 'handler FL'),
               ('summary', 'summary'), ('semantics', 'semantics'), ('fl', 'FL'), ('x3_src', 'x3_src'),
               ('confidence', 'confidence'), ('bugs', 'bugs'), ('notes', 'notes'))


def full_text(name, target=None, width=110):
    """every field of a native's spec entry as indented text (x3kc natives --full)"""
    import textwrap
    t = check(target)
    s = specs().get(name)
    out = ['%s  [group %s, %s; target %s lists it as %s]' % (
        name, name.split('_', 1)[0], where_word(name) or 'unknown', t, 'at once' if name in sync(t) else 'may wait')]
    if not s:
        return '\n'.join(out + ['  (no description in docs/natives)'])
    if s.get('callable') is False:
        out.append('  not a callable native: a call answers int 0')
    for k, label in FULL_FIELDS:
        v = s.get(k)
        if k == 'args':
            v = '%s(%s)' % (name, v or '')
        elif k == 'waits':
            v = 'may wait (a call is a savegame return site)' if v else 'answers at once'
        if v is None or v == '' or v == []:
            continue
        items = v if isinstance(v, list) else [str(v)]
        for i, it in enumerate(items):
            head = '  %-11s ' % (label + ':' if i == 0 else '')
            if len(items) > 1:
                head += '- '
            out += textwrap.wrap(it, width, initial_indent=head, subsequent_indent=' ' * len(head)) or [head]
    return '\n'.join(out)


def main():
    """x3kc natives [--target T] [--full] [filter]: the natives of a target, one per line (--full: every field)"""
    import argparse
    ap = argparse.ArgumentParser(prog='x3kc natives', description='The natives of a target: name, group, '
                                 'arguments -> result, at once / may wait, which exes have it, what it does (the '
                                 'summary of its entry in docs/natives/spec_*.jsonl; "(no description)" without '
                                 'one). The reference: docs/natives/README.md.')
    add_argument(ap)
    ap.add_argument('--full', action='store_true',
                    help='every field of each matching entry: semantics, FL and x3_src differences, bugs, notes')
    ap.add_argument('filter', nargs='?', default='',
                    help='a name or a prefix (SE_, B3D_Inst, P_SetJoy...), case-insensitive')
    a = ap.parse_args()
    t = a.target
    flt = a.filter.lower()
    names = sorted(n for n in known(t) if n.lower().startswith(flt))
    sp = specs()
    if a.full:
        for n in names:
            print(full_text(n, t))
            print()
        print('%d natives of target %s%s' % (len(names), t, ' matching %r' % a.filter if flt else ''))
        return 0 if names or not flt else 1
    rows = []
    for n in names:
        s = sp.get(n)
        sig = '(%s) -> %s' % (s.get('args', ''), s.get('returns', '?')) if s else '(?) -> ?'
        rows.append((n, n.split('_', 1)[0], sig, 'at once' if n in sync(t) else 'may wait', where_word(n),
                     summary(s) if s else '(no description)'))
    if rows:
        w = [max(len(r[k]) for r in rows) for k in range(5)]
        w[2] = min(w[2], 48)
        for r in rows:
            print('  '.join(r[k].ljust(w[k]) for k in range(5)) + '  ' + r[5])
    if flt:
        every = read('natives_ap.txt') | read('natives_fl.txt') | read('natives_x3src.txt')
        for n in sorted(x for x in every - known(t) if x.lower().startswith(flt)):
            print('(not in %s: %s - %s)' % (t, n, where_word(n)))
    described = sum(1 for n in names if n in sp)
    print('%d natives of target %s%s, %d described' % (len(names), t, ' matching %r' % a.filter if flt else '',
                                                        described))
    return 0 if rows or not flt else 1


def add_argument(ap):
    """the --target option of kcc, kcverify, savecheck and natives"""
    ap.add_argument('--target', choices=TARGETS, default=DEFAULT,
                    help='the exe the object is for: its natives and which of them answer at once (default %s)'
                    % DEFAULT)
