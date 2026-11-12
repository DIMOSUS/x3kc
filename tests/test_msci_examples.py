"""The worked example of docs/msci_manual.md (docs/examples/msci_hello): built by `x3kc msci new`, printed back by
`x3kc msci dis`, and run through the game's own interpreter (ScriptObject::__runScript) in the emulator.

Set X3_GAME to an Albion Prelude directory; X3_FL_GAME to a Terran Conflict directory with Farnham's Legacy
(addon2) runs the same checks on the FL x3story.
"""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from x3kc import gameio, kcmsci, kcobj, kcvm, msci
from x3kc.__main__ import main as cli

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), 'docs', 'examples', 'msci_hello')
SCRIPTS = ['lib.x3kc.hello.thousands', 'plugin.x3kc.hello', 'setup.x3kc.hello']
GAMES = [(os.environ.get('X3_GAME'), 'addon'), (os.environ.get('X3_FL_GAME'), 'addon2')]


def texts_of(path):
    """(page, id) -> text as the game shows it, from a language file."""
    raw = open(path, encoding='utf-8').read()
    out = {}
    for page, body in re.findall(r'<page id="(\d+)"[^>]*>(.*?)</page>', raw, re.S):
        for i, t in re.findall(r'<t id="(\d+)">(.*?)</t>', body, re.S):
            out[int(page), int(i)] = msci.game_text(msci.html.unescape(t))
    return out


class Example:
    addon = None
    game = None

    @classmethod
    def setUpClass(cls):
        if not cls.game or not os.path.isdir(cls.game):
            raise unittest.SkipTest('set %s to the game directory' % ('X3_GAME' if cls.addon == 'addon' else 'X3_FL_GAME'))
        cls.dir = tempfile.mkdtemp(prefix='x3kcex')
        work = os.path.join(cls.dir, 'msci')
        with contextlib.redirect_stdout(io.StringIO()):
            gameio.msci_setup(cls.game, work, addon=cls.addon)
        cls.dbp = os.path.join(work, 'msci_db.json')
        with open(cls.dbp, encoding='utf-8') as f:
            cls.db = json.load(f)
        cls.obj = kcobj.load(os.path.join(work, 'x3story.obj'))
        cls.out = os.path.join(cls.dir, 'out')
        os.makedirs(cls.out)
        cls.code = {}
        for n in SCRIPTS:                       # the library first: `call script` lines name its argument
            xml = os.path.join(cls.out, n + '.xml')
            with contextlib.redirect_stdout(io.StringIO()):
                assert cli(['msci', 'new', cls.dbp, os.path.join(EXAMPLE, n + '.txt'), '-o', xml]) == 0
            cls.code[n], _ = msci.read_script(xml)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def msci(self):
        vm = kcvm.VM(self.obj)
        vm.texts.update(texts_of(os.path.join(EXAMPLE, '8870-L044.xml')))
        return kcmsci.Msci(vm, self.db)

    def test_dis_prints_the_source(self):
        """`dis` of the built XML gives the source text back (indentation, `@` and the engine line aside)."""
        for n in SCRIPTS:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli(['msci', 'dis', self.dbp, os.path.join(self.out, n + '.xml')])

            def norm(s, drop=()):
                out = []
                for l in s.replace('\r', '').split('\n'):
                    l = ' '.join(l.strip().lstrip('@').split())
                    if l and not any(l.startswith(d) for d in drop):
                        out.append(l.replace('- $', '-$'))
                return out
            src = open(os.path.join(EXAMPLE, n + '.txt'), encoding='utf-8').read()
            self.assertEqual(norm(buf.getvalue(), ('// engine:',)), norm(src), n)

    def test_codearray_header(self):
        lib = self.code['lib.x3kc.hello.thousands']
        self.assertEqual(lib[0], 'lib.x3kc.hello.thousands')
        self.assertGreaterEqual(lib[1], 40)              # LoadScript refuses engine versions below 40
        self.assertEqual(lib[5][0], 'value')             # arguments are the first variables
        self.assertEqual(lib[7], [[10, 'Value']])        # 10 = Var/Number (text page 2000)

    def test_library_through_the_interpreter(self):
        cases = {0: '0', 7: '7', 999: '999', 1000: '1,000', 1005: '1,005', 1234567: '1,234,567', -1234: '-1,234',
                 2147483647: '2,147,483,647',
                 # -(-2147483648) overflows back to itself: the documented pitfall of the example
                 -2147483648: '--648'}
        for v, want in cases.items():
            r = self.msci().run(self.code['lib.x3kc.hello.thousands'], args=kcmsci.to_cells([4, v]))
            self.assertEqual((r.type, r.value), (5, want), v)

    def test_plugin_calls_the_library_and_shows_the_text(self):
        m = self.msci()
        m.vm.call(700, 'AddScript', [kcmsci.to_cells(self.code['lib.x3kc.hello.thousands'])])
        shown = []
        m.vm.overrides['ShowSubTitleText'] = lambda vm, t, o, a: shown.append(a[0]) or 0
        r = m.run(self.code['plugin.x3kc.hello'])
        self.assertEqual(r.type, 0)
        self.assertEqual(len(shown), 1)
        # no player in the emulator: no name, 0 credits (formatted by the library), no sector, 0 ships
        self.assertRegex(shown[0], r"^Hello, .*! You have 0 credits; 0 ships are in this sector\. \(x3kc example\)$")

    def test_setup_registers_the_hotkey(self):
        m = self.msci()
        m.vm.call(700, 'AddScript', [kcmsci.to_cells(self.code['plugin.x3kc.hello'])])
        loaded = []
        m.vm.overrides['LoadText'] = lambda vm, t, o, a: loaded.append(a[0]) or 1
        r = m.run(self.code['setup.x3kc.hello'])
        self.assertEqual(r.type, 0)
        self.assertEqual(loaded, [8870])            # its own text file, before the hotkey names a text on it
        self.assertTrue(m.vm.called('RegisterCustomHotkeyTextID'))


class AP(Example, unittest.TestCase):
    game, addon = GAMES[0]


class FL(Example, unittest.TestCase):
    game, addon = GAMES[1]


if __name__ == '__main__':
    unittest.main()
