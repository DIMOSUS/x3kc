"""MSCI scripts in their text form (.msci: `x3kc msci dis` output, read by `x3kc msci new`) for the language server.

Diagnostics come from x3kc's assembler (msci_asm.Assembler), line by line so that every bad line is reported, plus
blocks left open and undefined labels. The assembler needs the game's msci_db.json (`x3kc msci-setup`), looked for
in this order:
  KCLSP_MSCI_DB_AP / KCLSP_MSCI_DB_FL, KCLSP_MSCI_DB        environment
  msci_db.json in the script's folder or any folder above it
  <repo>/build/msci/<edition>/msci_db.json, <repo>/build/tmp/queue/msci/<edition>/msci_db.json
Hover, completion and the command documentation use docs/msci_commands_<edition>.json of the x3kc checkout.
Edition: `fl` when a path component is `fl` (or `addon2`), else `ap`; KCLSP_MSCI_EDITION overrides.
"""
import json
import os
import re

from . import project as P

HEAD_RX = re.compile(r'^//\s*(\S+)\s+\((.*)\)\s*$')
ARG_RX = re.compile(r'^//\s+\$([\w.]+):\s*(.*?)\s+"(.*)"\s*$')
LABEL_RX = re.compile(r'^\s*([\w.]+):\s*$')
GOTO_RX = re.compile(r'\b(?:gosub|goto label)\s+([\w.]+)')
CALL_RX = re.compile(r'(?<![\w$.])(?:call|interrupt with|start|run|unregister) script\s+([A-Za-z!_][\w.!\-]*)|'
                     r'(?<![\w$.])(?:event |callback |check)?script=\s*([A-Za-z!_][\w.!\-]*)')
TEXT_RXS = [re.compile(r'\{\s*(\d+)\s*,\s*(\d+)\s*\}'),
            re.compile(r'(?<![\w$.])(?:pageid|page)=\s*(-?\d+),?\s*(?:textid|id)=\s*(-?\d+)')]


def edition_of(path):
    e = os.environ.get('KCLSP_MSCI_EDITION')
    if e:
        return e.lower()
    parts = [x.lower() for x in os.path.abspath(path).replace('\\', '/').split('/')]
    return 'fl' if ('fl' in parts or 'addon2' in parts) else 'ap'


def find_db(path, ed):
    for k in ('KCLSP_MSCI_DB_' + ed.upper(), 'KCLSP_MSCI_DB'):
        v = os.environ.get(k)
        if v and os.path.isfile(v):
            return v
    d = os.path.dirname(os.path.abspath(path))
    while True:
        c = os.path.join(d, 'msci_db.json')
        if os.path.isfile(c):
            return c
        for sub in (('build', 'msci', ed), ('build', 'tmp', 'queue', 'msci', ed)):
            c = os.path.join(d, *sub, 'msci_db.json')
            if os.path.isfile(c):
                return c
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd


_CMDS = {}


def commands(ed):
    """docs/msci_commands_<ed>.json -> {id: command}."""
    if ed not in _CMDS:
        p = os.path.join(P.ROOT, 'docs', 'msci_commands_%s.json' % ed)
        try:
            with open(p, encoding='utf-8') as f:
                _CMDS[ed] = {c['id']: c for c in json.load(f)['commands']}
        except OSError:
            _CMDS[ed] = {}
    return _CMDS[ed]


_ASM = {}


def assembler(db_path):
    if db_path not in _ASM:
        from x3kc import msci_asm
        with open(db_path, encoding='utf-8') as f:
            db = json.load(f)
        _ASM[db_path] = (msci_asm.Assembler(db), db)
    return _ASM[db_path]


class Doc:
    def __init__(self, path):
        self.path = path
        self.key = P.norm(path)
        self.text = None
        self.open = False
        self.version = 0
        self.diags = None
        self.done = -1

    def source(self):
        if self.text is None:
            with open(self.path, encoding='utf-8-sig', errors='replace') as f:
                self.text = f.read()
        return self.text

    def set_text(self, t):
        self.text = t
        self.version += 1

    def lines(self):
        return self.source().replace('\r', '').split('\n')


def header(lines):
    name, args = None, []
    for i, l in enumerate(lines):
        s = l.strip()
        if not s.startswith('//'):
            continue
        m = ARG_RX.match(s)
        if m:
            args.append((m.group(1), m.group(2), m.group(3), i))
            continue
        m = HEAD_RX.match(s)
        if m and name is None:
            name = (m.group(1), m.group(2), i)
    return name, args


def span(line):
    a = len(line) - len(line.lstrip())
    return a, max(a + 1, len(line.rstrip()))


def sibling_args(folder):
    """Argument descriptions of the other .msci scripts in the folder (call script X : desc=value ...)."""
    out = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if n.lower().endswith('.msci'):
            try:
                with open(os.path.join(folder, n), encoding='utf-8-sig', errors='replace') as f:
                    head = [next(f, '') for _ in range(40)]
            except OSError:
                continue
            _, args = header([h.rstrip('\n') for h in head])
            out[n[:-5]] = [a[2] for a in args]
    return out


def diagnostics(doc):
    """[Diag] for the current text."""
    from x3kc import msci_asm
    lines = doc.lines()
    ed = edition_of(doc.path)
    dbp = find_db(doc.path, ed)
    out = []
    name, args = header(lines)
    if name is None:
        out.append(P.Diag(1, 0, 2, 'no header: the first line must be `// <script name>  (<description>)`'))
    elif os.path.splitext(os.path.basename(doc.path))[0] != name[0]:
        out.append(P.Diag(name[2] + 1, 0, len(lines[name[2]]), 'the script name %s differs from the file name' % name[0], 2))
    if dbp is None:
        out.append(P.Diag(1, 0, 2, 'no msci_db.json found for %s (x3kc msci-setup; or set KCLSP_MSCI_DB): only the '
                                   'header is checked' % ed.upper(), 3))
        return out
    A, db = assembler(dbp)
    types = set(db['text'].get('2000', {}).values())
    for a in args:
        if a[1] not in types:
            out.append(P.Diag(a[3] + 1, 0, len(lines[a[3]]), 'unknown argument type %r' % a[1]))
    A.scriptargs = sibling_args(os.path.dirname(os.path.abspath(doc.path)))
    A.vars = {}
    for a in args:
        A.var(a[0])
    code, extra, stack, labels = [], [], [], {}
    open_lines = []
    code_line = []          # source line of each code line
    body = [(i, l) for i, l in enumerate(lines) if not l.lstrip().startswith('//')]
    while body and body[0][1].strip() == '':
        body.pop(0)
    while body and body[-1][1].strip() == '':
        body.pop()

    def here():
        return len(code)
    for i, raw in body:
        if ']]>' in raw:
            a, b = span(raw)
            out.append(P.Diag(i + 1, a, b, ']]> cannot stand in the script text (it would end the CDATA section)'))
        depth, n0 = len(stack), len(code)
        try:
            A.line(raw.replace('\t', '  '), code, extra, stack, labels, here)
        except msci_asm.AsmError as x:
            a, b = span(raw)
            out.append(P.Diag(i + 1, a, b, str(x)))
        except Exception as x:          # the assembler's own limits: report, do not kill the server
            a, b = span(raw)
            out.append(P.Diag(i + 1, a, b, 'assembler: %s: %s' % (type(x).__name__, x)))
        code_line.extend([i] * (len(code) - n0))
        if len(stack) > depth:
            open_lines.append(i)
        elif len(stack) < depth:
            del open_lines[len(stack):]
    for i in open_lines[:len(stack)]:
        a, b = span(lines[i])
        out.append(P.Diag(i + 1, a, b, '%s without end' % lines[i].strip().split()[0]))
    for k, ln in enumerate(code):
        for v in ln:
            if isinstance(v, tuple) and len(v) == 2 and v[0] == 'label' and v[1] not in labels:
                i = code_line[k] if k < len(code_line) else 0
                a, b = span(lines[i])
                out.append(P.Diag(i + 1, a, b, 'undefined label %r' % v[1]))
    return out


def command_at(doc, line_no):
    """The command id of a line (through the assembler), or None."""
    lines = doc.lines()
    if line_no >= len(lines):
        return None
    s = lines[line_no].strip()
    if not s or s.startswith('//') or s.startswith('*') and not s.startswith('*$'):
        return None
    if s.startswith('@'):
        s = s[1:].strip()
    dbp = find_db(doc.path, edition_of(doc.path))
    if dbp is None:
        return None
    A, _ = assembler(dbp)
    saved = getattr(A, 'vars', {})
    A.vars = dict(saved) if isinstance(saved, dict) else {}
    try:
        ln, _ = A.command(s)
        return ln[0]
    except Exception:           # AsmError, or a line the assembler cannot read at all
        return None
    finally:
        A.vars = saved


def command_doc(c):
    sem = c.get('semantics') or {}
    out = ['**%s**' % c.get('syntax', c.get('text', '')).replace('<', '&lt;').replace('*', '\\*'),
           '', 'command %d - %s' % (c['id'], ' / '.join(c.get('group') or []))]
    if sem.get('summary'):
        out += ['', sem['summary']]
    if c.get('params'):
        out += ['', 'Parameters: ' + ', '.join('%s' % p['type'] for p in c['params'])]
    for k, label in (('params', 'Parameter notes'), ('returns', 'Returns'), ('effects', 'Effects'), ('edge', 'Edge'),
                     ('bug', 'Bug')):
        if sem.get(k):
            out.append('%s: %s' % (label, sem[k]))
    if sem.get('waits'):
        out.append('Waits (the script is suspended).')
    if c.get('calls'):
        out.append('KC handler calls: ' + ', '.join(c['calls']))
    if c.get('used') is not None:
        out.append('Used %d times in the stock scripts%s.' % (c['used'], (' (e.g. %s)' % c['example_script'])
                                                               if c.get('example_script') else ''))
    return '\n'.join(out)


def completion(doc, line_no, col):
    """Command templates (all commands of the edition) as completion items; the parameters stay as <Type> text."""
    items = []
    for cid, c in sorted(commands(edition_of(doc.path)).items()):
        syn = c.get('syntax') or ''
        if not syn or c.get('hidden') or cid <= 3:
            continue
        sem = (c.get('semantics') or {}).get('summary', '')
        items.append({'label': syn, 'kind': 15, 'detail': 'command %d' % cid, 'filterText': syn,
                      'insertText': syn.replace('$ret =|if ', '$ret = ').replace('$ret =|if/START ', '$ret = '),
                      'documentation': {'kind': 'markdown', 'value': command_doc(c)},
                      'sortText': '%05d' % (100000 - min(99999, c.get('used') or 0))})
    return items


def text_ref_at(line, col):
    for rx in TEXT_RXS:
        for m in rx.finditer(line):
            if m.start() <= col <= m.end():
                return int(m.group(1)), int(m.group(2))
    return None


def hover(doc, line_no, col):
    lines = doc.lines()
    if line_no >= len(lines):
        return None
    line = lines[line_no]
    tr = text_ref_at(line, col)
    if tr:
        return ('text',) + tr
    cid = command_at(doc, line_no)
    if cid is None:
        return None
    c = commands(edition_of(doc.path)).get(cid)
    return ('md', command_doc(c)) if c else None


def definition(doc, line_no, col):
    """[(path, line 1-based, col, length)]"""
    lines = doc.lines()
    if line_no >= len(lines):
        return []
    line = lines[line_no]
    for m in GOTO_RX.finditer(line):
        if m.start(1) <= col <= m.end(1):
            for i, l in enumerate(lines):
                lm = LABEL_RX.match(l)
                if lm and lm.group(1) == m.group(1):
                    return [(doc.path, i + 1, l.index(lm.group(1)), len(lm.group(1)))]
            return []
    for m in CALL_RX.finditer(line):
        g = 1 if m.group(1) else 2
        if m.start(g) <= col <= m.end(g):
            name = m.group(g)
            folder = os.path.dirname(os.path.abspath(doc.path))
            for d in (folder, os.path.join(os.path.dirname(folder), 'scripts')):
                p = os.path.join(d, name + '.msci')
                if os.path.isfile(p):
                    return [(p, 1, 0, 2)]
            return []
    return []


def symbols(doc):
    """(name, kind, (l, c, l2, c2), selection, children) like Project.symbols."""
    lines = doc.lines()
    out = []
    name, args = header(lines)
    if name:
        kids = [('$' + a[0] + ': ' + a[1], 13, (a[3] + 1, 0, a[3] + 1, len(lines[a[3]])),
                 (a[3] + 1, 0, a[3] + 1, len(lines[a[3]])), []) for a in args]
        r = (name[2] + 1, 0, name[2] + 1, len(lines[name[2]]))
        out.append((name[0], 12, r, r, kids))
    for i, l in enumerate(lines):
        m = LABEL_RX.match(l)
        if m:
            c = l.index(m.group(1))
            r = (i + 1, c, i + 1, c + len(m.group(1)))
            out.append((m.group(1), 20, r, r, []))
    return out
