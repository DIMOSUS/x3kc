"""x3kc add / remove, and compile's check that the index lists every part file."""
import os
import shutil
import tempfile
import unittest

from x3kc import kcc, kcobj, kcsplit
from x3kc.__main__ import main as cli

from test_kc import SOURCE


class Add(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kcadd')
        p = os.path.join(self.dir, 't.kc')
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(SOURCE)
        self.d = os.path.join(self.dir, 'p')
        self.assertEqual(cli(['split', p, self.d]), 0)
        self.index = os.path.join(self.d, 't.kcs')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def parts(self):
        return [f for f, _ in kcsplit.read_index(self.index)]

    def test_add_class_and_function(self):
        self.assertEqual(cli(['add', self.d, 'class', 'Boat', '--parent', 'Ship']), 0)
        self.assertEqual(cli(['add', self.index, 'function', 'Launch']), 0)
        parts = self.parts()
        self.assertEqual(parts.index('Boat.kc'), parts.index('Ship.kc') + 1)       # after the last class part
        self.assertEqual(parts[-1], '@Launch.kc')                                   # after the last function
        o = kcc.compile_source(self.d)[0]
        boat = next(c for c in o.classes if c.id == 9500)
        self.assertEqual(boat.parent, 11)
        self.assertIn('Launch', [o.string(r[1]) for r in o.symb])
        # the next id is above every id of the project
        self.assertEqual(cli(['add', self.d, 'class', 'Raft']), 0)
        self.assertIn(9501, [c.id for c in kcc.compile_source(self.d)[0].classes])

    def test_after_and_explicit_id(self):
        self.assertEqual(cli(['add', self.d, 'class', 'Early', '--id', '42', '--after', 'Thing.kc']), 0)
        parts = self.parts()
        self.assertEqual(parts.index('Early.kc'), parts.index('Thing.kc') + 1)
        self.assertIn(42, [c.id for c in kcc.compile_source(self.d)[0].classes])

    def test_refusals(self):
        self.assertEqual(cli(['add', self.d, 'class', 'Ship']), 1)                    # name exists
        self.assertEqual(cli(['add', self.d, 'class', 'Other', '--id', '10']), 1)     # id exists
        self.assertEqual(cli(['add', self.d, 'class', 'C10']), 1)                     # C<n> of an existing id
        self.assertEqual(cli(['add', self.d, 'function', 'Helper']), 1)               # function exists
        self.assertEqual(cli(['add', self.d, 'class', 'X', '--parent', 'Nope']), 1)   # unknown parent
        self.assertEqual(cli(['add', self.d, 'class', 'while']), 1)                   # keyword
        # the parent must stand before the class
        self.assertEqual(cli(['add', self.d, 'class', 'Y', '--parent', 'Ship', '--after', 'Thing.kc']), 1)
        self.assertEqual(self.parts(), [f for f, _ in kcsplit.read_index(self.index)])
        self.assertFalse(os.path.exists(os.path.join(self.d, 'Y.kc')))

    def test_remove(self):
        self.assertEqual(cli(['add', self.d, 'class', 'Boat']), 0)
        self.assertEqual(cli(['remove', self.d, 'Boat']), 0)
        self.assertNotIn('Boat.kc', self.parts())
        self.assertFalse(os.path.exists(os.path.join(self.d, 'Boat.kc')))
        self.assertEqual(cli(['remove', self.d, 'Nothing']), 1)

    def test_compile_reports_unlisted_and_missing_parts(self):
        with open(os.path.join(self.d, 'Forgotten.kc'), 'w') as f:
            f.write('class Forgotten = 77 {\n}\n')
        with self.assertRaises(kcc.CompileError) as c:
            kcc.compile_source(self.d)
        self.assertIn('Forgotten.kc: not in the index', str(c.exception))
        self.assertEqual(cli(['compile', self.d]), 1)
        os.remove(os.path.join(self.d, 'Forgotten.kc'))
        os.remove(os.path.join(self.d, 'Ship.kc'))
        with self.assertRaises(kcc.CompileError) as c:
            kcc.compile_source(self.d)
        self.assertIn('cannot read part Ship.kc', str(c.exception))


if __name__ == '__main__':
    unittest.main()
