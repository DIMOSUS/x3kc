"""The engine around MSCI scripts, run through the game's own KC in the emulator (docs/msci_manual.md, "Commands,
signals, tasks, AL, saves"): hotkeys, task slots, interrupts and their priorities, waits cut short, AL events, the
lookup of a ship command's script; and that FL commands without an editor text can be written (synthetic texts).

Set X3_GAME (Albion Prelude) and/or X3_FL_GAME (Terran Conflict directory with Farnham's Legacy in addon2).
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from x3kc import gameio, kcmsci, kcobj, kcvm, msci, msci_asm

SHIP = 65542


class Engine:
    game = addon = None

    @classmethod
    def setUpClass(cls):
        if not cls.game or not os.path.isdir(cls.game):
            raise unittest.SkipTest('set %s' % ('X3_GAME' if cls.addon == 'addon' else 'X3_FL_GAME'))
        cls.dir = tempfile.mkdtemp(prefix='x3kceng')
        work = os.path.join(cls.dir, 'msci')
        with contextlib.redirect_stdout(io.StringIO()):
            gameio.msci_setup(cls.game, work, addon=cls.addon)
        with open(os.path.join(work, 'msci_db.json'), encoding='utf-8') as f:
            cls.db = json.load(f)
        cls.obj = kcobj.load(os.path.join(work, 'x3story.obj'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def fresh(self):
        vm = kcvm.VM(self.obj)
        vm.load_names('auto:x3story')
        return vm, kcmsci.Msci(vm, self.db)

    def add(self, m, name, lines, args=()):
        m.vm.call(700, 'AddScript', [kcmsci.to_cells(m.codearray(lines, name=name, args=args))])

    def gvar(self, m, key):
        r = m.run_text(["$x = get global variable: name='%s'" % key, 'return $x'])
        return r.type, r.value

    def test_hotkey(self):
        vm, m = self.fresh()
        self.add(m, 'x.hk.target', ["set global variable: name='hk.arg' value=$a", 'return null'], args=('a',))
        hk = m.run_text(["$hk = register hotkey 'X' to call script x.hk.target", 'return $hk']).value
        self.assertGreaterEqual(hk, 1536)
        again = m.run_text(["$hk = register hotkey 'X' to call script x.hk.target", 'return $hk']).value
        self.assertEqual(again, hk)                                     # the same script: the same id
        if self.addon != 'addon':
            return      # FL's TriggerCustomHotkey(id, key) translates a real key code first: not emulated here
        vm.call(500, 'TriggerCustomHotkey', [hk])                       # InputMapper: the key is pressed
        vm.run(100)
        self.assertEqual(self.gvar(m, 'hk.arg'), (4, hk))               # argument 1 = the hotkey id

    def test_task_slots_from_30(self):
        vm, m = self.fresh()
        ship = vm.new(2004)
        self.add(m, 'x.bg', ['$r = wait 5000 ms', "set global variable: name='bg.r' value=$r", 'return null'])
        r = m.run_text(['$n = [THIS] -> get next available task: starting=0',
                        '[THIS] -> start task $n with script x.bg and prio 0: arg1=null arg2=null arg3=null arg4=null '
                        'arg5=null', '$u = [THIS] -> is task $n in use', 'return $u'], this=ship, this_type=SHIP)
        self.assertEqual(r.value, 1)
        nxt = m.run_text(['$n = [THIS] -> get next available task: starting=0', 'return $n'], this=ship,
                         this_type=SHIP).value
        self.assertEqual(nxt, 31)                                       # 30 was taken by x.bg
        vm.run(6000)
        self.assertEqual(self.gvar(m, 'bg.r'), (4, 0))                  # a wait that ran out gives 0

    def test_interrupt_priority_and_waits(self):
        vm, m = self.fresh()
        ship = vm.new(2004)
        self.add(m, 'x.long', ['$r = wait 5000 ms', "$k = 'long.r.' + $slot", 'set global variable: name=$k value=$r',
                               'return null'], args=('slot',))
        self.add(m, 'x.int', ["set global variable: name='int.ran' value=1", 'return null'])
        m.run_text(['[THIS] -> start task 31 with script x.long and prio 50: arg1=31 arg2=null arg3=null arg4=null '
                    'arg5=null', '[THIS] -> start task 32 with script x.long and prio 0: arg1=32 arg2=null arg3=null '
                    'arg4=null arg5=null', 'return null'], this=ship, this_type=SHIP)
        vm.run(1000)
        m.run_text(['[THIS] -> interrupt task 31 with script x.int and prio 10: arg1=null arg2=null arg3=null '
                    'arg4=null', 'return null'], this=ship, this_type=SHIP)
        vm.run(10)
        self.assertEqual(self.gvar(m, 'int.ran'), (0, 0))               # 10 <= 50: refused
        m.run_text(['[THIS] -> interrupt task 32 with script x.int and prio 10: arg1=null arg2=null arg3=null '
                    'arg4=null', 'return null'], this=ship, this_type=SHIP)
        vm.run(10)
        self.assertEqual(self.gvar(m, 'int.ran'), (4, 1))               # 10 > 0: accepted
        # TI_Interrupt(ship) ends the waits of every task of the ship: both get the remaining milliseconds
        self.assertEqual(self.gvar(m, 'long.r.31'), (4, 3990))
        self.assertEqual(self.gvar(m, 'long.r.32'), (4, 3990))

    def test_al_events(self):
        vm, m = self.fresh()
        self.add(m, 'x.al.ctrl', ["$k = 'al.' + $ev", 'set global variable: name=$k value=$id', 'return 1'],
                 args=('id', 'ev'))
        m.run_text(['al engine: register script=x.al.ctrl', 'return null'])
        vm.call(301, 'InitPlugins', [])
        vm.run(100)
        self.assertEqual(self.gvar(m, 'al.init'), (5, 'x.al.ctrl'))    # arg 1 = own name, arg 2 = event
        self.assertTrue(vm.call(301, 'ExecPluginIsEnabled', ['x.al.ctrl']))

    def test_command_script_lookup(self):
        vm, m = self.fresh()
        ship = vm.new(2004)
        self.add(m, 'x.glob', ['return 1'])
        self.add(m, 'x.own', ['return 2'])

        def bound():
            r = vm.call(ship, 'GetCommandScript', [715])
            return (r.items[0].items[0], r.items[1]) if hasattr(r, 'items') else r
        m.run_text(['global script map: set: key=COMMAND_TYPE_CUSTOM_15, class=Ship, race=null, script=x.glob, '
                    'prio=5', 'return null'])
        self.assertEqual(bound(), ('x.glob', 5))
        m.run_text(['[THIS] -> connect ship command/signal COMMAND_TYPE_CUSTOM_15 to script x.own with prio 7',
                    'return null'], this=ship, this_type=SHIP)
        self.assertEqual(bound(), ('x.own', 7))                          # the ship's own binding first
        m.run_text(['[THIS] -> ignore ship command/signal COMMAND_TYPE_CUSTOM_15', 'return null'], this=ship,
                   this_type=SHIP)
        self.assertEqual(bound(), 0)                                     # ignored: the global entry is not used


class AP(Engine, unittest.TestCase):
    game, addon = os.environ.get('X3_GAME'), 'addon'


class FL(Engine, unittest.TestCase):
    game, addon = os.environ.get('X3_FL_GAME'), 'addon2'

    def test_synthetic_texts_write_and_print(self):
        """FL commands no shipped script uses get `fl.<id>` texts: written by the assembler, printed by dis."""
        syn = self.db.get('learned', {}).get('synthetic', [])
        self.assertGreater(len(syn), 900)
        self.assertIn(1789, syn)                                         # [RefObj, ship/station type]
        A = msci_asm.Assembler(self.db)
        names, code, extra = A.script(['$hq -> fl.1789: $type', 'return null'], [])
        self.assertEqual(code[0], [1789, 131074, 0, 131074, 1])
        pr = msci.Printer(self.db)
        line = pr.line([0, 40, '', 0, 0, names, code, 0, 0, 0], code[0])
        self.assertEqual(' '.join(line.split()), '$hq -> fl.1789: $type')


if __name__ == '__main__':
    unittest.main()
