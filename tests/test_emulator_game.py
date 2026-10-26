"""The emulator and the verifier on the game's own objects: set X3_GAME to the game directory.

  * every object of the game passes kcverify;
  * MSCI scripts run through the game's interpreter (ScriptObject::__runScript in x3story);
  * a mod that adds an MSCI command goes through the whole pipeline: x3story decompiled with the bundled names gets
    command 5000 "%0triple value: %1", kcc builds it against the stock object (--base), the bytecode verifies,
    every savegame return site maps, the command runs, and the same script on the stock object neither crashes
    nor hangs;
  * a script task saved in the middle of a wait on the stock object goes on running on the modded build
    (its pcs moved through PTCH, as the game does when it loads a save).
"""
import copy
import json
import os
import shutil
import tempfile
import unittest

from x3kc import gameio, kcc, kcd, kcmsci, kcobj, kcsave, kcverify, kcvm
from x3kc.__main__ import main as cli

GAME = os.environ.get('X3_GAME')

SIGNATURE = ('        cmdDefs = {1: {{8}, 0}, ', '        cmdDefs = {1: {{8}, 0}, 5000: {{15, 10}, 0}, ')
CASE_ANCHOR = '                case 129:\n'
CASE = '''                case 5000:
                    // 5000: %0triple value: %1 (a command added by a mod)
                    resultVar = instruction[1];
                    {
                        var argType = instruction[2];
                        var argValue = instruction[3];
                        if (argType == 131074) {
                            argType = varTypes[argValue];
                            argValue = varValues[argValue];
                        }
                        resultType = 4;
                        resultValue = 3 * argValue;
                    }
                    break;
'''


@unittest.skipUnless(GAME and os.path.isdir(GAME), 'set X3_GAME to the game directory')
class Emulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix='x3kcemu')
        cls.objs = os.path.join(cls.dir, 'objs')
        gameio.extract(GAME, cls.objs)
        work = os.path.join(cls.dir, 'msci')
        gameio.msci_setup(GAME, work)
        cls.db = json.load(open(os.path.join(work, 'msci_db.json'), encoding='utf-8'))
        cls.stock_path = os.path.join(cls.objs, 'x3story.obj')
        cls.stock = kcobj.load(cls.stock_path)
        cls.modded = cls.build_mod()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def build_mod(cls):
        src = os.path.join(cls.dir, 'named')
        assert cli(['decompile', cls.stock_path, src, '--names', 'auto']) == 0
        p = os.path.join(src, 'x3story.kc')
        s = open(p, encoding='latin1').read()
        assert s.count(SIGNATURE[0]) == 1 and s.count(CASE_ANCHOR) == 1
        s = s.replace(SIGNATURE[0], SIGNATURE[1]).replace(CASE_ANCHOR, CASE + CASE_ANCHOR)
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(s)
        out = os.path.join(cls.dir, 'mod.obj')
        assert cli(['compile', p, '-o', out, '--base', cls.stock_path]) == 0
        return kcobj.load(out)

    def vm(self, obj):
        vm = kcvm.VM(obj)
        vm.load_names('auto:x3story')
        return vm

    def msci(self, obj, db=None):
        return kcmsci.Msci(self.vm(obj), db or self.db)

    def mod_db(self):
        db = copy.deepcopy(self.db)
        db['cmds']['5000'] = [[15, 10], 0]
        db['text']['2003']['5000'] = '%0triple value: %1'
        return db

    def test_every_object_verifies(self):
        natives = kcverify.load_natives()
        for f in sorted(os.listdir(self.objs)):
            V = kcverify.Verifier(kcobj.load(os.path.join(self.objs, f)), natives)
            V.run()
            self.assertEqual(dict(V.errors), {}, f)

    def test_msci_expressions(self):
        def ev(lines):
            return self.msci(self.stock).run_text(lines).value
        r = self.msci(self.stock).run_text(['$a = 5', 'return $a'])
        self.assertEqual((r.type, r.value), (4, 5))
        self.assertEqual(ev(['$a = 7', '$b = ( $a + 1 ) * 2 - 10 / 3', 'return $b']), 13)
        self.assertEqual(ev(['$a = 6', '$b = $a mod 4 + -$a', 'return $b']), -4)
        self.assertEqual(ev(['$a = 3', '$b = $a == 3 AND $a < 5', 'return $b']), 1)
        self.assertEqual(ev(["$s = 'ab'", "$t = $s + 'cd'", 'return $t']), 'abcd')

    def test_msci_control_flow(self):
        src = ['$i = 0', '$s = 0',
               'while $i < 10',
               ' inc $i =',
               ' skip if $i != 3',
               '  continue',
               ' if $i == 8',
               '  break',
               ' else if $i > 5',
               '  $s = $s + 100',
               ' else',
               '  $s = $s + $i',
               ' end',
               'end',
               'return $s']
        # i = 1..7: 1 + 2 + 4 + 5 + 100 + 100 = 212 (3 skipped by continue, 8 breaks)
        self.assertEqual(self.msci(self.stock).run_text(src).value, 212)

    def test_new_command_verifies_and_keeps_saves(self):
        V = kcverify.Verifier(self.modded, kcverify.load_natives())
        V.run()
        self.assertEqual(dict(V.errors), {})
        segs = kcsave.code_map(self.stock, self.modded)
        ok, gone, bad = kcsave.check_return_sites(self.stock, self.modded, segs)
        self.assertEqual(bad, 0)
        self.assertGreater(ok, 0)

    def test_new_command_runs(self):
        m = self.msci(self.modded, self.mod_db())
        r = m.run_text(['$x = triple value: 14', 'return $x'])
        self.assertEqual((r.type, r.value), (4, 42))
        r = m.run_text(['$a = 5', '$x = triple value: $a', 'return $x'])
        self.assertEqual((r.type, r.value), (4, 15))

    def test_new_command_on_the_stock_object(self):
        """The same script on an object without command 5000: no crash, no hang."""
        r = self.msci(self.stock, self.mod_db()).run_text(['$x = triple value: 14', 'return $x'])
        self.assertNotEqual(r.value, 42)

    def test_saved_wait_continues_on_the_modded_build(self):
        vm = self.vm(self.stock)
        m = kcmsci.Msci(vm, self.db)
        code = m.codearray(['$a = 6', '@ = wait 5000 ms', '$b = $a * 7', 'return $b'])
        out = kcvm.KArray([0, 0])
        task = kcvm.KArray([0] * 13)
        it = task.items
        it[1], it[3], it[4], it[5], it[12] = 0, 3, kcvm.KArray([0, 0, 0]), kcvm.KArray([0] * 36), 1
        t = vm.call(702, '__runScript', [task, kcmsci.to_cells(code), 0, 0, 0, out], run=False)
        vm.execute(t)
        vm.run(1000)
        self.assertEqual(t.state, 'wait')                     # saved here
        nv = vm.migrate(self.modded)
        nv.run(10000)
        self.assertIsNone(t.error)
        self.assertEqual(t.state, 'done')
        self.assertEqual((out.items[0], out.items[1]), (4, 42))


if __name__ == '__main__':
    unittest.main()
