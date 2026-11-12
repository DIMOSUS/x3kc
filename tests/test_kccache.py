"""compile --cache: an object built with a warm cache is byte for byte the one built cold, whatever was edited."""
import os
import shutil
import tempfile
import unittest

from x3kc import kccache, kcc
from x3kc.__main__ import main as cli

from test_kc import SOURCE

EDITS = {
    'method body': lambda s: s.replace('m0 = a + b * 2;', 'm0 = a + b * 3;'),
    'field added': lambda s: s.replace('    var m1 = {:};\n', '    var m1 = {:};\n    var extra = 5;\n'),
    'field removed': lambda s: s.replace('    var m1 = {:};\n', '').replace('        m1["k"] = b;\n', ''),
    'class added': lambda s: s + '\nclass Boat = 12 : Thing {\n    function Go() {\n        return Sum(2);\n    }\n}\n',
    'method renamed': lambda s: s.replace('function Sum(n)', 'function Total(n)').replace('Sum(', 'Total('),
    'global renamed': lambda s: s.replace('gCount', 'gTotal'),
    'global value changed': lambda s: s.replace('var gCount = 0;', 'var gCount = 7;'),
    'global added': lambda s: s.replace('var gList = {};', 'var gList = {};\nvar gMore = 1;'),
    'header changed': lambda s: s.replace('#object 801 100', '#object 801 101'),
    'parent changed': lambda s: s.replace('class Ship = 11 : Thing', 'class Ship = 11'),
}


class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kccache')
        self.cache = os.path.join(self.dir, 'cache')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def project(self, text):
        """The source as a split project (one part per class and function), as the builds use it."""
        p = os.path.join(self.dir, 's.kc')
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(text)
        d = os.path.join(self.dir, 'split')
        shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(cli(['split', p, d]), 0)
        return d

    def build(self, d, cache):
        return kcc.compile_source(d, kccache.Cache(self.cache) if cache else None)[0].serialize()

    def test_every_edit(self):
        for what, edit in EDITS.items():
            with self.subTest(what):
                changed = edit(SOURCE)
                self.assertNotEqual(changed, SOURCE)
                shutil.rmtree(self.cache, ignore_errors=True)
                self.build(self.project(SOURCE), True)          # warm the cache with the original
                d = self.project(changed)
                cold = self.build(d, False)
                self.assertEqual(self.build(d, True), cold)     # edited parts parsed, the rest from the cache
                self.assertEqual(self.build(d, True), cold)     # all from the cache
                self.assertEqual(self.build(self.project(SOURCE), True), self.build(self.project(SOURCE), False))

    def test_stale_or_broken_cache_costs_only_time(self):
        d = self.project(SOURCE)
        cold = self.build(d, False)
        self.build(d, True)
        for f in os.listdir(self.cache):
            with open(os.path.join(self.cache, f), 'wb') as h:
                h.write(b'not a pickle')
        self.assertEqual(self.build(d, True), cold)

    def test_cli_cache_and_no_cache(self):
        d = self.project(SOURCE)
        a, b, c = (os.path.join(self.dir, x + '.obj') for x in 'abc')
        self.assertEqual(cli(['compile', d, '-o', a, '--no-cache']), 0)
        self.assertEqual(cli(['compile', d, '-o', b, '--cache', self.cache]), 0)
        self.assertTrue(os.listdir(self.cache))
        self.assertEqual(cli(['compile', d, '-o', c, '--cache', self.cache]), 0)
        self.assertEqual(open(a, 'rb').read(), open(b, 'rb').read())
        self.assertEqual(open(a, 'rb').read(), open(c, 'rb').read())

    def test_base_sites_cache(self):
        o = kcc.compile_source(self.project(SOURCE))[0]
        base = os.path.join(self.dir, 'base.obj')
        with open(base, 'wb') as f:
            f.write(o.serialize())
        d = self.project(EDITS['method body'](SOURCE))
        outs = []
        for k, extra in enumerate(([], ['--cache', self.cache], ['--cache', self.cache])):
            out = os.path.join(self.dir, 'm%d.obj' % k)
            self.assertEqual(cli(['compile', d, '-o', out, '--base', base, '--build', '200'] + extra), 0)
            outs.append(open(out, 'rb').read())
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])
        self.assertTrue(any(f.startswith('base-sites-') for f in os.listdir(self.cache)))


if __name__ == '__main__':
    unittest.main()
