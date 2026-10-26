"""MSCI assembler: script text as the editor shows it (sourceplaintext) -> script array (codearray).

    x3kc msci verify <msci_db.json> <dir of *.xml>     assemble every <sourceplaintext>, compare with <codearray>
    x3kc msci asm <msci_db.json> <script.xml> <edited.txt> > new.xml
        header, arguments and version are taken from script.xml; other *.xml next to it give the argument names
        of called scripts. The output has no <sourcetext> and no <signature> (the game reads only <codearray>).

Rules (see MSCI.md): commands are matched against their texts (pages 2004/2003); parameters are decoded by their
types; `if / else if / else / end / while / break / continue / skip if / do if` become condition forms of the result
variable plus hidden `goto` (112) lines and editor-only lines (script[8]); expressions are compiled like the game
editor's C701.__exprtree (precedence climbing, postfix operand list + display list).
"""
import json
import re
import sys

from . import msci

NOVAR, START = -2147483647, -2147483646        # 0x80000001, 0x80000002
FORMS = {'if': 3, 'if not': 4, 'else if': 5, 'else if not': 6, 'skip if': 7, 'do if': 8, 'skip if not': 8,
         'while': 9, 'while not': 10}
JUMP_IF_TRUE = {4, 6, 7, 10}
OPS = {'==': 0, '!=': 1, '>': 2, '<': 3, '>=': 4, '<=': 5, '&': 6, '|': 7, '^': 8, 'AND': 9, 'OR': 10, '+': 11,
       '-': 12, '*': 13, '/': 14, 'mod': 15, '(': 16, ')': 17, '~': 65554, '!': 65556}
OP_TOKENS = sorted(OPS, key=len, reverse=True)
VAR_RX = r'\$[\w.]+'
RESULT_RX = r'((?:\$\S+\s*=|=|START|while not|while|skip if not|skip if|do if|else if not|else if|if not|if)?)'


class AsmError(Exception):
    pass


def form_value(form, target=0):
    v = 0x80000000 | 0x20000000 | ((target & 0xffff) << 8) | form
    if form in JUMP_IF_TRUE:
        v |= 0x40000000
    return v - (1 << 32)


class Assembler:
    def __init__(self, db):
        self.p = msci.Printer(db)
        self.cmds = self.p.cmds
        self.pf = self.p.pf
        T = self.p.text
        # reverse value tables: shown text -> [(type, value)]
        self.names = {}

        def add(s, tv):
            if s:
                self.names.setdefault(s.strip(), []).append(tv)
        for k, v in self.p.wares.items():
            m, s = k.split('.')
            add(v, (9, (int(m) << 16) | int(s)))
        for i, s in T.get(2006, {}).items():
            add(s, (12, i))
        for i, s in T.get(1266, {}).items():
            add(s, (10, i))
        add('Player', (10, 10))
        add(T.get(35, {}).get(1102422), (14, 1))
        add(T.get(35, {}).get(1102424), (14, 0))
        add(T.get(35, {}).get(1102423), (14, -1))
        for page, typ in ((2008, 18), (2009, 19), (2028, 25)):
            for i, s in T.get(page, {}).items():
                add(s, (typ, i))
        for i, s in T.get(2013, {}).items():
            add(s, (20, i))
        for i, s in T.get(2000, {}).items():
            add(s, (24, i))
        self.consts = {s: i for i, s in T.get(2002, {}).items()}
        self.name_list = sorted(self.names, key=len, reverse=True)
        # command templates
        self.templates = []
        for cid in self.cmds:
            if cid <= 7:
                continue
            fmt = self.p.cmdtext(cid)
            if not fmt or fmt.startswith('<cmd'):
                continue
            parts = re.split(r'(%\d)', fmt)
            rx, order = '', []
            pars = self.p.pars(cid)
            for k, part in enumerate(parts):
                if re.fullmatch(r'%\d', part):
                    n = int(part[1])
                    order.append(n)
                    pt = pars[n] if n < len(pars) else None
                    if pt is not None and self.pf.get(pt + 1, 0) & 131072:
                        rx += RESULT_RX
                    elif pt == 20:
                        rx += r'((?:\S+\s*->)?)'
                    else:
                        rx += r"('[^']*'|.*?)"        # a quoted string is one value even with commas
                else:
                    if part.strip():
                        lit = r'\s*'.join(re.escape(w) for w in part.split())
                        if part[:1] == ' ' and part.strip()[0].isalnum():
                            lit = r'\b' + lit           # a separate word never starts inside a value ($target.owner)
                        if part[-1:] == ' ' and part.strip()[-1].isalnum():
                            lit += r'\b'
                        rx += r'\s*' + lit + r'\s*'
                    else:
                        rx += r'\s*'
            lits = [w for p in parts if not re.fullmatch(r'%\d', p) for w in p.split()]
            weight = sum(len(w) for w in lits)
            self.templates.append((cid, re.compile(r'^\s*' + rx + r'\s*$'), order, lits[0] if lits else '', weight))
        self.by_word = {}
        for t in self.templates:
            self.by_word.setdefault(t[3], []).append(t)
        self.scriptargs = {}

    # ---- values ------------------------------------------------------------------------------------------------
    def var(self, name):
        if name not in self.vars:
            self.vars[name] = len(self.vars)
        return self.vars[name]

    def operand(self, s, partype=None):
        """One shown value -> (data type, value)."""
        s = s.strip()
        if s.startswith('$'):
            if not re.fullmatch(VAR_RX, s):
                raise AsmError('bad variable name %r (letters, digits, "." and "_")' % s)
            return (131074, self.var(s[1:]))
        if s == 'null' or s == '':          # parameters the text does not show are empty pairs
            return (0, 0)
        if re.fullmatch(r'-?\d+', s):
            return (4, int(s))
        if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
            return (5, s[1:-1])
        if s.startswith('[') and s.endswith(']') and s[1:-1] in self.consts:
            return (131075, self.consts[s[1:-1]])
        c = self.names.get(s)
        if c:
            return self.pick(c, partype)
        raise AsmError('unknown value %r' % s)

    PREFER = {13: 9, 42: 9, 19: 9, 17: 9, 27: 9, 23: 12, 16: 10, 29: 14, 31: 18, 33: 18, 32: 19, 44: 20, 34: 25,
              24: 11}

    def pick(self, cands, partype):
        want = self.PREFER.get(partype)
        for c in cands:
            if c[0] == want:
                return c
        return cands[0]

    def retvar(self, s):
        s = s.strip()
        if s in ('', '='):
            return NOVAR, None
        if s == 'START':
            return START, None
        for f in sorted(FORMS, key=len, reverse=True):
            if s == f:
                return None, FORMS[f]
        m = re.fullmatch(r'\$([\w.]+)\s*=', s)
        if m:
            return self.var(m.group(1)), None
        raise AsmError('bad result %r' % s)

    # ---- expressions -------------------------------------------------------------------------------------------
    def tokens(self, s):
        out, i = [], 0
        while i < len(s):
            if s[i].isspace():
                i += 1
                continue
            if s[i] == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise AsmError('unterminated string at %r' % s[i:])
                out.append(('v', s[i:j + 1]))
                i = j + 1
                continue
            m = re.match(VAR_RX + r'(?=\s|$|\))|\[[^\]]+\]|-?\d+(?=\s|$|\))', s[i:])
            if m:
                out.append(('v', m.group()))
                i += m.end()
                continue
            for op in OP_TOKENS:
                if s.startswith(op, i) and (not op[0].isalpha() or not s[i + len(op):i + len(op) + 1].isalnum()):
                    out.append(('o', op))
                    i += len(op)
                    break
            else:
                for n in self.name_list:
                    if s.startswith(n, i) and (i + len(n) == len(s) or s[i + len(n)] == ' '):
                        out.append(('v', n))
                        i += len(n)
                        break
                else:
                    if s.startswith('null', i):
                        out.append(('v', 'null'))
                        i += 4
                        continue
                    raise AsmError('cannot read expression at %r' % s[i:])
        toks = []
        for k, (kind, t) in enumerate(out):
            if kind == 'v':
                toks.extend(self.operand(t))
            else:
                toks.extend((15, OPS[t]))
        return toks

    def expression(self, s):
        toks = self.tokens(s)
        if not toks or (toks[-2] == 15 and toks[-1] != 17):
            # the editor's parser accepts a trailing operator; the VM would then run short of operands
            raise AsmError('expression %r is empty or ends with an operator' % s)
        operands, ops = [], []
        r = self._tree(toks, 0, len(toks), operands, ops)
        if r != -1:
            raise AsmError('bad expression %r' % s)
        flat = []
        for a, b in operands:
            flat += [a, b]
        return [len(operands)] + flat + [len(ops)] + ops

    def _level(self, accept, sub):
        def f(t, pos, end, operands, ops):
            pos = sub(t, pos, end, operands, ops)
            while True:
                if pos < 0:
                    return pos
                if t[pos] != 15 or t[pos + 1] not in accept:
                    return pos
                v = t[pos + 1]
                ops.append(v)
                pos = sub(t, pos + 2, end, operands, ops)
                if pos == -2:
                    return pos
                operands.append((15, v))
        return f

    def _oneop(self, t, pos, end, operands, ops):
        if pos >= end:
            return -1
        pre = []
        while pos < end and t[pos] == 15 and t[pos + 1] in (65555, 65554, 65556, 12, 11):
            v = 65555 if t[pos + 1] == 12 else t[pos + 1]
            ops.append(v)
            pos += 2
            if v != 11:
                pre.append((15, v))
        pos = self._brace(t, pos, end, operands, ops)
        for x in reversed(pre):
            operands.append(x)
        return pos

    def _brace(self, t, pos, end, operands, ops):
        if pos >= end:
            return -2
        if t[pos] == 15 and t[pos + 1] == 16:
            ops.append(16)
            pos = self._tree(t, pos + 2, end, operands, ops)
            if pos < 0:
                return -2
            if t[pos] != 15 or t[pos + 1] != 17:
                return -2
            pos += 2
            ops.append(17)
        elif t[pos] != 15:
            ops.append(-1 - len(operands))
            operands.append((t[pos], t[pos + 1]))
            pos += 2
        else:
            return -2
        return -1 if pos >= end else pos

    def _tree(self, t, pos, end, operands, ops):
        md = self._level({13, 14, 15}, self._oneop)
        asb = self._level({11, 12}, md)
        comp = self._level({2, 3, 4, 5}, asb)
        eq = self._level({0, 1}, comp)
        band = self._level({6}, eq)
        bxor = self._level({8}, band)
        bor = self._level({7}, bxor)
        land = self._level({9}, bor)
        lor = self._level({10}, land)
        return lor(t, pos, end, operands, ops)

    # ---- one command line ------------------------------------------------------------------------------------
    def command(self, text):
        """-> (code line, condition form or None)."""
        text = text.strip()
        cands = [t for w, ts in self.by_word.items() if w in text for t in ts]
        best, err = None, None
        saved = dict(self.vars)
        for cid, rx, order, _, weight in cands:
            m = rx.match(text)
            if not m:
                continue
            self.vars = dict(saved)             # trying a template must not create variables
            try:
                line, form = self.build(cid, dict(zip(order, m.groups())))
            except AsmError as x:
                err = x
                continue
            if best is None or weight > best[2]:
                best = (line, form, weight, self.vars)
        self.vars = best[3] if best else saved
        if best is None:
            # expression line: <result> <expression>
            m = re.match(r'^(\$\S+\s*=|=|START|while not|while|skip if not|skip if|do if|else if not|else if|if not|if)\s*(.*)$', text)
            if m:
                rv, form = self.retvar(m.group(1))
                return ([104, rv if form is None else form] + self.expression(m.group(2)),
                        None if form is None else (form, 1))
            raise err or AsmError('no command matches %r' % text)
        return best[0], best[1]

    def build(self, cid, groups):
        pars = self.p.pars(cid)
        line = [cid]
        form = None
        for k, p in enumerate(pars):
            s = groups.get(k, '')
            f = self.pf.get(p + 1, 0)
            if cid == 102 and p == 5:
                args = self.call_args(groups.get(0, ''), s)
                line.append(len(args))
                for a in args:
                    line.extend(self.operand(a))
                continue
            if cid == 104 and p == 30:
                line.extend(self.expression(s))
                continue
            if f & 65536 or p == 4:
                if p == 20:
                    s = re.sub(r'\s*->\s*$', '', s)
                line.extend(self.operand(s, p))
                continue
            if f & 131072:
                v, fm = self.retvar(s)
                if fm is not None:
                    form = (fm, len(line))          # form and its slot in the line
                    v = fm
                line.append(v)
                continue
            if p == 3:
                if not re.fullmatch(r'-?\d+', s.strip()):
                    raise AsmError('number expected')
                line.append(int(s))
            elif p == 1:
                line.append(('label', s.strip()))
            else:
                line.append(s.strip())
        return line, form

    def call_args(self, script, s):
        """Shown arguments `desc=value  desc=value ...` (as many as the caller passes; the callee may have more)."""
        names = self.scriptargs.get(script.strip())
        s = s.strip()
        if not s:
            return []
        if names is None or not s.startswith(names[0] + '=') if names else True:
            # callee unknown (shown as ???=): values are separated by two spaces
            parts = re.split(r'\s{2,}', s)
            out = []
            for p in parts:
                if '=' not in p:
                    raise AsmError('argument expected in %r' % p)
                out.append(p.split('=', 1)[1])
            return out
        out = []
        for k, n in enumerate(names):
            pre = n + '='
            if not s:
                break
            if not s.startswith(pre):
                raise AsmError('argument %s expected' % n)
            s = s[len(pre):]
            nxt = names[k + 1] + '=' if k + 1 < len(names) else None
            j = s.find(' ' + nxt) if nxt else -1
            if j >= 0:
                out.append(s[:j])
                s = s[j + 1:].lstrip()
            else:
                out.append(s.strip())
                s = ''
        return out

    # ---- whole script ------------------------------------------------------------------------------------------
    def script(self, lines, argnames):
        self.vars = {}
        for a in argnames:
            self.var(a)
        code, extra, stack, labels = [], [], [], {}
        lines = list(lines)
        while lines and lines[0] == '':         # the CDATA line breaks, not empty editor lines
            lines.pop(0)
        while lines and lines[-1] == '':
            lines.pop()

        def here():
            return len(code)
        for raw in lines:
            try:
                self.line(raw, code, extra, stack, labels, here)
            except AsmError as x:
                raise AsmError('%s  [line: %s]' % (x, raw.strip()))
        if stack:
            raise AsmError('%s without end' % stack[-1]['kind'])

        def resolve(ln, commented):
            for k, v in enumerate(ln):
                if isinstance(v, tuple) and v[0] == 'label':
                    if v[1] in labels:
                        ln[k] = labels[v[1]]
                    elif commented:
                        ln[k] = v[1]    # the game keeps a commented-out goto to a missing label as its name
                    else:
                        raise AsmError('undefined label %r' % v[1])
        for ln in code:
            resolve(ln, False)
        for ln in extra:
            resolve(ln, True)
        names = [None] * len(self.vars)
        for n, i in self.vars.items():
            names[i] = n
        return names, code, extra

    def line(self, raw, code, extra, stack, labels, here):
        """One editor line -> code lines and editor-only lines; checks that blocks nest like the editor's."""
        s = raw.strip()
        if s.startswith('@'):
            s = s[1:].strip()
        if not s:
            extra.append([here(), 2])
            return
        if s.startswith('* ') or s == '*':
            extra.append([here(), 1, s[2:]])
            return
        if s[1:] in ('end', 'else', 'continue', 'break') and s.startswith('*'):
            extra.append([here(), 3, {'end': 4, 'else': 5, 'continue': 6, 'break': 7}[s[1:]]])
            return
        if s.startswith('*'):
            saved = dict(self.vars)
            ln, _ = self.command(s[1:])
            self.vars = saved
            extra.append([here(), 3] + ln)
            return
        if s == 'end':
            if not stack:
                raise AsmError('end without if/while')
            top = stack.pop()
            if top['kind'] == 'while':
                code.append([112, top['line']])
            if top['cond'] is not None:
                top['cond_target'](here())
            for g in top['gotos']:
                code[g][1] = here()
            extra.append([here(), 4])
            return
        if s == 'else' or re.match(r'else if\b', s):
            if not stack or stack[-1]['kind'] != 'if':
                raise AsmError('%s without if' % s.split()[0])
            top = stack[-1]
            if top['cond'] is None:
                raise AsmError('%s after else' % ('else' if s == 'else' else 'else if'))
            top['gotos'].append(here())
            code.append([112, 0])
            top['cond_target'](here())
            top['cond'] = None
            if s == 'else':
                extra.append([here(), 5])
                return
        elif s in ('break', 'continue'):
            loop = next((b for b in reversed(stack) if b['kind'] == 'while'), None)
            if loop is None:
                raise AsmError('%s outside while' % s)
            if s == 'break':
                loop['gotos'].append(here())
                code.append([112, 0])
            else:
                code.append([112, loop['line']])
            extra.append([here(), 7 if s == 'break' else 6])
            return
        ln, form = self.command(s)
        idx = here()
        code.append(ln)
        if ln[0] == 101:
            if ln[1] in labels:
                raise AsmError('label %s defined twice' % ln[1])
            labels[ln[1]] = idx
        if form is None:
            return
        form, slot = form

        def setter(i=idx, k=slot, f=form):
            return lambda t: code[i].__setitem__(k, form_value(f, t))
        if form in (7, 8):
            setter()(idx + 2)
        elif form in (3, 4):
            stack.append({'kind': 'if', 'gotos': [], 'cond': idx, 'cond_target': setter()})
        elif form in (5, 6):
            if not stack or stack[-1]['kind'] != 'if':
                raise AsmError('else if without if')
            stack[-1]['cond'] = idx
            stack[-1]['cond_target'] = setter()
        elif form in (9, 10):
            stack.append({'kind': 'while', 'line': idx, 'gotos': [], 'cond': idx, 'cond_target': setter()})


def compare(asm, code_ref):
    names, code, extra = asm
    return names == code_ref[5], code, extra


def sval_xml(v, d=1):
    pad = '  ' * d
    if isinstance(v, list):
        return '%s<sval type="array" size="%d">\n%s%s</sval>\n' % (pad, len(v), ''.join(sval_xml(c, d + 1) for c in v),
                                                                 pad)
    if isinstance(v, int):
        return '%s<sval type="int" val="%d"/>\n' % (pad, v)
    return '%s<sval type="string" val="%s"/>\n' % (pad, msci.html.escape(v, quote=True))


def write_script(path_xml, path_txt, A):
    """Template script XML (name, description, arguments, version) + edited text -> new script XML on stdout."""
    raw = open(path_xml, encoding='utf-8', errors='replace').read()
    code, _ = msci.read_script(path_xml)
    lines = open(path_txt, encoding='utf-8-sig').read().replace('\r', '').split('\n')
    lines = [l for l in lines if not l.lstrip().startswith('//')]       # the header `msci.py dis` prints
    if any(']]>' in l for l in lines):
        raise AsmError(']]> cannot stand in the script text (it would end the CDATA section)')
    args = code[5][:len(code[7] or [])] if isinstance(code[5], list) else []
    names, body, extra = A.script(lines, args)
    # the game writes empty arrays as int 0
    new = [code[0], code[1], code[2], code[3], code[4], names or 0, body, code[7] or 0, extra or 0,
           code[9] if len(code) > 9 else 0]
    head = raw[:raw.index('<sourceplaintext>')] if '<sourceplaintext>' in raw else raw[:raw.index('<codearray>')]
    out = (head + '<sourceplaintext>\n<![CDATA[\n' + '\n'.join(lines).strip('\n') + '\n]]>\n</sourceplaintext>\n'
           + '<codearray>\n' + sval_xml(new, 0) + '</codearray>\n</script>\n')
    sys.stdout.buffer.write(out.replace('\n', '\r\n').encode('utf-8'))


def main():
    cmd = sys.argv[1]
    db = json.load(open(sys.argv[2], encoding='utf-8'))
    A = Assembler(db)
    if cmd == 'asm':
        import glob
        import os
        for f in glob.glob(os.path.join(os.path.dirname(os.path.abspath(sys.argv[3])), '*.xml')):
            c, _ = msci.read_script(f)
            if c:
                raw = open(f, encoding='utf-8', errors='replace').read()
                A.scriptargs[c[0]] = [msci.html.unescape(d) for d in re.findall(r'<argument [^>]*desc="([^"]*)"', raw)]
        try:
            write_script(sys.argv[3], sys.argv[4], A)
        except AsmError as x:
            print('error:', x, file=sys.stderr)
            return 1
        return
    if cmd == 'check':
        import glob
        import os
        from collections import Counter
        files = sorted(glob.glob(os.path.join(sys.argv[3], '*.xml')))
        scripts = []
        for f in files:
            code, plain = msci.read_script(f)
            if code and plain is not None:
                scripts.append((f, code, plain))
                A.scriptargs[code[0]] = [a for a in re.findall(r'desc="([^"]*)"', '')]
        for f, code, plain in scripts:
            raw = open(f, encoding='utf-8', errors='replace').read()
            A.scriptargs[code[0]] = [msci.html.unescape(d) for d in re.findall(r'<argument [^>]*desc="([^"]*)"', raw)]
        stats = Counter()
        shown = 0
        for f, code, plain in scripts:
            if not isinstance(code[5], list):
                code[5] = []
            args = code[5][:len(code[7] or [])]
            try:
                names, mine, extra = A.script(plain, args)
            except Exception as x:
                stats['script failed'] += 1
                if shown < int(os.environ.get('SHOW', '10')):
                    shown += 1
                    print('FAIL %s: %s' % (os.path.basename(f), x))
                continue
            ok = mine == code[6] and names == code[5] and extra == (code[8] or [])
            stats['scripts equal' if ok else 'scripts different'] += 1
            stats['vars equal' if names == code[5] else 'vars different'] += 1
            stats['extras equal' if extra == (code[8] or []) else 'extras different'] += 1
            for a, b in zip(mine, code[6]):
                stats['lines equal' if a == b else 'lines different'] += 1
                if a != b and shown < int(os.environ.get('SHOW', '10')):
                    shown += 1
                    print('%s\n  mine: %s\n  game: %s' % (os.path.basename(f), a, b))
            if len(mine) != len(code[6]):
                stats['line count differs'] += 1
                stats['lines different'] += abs(len(mine) - len(code[6]))
        for k, v in sorted(stats.items()):
            print('%8d  %s' % (v, k))


if __name__ == '__main__':
    sys.exit(main())
