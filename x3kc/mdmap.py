"""The cue map: Mission Director source (a director file's .xmd text or its XML) -> what the running game has.

The game's director (x3story class 307 CueDirector, `LoadFile` / `LoadArray` / `LoadTable`) parses each file with
SE_XMLImport and turns every `<cue>` into a raw record whose parts are tables of the XML nodes; a Cue object (class
308) keeps them in its fields:

    cueName        the cue's name (a `<cue ref="L">` reference: the library cue L's name)
    conditionDef   LoadTable(the first element inside <condition>)      (rawCue[8])
    timingDef      LoadTable(<timing>)                                  (rawCue[9])
    actionDef      LoadTable(the first element inside <action>)         (rawCue[10])

LoadTable(node) is the node's attribute table plus `code` (the tag, when it has a '_' after its first letter) and,
for a group node (check_all, check_any, do_*, data), `data`: the array of its element children in document order;
any other node keeps its element children by tag (`t["sector"]`, a repeated tag an array `t["ware"][k]`). So every
condition and action element of a cue is one table reached from the cue's field by a fixed path - the element's
identity at run time (instances share their static cue's tables). Cue::CheckCondition(condition, ...) evaluates one
condition element (Cue::Condition rolls its chance and applies negate first), Cue::DoAction(action) runs one action
element (Cue::Action rolls the chance, forks for task="1"); the children of a do_choose (do_when, do_otherwise) run
through Cue::DoAll(action).

    m = MdMap.load(paths)               .xmd / .xml files or folders of them
    m.cues_named(name)                  [Cue]
    m.at(file, line)                    (Cue, Elem or None)   a .xmd line (or an .xml line of an .xml file)
    elem.kc_expr()                      'actionDef["data"][2]'  (a KC expression on the cue)
    elem.read_steps()                   [["field", slot] ...] needs the slot of the root field
"""
import hashlib
import json
import os
import pickle
import tempfile

from . import md

ROOT_FIELD = {'condition': 'conditionDef', 'action': 'actionDef', 'timing': 'timingDef', 'force': 'forceDef'}


def is_group(tag):
    u = tag.find('_')
    if u > 0:
        return tag in ('check_all', 'check_any') or tag[:u] == 'do'
    return tag == 'data'


def has_code(tag):
    return tag.find('_') > 0


class Elem:
    """one XML element of a cue's condition / action / timing / force: tag, attrs [(name, decoded value)], lines,
    kind (the cue part), steps (from the part's root field: ('key', 'data'), ('index', i), ('key', tag)), exec (the
    Cue method that receives its table: 'CheckCondition', 'DoAction', 'DoAll' or None: an argument node), children"""
    __slots__ = ('tag', 'attrs', 'line', 'xml_line', 'kind', 'steps', 'exec', 'children', 'parent', 'cue', 'text')

    def __init__(self, tag, attrs, line, xml_line, kind, steps, parent, cue):
        self.tag, self.attrs, self.line, self.xml_line, self.kind = tag, attrs, line, xml_line, kind
        self.steps, self.parent, self.cue, self.children, self.exec, self.text = steps, parent, cue, [], None, ''

    def attr(self, k):
        for a, v in self.attrs:
            if a == k:
                return v
        return None

    def kc_expr(self):
        s = ROOT_FIELD[self.kind]
        for k, v in self.steps:
            s += '[%s]' % (json.dumps(v) if k == 'key' else str(v))
        return s

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def where(self):
        return '%s:%d' % (self.cue.file, self.line)


class Cue:
    """one <cue> of a file. name: the runtime cueName; ref: the library name of a <cue ref=...> (its parts come from
    the library cue); library: a library cue (never runs itself, only through references)"""
    __slots__ = ('name', 'file', 'xml_file', 'line', 'xml_line', 'parent', 'children', 'parts', 'attrs', 'ref',
                 'library', 'text', 'part_lines', 'notes', 'part_refs', 'target')

    def __init__(self):
        self.parts, self.children, self.attrs, self.part_lines, self.notes = {}, [], [], {}, []
        self.part_refs = {}      # part -> library name: <condition ref="L"/>, <action ref="L"/> ...
        self.ref, self.library, self.parent, self.target = None, False, None, None   # target: a ref's cue

    def path(self):
        out, c = [], self
        while c is not None:
            out.append(c.name)
            c = c.parent
        return list(reversed(out))

    def elements(self):
        for k in ('condition', 'timing', 'action', 'force'):
            if k in self.parts:
                yield from self.parts[k].walk()

    def where(self):
        return '%s:%d' % (self.file, self.line)


def _els(node):
    return [c for c in node.children if c.kind == 'el']


def _zip(a, b, fname):
    """pairs of the same elements of two parses of one file (a: the .xmd's, b: the XML's)"""
    out = [(a, b)]
    ka, kb = _els(a), _els(b)
    if len(ka) != len(kb) or any(x.name != y.name for x, y in zip(ka, kb)):
        raise md.MdError('%s: the text and the XML differ in the children of <%s> (line %d)' % (fname, a.name, a.line))
    for x, y in zip(ka, kb):
        out.extend(_zip(x, y, fname))
    return out


def parse_file(path):
    """-> (xmd lines, xml lines, root node with .line (text) and .xml_line) of one director file"""
    with open(path, 'rb') as h:
        data = h.read()
    if path.lower().endswith(md.EXT):
        text = data.decode('utf-8-sig').replace('\r\n', '\n')
        p = md.Parser(text, path)
        doc = p.document()
        md.layout(doc)
        xml = md.write_xml(doc, path)
    else:
        xml = data
        text = md.dis_doc(md.parse_xml(data, path))
        p = md.Parser(text, path)
        doc = p.document()
    xdoc = md.parse_xml(xml, path)
    pairs = _zip(doc.root, xdoc.root, path)
    xml_line = {id(a): b.line for a, b in pairs}
    return text.split('\n'), xml_line, doc.root


class MdMap:
    def __init__(self):
        self.files = []          # [(xmd-or-xml path, display name)]
        self.cues = []
        self.by_name = {}

    @staticmethod
    def sources(paths):
        out = []
        for p in paths:
            if os.path.isdir(p):
                names = sorted(os.listdir(p))
                xmd = [n for n in names if n.lower().endswith(md.EXT)]
                if xmd:
                    out += [os.path.join(p, n) for n in xmd]
                else:
                    out += [os.path.join(p, n) for n in names if n.lower().endswith('.xml') and
                            md.is_director(os.path.join(p, n))]
            elif os.path.isfile(p):
                out.append(p)
        return out

    @classmethod
    def load(cls, paths, cache=True):
        files = cls.sources(paths)
        if not files:
            raise md.MdError('no director files in %s' % ', '.join(paths))
        key = hashlib.sha1(json.dumps([(os.path.abspath(f), os.path.getsize(f), os.path.getmtime(f)) for f in files]
                                      + [os.path.getmtime(__file__), os.path.getmtime(md.__file__)]).encode()).hexdigest()
        d = os.path.join(tempfile.gettempdir(), 'x3kc_dbg')
        cpath = os.path.join(d, 'mdmap-%s.pickle' % key[:16])
        if cache and os.path.isfile(cpath):
            try:
                with open(cpath, 'rb') as h:
                    return pickle.load(h)
            except Exception:
                pass
        m = cls()
        for f in files:
            m.add_file(f)
        m.resolve_refs()
        if cache:
            os.makedirs(d, exist_ok=True)
            with open(cpath + '.tmp', 'wb') as h:
                pickle.dump(m, h, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(cpath + '.tmp', cpath)
        return m

    def add_file(self, path):
        lines, xml_line, root = parse_file(path)
        name = os.path.basename(path)
        self.files.append(path)
        xml_name = os.path.splitext(name)[0] + '.xml'

        def text_at(n):
            return lines[n - 1].strip() if 0 < n <= len(lines) else ''

        def cue_of(node, parent):
            c = Cue()
            c.attrs = [(a[1], md.xml_unescape(a[4])) for a in node.attrs]
            # (LoadArray tests ref first: a cue with a ref is a reference whatever its name; Activate runs the
            # library's record, so the live cue has the library's name)
            c.ref = dict(c.attrs).get('ref')
            c.name = c.ref or dict(c.attrs).get('name') or '?'
            c.library = dict(c.attrs).get('library') not in (None, '', '0')
            c.file, c.xml_file, c.line, c.xml_line, c.parent = name, xml_name, node.line, xml_line.get(id(node), 0), parent
            c.text = text_at(node.line)
            self.cues.append(c)
            self.by_name.setdefault(c.name, []).append(c)
            for ch in _els(node):
                if ch.name == 'cues':
                    for sub in _els(ch):
                        if sub.name == 'cue':
                            c.children.append(cue_of(sub, c))
                elif ch.name in ROOT_FIELD:
                    c.part_lines[ch.name] = (ch.line, xml_line.get(id(ch), 0))
                    kids = _els(ch)
                    pref = dict((a[1], md.xml_unescape(a[4])) for a in ch.attrs).get('ref')
                    if pref:
                        c.part_refs[ch.name] = pref   # (another cue's part: resolved with the references)
                        continue
                    if ch.name == 'timing':
                        top = ch          # (timingDef = LoadTable(<timing>) itself)
                    elif kids:
                        top = kids[0]     # (LoadTable(the first element child); the loader ignores the rest)
                        if len(kids) > 1:
                            c.notes.append('%s:%d: <%s> has %d elements: the loader takes only the first (<%s>)'
                                           % (name, ch.line, ch.name, len(kids), kids[0].name))
                    else:
                        continue
                    c.parts[ch.name] = elem_of(top, ch.name, [], None, c)
                    if top.hidden:
                        # (the text's implicit <do_all> of `action { a; b; }`: the action's own line)
                        c.parts[ch.name].line, c.parts[ch.name].text = ch.line, text_at(ch.line)
            return c

        def elem_of(node, kind, steps, parent, cue):
            e = Elem(node.name, [(a[1], md.xml_unescape(a[4])) for a in node.attrs], node.line,
                     xml_line.get(id(node), 0), kind, list(steps), parent, cue)
            e.text = text_at(node.line)
            if kind == 'condition' and has_code(node.name):
                e.exec = 'CheckCondition'
            elif kind in ('action', 'force') and has_code(node.name):
                e.exec = 'DoAll' if parent is not None and parent.tag == 'do_choose' else 'DoAction'
            kids = _els(node)
            if is_group(node.name):
                for i, k in enumerate(kids):
                    e.children.append(elem_of(k, kind, steps + [('key', 'data'), ('index', i)], e, cue))
            else:
                count = {}
                for k in kids:
                    count[k.name] = count.get(k.name, 0) + 1
                seen = {}
                for k in kids:
                    if count[k.name] == 1:
                        st = steps + [('key', k.name)]
                    else:
                        st = steps + [('key', k.name), ('index', seen.get(k.name, 0))]
                        seen[k.name] = seen.get(k.name, 0) + 1
                    sub = elem_of(k, kind, st, e, cue)
                    sub.exec = None   # (an argument node: read by its parent's code, never run itself)
                    e.children.append(sub)
            return e

        top = [k for k in _els(root) if k.name == 'cues']
        for cs in top:
            for node in _els(cs):
                if node.name == 'cue':
                    cue_of(node, None)

    def resolve_refs(self):
        """a <cue ref="L"> runs the library cue L's record (Cue::GetLibrary: the first library of that name in the
        parent's sub-cues, then up the parents; here: a library of that name, the nearest by file); its sub-cues
        are the library's"""
        for c in self.cues:
            if c.ref is not None:
                lib = self.library(c.ref, c)
                c.target = lib
                if lib is not None:
                    for k, v in lib.parts.items():
                        c.parts.setdefault(k, v)
                    c.part_lines = dict(lib.part_lines, **c.part_lines)
            # a part reference (<action ref="L"/>): Cue::Activate / Refresh put GetLibrary(L, part) - the library
            # cue's own table - into the sub-cue's record: the same tables, under this cue's name
            for k, ref in c.part_refs.items():
                lib = self.library(ref, c)
                if lib is not None and k in lib.parts:
                    c.parts[k] = lib.parts[k]
                elif lib is not None:
                    c.notes.append('%s: library cue %r has no <%s>' % (c.where(), ref, k))

    def library(self, name, c):
        """the cue a reference of c names (Cue::GetLibrary takes any entry of that name that is not itself a
        reference - a library or a normal cue - in the parent's sub-cues, then up the parents): here a library of
        that name in c's file, else any cue of that name in c's file, else the first library, else the first cue"""
        cands = [x for x in self.by_name.get(name, []) if x.ref is None]
        libs = sorted(cands, key=lambda x: (x.file != c.file, not x.library))
        if not libs:
            c.notes.append('%s: library cue %r not found in the map' % (c.where(), name))
            return None
        return libs[0]

    def runners(self, cue, kind=None):
        """the definitions whose live cues run cue's tables (its part `kind`): the cue itself; for a library cue
        the cue references of it (their live cues carry the library's name) and the cues whose part `kind` refers
        to it (their own names)"""
        out = [] if cue.library else [cue]
        out += [c for c in self.cues if c.ref == cue.name and not c.library]
        if kind is not None:
            out += [c for c in self.cues if c.part_refs.get(kind) == cue.name and not c.library]
        return out

    def ambiguous(self, name):
        """the name has definitions that run different tables (references of one cue are one)"""
        return len({id(d.target or d) for d in self.runnable(name)}) > 1

    def cues_named(self, name):
        return [c for c in self.by_name.get(name, [])]

    def runnable(self, name):
        """the definitions a live cue of this name runs: the cue itself, or the references of that library"""
        out = [c for c in self.by_name.get(name, []) if not c.library]
        return out or self.by_name.get(name, [])

    def file_named(self, fname):
        want = os.path.basename(fname).lower()
        base = os.path.splitext(want)[0]
        for f in self.files:
            b = os.path.basename(f).lower()
            if b == want or os.path.splitext(b)[0] == base:
                return os.path.basename(f)
        return None

    def at(self, fname, line, xml=False):
        """the cue and element at a line of a file: the element that starts on it, else the innermost one whose
        block holds it (the cue's own line: (cue, None)); xml: an XML line"""
        f = self.file_named(fname)
        if f is None:
            raise md.MdError('%s: no such director file in the map' % fname)
        best = None
        for c in self.cues:
            if c.file != f:
                continue
            cl = c.xml_line if xml else c.line
            if cl == line:
                return c, None
            for e in (x for x in c.elements() if x.cue is c):
                el = e.xml_line if xml else e.line
                if el == line and (best is None or best[1].line != line or e.exec and not best[1].exec):
                    best = (c, e)
        if best is not None:
            return best
        # the part's own line (condition {, action {) -> its top element
        for c in self.cues:
            if c.file != f:
                continue
            for k, (pl, pxl) in c.part_lines.items():
                if (pxl if xml else pl) == line and k in c.parts:
                    return c, c.parts[k]
        raise md.MdError('%s:%d: no cue or element starts on that line' % (f, line))

    def find_elem(self, cue, kind, steps):
        root = cue.parts.get(kind)
        if root is None:
            return None
        for e in root.walk():
            if e.steps == list(steps):
                return e
        return None
