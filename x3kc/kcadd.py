"""Add a class or a global function to a split project (a part file and its index line), or remove one.

    x3kc add <index.kcs | dir> class <Name> [--id N] [--parent <Class>] [--after <part>]
    x3kc add <index.kcs | dir> function <Name> [--after <part>]
    x3kc remove <index.kcs | dir> <Name | part.kc>

add writes <Name>.kc (a class) or @<Name>.kc (a global function) from a minimal valid template and inserts its line
into the index: by default after the last class part (a class) or the last global function part (a function),
with --after right after the given part. A class id is free when no class of the project has it; without --id
the next one above every id of the project and at least 9500 (the stock objects use 50..9003). The parent must
exist and stand before the new class in the index: the game counts inherited fields only for a parent that comes
first in CLAS. A name or id that exists is refused. remove deletes the part file and its index line.
"""
import argparse
import os
import re
import sys

from . import kcparse
from . import kcsplit

FIRST_FREE_ID = 9500
KEYWORDS = {'var', 'function', 'class', 'if', 'else', 'while', 'for', 'do', 'switch', 'case', 'default', 'return',
            'break', 'continue', 'fork', 'new', 'delete', 'this', 'native', 'frame', 'asm', 'dense', 'sparse'}


class AddError(Exception):
    pass


def project(path):
    index = kcsplit.find_index(path)
    if index is None:
        raise AddError('%s is not a split project (a directory or a %s index)' % (path, kcsplit.INDEX_EXT))
    base = os.path.dirname(os.path.abspath(index))
    parts = []          # (file, index line number, kind of its first declaration, its name)
    for f, n in kcsplit.read_index(index):
        p = os.path.join(base, f)
        if not os.path.isfile(p):
            raise AddError('%s:%d: part %s is missing' % (os.path.basename(index), n, f))
        with open(p, 'rb') as h:
            text = h.read().decode('latin1')
        d = [x for x in kcsplit.declarations(text) if x[0] in ('class', 'function')]
        parts.append((f, n, d[0][0] if d else 'other', d[0][1] if d else None))
    src = kcsplit.load(index)
    u = kcparse.Parser(src.text, src.name, src.loc).unit(resolve=False)
    return index, base, parts, u


def insert_line(index, after_line, entry):
    """Insert `entry` into the index after its line number after_line (0: at the top of the part lines)."""
    with open(index, encoding='utf-8', newline='') as f:
        lines = f.read().splitlines(True)
    if after_line == 0:
        after_line = next((k for k, l in enumerate(lines, 1) if l.strip() and not l.strip().startswith('//')), 1) - 1
    nl = '\r\n' if lines and lines[0].endswith('\r\n') else '\n'
    if after_line and after_line <= len(lines) and not lines[after_line - 1].endswith(('\n', '\r')):
        lines[after_line - 1] += nl
    lines.insert(after_line, entry + nl)
    with open(index, 'w', encoding='utf-8', newline='') as f:
        f.write(''.join(lines))


def free_name(base, parts, stem):
    taken = {f.lower() for f, _, _, _ in parts} | {f.lower() for f in os.listdir(base)}
    if (stem + '.kc').lower() in taken or stem.split('.')[0].upper() in kcsplit.RESERVED:
        raise AddError('a part %s.kc exists already (or the name is a Windows device name)' % stem)
    return stem + '.kc'


def add(path, kind, name, cid=None, parent=None, after=None):
    """-> (part file path, index line text). Raises AddError."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or name in KEYWORDS:
        raise AddError('%r is not a valid name (letters, digits and _, not a keyword)' % name)
    index, base, parts, u = project(path)
    S = u.syms
    if kind == 'class':
        if name in S.by_name or (re.fullmatch(r'C\d+', name) and int(name[1:]) in S.classes):
            raise AddError('class %s exists already' % name)
        m = re.fullmatch(r'C(\d+)', name)
        if m and cid is not None and int(m.group(1)) != cid:
            raise AddError('class %s would have id %s: a C<n> name is class n' % (name, m.group(1)))
        if m:
            cid = int(m.group(1))
        if cid is None:
            cid = max([FIRST_FREE_ID - 1] + list(S.classes)) + 1
        if cid <= 0:
            raise AddError('class ids are positive (0 is the globals)')
        if cid in S.classes:
            raise AddError('class id %d is %s already' % (cid, S.class_name(cid)))
        if parent is not None and parent not in S.by_name and not (
                re.fullmatch(r'C\d+', parent) and int(parent[1:]) in S.classes):
            raise AddError('unknown parent class %s' % parent)
        stem = name
    else:
        if S.global_function(name):
            raise AddError('global function %s exists already' % name)
        stem = '@' + name
    entry = free_name(base, parts, stem)
    # where: after the given part, or after the last part of the same kind (or at the end)
    if after is not None:
        hit = [p for p in parts if p[0].lower() == after.lower() or p[0].lower() == (after + '.kc').lower()]
        if not hit:
            raise AddError('no part %s in the index' % after)
        pos = parts.index(hit[0])
    else:
        same = [k for k, p in enumerate(parts) if p[2] == kind]
        pos = same[-1] if same else len(parts) - 1
    if kind == 'class' and parent is not None:
        ppos = next((k for k, p in enumerate(parts) if p[2] == 'class' and p[3] == parent), None)
        if ppos is not None and ppos > pos:
            raise AddError('the parent %s stands after %s in the index: the class must come after its parent '
                           '(--after %s or later)' % (parent, parts[pos][0], parts[ppos][0]))
    if kind == 'class':
        head = 'class %s%s%s' % (name, '' if re.fullmatch(r'C\d+', name) else ' = %d' % cid,
                                 ' : %s' % parent if parent else '')
        text = '\n%s {\n}\n' % head
    else:
        text = '\nfunction %s() {\n}\n' % name
    p = os.path.join(base, entry)
    with open(p, 'w', encoding='latin1', newline='\n') as f:
        f.write(text)
    insert_line(index, parts[pos][1] if parts else 0, entry)
    return p, cid


def remove(path, what):
    index, base, parts, u = project(path)
    hit = [p for p in parts if p[0].lower() in (what.lower(), (what + '.kc').lower())] or \
        [p for p in parts if p[3] == what]
    if not hit:
        raise AddError('no part or class / function %s in the index' % what)
    if len(hit) > 1:
        raise AddError('%s names %d parts: give the part file' % (what, len(hit)))
    f, n = hit[0][0], hit[0][1]
    with open(index, encoding='utf-8', newline='') as h:
        lines = h.read().splitlines(True)
    del lines[n - 1]
    with open(index, 'w', encoding='utf-8', newline='') as h:
        h.write(''.join(lines))
    os.remove(os.path.join(base, f))
    return f


def main_add():
    ap = argparse.ArgumentParser(prog='x3kc add', description=__doc__.split('\n\n')[0])
    ap.add_argument('project')
    ap.add_argument('kind', choices=('class', 'function'))
    ap.add_argument('name')
    ap.add_argument('--id', type=lambda x: int(x, 0))
    ap.add_argument('--parent')
    ap.add_argument('--after', metavar='PART')
    a = ap.parse_args()
    if a.kind == 'function' and (a.id is not None or a.parent):
        print('error: --id and --parent are for classes')
        return 2
    try:
        p, cid = add(a.project, a.kind, a.name, a.id, a.parent, a.after)
    except (AddError, kcsplit.SplitError, kcparse.ParseError) as x:
        print('error:', x)
        return 1
    print('wrote %s%s and listed it in the index' % (p, ' (class id %d)' % cid if a.kind == 'class' else ''))
    return 0


def main_remove():
    a = sys.argv[1:]
    if len(a) != 2:
        print(__doc__)
        return 2
    try:
        f = remove(a[0], a[1])
    except (AddError, kcsplit.SplitError, kcparse.ParseError) as x:
        print('error:', x)
        return 1
    print('removed %s and its index line' % f)
    return 0
