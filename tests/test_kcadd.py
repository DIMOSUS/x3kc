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

    def test_parent_as_c_number(self):
        # Ship is class 11: C11 becomes the declared name, so the part compiles; C0 (the globals) is refused
        self.assertEqual(cli(['add', self.d, 'class', 'Boat', '--parent', 'C11']), 0)
        with open(os.path.join(self.d, 'Boat.kc')) as f:
            self.assertIn(': Ship {', f.read())
        self.assertEqual(next(c for c in kcc.compile_source(self.d)[0].classes if c.id == 9500).parent, 11)
        self.assertEqual(cli(['add', self.d, 'class', 'Raft', '--parent', 'C0']), 1)
        # the ordering check applies to the C<n> form too
        self.assertEqual(cli(['add', self.d, 'class', 'Y', '--parent', 'C11', '--after', 'Thing.kc']), 1)

    def test_add_adopts_an_unlisted_part(self):
        with open(os.path.join(self.d, 'Stray.kc'), 'w') as f:
            f.write('\nfunction Stray() {\n    return 3;\n}\n')
        self.assertEqual(cli(['compile', self.d]), 1)                                  # not in the index
        self.assertEqual(cli(['add', self.d, 'class', 'Stray']), 1)                    # it declares a function
        os.rename(os.path.join(self.d, 'Stray.kc'), os.path.join(self.d, '@Stray.kc'))
        self.assertEqual(cli(['add', self.d, 'function', 'Stray']), 0)
        with open(os.path.join(self.d, '@Stray.kc')) as f:
            self.assertIn('return 3;', f.read())                                       # the file is kept
        self.assertIn('@Stray.kc', self.parts())
        self.assertEqual(cli(['compile', self.d]), 0)
        # a class file with its own id and parent
        with open(os.path.join(self.d, 'Hull.kc'), 'w') as f:
            f.write('\nclass Hull = 300 : Ship {\n}\n')
        self.assertEqual(cli(['add', self.d, 'class', 'Hull', '--id', '301']), 1)      # the file says 300
        self.assertEqual(cli(['add', self.d, 'class', 'Hull']), 0)
        self.assertEqual(next(c for c in kcc.compile_source(self.d)[0].classes if c.id == 300).parent, 11)
        # remove works with an unlisted file around
        with open(os.path.join(self.d, 'Other.kc'), 'w') as f:
            f.write('class Other = 301 {\n}\n')
        self.assertEqual(cli(['remove', self.d, 'Hull']), 0)

    def test_join_and_lock_files(self):
        self.assertEqual(cli(['join', self.d, os.path.join(self.d, 'all.kc')]), 2)    # inside the project: refused
        self.assertFalse(os.path.exists(os.path.join(self.d, 'all.kc')))
        for n in ('.#Ship.kc', '#Ship.kc'):
            with open(os.path.join(self.d, n), 'w') as f:
                f.write('lock')
        self.assertEqual(cli(['compile', self.d]), 0)                                  # editor files are ignored

    def test_remove_refuses_header_and_parents(self):
        self.assertEqual(cli(['remove', self.d, '@header']), 1)
        self.assertEqual(cli(['remove', self.d, 'Thing']), 1)                          # Ship's parent
        self.assertIn('Thing.kc', self.parts())

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
