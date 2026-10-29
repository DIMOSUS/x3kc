"""Fields added by a compile --base build, loaded from an old save the way Script::LoadVmState does (bug B19 of
the game): a new field on a level that had saved fields gets its initial value, a new field below the last level
with saved fields stays null, and compile --base warns about the second case."""
import contextlib
import io
import os
import tempfile
import unittest

from x3kc import kcc, kcobj, kcvm
from x3kc.__main__ import main as cli

BASE = r'''
class Thing = 10 {
    var m0 = 5;
}
class Ship = 20 : Thing {
}
class Gone = 30 {
    var g0 = 1;
}
function Main() {
    return 0;
}
'''

NEW = r'''
class Thing = 10 {
    var m0 = 5;
    var m1 = 6;
}
class Ship = 20 : Thing {
    var s0 = 7;
}
function Main() {
    return 0;
}
'''


def build(base_src, new_src):
    d = tempfile.mkdtemp()
    paths = {}
    for name, text in (('base', base_src), ('new', new_src)):
        paths[name] = os.path.join(d, name + '.kc')
        with open(paths[name], 'w') as f:
            f.write(text)
    o, _, _ = kcc.compile_source(paths['base'])
    with open(os.path.join(d, 'base.obj'), 'wb') as f:
        f.write(o.serialize())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli(['compile', paths['new'], '--base', os.path.join(d, 'base.obj'), '-o', os.path.join(d, 'new.obj'),
                  '--force'])
    return kcobj.load(os.path.join(d, 'base.obj')), kcobj.load(os.path.join(d, 'new.obj')), rc, out.getvalue()


class B19(unittest.TestCase):
    def test_lost_initial_value(self):
        base, new, rc, log = build(BASE, NEW)
        self.assertEqual(rc, 0)
        self.assertIn('fields 0 of class 20 (B19', log)
        vm = kcvm.VM(base)
        ship, thing = vm.new(20), vm.new(10)
        vm.fields_of(ship)[0] = 99                       # a value the save keeps
        nv = vm.migrate(new)
        # Thing level: m0 from the save, m1 is new on a level with saved fields -> its init
        self.assertEqual(nv.fields_of(thing), [5, 6])
        self.assertEqual(nv.fields_of(ship)[:2], [99, 6])
        # Ship level is below the last level with saved fields: the game leaves s0 null, not 7
        self.assertIs(nv.fields_of(ship)[2], kcvm.EMPTY)

    def test_removed_class_with_objects(self):
        base, new, rc, log = build(BASE, NEW)
        self.assertIn('class 30 was removed', log)
        self.assertEqual(rc, 0)                           # written only because of --force
        vm = kcvm.VM(base)
        vm.new(30)
        with self.assertRaises(kcvm.VMError):
            vm.migrate(new)

    def test_no_warning_when_the_level_had_fields(self):
        base_src = BASE.replace('class Ship = 20 : Thing {\n}', 'class Ship = 20 : Thing {\n    var s0 = 7;\n}')
        new_src = NEW.replace('var s0 = 7;', 'var s0 = 7;\n    var s1 = 8;')
        base, new, rc, log = build(base_src, new_src)
        self.assertNotIn('B19', log)
        vm = kcvm.VM(base)
        ship = vm.new(20)
        nv = vm.migrate(new)
        self.assertEqual(nv.fields_of(ship), [5, 6, 7, 8])


if __name__ == '__main__':
    unittest.main()
