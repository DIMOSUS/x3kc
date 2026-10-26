"""MSCI scripts (addon/scripts/*.xml|pck): read the <codearray>, print it as the in-game script editor does.

    x3kc msci-setup <game dir> <work dir>        command/parameter database from C700::InitDatabase
    x3kc msci dis <db.json> <script.xml>...                       print scripts as editor text
    x3kc msci check <db.json> <dir>                               compare with <sourceplaintext> of every script

Script array (C700::LoadScript / SaveScript):
  [0] name  [1] engine version (>= 40)  [2] description  [3] version  [4] flags (1 disabled?, 2 sector data encoded,
  4 signature ok)  [5] variable names (arguments first)  [6] code lines  [7] arguments [(type, description)]
  [8] more encoded data  [9] ?
Code line: [command id, parameters...]; command 3 = a commented-out line [3, real id, ...].
Parameter kinds by parameter type (page 2000) and its flags (C700 m3):
  flag 0x10000 or type 4: a (data type, value) pair;   flag 0x20000: return variable (index >= 0, or a special form
  from page 2007: if, while, skip if ...);  types 5 / 30 (in commands 102 / 104): count + pairs;
  1: label (line index of its `define label`);  2, 6, 7, 8: a plain value;  3: a number.
Data types: 0x20002 variable, 0x20003 constant (page 2002), 4 int, 5 string, 65542 ship, 65543 station, 65544 sector,
  9 ware ((main << 16) | sub), 10 race, 12 object class (page 2006), 15 operator (page 2001), 18 object command (2008),
  20 data type (2013), 21 array ...
"""
import html
import json
import re
import sys
import xml.etree.ElementTree as ET

INTERRUPTABLE = 65536


def load_texts(path):
    t = {}
    for line in open(path, encoding='utf-8'):
        k, _, v = line.rstrip('\n').partition('\t')
        p, _, i = k.partition(':')
        if i.lstrip('-').isdigit():
            v = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), v)   # "\053" = "+"
            t[int(p), int(i)] = game_text(v)
    return t


def game_text(v):
    """Text as the game shows it: an unescaped (...) is a comment and dropped, backslash + x is x."""
    out, depth, i = [], 0, 0
    while i < len(v):
        c = v[i]
        if c == '\\' and i + 1 < len(v):
            if not depth:
                out.append(v[i + 1])
            i += 2
            continue
        if c == '(':
            depth += 1
        elif c == ')' and depth:
            depth -= 1
        elif not depth:
            out.append(c)
        i += 1
    return ''.join(out)


# ---- database from the decompiled object --------------------------------------------------------------------
def literal(e):
    from . import kcast as A
    t = type(e)
    if t is A.Int:
        return e.v
    if t is A.Str:
        return e.s
    if t is A.MkArr:
        return [literal(x) for x in e.items]
    if t is A.MkTab:
        return {str(literal(k)): literal(v) for k, v in e.pairs}
    raise ValueError(t.__name__)


def build_db(objp, textsp, outp, waresp=None):
    from . import kcast as A
    from . import kcdec
    from . import kcobj
    from . import kcsave
    o = kcobj.load(objp)
    spans = kcsave.method_spans(o)
    methods = {}
    rows = sorted([(r[0], 0, r) for r in o.symb] + [(r[0], c.id, r) for c in o.classes for r in c.methods])
    for e, cid, r in rows:
        methods[e] = (cid, o.string(r[1]))
    e, end = spans[(700, 'InitDatabase')]
    nparams = next(r[3] for c in o.classes if c.id == 700 for r in c.methods if o.string(r[1]) == 'InitDatabase')
    fn = kcdec.decompile(o.code, e, end, nparams, methods, o.string)
    mem = {}
    for s in fn.body:
        if type(s) is A.ExprStmt and type(s.e) is A.Assign and type(s.e.t) is A.Mem:
            try:
                mem[s.e.t.k] = literal(s.e.v)
            except ValueError:
                pass
    db = {'cmds': mem[0], 'parflags': mem[3], 'consts': mem[4]}
    texts = load_texts(textsp)
    db['text'] = {str(p): {str(i): v for (pp, i), v in texts.items() if pp == p}
                  for p in (2000, 2001, 2002, 2003, 2004, 2006, 1266, 35, 2007, 2008, 2009, 2013, 2028, 1999, 12, 1265)}
    import os
    wp = waresp or os.path.join(os.path.dirname(outp), 'wares_L044.json')
    if os.path.exists(wp):
        db['wares'] = json.load(open(wp, encoding='utf-8'))
    with open(outp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    print('commands', len(db['cmds']), 'parameter types', len(db['parflags']), 'constants', len(db['consts']))


# ---- codearray ----------------------------------------------------------------------------------------------
def sval(el):
    t = el.get('type')
    if t == 'array':
        return [sval(c) for c in el]
    if t == 'int':
        return int(el.get('val'))
    if t == 'string':
        return el.get('val')
    return None


def read_script(path):
    raw = open(path, 'rb').read().decode('utf-8', 'replace')
    m = re.search(r'<codearray>(.*?)</codearray>', raw, re.S)
    if not m:
        return None, None
    code = sval(ET.fromstring(m.group(1).strip()))
    p = re.search(r'<sourceplaintext>\s*<!\[CDATA\[(.*?)\]\]>\s*</sourceplaintext>', raw, re.S)
    plain = p.group(1).replace('\r', '').split('\n') if p else None
    return code, plain


class Printer:
    def __init__(self, db):
        self.db = db
        self.cmds = {int(k): v for k, v in db['cmds'].items()}
        self.pf = {int(k): v for k, v in db['parflags'].items()}
        self.text = {int(p): {int(i): v for i, v in d.items()} for p, d in db['text'].items()}
        self.wares = db.get('wares', {})

    def t(self, page, i, default=None):
        return self.text.get(page, {}).get(i, default if default is not None else '<%d:%d>' % (page, i))

    def cmdtext(self, cid):
        """SCRIPT_GET_CMDTEXT: page 2004 if it has the id, else 2003; NOP (2) is an empty line."""
        if cid == 2:
            return ''
        return self.text.get(2004, {}).get(cid) or self.t(2003, cid, '<cmd %d>' % cid)

    def fill(self, fmt, ps):
        return re.sub(r'%(\d)', lambda m: str(ps[int(m.group(1))]) if int(m.group(1)) < len(ps) else '', fmt)

    def pars(self, cid):
        d = self.cmds.get(cid)
        return d[0] if d and d[0] else []

    def flags(self, cid):
        d = self.cmds.get(cid)
        return d[1] if d else 0

    def value(self, script, typ, v):
        if typ == 131074:
            if isinstance(v, int) and v >= 0 and script and v < len(script[5]):
                return '$' + script[5][v]
            return '$%s' % v
        if typ == 5:
            return "'%s'" % v
        if typ == 0:
            return self.t(1265, 33, 'null')
        if typ == 131075:
            return '[' + self.t(2002, v) + ']'
        if typ == 4:
            return '%d' % v
        if typ == 9:
            k = '%d.%d' % ((v >> 16) & 255, v & 65535)
            return self.wares.get(k, '{ware %s}' % k)
        if typ == 10:
            return 'Player' if v == 10 else self.t(1266, v, '{race %d}' % v)
        simple = {11: (12, 100), 12: (2006, 0), 13: (1999, 10), 15: (2001, 0), 18: (2008, 0), 25: (2028, 0),
                  19: (2009, 0), 20: (2013, 0), 24: (2000, 0)}
        if typ in simple:
            page, add = simple[typ]
            return self.t(page, (v & 65535 if typ in (15, 20) else v) + add)
        if typ == 14:
            return self.t(35, 1102422 if v > 0 else 1102424 if v == 0 else 1102423, str(v))
        if typ == 21:
            return 'ARRAY(...)'
        return '{%d:%s}' % (typ, v)

    def par_strings(self, script, line):
        cid = line[0]
        k = 1
        if cid == 3:
            cid, k = line[1], 2
        out = []
        line = list(line) + [0] * 64          # short lines: missing trailing slots read as 0
        for p in self.pars(cid):
            if (cid in (102, 104)) and p in (5, 30):
                n = line[k]
                k += 1
                toks = []
                for _ in range(n):
                    toks.append(self.value(script, line[k], line[k + 1]))
                    k += 2
                if cid == 104:
                    # tokens are in postfix order; a display list follows: -i-1 = token i, else operator code
                    m = line[k] if isinstance(line[k], int) else 0
                    k += 1
                    if m == 0:              # commented-out lines keep no display list
                        out.extend(toks)
                        continue
                    disp = []
                    for _ in range(m):
                        d = line[k]
                        k += 1
                        disp.append(toks[-d - 1] if d < 0 else self.t(2001, d & 65535))
                    toks = disp
                out.extend(toks)
                continue
            f = self.pf.get(p + 1, 0)       # C700.GetParDefFlags reads m3[p + 1]
            if f & 65536 or p == 4:
                s = self.value(script, line[k], line[k + 1])
                k += 2
                out.append(s + ' ->' if p == 20 else s)
                continue
            if f & 131072:
                v = line[k]
                k += 1
                if isinstance(v, int) and v >= 0 or (isinstance(v, str) and v):
                    s = self.value(script, 131074, v)
                    if p != 0:
                        s += self.t(2007, 0)
                else:
                    if not v:
                        v = -2147483647
                    s = self.t(2007, v & 255)
                out.append(s)
                continue
            v = line[k] if k < len(line) else None
            k += 1
            if p == 1:
                if isinstance(v, int) and script and 0 <= v < len(script[6]):
                    v = script[6][v][1]
                out.append(str(v))
            elif p == 3:
                out.append('%d' % v)
            else:
                out.append('' if v is None else str(v))
        return cid, out

    def line(self, script, line, interruptable=True):
        cid, ps = self.par_strings(script, line)
        fmt = self.cmdtext(cid)
        pars = self.pars(cid)
        res = []
        i = 0
        while i < len(fmt):
            c = fmt[i]
            if c == '%' and i + 1 < len(fmt) and fmt[i + 1].isdigit():
                n = int(fmt[i + 1])
                i += 2
                if cid == 102 and n < len(pars) and pars[n] == 5:
                    args = (self.scriptargs.get(line[1]) or []) if hasattr(self, 'scriptargs') else []
                    res.append(''.join('%s=%s ' % (args[j] if j < len(args) else '???', ps[n + j])
                                       for j in range(len(ps) - n)))
                elif cid == 104 and n < len(pars) and pars[n] == 30:
                    res.append(''.join(x + ' ' for x in ps[n:]))
                elif n < len(ps):
                    res.append(ps[n])
                continue
            res.append(c)
            i += 1
        s = ''.join(res)
        if line[0] == 3:
            s = '* ' + s
        if interruptable and self.flags(cid) & INTERRUPTABLE:
            s = '@ ' + s
        return s

    def script(self, script):
        """Editor lines: code lines without hidden commands (flag 0x20000), plus the editor-only lines of
        script[8] ([code index, page 2003 id]: end, else, continue, break ...) placed before that code index."""
        extra = {}
        for e in script[8] or []:
            if e[1] == 3 and len(e) > 2:          # a commented-out command: [index, 3, command...]
                extra.setdefault(e[0], []).append('*' + self.line(script, e[2:], interruptable=False))
            else:
                extra.setdefault(e[0], []).append(self.fill(self.cmdtext(e[1]), e[2:]))
        out = []
        for i, l in enumerate(script[6]):
            out += extra.get(i, [])
            cid = l[1] if l[0] == 3 else l[0]
            if self.flags(l[0]) & 131072:
                continue
            out.append(self.line(script, l))
        for i in sorted(extra):
            if i >= len(script[6]):
                out += extra[i]
        return out


def norm(s):
    s = ' '.join(s.replace('\xa0', ' ').split())
    return s.replace('skip if not', 'do if')       # the shipped source text predates the rename


def main():
    cmd = sys.argv[1]
    if cmd == 'db':
        build_db(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    db = json.load(open(sys.argv[2], encoding='utf-8'))
    pr = Printer(db)
    if cmd == 'dis':
        import glob
        import os
        pr.scriptargs = {}
        for d in {os.path.dirname(os.path.abspath(f)) for f in sys.argv[3:]}:      # argument names of called scripts
            for g in glob.glob(os.path.join(d, '*.xml')):
                c, _ = read_script(g)
                if c:
                    pr.scriptargs[c[0]] = [a[1] for a in (c[7] or [])]
        for f in sys.argv[3:]:
            code, plain = read_script(f)
            print('// %s  (%s)' % (code[0], code[2]))
            for k, a in enumerate(code[7] or []):
                print('//   $%s: %s  "%s"' % (code[5][k], db['text']['2000'].get(str(a[0]), a[0]), a[1]))
            depth, once = 0, False
            for l in pr.script(code):
                s = l.strip()
                t = s[1:].strip() if s.startswith('@') else s       # '@ if ...': interruptible condition
                w = t.split(' ')[0]
                if t == 'end' or t == 'else' or t.startswith('else if'):
                    depth -= 1
                print('  ' * (max(depth, 0) + once) + s)
                once = False
                if w in ('if', 'while', 'else') or t.startswith('else if'):
                    depth += 1
                elif t.startswith('skip if') or t.startswith('do if'):
                    once = True
        return
    if cmd == 'check':
        import glob
        import os
        files = sorted(glob.glob(os.path.join(sys.argv[3], '*.xml')))
        scripts = {}
        for f in files:
            code, plain = read_script(f)
            if code:
                scripts[f] = (code, plain)
        pr.scriptargs = {c[0]: [a[1] for a in (c[7] or [])] for c, _ in scripts.values()}
        ok = bad = 0
        shown = 0
        from collections import Counter
        per_cmd = Counter()
        for f, (code, plain) in scripts.items():
            if plain is None:
                continue
            mine = pr.script(code)
            ref = [l for l in plain if l.strip()]
            mine = [l for l in mine if l.strip()]
            if len(mine) != len(ref):
                bad += abs(len(mine) - len(ref))        # lines one side lacks count as different
            for k, (a, b) in enumerate(zip(mine, ref)):
                if norm(a) == norm(b):
                    ok += 1
                else:
                    bad += 1
                    line = ''
                    per_cmd[norm(b).split(' ')[0]] += 1
                    if shown < int(os.environ.get('SHOW', '15')):
                        shown += 1
                        print('%s:%d\n  mine: %s\n  game: %s\n  code: %s' % (os.path.basename(f), k, norm(a), norm(b), line))
        print('lines equal %d, different %d' % (ok, bad))
        print('worst commands:', per_cmd.most_common(15))


if __name__ == '__main__':
    main()
