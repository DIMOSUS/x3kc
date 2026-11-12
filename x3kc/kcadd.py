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
    src = kcsplit.load(index, check_unlisted=False)     # add adopts, remove may follow an unlisted file
    u = kcparse.Parser(src.text, src.name, src.loc).unit(resolve=False)
    return index, base, parts, u


def class_ref(S, name, what):
    """A class given by name or as C<n> -> its declared name (the parser resolves parents by name only)."""
    m = re.fullmatch(r'C(\d+)', name)
    if m and int(m.group(1)) == 0:
        raise AddError('%s C0 is the globals, not a class' % what)
    if name in S.by_name:
        return name
    if m and int(m.group(1)) in S.classes:
        return S.classes[int(m.group(1))].name
    raise AddError('unknown %s class %s' % (what, name))


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
    """-> (part file name, True when an unlisted file of that name is there to be adopted)."""
    if stem.split('.')[0].upper() in kcsplit.RESERVED:
        raise AddError('%s is a Windows device name: not usable as a file name' % stem)
    if (stem + '.kc').lower() in {f.lower() for f, _, _, _ in parts}:
        raise AddError('the index lists a part %s.kc already' % stem)
    on_disk = [f for f in os.listdir(base) if f.lower() == (stem + '.kc').lower()]
    return (on_disk[0] if on_disk else stem + '.kc'), bool(on_disk)


def adopted(base, entry, kind, name, S):
    """The class / function an unlisted part file declares: it must be exactly the one asked for."""
    with open(os.path.join(base, entry), 'rb') as h:
        text = h.read().decode('latin1')
    try:
        u = kcparse.Parser(text, entry).unit(resolve=False)
    except kcparse.ParseError as x:
        raise AddError('%s is there but does not parse: %s' % (entry, x))
    items = [(k, x) for k, x in u.items]
    if len(items) != 1 or items[0][0] != ('class' if kind == 'class' else 'func') or items[0][1].name != name:
        raise AddError('%s is there but does not declare just the %s %s: fix or move it first' % (entry, kind, name))
    if kind == 'class':
        k = items[0][1]
        pname = u.class_parent_names[k.id][0]
        return k.id, pname
    return None, None


def add(path, kind, name, cid=None, parent=None, after=None):
    """-> (part file path, index line text). Raises AddError."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or name in KEYWORDS:
        raise AddError('%r is not a valid name (letters, digits and _, not a keyword)' % name)
    index, base, parts, u = project(path)
    S = u.syms
    if kind == 'class':
        if name in S.by_name or (re.fullmatch(r'C\d+', name) and int(name[1:]) in S.classes):
            raise AddError('class %s exists already' % name)
    elif S.global_function(name):
        raise AddError('global function %s exists already' % name)
    entry, adopt = free_name(base, parts, name if kind == 'class' else '@' + name)
    if adopt:
        # an unlisted part file of that name (written by hand, or the "not in the index" case of compile): list it
        fid, fparent = adopted(base, entry, kind, name, S)
        if kind == 'class':
            if cid is not None and cid != fid:
                raise AddError('%s declares class id %d, not %d' % (entry, fid, cid))
            if parent is not None and parent != fparent:
                raise AddError('%s declares parent %s, not %s' % (entry, fparent, parent))
            cid, parent = fid, fparent
    if kind == 'class':
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
        if parent is not None:
            parent = class_ref(S, parent, 'parent')
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
    p = os.path.join(base, entry)
    if not adopt:
        if kind == 'class':
            head = 'class %s%s%s' % (name, '' if re.fullmatch(r'C\d+', name) else ' = %d' % cid,
                                     ' : %s' % parent if parent else '')
            text = '\n%s {\n}\n' % head
        else:
            text = '\nfunction %s() {\n}\n' % name
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(text)
    insert_line(index, parts[pos][1] if parts else 0, entry)
    return p, cid, adopt


def remove(path, what):
    index, base, parts, u = project(path)
    hit = [p for p in parts if p[0].lower() in (what.lower(), (what + '.kc').lower())] or \
        [p for p in parts if p[3] == what]
    if not hit:
        raise AddError('no part or class / function %s in the index' % what)
    if len(hit) > 1:
        raise AddError('%s names %d parts: give the part file' % (what, len(hit)))
    f, n, kind, name = hit[0]
    if kind == 'other':
        raise AddError('%s holds no class or function (the header: directives and globals): not removed' % f)
    if kind == 'class':
        cid = u.syms.by_name.get(name)
        kids = sorted(u.syms.classes[c].name for c, (pn, _) in u.class_parent_names.items()
                      if c != cid and pn is not None and (pn == name or pn == 'C%s' % cid))
        if kids:
            raise AddError('class %s is the parent of %s: remove or re-parent those first' % (name, ', '.join(kids)))
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
        p, cid, adopt = add(a.project, a.kind, a.name, a.id, a.parent, a.after)
    except (AddError, kcsplit.SplitError, kcparse.ParseError) as x:
        print('error:', x)
        return 1
    print('%s %s%s and listed it in the index' % ('kept' if adopt else 'wrote', p,
                                                   ' (class id %d)' % cid if a.kind == 'class' else ''))
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
