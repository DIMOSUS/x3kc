"""Every claim of docs/msci_manual.md about expressions, conditions, objects and arrays, run through the game's own
interpreter (ScriptObject::__runScript of the installed x3story) in the emulator.

Set X3_GAME (Albion Prelude) and/or X3_FL_GAME (Terran Conflict directory with Farnham's Legacy in addon2).
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from x3kc import gameio, kcmsci, kcobj, kcvm

NULL, INT, STR = 0, 4, 5
# (lines, (type, value) returned)
CASES = {
    'int wraps': (['$a = 2147483647', '$b = $a + 1', 'return $b'], (INT, -2147483648)),
    'compare by 32-bit difference': (['$a = 2000000000', '$b = -2000000000', '$c = $a > $b', 'return $c'], (NULL, 0)),
    'false is null': (['$c = 1 > 2', 'return $c'], (NULL, 0)),
    'true is 1': (['$c = 2 > 1', 'return $c'], (INT, 1)),
    'a stored false is not 0': (['$c = 1 > 2', '$e = $c == 0', 'return $e'], (NULL, 0)),
    'null + int concatenates': (['$c = null', '$e = $c + 1', '$t = datatype[ $e ]', 'return $t'], (20, 5)),
    'divide by zero': (['$a = 5', '$b = $a / 0', 'return $b'], (NULL, 0)),
    'mod by zero': (['$a = 5', '$b = $a mod 0', 'return $b'], (NULL, 0)),
    'division truncates': (['$b = -7 / 2', 'return $b'], (INT, -3)),
    'mod keeps the left sign': (['$b = -7 mod 3', 'return $b'], (INT, -1)),
    'string + int': (["$s = 'abc'", '$t = $s + 5', 'return $t'], (STR, 'abc5')),
    'int + string': (["$s = 'abc'", '$t = 5 + $s', 'return $t'], (STR, '5abc')),
    'empty string + int': (["$s = ''", '$t = $s + 5', 'return $t'], (STR, '5')),
    'null + string prints null': (["$s = 'a'", '$t = $s + $zz', '$u = $t == \'a\'', 'return $u'], (NULL, 0)),
    'string - int is null': (["$s = 'abc'", '$t = $s - 1', 'return $t'], (NULL, 0)),
    'bit op on a string is null': (["$s = 'abc'", '$t = $s & 1', 'return $t'], (NULL, 0)),
    'null == 0 is false': (['$a = null', '$c = $a == 0', 'return $c'], (NULL, 0)),
    'unset variable is null': (['$c = $zz == null', 'return $c'], (INT, 1)),
    'strings are case-sensitive': (["$a = 'abc'", "$c = $a == 'ABC'", 'return $c'], (NULL, 0)),
    'strings order by bytes': (["$a = 'abc'", "$c = $a < 'abd'", 'return $c'], (INT, 1)),
    'empty string is true': (["$a = ''", '$r = 0', 'if $a', ' $r = 1', 'end', 'return $r'], (INT, 1)),
    'not of a string is false': (["$a = ''", '$r = 0', 'if not $a', ' $r = 1', 'end', 'return $r'], (INT, 0)),
    'negative is true': (['$a = -1', '$r = 0', 'if $a', ' $r = 1', 'end', 'return $r'], (INT, 1)),
    'AND gives 1': (['$c = 5 AND 7', 'return $c'], (INT, 1)),
    'precedence': (['$x = 3 + 4 * 2 - ( 1 + 1 )', 'return $x'], (INT, 9)),
    'unary minus binds tighter': (['$a = 2', '$x = -$a * 3', 'return $x'], (INT, -6)),
    'array read past the end': (['$a = array alloc: size=2', '$x = $a[5]', 'return $x'], (NULL, 0)),
    'array negative index': (['$a = array alloc: size=2', '$x = $a[-1]', 'return $x'], (NULL, 0)),
    'array write past the end ignored': (['$a = array alloc: size=2', '$a[5] = 7', '$x = size of array $a',
                                          'return $x'], (INT, 2)),
    'arrays are references': (['$a = array alloc: size=1', '$b = $a', '$b[0] = 9', '$x = $a[0]', 'return $x'],
                              (INT, 9)),
    'size of null': (['$x = size of array $zz', 'return $x'], (INT, 0)),
    'gosub': (['$x = 1', 'gosub sub1', '$x = $x * 10', 'return $x', 'sub1:', '$x = $x + 1', 'endsub'], (INT, 20)),
    'unknown script gives null': (["$x = [THIS] -> call script no.such.script : ", 'return $x'], (NULL, 0)),
    'wait that runs out gives 0': (['$x = wait 100 ms', 'return $x'], (INT, 0)),
}
# with a ship argument $s, alive and destroyed: (alive result, destroyed result)
OBJECT_CASES = {
    'if $s': (['$r = 0', 'if $s', ' $r = 1', 'end', 'return $r'], (1, 1)),
    'if not $s': (['$r = 0', 'if not $s', ' $r = 1', 'end', 'return $r'], (0, 0)),
    '$s == null': (['$c = $s == null', 'return $c'], (0, 0)),
    '-> exists': (['$c = $s -> exists', 'return $c'], (1, 0)),
    '$s AND 1': (['$c = $s AND 1', 'return $c'], (1, 0)),
    'command on it': (['$c = $s -> get sector', 'return $c'], (0, 0)),
}


class Semantics:
    game = addon = None

    @classmethod
    def setUpClass(cls):
        if not cls.game or not os.path.isdir(cls.game):
            raise unittest.SkipTest('set %s' % ('X3_GAME' if cls.addon == 'addon' else 'X3_FL_GAME'))
        cls.dir = tempfile.mkdtemp(prefix='x3kcsem')
        work = os.path.join(cls.dir, 'msci')
        with contextlib.redirect_stdout(io.StringIO()):
            gameio.msci_setup(cls.game, work, addon=cls.addon)
        with open(os.path.join(work, 'msci_db.json'), encoding='utf-8') as f:
            cls.db = json.load(f)
        cls.obj = kcobj.load(os.path.join(work, 'x3story.obj'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_expressions_and_values(self):
        for name, (lines, want) in CASES.items():
            m = kcmsci.Msci(kcvm.VM(self.obj), self.db)
            r = m.run_text(lines)
            self.assertEqual((r.type, r.value), want, name)

    def test_object_references(self):
        for name, (lines, want) in OBJECT_CASES.items():
            got = []
            for alive in (True, False):
                vm = kcvm.VM(self.obj)
                m = kcmsci.Msci(vm, self.db)
                ship = vm.new(2004)
                if not alive:
                    vm.delete(ship)
                r = m.run(m.codearray(lines, args=('s',)), args=kcmsci.to_cells([65542, ship]))
                got.append(r.value if r.type == INT else 0)
            self.assertEqual(tuple(got), want, name)


class AP(Semantics, unittest.TestCase):
    game, addon = os.environ.get('X3_GAME'), 'addon'


class FL(Semantics, unittest.TestCase):
    game, addon = os.environ.get('X3_FL_GAME'), 'addon2'


if __name__ == '__main__':
    unittest.main()
