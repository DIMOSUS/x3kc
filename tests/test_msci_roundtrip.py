"""Stock MSCI scripts as text: `msci dis` -> `msci new` / the assembler gives the same <codearray>.

Set X3_GAME (Albion Prelude) and/or X3_FL_GAME (Terran Conflict directory with Farnham's Legacy in addon2).
Every cause of a difference fixed so far has a test on a stock script that showed it; `test_whole_set` runs all
scripts and fails when fewer round-trip than recorded (MINIMUM).
"""
import contextlib
import glob
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from x3kc import gameio, msci, msci_asm


class RoundTrip:
    addon = None
    game = None
    MINIMUM = 0

    @classmethod
    def setUpClass(cls):
        if not cls.game or not os.path.isdir(cls.game):
            raise unittest.SkipTest('set %s' % ('X3_GAME' if cls.addon == 'addon' else 'X3_FL_GAME'))
        cls.dir = tempfile.mkdtemp(prefix='x3kcrt')
        cls.work = os.path.join(cls.dir, 'msci')
        with contextlib.redirect_stdout(io.StringIO()):
            gameio.msci_setup(cls.game, cls.work, addon=cls.addon)
        with open(os.path.join(cls.work, 'msci_db.json'), encoding='utf-8') as f:
            cls.db = json.load(f)
        cls.pr = msci.Printer(cls.db)
        cls.pr.scriptargs = {}
        cls.A = msci_asm.Assembler(cls.db)
        cls.codes = {}
        for f in sorted(glob.glob(os.path.join(cls.work, 'scripts', '*.xml'))):
            c, _ = msci.read_script(f)
            if not c:
                continue
            cls.codes[c[0]] = c
            cls.pr.scriptargs[c[0]] = [a[1] for a in (c[7] or [])]
            with open(f, encoding='utf-8', errors='replace') as h:
                raw = h.read()
            cls.A.scriptargs[c[0]] = [msci.html.unescape(d) for d in re.findall(r'<argument [^>]*desc="([^"]*)"', raw)]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def new(self, name):
        """dis text -> `msci new` -> codearray."""
        p = os.path.join(self.dir, 'x.txt')
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(msci.dis_lines(self.pr, self.codes[name], self.A)) +'\n')
        xml = msci_asm.new_script(self.A, self.db, p)
        m = re.search(r'<codearray>(.*?)</codearray>', xml, re.S)
        return msci.sval(msci.ET.fromstring(m.group(1).strip()))

    def check(self, name, why):
        if name not in self.codes:
            self.skipTest('%s is not in this game' % name)
        self.assertEqual(self.new(name), self.codes[name], why)

    def test_commented_out_call_keeps_variables_by_name(self):
        self.check('!fight.attack.object.bomber', '* @ $best.miss = ... call script: variables by name')

    def test_commented_out_expression_keeps_the_tokens(self):
        self.check('!job.trade.planetsupply', '* $x = $x * ( 500000 / $tmp ): infix tokens only')

    def test_empty_lines_at_the_ends(self):
        self.check('Graph.SX.total.owned.shares', 'empty editor lines at the start of the script')

    def test_comment_keeps_trailing_spaces(self):
        self.check('!god.tl.deliver.factory', 'a `*` comment ending in a space')

    def test_script_command(self):
        c = self.codes.get('!fight.board.launchmarines')
        if c is None:
            self.skipTest('not in this game')
        self.assertTrue(c[9])
        self.assertIn('// command: %d' % c[9], msci.dis_lines(self.pr, c))
        self.check('!fight.board.launchmarines', 'script[9], the script command')

    def pinned(self, name, needle):
        if name not in self.codes:
            self.skipTest('%s is not in this game' % name)
        text = msci.dis_lines(self.pr, self.codes[name], self.A)
        lines = [l for l in text if needle in l]
        if not lines:
            self.skipTest('%s has no line with %r in this game' % (name, needle))
        self.assertTrue(all('//= ' in l for l in lines), lines)
        self.check(name, needle)

    def test_ambiguous_name_is_pinned(self):
        self.pinned('!move.mine.loop', '$mineral = Debris')         # the ware Debris, not the class

    def test_hidden_parameters_are_pinned(self):
        self.pinned('!job.fight.attackallenemies', 'delayed command')   # 1185: two parameters the text does not show

    def test_unreadable_value_is_pinned(self):
        self.pinned('!job.trade.docktrader', '{race 23}')            # FL: a race without a name

    def test_commented_out_call_of_an_unknown_script(self):
        self.pinned('!move.scan.asteroid', 'call script lib.xi.find')

    def test_script_command_by_name(self):
        c = self.codes.get('!ship.cmd.returnhome.std')
        if c is None or not isinstance(c[9], str):
            self.skipTest('no script command by name in this game')
        self.assertIn("// command: '%s'" % c[9], msci.dis_lines(self.pr, c))
        self.check('!ship.cmd.returnhome.std', 'script[9] as a name')

    def test_line_without_its_pin(self):
        # the pin only adds what the text cannot say: without it the assembler gives its own reading of the line
        name = '!job.fight.attackallenemies'
        if name not in self.codes:
            self.skipTest('not in this game')
        c = self.codes[name]
        text = msci.dis_lines(self.pr, c, self.A)
        body = [msci_asm.PIN_RX.sub('', l) if 'delayed command' in l else l for l in text if not l.startswith('//')]
        _, code, _ = self.A.script(body, c[5][:len(c[7] or [])], exact=True)
        self.assertNotEqual(code, c[6])
        self.assertEqual(len(code), len(c[6]))

    def test_raw_layout(self):
        # FL: an `else if not` without the jump the editor writes before it; only the raw layout gives it back
        name = '!lib.repair.target.is.stationary'
        if name not in self.codes:
            self.skipTest('not in this game')
        text = msci.dis_lines(self.pr, self.codes[name], self.A)
        if '// layout: raw' not in text:
            self.skipTest('this game has the script in an editor structure')
        self.check(name, 'layout raw')

    def test_whole_set(self):
        ok, bad = 0, []
        for name in self.codes:
            try:
                same = self.new(name) == self.codes[name]
            except msci_asm.AsmError:
                same = False
            ok += same
            if not same:
                bad.append(name)
        self.assertGreaterEqual(ok, self.MINIMUM, 'round trip of %d of %d scripts; failing: %s'
                                % (ok, len(self.codes), ' '.join(bad[:40])))


class AP(RoundTrip, unittest.TestCase):
    game, addon = os.environ.get('X3_GAME'), 'addon'
    MINIMUM = 697


class FL(RoundTrip, unittest.TestCase):
    game, addon = os.environ.get('X3_FL_GAME'), 'addon2'
    MINIMUM = 1020


if __name__ == '__main__':
    unittest.main()
