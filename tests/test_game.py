"""Tests on the files of an installed game: set X3_GAME to the game directory (the one with X3AP.exe or X3TC.exe)."""
import os
import shutil
import tempfile
import unittest

from x3kc import gameio
from x3kc.__main__ import main as cli

GAME = os.environ.get('X3_GAME')


@unittest.skipUnless(GAME and os.path.isdir(GAME), 'set X3_GAME to the game directory')
class Game(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix='x3kcgame')
        cls.objs = os.path.join(cls.dir, 'objs')
        gameio.extract(GAME, cls.objs)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_round_trip_every_object(self):
        objs = sorted(os.path.join(self.objs, f) for f in os.listdir(self.objs))
        self.assertEqual(cli(['selftest'] + objs + ['--work', os.path.join(self.dir, 'rt')]), 0)

    def test_bundled_names_rebuild_the_same_object(self):
        for name in ('x3story', 'x3galedit', 'x3intro'):
            obj = os.path.join(self.objs, name + '.obj')
            if not os.path.exists(obj):
                continue
            out = os.path.join(self.dir, 'named')
            self.assertEqual(cli(['decompile', obj, out, '--names', 'auto']), 0)
            self.assertEqual(cli(['compile', os.path.join(out, name + '.kc'), '--check', obj]), 0)


if __name__ == '__main__':
    unittest.main()
