"""Split source projects: one file per class and per global function, joined by an index file.

    x3kc split <file.kc> <dir>          single-file project -> split project in <dir>
    x3kc join <dir | index.kcs> <file.kc>   split project -> single-file project

A split project is a directory with
  <name>.kcs        the index: `//` comment lines and one part file per line, in source order (the only order)
  @header.kc        everything before the first function or class: directives, global variables
  <Class>.kc        one per class (`C2259.kc` for an unnamed one), with the comments above it
  @<Function>.kc    one per global function
  @globals.<x>.kc   a later run of directives / global variables (hand edits; the decompiler never writes one)
  <name>.pool, <name>.ptch   unchanged (`#pool` / `#patch` resolve relative to the index's directory)

Parts are byte slices of the single file, so join is the concatenation of the parts in index order and
join(split(x)) == x for any file the lexer accepts, comments and formatting included. A part starts at the line after
the previous declaration's last line; text after the last declaration stays in the last part. File names only
help a human find a class: the first one in source order keeps `Name.kc`, a later one whose name collides
case-insensitively (or is a Windows device name such as CON) gets `Name~<class id>.kc` (`@Name~2.kc` for functions).
Each part parses on its own (`parse_part`); compile joins the parts in memory and reports `part:line`.
"""
import bisect
import os
import re
import shutil
import sys

from . import kcparse

INDEX_EXT = '.kcs'
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} | {'LPT%d' % i for i in range(1, 10)}


class SplitError(Exception):
    pass


# ---- cutting a single file into parts ----------------------------------------------------------------------------
def declarations(text):
    """Top-level declarations: [(kind, name, id text, start offset, end offset)], kind dir/var/function/class."""
    out = []
    pos, n = 0, len(text)
    depth = 0
    cur = None          # [kind, name, id, start, end, state]
    TOKEN = kcparse.TOKEN
    last = ''
    while pos < n:
        m = TOKEN.match(text, pos)
        if not m:
            raise SplitError('line %d: bad character %r' % (text.count('\n', 0, pos) + 1, text[pos]))
        k = m.lastgroup
        s, e = m.start(), m.end()
        pos = e
        if k == 'ws':
            continue
        t, before = m.group(), last
        last = t
        if cur is None:
            if k == 'dir':
                out.append(('dir', t[1:].split(None, 1)[0] if t[1:].split() else '', '', s, e))
                continue
            if k == 'id' and t in ('var', 'function', 'class'):
                cur = [t, None, '', s, e, 0]
                continue
            # anything else at the top level (a syntax error for the parser) stays with the previous declaration
            if out:
                kd, nm, cid, st, _ = out[-1]
                out[-1] = (kd, nm, cid, st, e)
            else:
                out.append(('junk', '', '', s, e))
            continue
        cur[4] = e
        if cur[1] is None and k == 'id':
            cur[1] = t
            continue
        if cur[0] == 'class' and cur[5] == 0 and before == '=' and k == 'num':
            cur[2] = t          # class Name = <id>
        if k == 'op':
            if t == '{':
                depth += 1
                cur[5] = 1
            elif t == '}':
                depth -= 1
                if depth <= 0 and cur[0] != 'var':
                    depth = 0
                    out.append(tuple(cur[:5]))
                    cur = None
            elif t == ';' and cur[0] == 'var' and depth == 0:
                out.append(tuple(cur[:5]))
                cur = None
    if cur is not None:         # unterminated: runs to the end of the file
        out.append(tuple(cur[:5]))
    return out


def cut_after(text, end, nxt):
    """Where the part after a declaration ending at `end` starts (the next declaration starts at `nxt`): after the
    first line break outside a block comment, so a trailing `// comment` stays with its declaration."""
    pos = end
    while pos < nxt:
        m = kcparse.TOKEN.match(text, pos, nxt)
        t = m.group()
        if t.startswith('/*'):
            return pos
        if not t.startswith('//'):
            i = t.find('\n')
            if i >= 0:
                return pos + i + 1
        pos = m.end()
    return nxt


def part_name(kind, name, cid):
    if kind == 'class':
        return name, ('~' + cid) if cid else None
    if kind == 'function':
        return '@' + name, None
    return '@globals.' + (name or 'directive'), None


def split_text(text):
    """Single-file source text -> [(file name, text)] in source order; ''.join of the texts is `text`."""
    decls = declarations(text)
    groups = []         # [start offset, kind, name, id]
    prev = None
    for d in decls:
        kind, name, cid, s, e = d
        if kind == 'class' and not cid:
            m = re.fullmatch(r'C(\d+)', name or '')
            cid = m.group(1) if m else ''
        if not groups:
            groups.append([0, kind, name, cid])
        elif kind in ('class', 'function') or prev[0] in ('class', 'function'):
            groups.append([cut_after(text, prev[4], s), kind, name, cid])
        prev = d
    if not groups:
        groups.append([0, 'dir', '', ''])
    if groups[0][1] in ('dir', 'var', 'junk'):
        groups[0][1:3] = ['header', '']
    seen = set()
    count = {}
    out = []
    for k, (s, kind, name, cid) in enumerate(groups):
        e = groups[k + 1][0] if k + 1 < len(groups) else len(text)
        stem, alt = ('@header', None) if kind == 'header' else part_name(kind, name or 'noname', cid)
        key = stem.lower()
        if key in seen or stem.split('.')[0].upper() in RESERVED:
            if alt:
                stem = stem + alt
            key = stem.lower()
            n = 2
            base = stem
            while key in seen or stem.split('.')[0].upper() in RESERVED:
                stem = '%s~%d' % (base, n)
                key = stem.lower()
                n += 1
        seen.add(key)
        out.append((stem + '.kc', text[s:e]))
    return out


# ---- writing and reading split projects --------------------------------------------------------------------------
def read_index(path):
    parts = []
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('//'):
                continue
            parts.append((line, n))
    return parts


def find_index(path):
    """A .kcs file or a directory holding exactly one -> the index path; anything else -> None."""
    if os.path.isdir(path):
        idx = [f for f in os.listdir(path) if f.lower().endswith(INDEX_EXT)]
        if len(idx) != 1:
            raise SplitError('%s: a split project directory holds exactly one %s index (found %d)'
                             % (path, INDEX_EXT, len(idx)))
        return os.path.join(path, idx[0])
    if path.lower().endswith(INDEX_EXT):
        return path
    return None


def is_project(path):
    return os.path.isdir(path) or path.lower().endswith(INDEX_EXT)


def side_files(text):
    """The files `#pool` / `#patch` name, from the directive lines of a source text."""
    out = []
    for m in re.finditer(r'^[ \t]*#(pool|patch)[ \t]+([^\n/]+)', text, re.M):
        out.append(m.group(2).strip())
    return out


def _copy_side(text, src_dir, dst_dir):
    copied = []
    for f in side_files(text):
        a, b = os.path.join(src_dir, f), os.path.join(dst_dir, f)
        if os.path.exists(a) and os.path.abspath(a) != os.path.abspath(b):
            os.makedirs(os.path.dirname(os.path.abspath(b)), exist_ok=True)
            shutil.copyfile(a, b)
            copied.append(f)
    return copied


def write_project(dirpath, name, text):
    """Write `text` (the single-file source, str in latin1 terms) as split project <dirpath>/<name>.kcs."""
    os.makedirs(dirpath, exist_ok=True)
    index = os.path.join(dirpath, name + INDEX_EXT)
    other = [f for f in os.listdir(dirpath) if f.lower().endswith(INDEX_EXT) and f != name + INDEX_EXT]
    if other:
        raise SplitError('%s already holds the split project %s: one project per directory' % (dirpath, other[0]))
    old = [p for p, _ in read_index(index)] if os.path.exists(index) else []
    parts = split_text(text)
    data = [(f, t.encode('latin1')) for f, t in parts]      # all encoded before any file is opened
    names = {f.lower() for f, _ in parts}
    for f, b in data:
        with open(os.path.join(dirpath, f), 'wb') as h:
            h.write(b)
    for f in old:                    # parts of the previous split that are gone now
        p = os.path.join(dirpath, f)
        if f.lower() not in names and os.path.isfile(p):
            os.remove(p)
    with open(index, 'w', encoding='utf-8', newline='\n') as h:
        h.write('// x3kc split project: the parts of %s.kc in source order (x3kc join rebuilds the single file)\n'
                % name)
        for f, _ in parts:
            h.write(f + '\n')
    return index, parts


class Source:
    """A source project read for compiling: the joined text, where its lines come from, the base directory."""

    def __init__(self, text, name, base, parts=None):
        self.text, self.name, self.base = text, name, base
        self.parts = parts or []        # [(file, first global line)]
        self._starts = [s for _, s in self.parts]

    def loc(self, line):
        if not self.parts:
            return '%s:%d' % (self.name, line)
        i = max(0, bisect.bisect_right(self._starts, line) - 1)
        f, s = self.parts[i]
        return '%s:%d' % (f, line - s + 1)


def load(path):
    """A single .kc file, a .kcs index or a split project directory -> Source."""
    index = find_index(path)
    if index is None:
        with open(path, encoding='latin1') as f:
            return Source(f.read(), os.path.basename(path), os.path.dirname(os.path.abspath(path)))
    base = os.path.dirname(os.path.abspath(index))
    chunks, parts = [], []
    line = 1
    listed = read_index(index)
    unlisted = unlisted_parts(index, [f for f, _ in listed])
    if unlisted:
        raise SplitError('%s: not in the index %s, so not compiled (list it with `x3kc add`, add its line to the '
                         'index, or delete the file)' % (unlisted[0], os.path.basename(index))
                         + (' (and %d more: %s)' % (len(unlisted) - 1, ', '.join(unlisted[1:6]))
                            if len(unlisted) > 1 else ''))
    for f, n in listed:
        p = os.path.join(base, f)
        try:
            with open(p, 'rb') as h:
                t = h.read().decode('latin1')
        except OSError as x:
            raise SplitError('%s:%d: cannot read part %s (%s)' % (os.path.basename(index), n, f, x.strerror))
        parts.append((f, line))
        chunks.append(t)
        line += t.count('\n')
    if not parts:
        raise SplitError('%s: the index lists no parts' % index)
    return Source(''.join(chunks), os.path.basename(index), base, parts)


def unlisted_parts(index, listed):
    """.kc files in the index's directory (and in the subdirectories its parts lie in) that the index does not list:
    a part written but never added would silently not be compiled."""
    base = os.path.dirname(os.path.abspath(index))
    names = {os.path.normcase(os.path.normpath(f)) for f in listed}
    dirs = {''} | {os.path.dirname(os.path.normpath(f)) for f in listed}
    out = []
    for d in sorted(dirs):
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            continue
        for f in sorted(os.listdir(p)):
            rel = os.path.normpath(os.path.join(d, f))
            if f.lower().endswith('.kc') and os.path.isfile(os.path.join(p, f)) \
                    and os.path.normcase(rel) not in names:
                out.append(rel.replace(os.sep, '/'))
    return out


def parse_part(path):
    """Pass 1 of one part file alone (no other part read): classes keep their parent names unresolved
    (Unit.class_parent_names); bodies are parsed at compile, when every symbol is known."""
    with open(path, encoding='latin1') as f:
        return kcparse.Parser(f.read(), os.path.basename(path)).unit(resolve=False)


def join_bytes(index):
    base = os.path.dirname(os.path.abspath(index))
    out = []
    for f, n in read_index(index):
        with open(os.path.join(base, f), 'rb') as h:
            out.append(h.read())
    return b''.join(out)


# ---- commands ----------------------------------------------------------------------------------------------------
def main_split():
    a = sys.argv[1:]
    if len(a) != 2:
        print(__doc__)
        return 2
    src, d = a
    if is_project(src):
        print('error: %s is already a split project' % src)
        return 2
    with open(src, 'rb') as h:
        raw = h.read()
    name = os.path.splitext(os.path.basename(src))[0]
    text = raw.decode('latin1')
    try:
        index, parts = write_project(d, name, text)
    except SplitError as x:
        print('error:', x)
        return 1
    if join_bytes(index) != raw:
        print('error: the parts do not join back to %s' % src)
        return 1
    copied = _copy_side(text, os.path.dirname(os.path.abspath(src)), d)
    print('wrote %s: %d parts%s' % (index, len(parts), (' (copied %s)' % ', '.join(copied)) if copied else ''))
    return 0


def main_join():
    a = sys.argv[1:]
    if len(a) != 2:
        print(__doc__)
        return 2
    src, out = a
    try:
        index = find_index(src)
    except SplitError as x:
        print('error:', x)
        return 1
    if index is None:
        print('error: %s is not a split project (a directory or a %s index)' % (src, INDEX_EXT))
        return 2
    try:
        raw = join_bytes(index)
    except OSError as x:
        print('error: %s' % x)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'wb') as h:
        h.write(raw)
    copied = _copy_side(raw.decode('latin1'), os.path.dirname(os.path.abspath(index)),
                        os.path.dirname(os.path.abspath(out)))
    print('wrote %s (%d bytes, %d parts)%s' % (out, len(raw), len(read_index(index)),
                                                (' (copied %s)' % ', '.join(copied)) if copied else ''))
    return 0
