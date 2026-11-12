"""Editor texts the game data lacks, learned from the <sourceplaintext> of the installed scripts.

    x3kc msci learn <db.json> <dir of *.xml> [-o learned.json] [--xscript x3fl.xml] [--no-synthetic]
                                                                 (msci-setup runs it on its own)

Commands still without a text afterwards get a synthetic one from their signature (`fl.<id>`, or XScriptCompiler's
function name with --xscript), so they can be written; see synthesize().

Farnham's Legacy adds ~1 700 MSCI commands and many constants (page 2002), object classes and commands whose editor
texts are not in its text files: the shipped scripts were saved by an editor that had them. Every saved script still
carries the text that editor printed, so each line the decoder cannot print (`<cmd N>`, `[<2002:N>]`,
`{ware 7.410}`) is matched against the decoded parameters:
  - a name (pages 2002, 2006, 2008 ... and wares): the decoded line, unknown names as wildcards, must match the
    saved line; the name is voted;
  - a command: every template (literal text with %0..%n) that prints exactly the saved line from the decoded
    parameter strings is a candidate; the template that prints the most uses wins (then the one placing the most
    parameters), and the uses it does not print get alternative templates (db['alt'], read by the assembler). An old command whose text leaves out parameters FL added (`send
    incoming message` got a message type) gets a page 2004 text, which wins over 2003 as in the game.
Two rounds: names learned in round one let more command lines match in round two, and the other way round.
The result is merged into the database (db['text'], db['wares']) and listed in db['learned'], so `dis`, `asm` and
`check` use it. Commands no installed script uses stay unknown (`<cmd N>`): they cannot be written as text.
"""
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

from . import msci

MAXCAND = 64
UNK = re.compile(r'<(\d+):(-?\d+)>|\{ware (\d+\.\d+)\}')


def lines_of(pr, code):
    """(command id or None, parameter strings, printed line) for every non-empty editor line, as Printer.script
    orders them."""
    extra = {}
    for e in code[8] or []:
        if e[1] == 3 and len(e) > 2:
            extra.setdefault(e[0], []).append((None, None, '*' + pr.line(code, e[2:], interruptable=False)))
        else:
            extra.setdefault(e[0], []).append((None, None, pr.fill(pr.cmdtext(e[1]), e[2:])))
    out = []
    for i, l in enumerate(code[6]):
        out += extra.get(i, [])
        if pr.flags(l[0]) & 131072:
            continue
        cid, ps = pr.par_strings(code, l)
        out.append((cid, ps, pr.line(code, l)))
    for i in sorted(extra):
        if i >= len(code[6]):
            out += extra[i]
    return [x for x in out if x[2].strip()]         # empty lines are not compared (as `check` does)


def strip_marks(s):
    s = msci.norm(s)
    for m in ('@ ', '* '):
        if s.startswith(m):
            s = s[len(m):]
    return s


def wild(s):
    """Regex of a printed string whose unknown names match anything, and the keys of those names."""
    rx, keys, k = '', [], 0
    for m in UNK.finditer(s):
        rx += re.escape(s[k:m.start()]) + '(.+?)'
        keys.append(('ware', m.group(3)) if m.group(3) else (m.group(1), m.group(2)))
        k = m.end()
    return rx + re.escape(s[k:]), keys


def templates(ref, ps, limit=MAXCAND):
    """Every template that prints `ref` from the parameter strings `ps` (each non-empty one used at most once;
    unknown names inside a parameter match any text)."""
    # a result variable prints as '$x = ' and a command text right after it has no space of its own: keep one
    # trailing space so the learned text does not get a double one
    ps = [(msci.norm(p) + (' ' if p.endswith(' ') and msci.norm(p) else '')) if p else '' for p in ps]
    rxs = [re.compile(wild(p)[0]) if p and UNK.search(p) else None for p in ps]
    res = []

    def go(i, used, parts):
        if len(res) >= limit:
            return
        if i == len(ref):
            res.append(''.join(parts))
            return
        for j, p in enumerate(ps):
            if not p or j in used:
                continue
            if rxs[j] is None:
                if ref.startswith(p, i):
                    go(i + len(p), used | {j}, parts + ['%%%d' % j])
            else:
                for e in range(i + 1, len(ref) + 1):
                    if rxs[j].fullmatch(ref, i, e):
                        go(e, used | {j}, parts + ['%%%d' % j])
        c = ref[i]
        if c != '%' or not ref[i + 1:i + 2].isdigit():     # a literal % must not read as a placeholder
            go(i + 1, used, parts + [c])
    sys.setrecursionlimit(max(10000, len(ref) * 4 + 100))
    go(0, frozenset(), [])
    return res


def pairs(pr, scripts):
    for code, plain in scripts:
        mine = lines_of(pr, code)
        ref = [l for l in plain if l.strip()]
        if len(mine) == len(ref):                   # a script whose lines do not pair up teaches nothing
            yield from zip(mine, ref)


def learn_names(db, pr, scripts):
    votes = defaultdict(Counter)
    for (_, _, a), b in pairs(pr, scripts):
        a, b = msci.norm(a), msci.norm(b)
        if a == b or '<cmd' in a or not UNK.search(a):
            continue
        rx, keys = wild(a)
        m = re.fullmatch(rx, b)
        if m:
            for key, name in zip(keys, m.groups()):
                if not UNK.fullmatch(name):
                    votes[key][name] += 1
    names = {}
    for (page, i), c in votes.items():
        name = c.most_common(1)[0][0]
        if page == 'ware':
            db.setdefault('wares', {}).setdefault(i, name)
        else:
            db['text'].setdefault(page, {}).setdefault(i, name)
        names[page, i] = name
    return names


def nplaced(t):
    return len(set(re.findall(r'%\d', t)))


def learn_commands(db, pr, scripts, ours):
    """`ours`: {command id: page} of the texts learned in an earlier round (their uses are all looked at again)."""
    uses, stale = defaultdict(list), {c for c, p in ours.items() if p == '2004'}
    for (cid, ps, a), b in pairs(pr, scripts):
        if cid is None or cid in (102, 104):
            continue
        same = msci.norm(a) == msci.norm(b)
        if cid not in ours:
            if same:
                continue
            if not msci.norm(a).lstrip('@* ').startswith('<cmd'):
                shown = {int(x) for x in re.findall(r'%(\d)', pr.cmdtext(cid))}
                if all(i in shown for i in range(len(pr.pars(cid)))):
                    continue                        # the text shows every parameter: not a stale text
                stale.add(cid)
        uses[cid].append(set(templates(strip_marks(b), ps)))
    learned = {}
    page = db['text'].setdefault('2003', {})
    over = db['text'].setdefault('2004', {})        # page 2004 wins over 2003, as SCRIPT_GET_CMDTEXT reads them
    alt = db.setdefault('alt', {})
    for cid, inst in uses.items():
        # greedy cover: the template that prints the most uses (then places the most parameters); scripts saved by
        # different editor versions disagree (`send incoming message` with and without `log it`): the uses it
        # leaves get the next template, kept in db['alt'] for the assembler
        left, chosen = [s for s in inst if s], []
        while left and len(chosen) < 3:
            n = Counter(t for s in left for t in s)
            best = max(sorted(n), key=lambda t: (n[t], nplaced(t), -len(t)))
            if n[best] < 2 and chosen:
                break
            chosen.append(best)
            left = [s for s in left if best not in s]
        if not chosen:
            continue
        if cid in stale:
            old = pr.cmdtext(cid) if ours.get(cid) != '2004' else ''
            if nplaced(chosen[0]) <= nplaced(old):
                continue
            over[str(cid)] = chosen[0]
            learned[cid] = '2004'
        else:
            page[str(cid)] = chosen[0]
            learned[cid] = '2003'
        if len(chosen) > 1:
            alt[str(cid)] = chosen[1:]
    return learned, {c for c, inst in uses.items() if not any(inst)} - set(learned)


def learn(db, files):
    scripts = []
    for f in files:
        code, plain = msci.read_script(f)
        if code and plain:
            scripts.append((code, plain))
    args = {c[0]: [a[1] for a in (c[7] or [])] for c, _ in scripts}
    names, cmds, unknown = {}, {}, set()
    for _ in range(2):
        pr = msci.Printer(db)
        pr.scriptargs = args
        names.update(learn_names(db, pr, scripts))
        pr = msci.Printer(db)
        pr.scriptargs = args
        c, unknown = learn_commands(db, pr, scripts, cmds)
        cmds.update(c)
    lr = db.setdefault('learned', {})
    for (page, i) in names:
        lr.setdefault(page, []).append(i)
    for cid, page in cmds.items():
        lr.setdefault(page, []).append(str(cid))
    for k in lr:
        lr[k] = sorted(set(lr[k]), key=lambda x: float(x) if k == 'ware' else int(x))
    return names, cmds, sorted(unknown - set(cmds))


RESULT_TYPES = (15, 25, 26, 28)


def xscript_names(path):
    """{command id: function name} from XScriptCompiler's x3fl.xml (the user's own copy; read with regular
    expressions, the file is not well-formed XML everywhere)."""
    raw = open(path, encoding='utf-8', errors='replace').read()
    return {int(i): n for i, n in re.findall(r'<Function id="(\d+)" name="([\w.]+)"', raw)}


def synthesize(db, names=None):
    """A text for every command that still has none, from its signature: `<result><refobj> <name>: <p>, <p>`, the
    name `fl.<id>` or, with `names`, XScriptCompiler's function name. Such a text is not the game editor's, but
    `dis` prints it and `asm`/`new` read it, so the command can be written. Returns the ids given a text."""
    have = set(db['text'].get('2003', {})) | set(db['text'].get('2004', {}))
    page = db['text'].setdefault('2003', {})
    done = []
    for k, (pars, _) in sorted(db['cmds'].items(), key=lambda kv: int(kv[0])):
        cid = int(k)
        if k in have or cid <= 7 or cid in (102, 104):
            continue
        pars = pars or []
        res = [i for i, p in enumerate(pars) if p in RESULT_TYPES]
        ref = [i for i, p in enumerate(pars) if p == 20]
        rest = [i for i in range(len(pars)) if i not in res and i not in ref]
        name = (names or {}).get(cid) or 'fl.%d' % cid
        text = ''.join('%%%d' % i for i in res[:1]) + ''.join('%%%d ' % i for i in ref[:1]) + name
        if rest:
            text += ': ' + ', '.join('%%%d' % i for i in rest)
        page[k] = text
        done.append(cid)
    if done:
        db.setdefault('learned', {})['synthetic'] = done
    return done


def main():
    argv = sys.argv[1:]
    out = xscript = None
    synthetic = True
    if '-o' in argv:
        i = argv.index('-o')
        out = argv[i + 1]
        del argv[i:i + 2]
    if '--xscript' in argv:
        i = argv.index('--xscript')
        xscript = argv[i + 1]
        del argv[i:i + 2]
    if '--no-synthetic' in argv:
        argv.remove('--no-synthetic')
        synthetic = False
    dbp, d = argv[0], argv[1]
    db = json.load(open(dbp, encoding='utf-8'))
    names, cmds, unknown = learn(db, sorted(glob.glob(os.path.join(d, '*.xml'))))
    have = set(db['text'].get('2003', {})) | set(db['text'].get('2004', {}))
    left = sorted(int(c) for c in db['cmds'] if c not in have and int(c) > 7)
    by_page = Counter(p for p, _ in names)
    print('learned: names %s, command texts %d (%d corrected); used but not learned %s; '
          '%d commands still without a text' % (dict(by_page), len(cmds),
                                                sum(1 for p in cmds.values() if p == '2004'), unknown, len(left)))
    if synthetic and left:
        done = synthesize(db, xscript_names(xscript) if xscript else None)
        print('synthetic texts (signature%s): %d' % (' + ' + os.path.basename(xscript) if xscript else '', len(done)))
    with open(out or dbp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    return 0


if __name__ == '__main__':
    main()
