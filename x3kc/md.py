"""Mission Director XML <-> a compact C-like text form (.xmd), lossless in both directions.

    x3kc md dis <file.xml | dir> [-o out]       director XML -> text (checks that the text builds the same bytes)
    x3kc md asm <file.xmd | dir> [-o out]       text -> director XML (unknown names, braces, cue references checked)
    x3kc md check <file.xml | dir>...           dis + asm in memory, compare byte for byte (no files written)
    x3kc md schema <out.json> <director dir>... element/attribute table from director.xsd + the stock files

The text form keeps every byte of the XML: the prolog, the indentation (a line that is not indented the way its
neighbours predict carries a `~"..."` mark), blank lines, comments, attribute order and spacing. The manual is
docs/md.md.
"""
import argparse
import json
import os
import re
import sys

EXT = '.xmd'
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'md_schema.json')


class MdError(Exception):
    pass


# ---------------------------------------------------------------- XML layer (byte exact)

class Node:
    """kind 'el' | 'com' | 'text'. attrs: [sep, key, eq, quote, raw value]. pre: the text before the node inside its
    parent (whitespace only); close_pre: the text before the end tag. Text-form annotations: nl (newlines before the
    statement), ind (~"..."), raw (~~"..."), inline (a comment on the line of the previous token)."""
    __slots__ = ('kind', 'name', 'attrs', 'tail', 'selfclose', 'children', 'pre', 'close_pre', 'text', 'indent',
                 'nl', 'ind', 'raw', 'inline', 'close_nl', 'close_ind', 'close_raw', 'line', 'literal', 'hidden',
                 'endtail')

    def __init__(self, kind, name=None):
        self.kind, self.name = kind, name
        self.attrs, self.tail, self.selfclose, self.children = [], '', False, []
        self.pre, self.close_pre, self.text, self.indent = '', '', '', ''
        self.nl, self.ind, self.raw, self.inline = 1, None, None, None
        self.close_nl, self.close_ind, self.close_raw = 1, None, None
        self.line, self.literal, self.hidden, self.endtail = 0, False, False, ''

    def attr(self, key):
        for a in self.attrs:
            if a[1] == key:
                return a[4]
        return None

    def keys(self):
        return [a[1] for a in self.attrs]


class Doc:
    def __init__(self):
        self.bom, self.encoding, self.prolog, self.root, self.epilog = False, 'ISO-8859-1', '', None, None
        self.eol, self.unit = '\n', '  '


TOK_RX = re.compile(r'<!--(.*?)-->|<\?(.*?)\?>|<([\w:.-]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'))*)(\s*)(/?)>'
                    r'|</([\w:.-]+)(\s*)>|([^<]+)', re.S)
ATTR_RX = re.compile(r'(\s+)([\w:.-]+)(\s*=\s*)("[^"]*"|\'[^\']*\')')
ENC_RX = re.compile(r'encoding\s*=\s*["\']([^"\']+)["\']')


def py_encoding(enc):
    e = enc.lower().replace('_', '-')
    if e in ('iso-8859-1', 'latin1', 'latin-1', 'l1'):
        return 'latin-1'
    return e


def parse_xml(data, name='<xml>'):
    """bytes -> Doc. Only what director files use: elements, attributes, comments, PIs before the root, text."""
    doc = Doc()
    if data.startswith(b'\xef\xbb\xbf'):
        doc.bom, data = True, data[3:]
    m = ENC_RX.search(data[:200].decode('latin-1'))
    doc.encoding = m.group(1) if m else 'utf-8'
    try:
        s = data.decode(py_encoding(doc.encoding))
    except (UnicodeDecodeError, LookupError) as e:
        raise MdError('%s: cannot decode as %s: %s' % (name, doc.encoding, e))
    crlf = s.count('\r\n')
    doc.eol = '\r\n' if crlf and crlf * 2 >= s.count('\n') else '\n'
    pos, n = 0, len(s)
    stack, root, chunk = [], None, ''
    line, lpos = 1, 0       # (the 1-based line at offset lpos: each node's start line, for the cue map)
    while pos < n:
        m = TOK_RX.match(s, pos)
        if not m:
            raise MdError('%s: unsupported XML at offset %d: %r' % (name, pos, s[pos:pos + 40]))
        line += s.count('\n', lpos, m.start())
        lpos = m.start()
        pos = m.end()
        if m.group(9) is not None:
            chunk += m.group(9)
            continue
        if root is None:
            if m.group(3) is None:          # prolog: PIs and whitespace, kept verbatim
                chunk += m.group(0)
                continue
            doc.prolog, chunk = chunk, ''
        if m.group(3) is not None or m.group(1) is not None:
            if root is not None and not stack:
                raise MdError('%s: content after the root element' % name)
            node = Node('el', m.group(3)) if m.group(3) is not None else Node('com')
            node.line = line
            if node.kind == 'com':
                node.text = m.group(1)
            else:
                node.attrs = [[a.group(1), a.group(2), a.group(3), a.group(4)[0], a.group(4)[1:-1]]
                              for a in ATTR_RX.finditer(m.group(4))]
                node.tail, node.selfclose = m.group(5), m.group(6) == '/'
            if stack:
                attach(stack[-1], chunk, node)
            chunk = ''
            if root is None:
                root = node
            if node.kind == 'el' and not node.selfclose:
                stack.append(node)
            continue
        if m.group(7) is not None:
            if not stack or stack[-1].name != m.group(7):
                raise MdError('%s: unexpected </%s>' % (name, m.group(7)))
            el = stack.pop()
            el.endtail = m.group(8)
            if chunk.strip():
                t = Node('text')
                t.text = chunk
                el.children.append(t)
                chunk = ''
            el.close_pre, chunk = chunk, ''
            if not stack:
                doc.root = root
            continue
        raise MdError('%s: unsupported XML (%r) at offset %d' % (name, m.group(0)[:30], m.start()))
    if stack or root is None:
        raise MdError('%s: unterminated document' % name)
    doc.epilog = chunk
    return doc


def attach(parent, chunk, node):
    if chunk.strip():
        t = Node('text')
        t.text = chunk
        parent.children.append(t)
        chunk = ''
    node.pre = chunk
    parent.children.append(node)


def encodable(s, codec):
    try:
        s.encode(codec)
        return True
    except UnicodeEncodeError:
        return False


def write_xml(doc, fname='<text>'):
    out = [doc.prolog]
    codec = py_encoding(doc.encoding)

    def ser(e):
        out.append(e.pre)
        if e.kind in ('com', 'text'):
            if not encodable(e.text, codec):
                bad = next(ch for ch in e.text if not encodable(ch, codec))
                raise MdError('%s:%d: error: %r (U+%04X) cannot be written in %s in a %s'
                              % (fname, e.line, bad, ord(bad), doc.encoding,
                                 'comment' if e.kind == 'com' else 'text'))
            out.append('<!--%s-->' % e.text if e.kind == 'com' else e.text)
            return
        out.append('<' + e.name)
        for sep, k, eq, q, v in e.attrs:
            if not encodable(v, codec):         # a character the encoding lacks: a character reference
                v = ''.join(ch if encodable(ch, codec) else '&#x%X;' % ord(ch) for ch in v)
            out.append('%s%s%s%s%s%s' % (sep, k, eq, q, v, q))
        out.append(e.tail)
        if e.selfclose:
            out.append('/>')
            return
        out.append('>')
        for c in e.children:
            ser(c)
        out.append(e.close_pre + '</%s%s>' % (e.name, e.endtail))

    ser(doc.root)
    out.append(doc.epilog)
    data = ''.join(out).encode(codec)
    return (b'\xef\xbb\xbf' if doc.bom else b'') + data


# ---------------------------------------------------------------- whitespace model shared by dis and asm

WS_RX = re.compile(r'[ \t\r\n]*\Z')
INDENT_RX = re.compile(r'[ \t]*\Z')


def unit_after(indent, unit):
    return indent + ('\t' if indent.endswith('\t') else unit)


def indent_of(pre, pred):
    return pre[pre.rfind('\n') + 1:] if '\n' in pre else pred


def choose_unit(doc):
    best, best_n = '  ', -1
    for unit in ('  ', '    ', '\t'):
        n = 0

        def walk(e, ind):
            nonlocal n
            pred = unit_after(ind, unit)
            for c in e.children:
                if c.kind == 'text':
                    continue
                if c.pre == doc.eol + pred:
                    n += 1
                pred = indent_of(c.pre, pred)
                if c.kind == 'el':
                    walk(c, pred)
        walk(doc.root, '')
        if n > best_n:
            best, best_n = unit, n
    return best


# ---------------------------------------------------------------- schema

_SCHEMA = None


def schema():
    global _SCHEMA
    if _SCHEMA is None:
        with open(DATA, encoding='utf-8') as f:
            _SCHEMA = json.load(f)['elements']
    return _SCHEMA


def positional(name):
    e = schema().get(name)
    return e['pos'] if e else []


def build_schema(dirs):
    """Element -> attributes from director.xsd (+ dirobjdb.xsd) of every given director directory, united with what
    the stock files use; the positional attribute is the one that comes first in >= 80 % of the stock uses."""
    import xml.etree.ElementTree as ET
    XS = '{http://www.w3.org/2001/XMLSchema}'
    elements = {}

    def add(name, attrs):
        elements.setdefault(name, set()).update(attrs)

    for d in dirs:
        for fn in ('director.xsd', 'dirobjdb.xsd'):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            tree = ET.parse(p).getroot()
            agroups = {g.get('name'): g for g in tree.iter(XS + 'attributeGroup') if g.get('name')}
            ctypes = {g.get('name'): g for g in tree.iter(XS + 'complexType') if g.get('name')}

            def attrs_of(node, seen):
                out = set()
                for c in node:
                    if c.tag == XS + 'attribute':
                        if c.get('name') or c.get('ref'):
                            out.add(c.get('name') or c.get('ref'))
                    elif c.tag == XS + 'attributeGroup' and c.get('ref'):
                        r = c.get('ref')
                        if r in agroups and ('g', r) not in seen:
                            out |= attrs_of(agroups[r], seen | {('g', r)})
                    elif c.tag in (XS + 'complexContent', XS + 'simpleContent', XS + 'extension',
                                   XS + 'restriction'):
                        b = c.get('base')
                        if b in ctypes and ('t', b) not in seen:
                            out |= attrs_of(ctypes[b], seen | {('t', b)})
                        out |= attrs_of(c, seen)
                    elif c.tag == XS + 'complexType':
                        out |= attrs_of(c, seen)
                return out
            for el in tree.iter(XS + 'element'):
                n = el.get('name')
                if not n:
                    continue
                a = attrs_of(el, set())
                if el.get('type') in ctypes:
                    a |= attrs_of(ctypes[el.get('type')], {('t', el.get('type'))})
                add(n, a)
    first, count = {}, {}
    for d in dirs:
        for f in sorted(os.listdir(d)):
            if not f.lower().endswith('.xml'):
                continue
            with open(os.path.join(d, f), 'rb') as fh:
                data = fh.read()
            if b'<director' not in data:
                continue
            doc = parse_xml(data, f)

            def walk(e):
                if e.kind != 'el':
                    return
                add(e.name, e.keys())
                count[e.name] = count.get(e.name, 0) + 1
                if e.attrs:
                    k = (e.name, e.attrs[0][1])
                    first[k] = first.get(k, 0) + 1
                for c in e.children:
                    walk(c)
            walk(doc.root)
    out = {}
    for n in sorted(elements):
        pos = []
        if n == 'cue':
            pos = ['name']
        elif n in count:
            cands = [(v, k[1]) for k, v in first.items() if k[0] == n]
            if cands:
                v, a = max(cands)
                if v >= 0.8 * count[n] and a not in ('comment', 'chance', 'negate', 'exact', 'min', 'max', 'list'):
                    pos = [a]
        out[n] = {'attrs': sorted(elements[n]), 'pos': pos}
    return {'note': 'generated by x3kc md schema from director.xsd/dirobjdb.xsd (AP, FL) and the stock files',
            'elements': out}


# ---------------------------------------------------------------- values

IDENT = r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*'
T_VAL = re.compile(r'\{value@(' + IDENT + r')\}(?!\w|\.[A-Za-z_])')
T_PAR = re.compile(r'(?<![\w}])\{param@(' + IDENT + r')\}(?!\w|\.[A-Za-z_])')
I_VAL = re.compile(r'\$(' + IDENT + ')')
I_PAR = re.compile(r'(?<![\w}])@(' + IDENT + ')')
BARE_CH = r'A-Za-z0-9_.{}@|+\-*%?$/'
BARE_RX = re.compile(r'[' + BARE_CH + r']+\Z')
KEYWORDS = {'if', 'else', 'in', 'cue', 'random'}
ASSIGN_OPS = {'add': '+=', 'subtract': '-=', 'multiply': '*=', 'divide': '/=', 'set': ':='}
OP_OF = {v: k for k, v in ASSIGN_OPS.items()}


def sugar(s):
    return T_PAR.sub(r'@\1', T_VAL.sub(r'$\1', s))


def unsugar(s):
    return I_PAR.sub(r'{param@\1}', I_VAL.sub(r'{value@\1}', s))


def xml_unescape(raw):
    if '&' not in raw:
        return raw

    def rep(m):
        t = m.group(1)
        if t.startswith('#x'):
            return chr(int(t[2:], 16))
        if t.startswith('#'):
            return chr(int(t[1:]))
        return {'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'"}.get(t, m.group(0))
    return re.sub(r'&(#x[0-9a-fA-F]+|#\d+|\w+);', rep, raw)


TEXT_ELEMENTS = {'signature'}      # the only elements with text content in the stock files


def text_token(raw):
    """Element text (raw XML) -> a string token: decoded "..." when that escapes back the same, else raw r"..."."""
    u = xml_unescape(raw)
    return cstr(u) if xml_escape_text(u) == raw else 'r' + cstr(raw)


def xml_escape_text(s):
    return s.replace('&', '&amp;').replace('<', '&lt;')


def xml_escape(s, q='"'):
    s = s.replace('&', '&amp;').replace('<', '&lt;')
    return s.replace('"', '&quot;') if q == '"' else s.replace("'", '&apos;')


def cstr(s):
    out = []
    for ch in s:
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append('\\x%02x' % ord(ch))
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def bare_ok(t):
    return (bool(BARE_RX.match(t)) and t not in KEYWORDS and '//' not in t and '/*' not in t
            and not t.startswith('}') and not t.startswith('{}') and not t.startswith('{/') and t != '{'
            and t[:2] not in ('+=', '-=', '*=', '/=', '++', '--'))


def fmt_value(s):
    """Attribute value (decoded) -> text token."""
    t = sugar(s)
    if unsugar(t) != s:
        return 'r' + cstr(s)
    return t if bare_ok(t) else cstr(t)


def std_attr(a):
    """An attribute written the usual way: one space before, key="value", value escaped the usual way."""
    return a[2] == '=' and a[3] == '"' and xml_escape(xml_unescape(a[4])) == a[4]


def plain(e):
    return e.tail == '' and all(a[0] == ' ' and std_attr(a) for a in e.attrs)


# ---------------------------------------------------------------- dis

class Printer:
    def __init__(self, doc):
        self.doc, self.lines, self.eol, self.unit = doc, [], doc.eol, doc.unit
        self.trailer = False        # the last line ends with a /// comment

    def emit(self, depth, text, trailer=False):
        self.lines.append('  ' * depth + text)
        self.trailer = trailer

    def blank(self, k):
        for _ in range(k):
            self.lines.append('')

    # -- whitespace classification
    def classify(self, pre, pred):
        """-> (blank lines, annotation text or '', inline ws or None)."""
        m = re.fullmatch('((?:%s)+)([ \t]*)' % re.escape(self.eol), pre)
        if m:
            k = len(m.group(1)) // len(self.eol)
            return k - 1, ('' if m.group(2) == pred else '~%s ' % cstr(m.group(2))), None
        return 0, '~~%s ' % cstr(pre), None

    # -- arguments
    def args(self, e, attrs=None, ops=True, allow_comment=True):
        """-> (argument text without parens, '///' trailer or None)."""
        attrs = e.attrs if attrs is None else attrs
        pos = positional(e.name)
        trailer = None
        if (allow_comment and attrs and attrs[-1][1] == 'comment' and attrs[-1][0] == ' ' and std_attr(attrs[-1])
                and '\n' not in attrs[-1][4] and '\r' not in attrs[-1][4]
                and attrs[-1][4] == attrs[-1][4].rstrip()):
            trailer = '///' + ((' ' + xml_unescape(attrs[-1][4])) if attrs[-1][4] else '')
            attrs = attrs[:-1]
        items, i, npos = [], 0, 0
        while i < len(attrs):
            a = attrs[i]
            pre = '' if a[0] == ' ' else '~%s ' % cstr(a[0])
            if not std_attr(a):
                items.append('`%s`' % ''.join([a[0], a[1], a[2], a[3], a[4], a[3]]))
                i += 1
                continue
            v = fmt_value(xml_unescape(a[4]))
            if npos == i and npos < len(pos) and a[1] == pos[npos]:
                npos += 1
                text = pre + v
                if ops and a[1] == 'value':
                    j = i + 1

                    def ok(k, key):
                        return k < len(attrs) and attrs[k][1] == key and attrs[k][0] == ' ' and std_attr(attrs[k])
                    while True:
                        if ok(j, 'exact'):
                            if ok(j + 1, 'negate') and attrs[j + 1][4] == '1':
                                text += ' != ' + fmt_value(xml_unescape(attrs[j][4]))
                                j += 2
                            else:
                                text += ' == ' + fmt_value(xml_unescape(attrs[j][4]))
                                j += 1
                        elif ok(j, 'min') and ok(j + 1, 'max'):
                            text += ' in [%s, %s]' % (fmt_value(xml_unescape(attrs[j][4])),
                                                      fmt_value(xml_unescape(attrs[j + 1][4])))
                            j += 2
                        elif ok(j, 'min'):
                            text += ' >= ' + fmt_value(xml_unescape(attrs[j][4]))
                            j += 1
                        elif ok(j, 'max'):
                            text += ' <= ' + fmt_value(xml_unescape(attrs[j][4]))
                            j += 1
                        else:
                            break
                    items.append(text)
                    i = j
                    continue
                items.append(text)
            else:
                npos = len(pos) + 1
                items.append('%s%s: %s' % (pre, a[1], v))
            i += 1
        text = ', '.join(items)
        if e.tail:
            text += (' ' if text else '') + '~%s' % cstr(e.tail)
        return text, trailer

    def head(self, e, prefix=''):
        a, trailer = self.args(e)
        return prefix + e.name + ('(%s)' % a if a else ''), trailer

    # -- statements
    def children(self, e, kids, depth, pred):
        """Print kids (children of e, or of a hidden wrapper) at text depth; pred: predicted indent of the first."""
        prev_text = False
        for c in kids:
            if c.kind == 'text':
                self.emit(depth, text_token(c.text) + ';')
                prev_text = True
                continue
            if prev_text:
                ann, blank = ('' if c.pre == '' else '~~%s ' % cstr(c.pre)), 0
                c.indent = indent_of(c.pre, pred)
            elif (c.kind == 'com' and re.fullmatch('[ \t]*', c.pre) and self.lines and not self.trailer
                  and self.lines[-1].strip() and not self.lines[-1].lstrip().startswith('#')):
                c.indent = pred
                txt = self.comment_text(c)
                self.lines[-1] += c.pre + txt
                self.trailer = txt.startswith('//')
                continue
            else:
                blank, ann, _ = self.classify(c.pre, pred)
                c.indent = indent_of(c.pre, pred)
            prev_text = False
            self.blank(blank)
            self.stmt(c, depth, ann, e.name)
            pred = c.indent

    def comment_text(self, c):
        t = c.text
        if self.eol == '\r\n':
            t = t.replace('\r\n', '\n')
        if ('\r' in t or '*/' in t or t.startswith('~') or '\x00' in t
                or (self.eol == '\r\n' and '\n' in c.text.replace('\r\n', ''))):
            return '//~' + cstr(c.text)          # line ends the file does not use: kept as an escaped string
        if '\n' not in t and t == t.rstrip() and not t.startswith('/') and not t.startswith('*'):
            return '//' + t
        return '/*' + t + '*/'

    def close(self, e, depth):
        """The closing brace of e's block (e.close_pre)."""
        et = '~>%s ' % cstr(e.endtail) if e.endtail else ''
        if not e.children:
            if e.close_pre == '':
                self.lines[-1] += (' ' + et if et else '') + '}'
            else:
                self.lines[-1] += ' ~~%s %s}' % (cstr(e.close_pre), et)
            return
        if e.children and e.children[-1].kind == 'text':
            if e.close_pre:
                self.emit(depth, '~~%s %s}' % (cstr(e.close_pre), et))
            else:
                self.emit(depth, et + '}')
            return
        blank, ann, _ = self.classify(e.close_pre, e.indent)
        self.blank(blank)
        self.emit(depth, ann + et + '}')

    def block(self, e, depth, head, trailer):
        """head + body (';' or '{ ... }') with implicit wrappers."""
        if e.selfclose:
            self.emit(depth, head + ';' + (' ' + trailer if trailer else ''), bool(trailer))
            return
        if (len(e.children) == 1 and e.children[0].kind == 'text' and e.children[0].pre == ''
                and e.close_pre == '' and not trailer and not e.endtail):
            self.emit(depth, head + ' ' + text_token(e.children[0].text) + ';')
            return
        self.emit(depth, head + ' {' + (' ' + trailer if trailer else ''), bool(trailer))
        if not e.children:
            self.close(e, depth)
            return
        kids = e.children
        pred = unit_after(e.indent, self.unit)
        if e.name == 'action' and self.hidden_do_all(e, pred):
            d = e.children[0]
            d.indent = pred
            self.children(d, d.children, depth + 1, unit_after(pred, self.unit))
            self.close(e, depth)
            return
        if e.name in ('cue', 'director') and self.hidden_cues(e, pred) is not None:
            self.with_cues(e, self.hidden_cues(e, pred), depth + 1, pred)
            self.close(e, depth)
            return
        self.children(e, kids, depth + 1, pred)
        self.close(e, depth)

    def with_cues(self, e, k, depth, pred):
        """e's children with the k-th (<cues>) left out: its cues are printed in its place."""
        front, cs, back = e.children[:k], e.children[k], e.children[k + 1:]
        self.children(e, front, depth, pred)
        cs.indent = front[-1].indent if front else pred
        self.children(cs, cs.children, depth, unit_after(cs.indent, self.unit))
        self.children(e, back, depth, cs.indent)

    def hidden_do_all(self, e, pred):
        if len(e.children) != 1:
            return False
        d = e.children[0]
        return (d.kind == 'el' and d.name == 'do_all' and not d.attrs and d.tail == '' and not d.selfclose
                and not d.endtail
                and sum(c.kind == 'el' for c in d.children) >= 2 and d.pre == self.eol + pred
                and d.close_pre == self.eol + pred and d.children[0].kind != 'text')

    def hidden_cues(self, e, pred):
        """The index of a <cues> child whose cues can stand directly in e's body, or None."""
        ks = [k for k, c in enumerate(e.children) if c.kind == 'el' and c.name == 'cues']
        if not ks or any(c.kind == 'text' or (c.kind == 'el' and c.name == 'cue') for c in e.children):
            return None
        k = ks[0]
        cs = e.children[k]
        if not (not cs.attrs and cs.tail == '' and not cs.selfclose and not cs.endtail and cs.children
                and cs.children[0].kind == 'el' and cs.children[0].name == 'cue'
                and cs.children[-1].kind == 'el' and cs.children[-1].name == 'cue'
                and all(c.kind == 'com' or (c.kind == 'el' and c.name == 'cue') for c in cs.children)):
            return None
        p = pred
        for c in e.children[:k]:
            p = indent_of(c.pre, p)
        return k if cs.pre == self.eol + p and cs.close_pre == self.eol + p else None

    def chain_ok(self, e, pred):
        if e.attrs or e.tail or e.selfclose or not e.children or e.endtail:
            return False
        names = [c.name if c.kind == 'el' else None for c in e.children]
        if None in names:
            return False
        n = len(names)
        if names[-1] == 'do_otherwise':
            if any(x != 'do_when' for x in names[:-1]) or n < 2:
                return False
        elif any(x != 'do_when' for x in names) or n < 2:
            return False
        p = unit_after(e.indent, self.unit)
        for c in e.children:
            if c.selfclose or c.pre != self.eol + p:
                return False
            if c.name == 'do_otherwise' and (c.attrs or c.tail):
                return False
            if c.children and c.children[0].kind == 'text':
                return False
        return e.close_pre == self.eol + e.indent

    def stmt(self, e, depth, ann, parent=None):
        if e.kind == 'com':
            txt = self.comment_text(e)
            self.emit(depth, ann + txt, txt.startswith('//'))
            return
        if e.kind == 'text':
            self.emit(depth, ann + text_token(e.text) + ';')
            return
        if e.name == 'do_choose' and self.chain_ok(e, e.indent):
            p = unit_after(e.indent, self.unit)
            for i, c in enumerate(e.children):
                c.indent = p
                if c.name == 'do_when':
                    a, tr = self.args(c)
                    h = ('if (%s) {' if i == 0 else 'else if (%s) {') % a
                else:
                    h, tr = 'else {', None
                h += (' ' + tr) if tr else ''
                if i == 0:
                    self.emit(depth, ann + h, bool(tr))
                else:
                    self.lines[-1] += ' ' + h
                    self.trailer = bool(tr)
                if c.children:
                    self.children(c, c.children, depth + 1, unit_after(p, self.unit))
                    blank, a2, _ = self.classify(c.close_pre, c.indent)
                    self.blank(blank)
                    self.emit(depth, a2 + ('~>%s ' % cstr(c.endtail) if c.endtail else '') + '}')
                else:
                    self.close(c, depth)
            return
        if e.name == 'do_if' and e.attrs:
            a, tr = self.args(e)
            self.block(e, depth, ann + 'if (%s)' % a, tr)
            return
        keys = e.keys()
        if e.name == 'cue' and keys and keys[0] == 'name' and e.attrs[0][0] == ' ' and std_attr(e.attrs[0]):
            a, tr = self.args(e, e.attrs[1:])
            h = ann + 'cue ' + fmt_value(xml_unescape(e.attrs[0][4])) + ('(%s)' % a if a else '')
            self.block(e, depth, h, tr)
            return
        if plain(e) and e.selfclose and len(keys) >= 2 and (e.name == 'param') == (parent == 'params'):
            sv = self.assignment(e)
            if sv:
                self.emit(depth, ann + sv[0] + ';' + (' ' + sv[1] if sv[1] else ''), bool(sv[1]))
                return
        h, tr = self.head(e, '@' if e.name == 'action' and not self.literal_ok(e) else '')
        self.block(e, depth, ann + h, tr)

    def literal_ok(self, e):
        """An <action> whose body reads back the same without '@' (no implicit <do_all>)."""
        return (e.selfclose or sum(c.kind == 'el' for c in e.children) < 2
                or self.hidden_do_all(e, unit_after(e.indent, self.unit)))

    def assignment(self, e):
        """set_value / param -> 'N = V[, k: v]' text and '///' trailer, or None."""
        keys = e.keys()
        vals = [xml_unescape(a[4]) for a in e.attrs]
        if keys[0] != 'name':
            return None
        target = fmt_value(vals[0])
        if not (re.fullmatch(r'[A-Za-z0-9_.{}@$]+', target) or target.startswith('"') or target.startswith('r"')):
            return None
        if e.name == 'param':
            if keys[1] != 'value':
                return None
            used, text = 2, '%s = %s' % (target, fmt_value(vals[1]))
        elif e.name == 'set_value':
            if keys[1] == 'exact' and len(keys) > 2 and keys[2] == 'operation' and vals[2] in ASSIGN_OPS:
                used, text = 3, '%s %s %s ~swap' % (target, ASSIGN_OPS[vals[2]], fmt_value(vals[1]))
            elif keys[1] == 'exact':
                used, text = 2, '%s = %s' % (target, fmt_value(vals[1]))
            elif keys[1] == 'operation' and vals[1] in ASSIGN_OPS and len(keys) > 2 and keys[2] == 'exact':
                used, text = 3, '%s %s %s' % (target, ASSIGN_OPS[vals[1]], fmt_value(vals[2]))
            elif keys[1] == 'operation' and vals[1] in ('add', 'subtract') and not target.endswith('"'):
                used, text = 2, target + ('++' if vals[1] == 'add' else '--')
            elif keys[1] == 'min' and len(keys) > 2 and keys[2] == 'max':
                used, text = 3, '%s = random(%s, %s)' % (target, fmt_value(vals[1]), fmt_value(vals[2]))
            else:
                return None
        else:
            return None
        rest = e.attrs[used:]
        trailer = None
        if rest:
            fake = Node('el', e.name)
            fake.attrs = rest
            a, trailer = self.args(fake, ops=False)
            for k in [x[1] for x in (rest[:-1] if trailer else rest)]:
                if k in positional(e.name):
                    return None
            if a:
                text += ', ' + a
        return text, trailer


def dis_doc(doc):
    doc.unit = choose_unit(doc)
    p = Printer(doc)
    L = p.lines
    if doc.bom:
        L.append('#bom')
    if doc.eol != '\n':
        L.append('#eol crlf')
    if doc.encoding != 'ISO-8859-1':
        L.append('#encoding ' + doc.encoding)
    if doc.prolog != canonical_prolog(doc.encoding, doc.eol):
        L.append('#prolog ' + cstr(doc.prolog))
    if doc.unit != '  ':
        L.append('#indent ' + cstr(doc.unit))
    if doc.epilog != doc.eol:
        L.append('#end ' + cstr(doc.epilog))
    r = doc.root
    r.indent = ''
    if r.name == 'director' and [a[1] for a in r.attrs] == ['name', 'xmlns:xsi', 'xsi:noNamespaceSchemaLocation'] \
            and plain(r) and r.attrs[1][4] == XSI and r.attrs[2][4] == 'director.xsd':
        L.append('#director ' + fmt_value(xml_unescape(r.attrs[0][4])))
    else:
        a, tr = p.args(r, allow_comment=False)
        L.append('#root %s(%s)' % (r.name, a))
    if r.selfclose:
        L.append('#selfclose')
    else:
        pred = unit_after('', doc.unit)
        if r.name == 'director' and p.hidden_cues(r, pred) is not None:
            p.with_cues(r, p.hidden_cues(r, pred), 0, pred)
        else:
            p.children(r, r.children, 0, pred)
        if r.close_pre != ('' if not r.children or r.children[-1].kind == 'text' else doc.eol):
            L.append('#close ' + cstr(r.close_pre))
    return '\n'.join(L) + '\n'


XSI = 'http://www.w3.org/2001/XMLSchema-instance'


def canonical_prolog(enc, eol):
    return ('<?xml version="1.0" encoding="%s" ?>%s<?xml-stylesheet href="director.xsl" type="text/xsl" ?>%s'
            % (enc, eol, eol))


# ---------------------------------------------------------------- asm: lexer

class Tok:
    __slots__ = ('t', 'v', 'line', 'nl', 'ws')

    def __init__(self, t, v, line, nl, ws):
        self.t, self.v, self.line, self.nl, self.ws = t, v, line, nl, ws

    def __repr__(self):
        return '%s:%r' % (self.t, self.v)


ESC = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"', "'": "'", '0': '\0'}
PUNCT2 = ('==', '>=', '<=', '!=', '+=', '-=', '*=', '/=', ':=', '~~', '~>')
PUNCT1 = '(){}[],;:=~'
BARE_SET = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.{}@|+-*%?$/')


def lex(text, fname):
    toks, i, n, line = [], 0, len(text), 1
    nl, ws = 0, ''

    def err(msg):
        raise MdError('%s:%d: error: %s' % (fname, line, msg))

    while i < n:
        c = text[i]
        if c == '\n':
            nl += 1
            line += 1
            ws = ''
            i += 1
            continue
        if c in ' \t':
            ws += c
            i += 1
            continue
        start_line = line
        if c == '#' and (not toks or nl > 0):
            j = text.find('\n', i)
            j = n if j < 0 else j
            toks.append(Tok('dir', text[i + 1:j].rstrip(), start_line, nl, ws))
            i = j
        elif text.startswith('///', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            v = text[i + 3:j]
            toks.append(Tok('acom', v[1:] if v.startswith(' ') else v, start_line, nl, ws))
            i = j
        elif text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            toks.append(Tok('com', text[i + 2:j], start_line, nl, ws))
            i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            if j < 0:
                err('unterminated /* comment')
            v = text[i + 2:j]
            toks.append(Tok('com', v, start_line, nl, ws))
            line += v.count('\n')
            i = j + 2
        elif c == '"' or (c == 'r' and text.startswith('r"', i)):
            raw = c == 'r'
            j = i + (2 if raw else 1)
            buf = []
            while True:
                if j >= n or text[j] == '\n':
                    err('unterminated string')
                ch = text[j]
                if ch == '"':
                    break
                if ch == '\\':
                    e = text[j + 1:j + 2]
                    if e == 'x':
                        try:
                            buf.append(chr(int(text[j + 2:j + 4], 16)))
                        except ValueError:
                            err('bad \\x escape')
                        j += 4
                        continue
                    if e not in ESC:
                        err('unknown escape \\%s' % e)
                    buf.append(ESC[e])
                    j += 2
                    continue
                buf.append(ch)
                j += 1
            toks.append(Tok('rstr' if raw else 'str', ''.join(buf), start_line, nl, ws))
            i = j + 1
        elif c == '`':
            j = text.find('`', i + 1)
            if j < 0:
                err('unterminated `raw attribute`')
            toks.append(Tok('rawattr', text[i + 1:j], start_line, nl, ws))
            i = j + 1
        elif text[i:i + 2] in PUNCT2:
            toks.append(Tok('p', text[i:i + 2], start_line, nl, ws))
            i += 2
        elif c == '}' or (c == '{' and (i + 1 >= n or text[i + 1] in ' \t\r\n}/~')):
            toks.append(Tok('p', c, start_line, nl, ws))      # a block brace, not an expression's
            i += 1
        elif c in BARE_SET:
            j = i
            while j < n and text[j] in BARE_SET and not text.startswith('//', j) and not text.startswith('/*', j):
                if j > i and text[j] in '+-*/' and text[j + 1:j + 2] == '=':
                    break                       # X+=1: the operator, not part of the name
                j += 1
            toks.append(Tok('bare', text[i:j], start_line, nl, ws))
            i = j
        elif c in PUNCT1:
            toks.append(Tok('p', c, start_line, nl, ws))
            i += 1
        else:
            err('unexpected character %r' % c)
        nl, ws = 0, ''
    toks.append(Tok('eof', None, line, nl, ws))
    return toks


# ---------------------------------------------------------------- asm: parser

class Parser:
    def __init__(self, text, fname):
        self.fname = fname
        self.toks = lex(text, fname)
        self.i = 0
        self.warnings = []
        self.eol = '\n'

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.i]
        self.i = min(self.i + 1, len(self.toks) - 1)
        return t

    def err(self, msg, tok=None):
        tok = tok or self.peek()
        raise MdError('%s:%d: error: %s' % (self.fname, tok.line, msg))

    def expect(self, v):
        t = self.peek()
        if t.t != 'p' or t.v != v:
            self.err('expected %r, found %s' % (v, self.desc(t)))
        return self.next()

    def desc(self, t):
        return 'end of file' if t.t == 'eof' else repr(t.v) if t.t in ('p', 'bare') else 'a %s' % {
            'str': 'string', 'rstr': 'string', 'com': 'comment', 'acom': '/// comment', 'dir': '#directive',
            'rawattr': 'raw attribute'}.get(t.t, t.t)

    def is_p(self, v, k=0):
        t = self.peek(k)
        return t.t == 'p' and t.v == v

    def value(self, what='a value'):
        t = self.peek()
        if t.t == 'bare':
            self.next()
            return unsugar(t.v)
        if t.t == 'str':
            self.next()
            return unsugar(t.v)
        if t.t == 'rstr':
            self.next()
            return t.v
        self.err('expected %s, found %s' % (what, self.desc(t)))

    # -- document
    def document(self):
        doc = Doc()
        enc_set = prolog = None
        doc.encoding = 'ISO-8859-1'
        root = Node('el', 'director')
        root.attrs = [[' ', 'name', '=', '"', ''], [' ', 'xmlns:xsi', '=', '"', XSI],
                      [' ', 'xsi:noNamespaceSchemaLocation', '=', '"', 'director.xsd']]
        root_close = None
        have_root = False
        while self.peek().t == 'dir':
            t = self.next()
            word, _, rest = t.v.partition(' ')
            rest = rest.strip()
            if word == 'bom':
                doc.bom = True
            elif word == 'eol':
                if rest not in ('crlf', 'lf'):
                    self.err('#eol takes crlf or lf', t)
                doc.eol = '\r\n' if rest == 'crlf' else '\n'
            elif word == 'encoding':
                enc_set = rest
            elif word in ('prolog', 'indent', 'end', 'close'):
                v = self.dir_string(rest, t)
                if word != 'prolog' and not WS_RX.match(v) or word == 'indent' and not (v and INDENT_RX.match(v)):
                    self.err('#%s takes %s only' % (word, 'spaces or tabs' if word == 'indent' else 'whitespace'), t)
                if word == 'prolog':
                    prolog = v
                elif word == 'indent':
                    doc.unit = v
                elif word == 'end':
                    doc.epilog = v
                else:
                    root_close = v
            elif word == 'director':
                have_root = True
                sub = Parser(rest, self.fname)
                sub.toks = [Tok(x.t, x.v, t.line, 0, x.ws) for x in sub.toks]
                root.attrs[0][4] = xml_escape(sub.value('the director name'))
            elif word.startswith('root') or word == 'selfclose':
                if word == 'selfclose':
                    root.selfclose = True
                    continue
                have_root = True
                sub = Parser(t.v[4:].strip(), self.fname)
                sub.toks = [Tok(x.t, x.v, t.line, 0, x.ws) for x in sub.toks]
                nm = sub.next()
                if nm.t != 'bare':
                    self.err('#root needs an element name', t)
                root = Node('el', nm.v)
                sub.expect('(')
                sub.arglist(root, ')')
            else:
                self.err('unknown directive #%s' % word, t)
        if not have_root:
            self.err('missing #director line')
        if enc_set:
            doc.encoding = enc_set
        if prolog is not None:
            doc.prolog = prolog
            m = ENC_RX.search(prolog)
            if m and not enc_set:
                doc.encoding = m.group(1)
        else:
            doc.prolog = canonical_prolog(doc.encoding, doc.eol)
        try:
            ''.encode(py_encoding(doc.encoding))
        except LookupError:
            self.err('unknown encoding %r' % doc.encoding)
        doc.root = root
        self.eol = doc.eol
        if doc.epilog is None:
            doc.epilog = doc.eol
        if not root.selfclose:
            self.body(root, None)
            if root_close is not None:
                root.close_raw = root_close
            root.close_nl = 1
        if self.peek().t != 'eof':
            self.err('unexpected %s' % self.desc(self.peek()))
        self.doc = doc
        return doc

    def dir_string(self, rest, t):
        sub = Parser(rest, self.fname)
        tk = sub.next()
        if tk.t not in ('str', 'rstr'):
            self.err('expected a string after the directive', t)
        return tk.v

    # -- blocks
    def body(self, parent, open_tok):
        """Statements until '}' (or end of file when open_tok is None) -> parent.children (with implicit wrappers)."""
        kids = []
        while True:
            t = self.peek()
            ind = raw = None
            endtail = ''
            first = t
            while t.t == 'p' and t.v in ('~', '~~', '~>'):
                self.next()
                s = self.peek()
                if s.t not in ('str', 'rstr'):
                    self.err('expected a string after %s' % t.v)
                self.next()
                if not (INDENT_RX if t.v == '~' else WS_RX).match(s.v):
                    self.err('the %s mark takes %s only' % (t.v, 'spaces and tabs' if t.v == '~' else 'whitespace'), s)
                if t.v == '~':
                    ind = s.v
                elif t.v == '~>':
                    endtail = s.v
                else:
                    raw = s.v
                t = self.peek()
            if t.t == 'eof':
                if open_tok is not None:
                    self.err('missing } for the block opened at line %d' % open_tok.line, open_tok)
                parent.close_ind, parent.close_raw = ind, raw
                break
            if t.t == 'p' and t.v == '}':
                if open_tok is None:
                    self.err("unbalanced '}'")
                self.next()
                parent.close_nl, parent.close_ind, parent.close_raw = first.nl, ind, raw
                parent.endtail = endtail
                break
            if endtail:
                self.err('~> (end tag spacing) belongs right before a }')
            node = self.stmt(parent)
            node.nl, node.ind, node.raw = first.nl, ind, raw
            if node.kind == 'com' and first.nl == 0 and ind is None and raw is None:
                node.inline = first.ws
            kids.append(node)
        self.wrap(parent, kids)

    def wrap(self, parent, kids):
        name = parent.name
        if name in ('cue', 'director') and not parent.literal:
            idx = [k for k, c in enumerate(kids) if c.kind == 'el' and c.name == 'cue']
            if idx:
                a, b = idx[0], idx[-1] + 1
                for c in kids[a:b]:
                    if not (c.kind == 'com' or (c.kind == 'el' and c.name == 'cue')):
                        self.err('the sub-cues of %s must stand together: %s at line %d is between them'
                                 % ('the file' if name == 'director' else 'a cue', c.name, c.line),
                                 self.tok_at(c.line))
                cs = Node('el', 'cues')
                cs.hidden = 'wrap'
                cs.children = kids[a:b]
                cs.line = kids[a].line
                kids = kids[:a] + [cs] + kids[b:]
            cues = [c for c in kids if c.kind == 'el' and c.name == 'cues']
            if len(cues) > 1:
                self.err('%s has two <cues>: the cues at lines %d and %d must go into one block (write the cues '
                         'inside the cues { } block, or drop it and write them directly)'
                         % ('the file' if name == 'director' else 'cue', cues[0].line, cues[1].line),
                         self.tok_at(cues[1].line))
        if name == 'action' and not parent.literal and sum(c.kind == 'el' for c in kids) >= 2:
            d = Node('el', 'do_all')
            d.hidden = 'wrap'
            d.children = kids
            d.line = kids[0].line
            kids = [d]
        parent.children = kids

    def tok_at(self, line):
        return Tok('p', '', line, 0, '')

    def stmt(self, parent):
        t = self.peek()
        if t.t == 'com':
            self.next()
            c = Node('com')
            c.text, c.line = t.v, t.line
            if t.v.startswith('~"'):
                sub = lex(t.v[1:], self.fname)
                if len(sub) != 2 or sub[0].t != 'str':
                    self.err('//~ must be followed by one string', t)
                c.text = sub[0].v
            elif self.eol == '\r\n':
                c.text = t.v.replace('\n', '\r\n')
            if '--' in c.text or c.text.endswith('-'):
                self.err("an XML comment cannot contain '--' or end with '-'", t)
            return c
        if t.t in ('str', 'rstr') and self.peek(1).t == 'p' and self.peek(1).v in ('=', '+=', '-=', '*=', '/=', ':='):
            return self.assign(parent, t)
        if t.t in ('str', 'rstr'):
            if parent.name not in TEXT_ELEMENTS:
                self.err('text is allowed only in %s; a value needs an element or an assignment'
                         % ', '.join(sorted(TEXT_ELEMENTS)))
            self.next()
            self.expect(';')
            c = Node('text')
            c.text, c.line = (t.v if t.t == 'rstr' else xml_escape_text(t.v)), t.line
            return c
        if t.t == 'acom':
            self.err('a /// comment belongs right after the ; or { of a statement')
        if t.t != 'bare':
            self.err('expected a statement, found %s' % self.desc(t))
        word = t.v
        nxt = self.peek(1)
        if word == 'if' and nxt.t == 'p' and nxt.v == '(':
            return self.if_chain()
        if word == 'else':
            self.err("'else' without 'if'")
        if word == 'cue' and nxt.t in ('bare', 'str', 'rstr'):
            self.next()
            e = Node('el', 'cue')
            e.line = t.line
            name = self.value('the cue name')
            e.attrs.append([' ', 'name', '=', '"', xml_escape(name)])
            if self.is_p('('):
                self.next()
                self.arglist(e, ')', positional_ok=False)
            self.check_element(e, t)
            return self.finish(e)
        if nxt.t == 'p' and nxt.v in ('=', '+=', '-=', '*=', '/=', ':='):
            return self.assign(parent, t)
        if (word.endswith('++') or word.endswith('--')) and nxt.t == 'p' and nxt.v == ';' and len(word) > 2:
            return self.assign(parent, t)
        literal = word.startswith('@')
        name = word[1:] if literal else word
        if not re.fullmatch(r'[A-Za-z_][\w.-]*', name):
            self.err('expected a statement, found %r' % word)
        self.next()
        e = Node('el', name)
        e.line, e.literal = t.line, literal
        if name not in schema():
            self.err('unknown element %s' % name, t)
        if self.is_p('('):
            self.next()
            self.arglist(e, ')')
        self.check_element(e, t)
        if self.peek().t in ('str', 'rstr') and self.is_p(';', 1):
            s = self.next()
            if name not in TEXT_ELEMENTS:
                self.err('text is allowed only in %s' % ', '.join(sorted(TEXT_ELEMENTS)), s)
            self.next()
            x = Node('text')
            x.text, x.line = (s.v if s.t == 'rstr' else xml_escape_text(s.v)), s.line
            e.children = [x]
            return e
        return self.finish(e)

    def finish(self, e):
        """';' or a block; then an optional /// comment."""
        t = self.peek()
        if t.t == 'p' and t.v == ';':
            self.next()
            e.selfclose = True
            self.attr_comment(e, t.line)
            return e
        if t.t == 'p' and t.v == '{':
            self.next()
            self.attr_comment(e, t.line)
            self.body(e, t)
            return e
        self.err("expected ';' or '{' after %s, found %s" % (e.name, self.desc(t)), self.tok_at(e.line))

    def attr_comment(self, e, line):
        t = self.peek()
        if t.t == 'acom' and t.line == line:
            self.next()
            self.check_attr(e, 'comment', t)
            if 'comment' in e.keys():
                self.err('a /// comment and comment: on one element', t)
            e.attrs.append([' ', 'comment', '=', '"', xml_escape(t.v)])

    def if_chain(self):
        t = self.next()
        self.expect('(')
        first = Node('el', 'do_if')
        first.line = t.line
        self.arglist(first, ')')
        self.check_element(first, t)
        if self.is_p(';'):
            return self.finish(first)
        brace = self.peek()
        self.finish(first)
        if not (self.peek().t == 'bare' and self.peek().v == 'else'):
            return first
        if not brace or brace.v != '{':
            self.err("'else' after 'if (...);'")
        ch = Node('el', 'do_choose')
        ch.line = t.line
        first.name = 'do_when'
        branches = [first]
        while self.peek().t == 'bare' and self.peek().v == 'else':
            et = self.next()
            if self.peek().t == 'bare' and self.peek().v == 'if':
                it = self.next()
                self.expect('(')
                w = Node('el', 'do_when')
                w.line = it.line
                self.arglist(w, ')')
                self.check_element(w, it)
                if not self.is_p('{'):
                    self.err("expected '{' after else if (...)")
                self.finish(w)
                branches.append(w)
            else:
                w = Node('el', 'do_otherwise')
                w.line = et.line
                if not self.is_p('{'):
                    self.err("expected '{' after else")
                self.finish(w)
                branches.append(w)
                break
        for b in branches:
            b.hidden = 'branch'
        ch.children = branches
        ch.literal = 'chain'
        return ch

    def assign(self, parent, t):
        self.next()
        word = t.v
        e = Node('el', 'param' if parent.name == 'params' else 'set_value')
        e.line = t.line
        if word.endswith('++') or word.endswith('--'):
            if e.name == 'param':
                self.err('++/-- is for set_value, not for a parameter')
            target = unsugar(word[:-2])
            e.attrs = [[' ', 'name', '=', '"', xml_escape(target)],
                       [' ', 'operation', '=', '"', 'add' if word.endswith('++') else 'subtract']]
        else:
            target = word if t.t == 'rstr' else unsugar(word)
            if t.t == 'bare' and word[-1] in '+-*/':
                self.err('the target %s ends with %r: write the operator with = (+=, -=, *=, /=)' % (word, word[-1]), t)
            op = self.next().v
            e.attrs.append([' ', 'name', '=', '"', xml_escape(target)])
            if e.name == 'param':
                if op != '=':
                    self.err('a parameter takes =, not %s' % op)
                e.attrs.append([' ', 'value', '=', '"', xml_escape(self.value())])
            elif op == '=':
                if self.peek().t == 'bare' and self.peek().v == 'random' and self.is_p('(', 1):
                    self.next()
                    self.next()
                    lo = self.value()
                    self.expect(',')
                    hi = self.value()
                    self.expect(')')
                    e.attrs += [[' ', 'min', '=', '"', xml_escape(lo)], [' ', 'max', '=', '"', xml_escape(hi)]]
                else:
                    e.attrs.append([' ', 'exact', '=', '"', xml_escape(self.value())])
            else:
                e.attrs += [[' ', 'operation', '=', '"', OP_OF[op]], [' ', 'exact', '=', '"', xml_escape(self.value())]]
                if self.is_p('~') and self.peek(1).t == 'bare' and self.peek(1).v == 'swap':
                    self.next()
                    self.next()
                    e.attrs[1], e.attrs[2] = e.attrs[2], e.attrs[1]
        if self.is_p(','):
            self.next()
            self.arglist(e, ';', positional_ok=False)
            self.i -= 1         # leave ';' for finish
        self.check_element(e, t)
        if not self.is_p(';'):
            self.err("expected ';' after the assignment, found %s" % self.desc(self.peek()), self.tok_at(e.line))
        return self.finish(e)

    def arglist(self, e, end, positional_ok=True):
        """Arguments up to and including `end` -> e.attrs (and e.tail)."""
        pos = positional(e.name) if positional_ok else []
        npos = 0
        named_seen = False
        while True:
            sep = ' '
            t = self.peek()
            if t.t == 'p' and t.v == end:
                self.next()
                return
            if t.t == 'p' and t.v == '~':
                self.next()
                s = self.next()
                if s.t not in ('str', 'rstr'):
                    self.err('expected a string after ~', s)
                if not WS_RX.match(s.v):
                    self.err('the ~ mark takes whitespace only', s)
                if self.is_p(end):
                    e.tail = s.v
                    self.next()
                    return
                if not s.v:
                    self.err('the ~ mark before an argument needs at least one space', s)
                sep = s.v
                t = self.peek()
            if t.t == 'rawattr':
                self.next()
                m = re.fullmatch(r'(\s+)([\w:.-]+)(\s*=\s*)("[^"]*"|\'[^\']*\')', t.v)
                if not m:
                    self.err('bad raw attribute `%s`' % t.v, t)
                self.check_attr(e, m.group(2), t)
                e.attrs.append([m.group(1), m.group(2), m.group(3), m.group(4)[0], m.group(4)[1:-1]])
                named_seen = True
            elif t.t == 'bare' and self.is_p(':', 1) and re.fullmatch(r'[A-Za-z_][\w.-]*', t.v):
                self.next()
                self.next()
                key = t.v
                # namespaced key: xmlns:xsi: value
                if self.peek().t == 'bare' and self.is_p(':', 1) and re.fullmatch(r'[A-Za-z_][\w.-]*', self.peek().v) \
                        and key in ('xmlns', 'xsi'):
                    key += ':' + self.next().v
                    self.next()
                self.check_attr(e, key, t)
                if key in e.keys():
                    self.err('attribute %s given twice' % key, t)
                e.attrs.append([sep, key, '=', '"', xml_escape(self.value())])
                named_seen = True
            else:
                if named_seen or npos >= len(pos):
                    self.err('%s: positional argument not allowed here (use name: value)' % e.name, t)
                v = self.value()
                e.attrs.append([sep, pos[npos], '=', '"', xml_escape(v)])
                npos += 1
                while True:
                    o = self.peek()
                    if ((o.t == 'p' and o.v in ('==', '!=', '>=', '<=')) or (o.t == 'bare' and o.v == 'in')) \
                            and pos[npos - 1] != 'value':
                        self.err('%s: a comparison follows only a positional value (here: %s)'
                                 % (e.name, pos[npos - 1]), o)
                    if o.t == 'p' and o.v in ('==', '!=', '>=', '<='):
                        self.next()
                        v = self.value()
                        key = {'==': 'exact', '!=': 'exact', '>=': 'min', '<=': 'max'}[o.v]
                        e.attrs.append([' ', key, '=', '"', xml_escape(v)])
                        if o.v == '!=':
                            e.attrs.append([' ', 'negate', '=', '"', '1'])
                    elif o.t == 'bare' and o.v == 'in':
                        self.next()
                        self.expect('[')
                        lo = self.value()
                        self.expect(',')
                        hi = self.value()
                        self.expect(']')
                        e.attrs += [[' ', 'min', '=', '"', xml_escape(lo)], [' ', 'max', '=', '"', xml_escape(hi)]]
                    else:
                        break
            t = self.peek()
            if t.t == 'p' and t.v == ',':
                self.next()
                continue
            if t.t == 'p' and t.v == '~':
                continue
            if t.t == 'p' and t.v == end:
                continue
            self.err("expected ',' or %r in the arguments of %s, found %s" % (end, e.name, self.desc(t)))

    def check_element(self, e, tok):
        sch = schema()
        if e.name not in sch:
            self.err('unknown element %s' % e.name, tok)
        allowed = set(sch[e.name]['attrs'])
        seen = set()
        for a in e.attrs:
            if a[1] not in allowed and not a[1].startswith('xmlns') and not a[1].startswith('xsi:'):
                self.err('%s has no attribute %s' % (e.name, a[1]), tok)
            if a[1] in seen:
                self.err('attribute %s given twice' % a[1], tok)
            seen.add(a[1])

    def check_attr(self, e, key, tok):
        sch = schema().get(e.name)
        if sch and key not in sch['attrs'] and not key.startswith('xmlns') and not key.startswith('xsi:'):
            self.err('%s has no attribute %s' % (e.name, key), tok)


def layout(doc):
    """Text-form annotations -> the exact whitespace of every node (the model dis predicts with)."""
    eol, unit = doc.eol, doc.unit

    def lay(e, indent):
        e.indent = indent
        pred = unit_after(indent, unit)
        prev_text = False
        for c in e.children:
            if c.kind == 'text':
                c.pre = ''
                prev_text = True
                continue
            if c.raw is not None:
                c.pre = c.raw
                c.indent = indent_of(c.raw, pred)
            elif prev_text:
                c.pre, c.indent = '', pred
            elif c.inline is not None:
                c.pre, c.indent = c.inline, pred
            elif c.hidden:
                c.pre, c.indent = eol + pred, pred
            else:
                ind = pred if c.ind is None else c.ind
                c.pre = eol * max(1, c.nl) + ind
                c.indent = ind
            prev_text = False
            pred = c.indent
            if c.kind == 'el':
                lay(c, c.indent)
        if e.selfclose:
            return
        if e.literal == 'chain':
            e.close_pre = eol + e.indent
            return
        if e.close_raw is not None:
            e.close_pre = e.close_raw
        elif not e.children or e.children[-1].kind == 'text':
            e.close_pre = ''
        elif e.hidden == 'wrap':
            e.close_pre = eol + e.indent
        else:
            e.close_pre = eol * max(1, e.close_nl) + (e.indent if e.close_ind is None else e.close_ind)
    lay(doc.root, '')


def asm_text(text, fname='<text>'):
    """-> (xml bytes, warnings). A leading UTF-8 BOM (an editor's; msci_asm reads utf-8-sig too) is skipped."""
    if text.startswith('﻿'):
        text = text[1:]
    text = text.replace('\r\n', '\n')
    p = Parser(text, fname)
    doc = p.document()
    layout(doc)
    warnings = check_cue_refs(doc, fname)
    return write_xml(doc, fname), warnings


def check_cue_refs(doc, fname):
    names, refs = set(), []

    def walk(e):
        if e.kind != 'el':
            return
        if e.name == 'cue' and e.attr('name') is not None:
            names.add(xml_unescape(e.attr('name')))
        v = e.attr('cue')
        if v is not None:
            refs.append((xml_unescape(v), e.line, e.name))
        for c in e.children:
            walk(c)
    walk(doc.root)
    out = []
    for v, line, el in refs:
        if '{' in v or v in names or v.startswith('this') or v.startswith('parent'):
            continue
        out.append('%s:%d: warning: %s refers to cue %r, which this file does not define' % (fname, line, el, v))
    return out


def dis_bytes(data, fname='<xml>', verify=True):
    doc = parse_xml(data, fname)
    text = dis_doc(doc)
    if verify:
        back, _ = asm_text(text, fname)
        if back != data:
            raise MdError('%s: internal error: the text form does not rebuild the file byte for byte (%s)'
                          % (fname, first_diff(data, back)))
    return text


def first_diff(a, b):
    n = min(len(a), len(b))
    i = next((k for k in range(n) if a[k] != b[k]), n)
    lo = max(0, i - 40)
    return 'offset %d: %r vs %r' % (i, a[lo:i + 40], b[lo:i + 40])


# ---------------------------------------------------------------- command line

def is_director(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(4096)
    except OSError:
        return False
    return b'<director' in head


def inputs(paths, ext):
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                fp = os.path.join(p, f)
                if f.lower().endswith(ext) and os.path.isfile(fp) and (ext != '.xml' or is_director(fp)):
                    yield fp
        else:
            yield p


def out_path(src, out, many, ext):
    base = os.path.splitext(os.path.basename(src))[0] + ext
    if out is None:
        return os.path.join(os.path.dirname(src), base)
    if many or os.path.isdir(out):
        os.makedirs(out, exist_ok=True)
        return os.path.join(out, base)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='x3kc md', description='Mission Director XML <-> text (.xmd)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('dis', help='director XML -> text')
    d.add_argument('src', nargs='+', help='.xml files or directories')
    d.add_argument('-o', '--out', help='output file (one input) or directory')
    d.add_argument('--no-verify', action='store_true', help='do not check that the text rebuilds the same bytes')
    a = sub.add_parser('asm', help='text -> director XML')
    a.add_argument('src', nargs='+', help='.xmd files or directories')
    a.add_argument('-o', '--out', help='output file (one input) or directory')
    a.add_argument('--no-warn', action='store_true', help='no warnings about cue references')
    c = sub.add_parser('check', help='round trip in memory, compare byte for byte')
    c.add_argument('src', nargs='+')
    c.add_argument('-v', action='store_true')
    s = sub.add_parser('schema', help='rebuild the element table from director.xsd and the stock files')
    s.add_argument('out')
    s.add_argument('dirs', nargs='+')
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd == 'schema':
        data = build_schema(args.dirs)
        with open(args.out, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=0, sort_keys=True)
            f.write('\n')
        print('%d elements' % len(data['elements']))
        return 0
    if args.cmd == 'check':
        files = list(inputs(args.src, '.xml'))
        bad = 0
        for fp in files:
            with open(fp, 'rb') as f:
                data = f.read()
            try:
                dis_bytes(data, fp)
                if args.v:
                    print('ok   %s' % fp)
            except MdError as e:
                bad += 1
                print('FAIL %s' % e)
        print('%d/%d identical' % (len(files) - bad, len(files)))
        return 1 if bad else 0
    ext_in, ext_out = ('.xml', EXT) if args.cmd == 'dis' else (EXT, '.xml')
    files = list(inputs(args.src, ext_in))
    many = len(files) > 1 or any(os.path.isdir(p) for p in args.src)
    rc = 0
    for fp in files:
        try:
            if args.cmd == 'dis':
                with open(fp, 'rb') as f:
                    data = f.read()
                text = dis_bytes(data, fp, not args.no_verify)
                with open(out_path(fp, args.out, many, ext_out), 'w', encoding='utf-8', newline='\n') as f:
                    f.write(text)
            else:
                with open(fp, encoding='utf-8') as f:
                    text = f.read()
                xml, warns = asm_text(text, fp)
                if not args.no_warn:
                    for w in warns:
                        print(w, file=sys.stderr)
                with open(out_path(fp, args.out, many, ext_out), 'wb') as f:
                    f.write(xml)
        except (MdError, UnicodeError, OSError) as e:
            print(e, file=sys.stderr)
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
