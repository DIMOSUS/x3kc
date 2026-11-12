"""MSCI command reference generated from the command database, grouped as the in-game editor's command menu.

    x3kc msci ref <db.json> <out.md> [--game NAME] [--map map.json] [--scripts DIR] [--other db.json --other-name N]

For every command: id, the editor syntax with typed placeholders, the parameter types, whether it can be a
condition (`if`, `skip if`, `while`), whether it is interruptible (`@`), how often the installed scripts use it (and
one script that does), what the interpreter calls for it (`--map`, from `x3kc msci map --json`), engine-side bugs
from our reading of the interpreter (x3kc/data/msci_notes.json), and how it differs from the other game's database
(`--other`). Groups and their order are the editor's (`db['menu']`, names from text page 2005).

Placeholders: `$ret =` result variable; `$ret =|if…` result that may also be a condition; `<RefObj> ->` the object
the command runs on; `<Var/Ship>` etc. a value of that parameter type (text page 2000).
"""
import argparse
import glob
import json
import os
import re
from collections import Counter

from . import msci

RESULT = 131072
TYPED = 65536
COND = {25: 'if', 26: 'if', 28: 'if/START'}         # RetVar/IF, RetVar/IF, RetVar/IF/START


def notes_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'msci_notes.json')


class Ref:
    def __init__(self, db, game):
        self.db, self.game = db, game
        self.pr = msci.Printer(db)
        self.types = {int(k): v for k, v in db['text'].get('2000', {}).items()}
        self.groups = {int(k): v for k, v in db['text'].get('2005', {}).items()}
        self.learned = {p: set(map(str, v)) for p, v in db.get('learned', {}).items()}

    def tname(self, p):
        return self.types.get(p, 'type %d' % p)

    def placeholder(self, cid, n):
        pars = self.pr.pars(cid)
        if n >= len(pars):
            return '%%%d' % n
        p = pars[n]
        if cid == 102 and p == 5:
            return '<arguments…>'
        if cid == 104 and p == 30:
            return '<expression>'
        if self.pr.pf.get(p + 1, 0) & RESULT and p in (15, 25, 26, 28):
            return '$ret = ' if p == 15 else '$ret =|%s ' % COND[p]
        if p == 20:
            return '<RefObj> ->'
        return '<%s>' % self.tname(p)

    def syntax(self, cid):
        fmt = self.pr.cmdtext(cid)
        if fmt.startswith('<cmd'):
            return None
        return re.sub(r'%(\d)', lambda m: self.placeholder(cid, int(m.group(1))), fmt)

    def shown(self, cid):
        return {int(x) for x in re.findall(r'%(\d)', self.pr.cmdtext(cid))}

    def walk(self, node, path, seen, out):
        """(group path, command id) in menu order; a command listed in several groups comes once, in the first."""
        gid = node[0]
        here = path + [self.groups.get(gid, 'Group %d' % gid)] if gid else path
        for e in node[1:]:
            if isinstance(e, list):
                self.walk(e, here, seen, out)
            elif isinstance(e, int) and e not in seen and str(e) in self.db['cmds']:
                seen.add(e)
                out.append((tuple(here), e))
        return out


def usage(db, d):
    count, where = Counter(), {}
    for f in sorted(glob.glob(os.path.join(d, '*.xml'))):
        code, _ = msci.read_script(f)
        if not code or not isinstance(code[6], list):
            continue
        for ln in code[6]:
            if isinstance(ln, list) and ln:
                c = ln[1] if ln[0] == 3 and len(ln) > 1 else ln[0]
                count[c] += 1
                where.setdefault(c, code[0])
    return count, where


def esc(s):
    return s.replace('|', '\\|')


def main():
    ap = argparse.ArgumentParser(prog='x3kc msci ref')
    ap.add_argument('db')
    ap.add_argument('out')
    ap.add_argument('--game', default='')
    ap.add_argument('--map')
    ap.add_argument('--scripts')
    ap.add_argument('--other')
    ap.add_argument('--other-name', default='the other game')
    ap.add_argument('--json', help='the same as JSON: {game, groups: menu tree with command ids, commands: [...]}')
    a = ap.parse_args()
    db = json.load(open(a.db, encoding='utf-8'))
    r = Ref(db, a.game)
    calls = {int(k): v.get('calls', []) for k, v in json.load(open(a.map, encoding='utf-8')).items()} if a.map else {}
    count, where = usage(db, a.scripts) if a.scripts else (Counter(), {})
    notes = {int(k): v for k, v in json.load(open(notes_file(), encoding='utf-8')).items() if k.isdigit()}
    other = json.load(open(a.other, encoding='utf-8'))['cmds'] if a.other else None
    game_word = a.game.split()[0] if a.game else ''

    order = r.walk(db['menu'], [], set(), []) if db.get('menu') else []
    listed = {c for _, c in order}
    rest = sorted(int(c) for c in db['cmds'] if int(c) not in listed)
    order += [(('Not in the editor menu',), c) for c in rest]

    nscripts = len(glob.glob(os.path.join(a.scripts, '*.xml'))) if a.scripts else 0
    head = ['# MSCI command reference%s' % (' — ' + a.game if a.game else ''), '',
            '<!-- generated by `x3kc msci ref`; do not edit by hand, regenerate (docs/msci_manual.md) -->', '',
            'Generated from the command database the game builds in `ScriptDatabase.InitDatabase` (x3story.obj), the '
            'editor texts (pages 2003/2004; parameter types page 2000; groups page 2005) and the interpreter '
            '`ScriptObject::__runScript`. How to read an entry and how to write scripts: [msci_manual.md]'
            '(msci_manual.md#reading-the-command-reference).', '',
            '- %d commands, %d with an editor text%s.' % (
                len(db['cmds']), sum(1 for c in db['cmds'] if r.syntax(int(c))),
                ', %d of them learned from the saved text of the shipped scripts (`msci learn`)' % (
                    len(r.learned.get('2003', ())) + len(r.learned.get('2004', ()))) if r.learned.get('2003') else ''),
            ]
    if nscripts:
        head.append('- "used" = lines in the %d shipped scripts (`addon%s/scripts`), with one script that uses it.' % (
            nscripts, '2' if 'FL' in a.game else ''))
    if other is not None:
        head.append('- "%s" = not in %s; "sig" = parameter list differs from %s.' % (
            'new', a.other_name, a.other_name))
    head += ['', '## Groups', '']
    groups = []
    for path, _ in order:
        if path not in groups:
            groups.append(path)
    for g in groups:
        n = sum(1 for p, _ in order if p == g)
        head.append('- [%s](#%s) (%d)' % (' / '.join(g), slug(' / '.join(g)), n))
    body = []
    records = []
    cur = None
    for path, cid in order:
        if path != cur:
            cur = path
            body += ['', '## %s' % ' / '.join(path), '']
        pars = r.pr.pars(cid)
        flags = r.pr.flags(cid)
        syn = r.syntax(cid)
        tags = []
        if flags & TYPED:
            tags.append('`@` interruptible')
        if flags & RESULT:
            tags.append('hidden')
        if any(p in COND for p in pars):
            tags.append('condition')
        if str(cid) in r.learned.get('2003', ()) or str(cid) in r.learned.get('2004', ()):
            tags.append('text learned')
        if other is not None:
            o = other.get(str(cid))
            if o is None:
                tags.append('new')
            elif (o[0] or []) != (pars or []):
                tags.append('sig: %s had %s' % (a.other_name, '[' + ', '.join(r.tname(p) for p in (o[0] or [])) + ']'))
        unshown = [i for i in range(len(pars)) if i not in r.shown(cid)] if syn else []
        line = '- **%d** %s' % (cid, '`%s`' % syn.strip() if syn else '*(no editor text: cannot be written as text)*')
        body.append(line)
        sub = []
        if pars:
            sub.append('params: ' + ', '.join('%%%d %s' % (i, r.tname(p)) for i, p in enumerate(pars)))
        if unshown and syn:
            sub.append('not in the text: ' + ', '.join('%%%d' % i for i in unshown) + ' (written as null)')
        if tags:
            sub.append(', '.join(tags))
        if count.get(cid):
            sub.append('used %d× (%s)' % (count[cid], where[cid]))
        if cid in calls and calls[cid]:
            sub.append('calls: ' + ', '.join('`%s`' % c for c in calls[cid] if c != 'C702.ScriptGetConst'))
        if sub:
            body.append('  ' + '; '.join(s for s in sub if s and s != 'calls: '))
        n = notes.get(cid)
        bug = n if n and (not game_word or game_word in n['games'].split()) else None
        if bug:
            body.append('  - **Engine bug (%s, %s):** %s' % (n['ref'], n['games'], n['note']))
        records.append({
            'id': cid, 'group': list(path), 'syntax': syn.strip() if syn else None,
            'text': r.pr.cmdtext(cid) if syn else None,
            'params': [{'n': i, 'type': r.tname(p), 'type_id': p} for i, p in enumerate(pars)],
            'result': any(r.pr.pf.get(p + 1, 0) & RESULT and p in (15, 25, 26, 28) for p in pars),
            'condition': any(p in COND for p in pars), 'interruptible': bool(flags & TYPED),
            'hidden': bool(flags & RESULT), 'not_in_text': unshown,
            'learned': 'text learned' in tags, 'new': 'new' in tags,
            'changed_from_other': next((t[5:] for t in tags if t.startswith('sig: ')), None),
            'used': count.get(cid, 0), 'example_script': where.get(cid),
            'calls': [c for c in calls.get(cid, []) if c != 'C702.ScriptGetConst'],
            'bug': {'ref': bug['ref'], 'note': bug['note']} if bug else None})
    with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(head + body) + '\n')
    if a.json:
        tree = {}
        for rec in records:
            node = tree
            for g in rec['group']:
                node = node.setdefault(g, {})
            node.setdefault('_commands', []).append(rec['id'])
        # one command per line: a grep for a word of the syntax returns the whole record
        with open(a.json, 'w', encoding='utf-8', newline='\n') as f:
            f.write('{"game": %s,\n"groups": %s,\n"commands": [\n%s\n]}\n' % (
                json.dumps(a.game), json.dumps(tree, ensure_ascii=False),
                ',\n'.join(json.dumps(x, ensure_ascii=False, separators=(',', ':')) for x in records)))
    print('%s: %d commands in %d groups' % (a.out, len(order), len(groups)))
    return 0


def slug(s):
    s = s.lower()
    s = re.sub(r'[^\w\- ]', '', s)
    return s.replace(' ', '-')


if __name__ == '__main__':
    main()
