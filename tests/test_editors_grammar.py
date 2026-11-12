"""The TextMate grammars of editors/ (KC, MSCI): well-formed, every regex compiles, the example sources tokenise
completely with no invalid.* scope (check_grammar's interpreter over Python re; no editor needed)."""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'editors', 'tools'))
import check_grammar as CG      # noqa: E402

SYN = os.path.join(ROOT, 'editors', 'kc-textmate', 'syntaxes')

MSCI = """// plugin.x3kc.test  (a test script)
// version: 3
// engine: 50
// command: 0
//   $ship: Ship  "the ship"
* a comment line
$name = get player name
@ $r = [THIS] -> call script lib.x3kc.hello.thousands :  Value=$money
if $ship -> exists
  $text = sprintf: pageid=8870 textid=2, $name, null, null, null, null
  $t = '{17,42} and more'
end
gosub pad3
return null
pad3:
$x = $x + 1
endsub
"""


def tokenise(grammar, text):
    root = CG.build(grammar)
    lines = text.split('\n')
    total = scoped = 0
    bad = []
    for no, (line, sc) in enumerate(zip(lines, CG.tokenise(root, lines)), 1):
        for ch, s in zip(line, sc):
            if ch.isspace():
                continue
            total += 1
            scoped += bool(s)
            if any(x.startswith('invalid.') for x in s):
                bad.append(no)
    return total, scoped, bad, root, lines


class Grammars(unittest.TestCase):
    def load(self, name):
        with open(os.path.join(SYN, name), encoding='utf-8') as f:
            g = json.load(f)
        self.assertEqual(CG.check_includes(g), [])
        for where, s in CG.all_regexes(g):
            re.compile(s)
        return g

    def test_kc(self):
        g = self.load('kc.tmLanguage.json')
        from test_kc import SOURCE
        total, scoped, bad, _, _ = tokenise(g, SOURCE)
        self.assertEqual((scoped, bad), (total, []))

    def test_msci(self):
        g = self.load('msci.tmLanguage.json')
        self.assertEqual(g['fileTypes'], ['msci'])
        total, scoped, bad, root, lines = tokenise(g, MSCI)
        self.assertEqual((scoped, bad), (total, []))
        sc = list(CG.tokenise(root, lines))

        def scope_of(needle):
            for line, s in zip(lines, sc):
                i = line.find(needle)
                if i >= 0:
                    return s[i][-1] if s[i] else None
        self.assertEqual(scope_of('get player name'), 'keyword.other.command.msci')
        self.assertEqual(scope_of('lib.x3kc'), 'entity.name.function.script.msci')
        self.assertEqual(scope_of('$name'), 'variable.other.msci')
        self.assertEqual(scope_of('{17,42}'), 'constant.other.text-reference.msci')
        self.assertEqual(scope_of('8870'), 'constant.other.text-reference.msci')
        self.assertEqual(scope_of('pad3:'), 'entity.name.label.msci')
        self.assertEqual(scope_of('* a comment'), 'comment.line.asterisk.msci')
        self.assertEqual(scope_of('command: 0'), 'keyword.other.header.msci')
        self.assertEqual(scope_of('if $ship'), 'keyword.control.msci')
        for ex in ('lib.x3kc.hello.thousands.txt', 'plugin.x3kc.hello.txt', 'setup.x3kc.hello.txt'):
            with open(os.path.join(ROOT, 'docs', 'examples', 'msci_hello', ex), encoding='utf-8') as f:
                total, scoped, bad, _, _ = tokenise(g, f.read())
            self.assertEqual((scoped, bad), (total, []), ex)


if __name__ == '__main__':
    unittest.main()
