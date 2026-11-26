"""The script profiler's client (x3kc/dbgprof.py) on a dump shaped as the game sends it (docs/debugging.md "Script
profiler"): the reports by method / native / MSCI / task / body, folded stacks, a valid speedscope file, the frames."""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from x3kc import dbgprof, kcdbg
from x3kc.__main__ import main as cli

SOURCE = '''#object 801 100
class Thing = 10 {
    function Sum(n) {
        var total = 0;
        var i;
        for (i = 0; i < n; i += 1) {
            total += Twice(i);
        }
        return total;
    }
    function Twice(x) {
        return x * 2;
    }
}
'''


class DbgProf(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kc')
        src = os.path.join(self.dir, 'story.kc')
        with open(src, 'w', encoding='latin1', newline='\n') as f:
            f.write(SOURCE)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(['compile', src, '-o', os.path.join(self.dir, 'story.obj')]), 0)
        self.info = kcdbg.DebugInfo.load(os.path.join(self.dir, 'story.kcdbg'))
        self.sum = [m for m in self.info.methods if m.name == 'Sum'][0]
        self.twice = [m for m in self.info.methods if m.name == 'Twice'][0]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def dump(self):
        s_pc = self.sum.lines[-1][0] + 1
        t_pc = self.twice.lines[0][0] + 1
        nodes = [[1, s_pc, 0], [1, t_pc, 0], [2, 3, 17, 'SE_ArraySize'], [3, '!job.test', 4], [1, self.twice.entry, 1],
                 [4]]
        stacks = [[30, 0, 1], [10, 0, 2], [5, 0, 3, 1], [5, 0, 4], [1, 5, 1]]
        return {'state': 'done', 'mode': 'sample', 'hz': 1000, 'object': {'name': 'story', 'size': self.info.size,
                                                                         'crc32': self.info.crc32},
                'frequency': 1000000, 'elapsedTicks': 2000000, 'frames': 100, 'stoppedFrames': 0,
                'frameTicks': 1900000, 'maxFrameTicks': 50000, 'scriptTicks': 51000, 'nativeTicks': 8000, 'runs': 400,
                'requests': 60, 'coalesced': 0, 'idle': 1900, 'skipped': 0, 'lost': 0, 'dropped': 0,
                'sitesDropped': 0, 'latencySumTicks': 51, 'latencyMaxTicks': 9, 'overheadTicks': 100,
                'nodes': nodes, 'stacks': stacks,
                'natives': [[s_pc - 6, 3, 17, 'SE_ArraySize', 2000, 8000, 0, 30, 10]],
                'bodies': [[self.twice.entry, 900, 3000, 20, 5]], 'cases': [],
                'roots': [[self.sum.entry, '', 400, 51000, 900]],
                'slowest': [{'index': 7, 'ticks': 50000, 'scriptTicks': 9000, 'nativeTicks': 2000, 'runs': 9,
                             'samples': 3, 'topRuns': [[self.sum.entry, '', 9000, 9]],
                             'topNatives': [[s_pc - 6, 2000, 50]], 'topLeaves': [[1, 2]]}],
                'msciLines': {'!job.test': {'4': [3, 1406]}}}

    def profile(self):
        d = self.dump()
        return dbgprof.Profile(d, self.info, lambda s, i: tuple(d['msciLines'].get(s, {}).get(str(i), ())) or None)

    def test_reports(self):
        p = self.profile()
        self.assertEqual(p.samples, 51)
        rows = {r['method']: r for r in p.by_method()}
        self.assertEqual(rows['Thing::Twice']['excl'], 36)       # (30 + 5 below an MSCI line + 1 truncated)
        self.assertEqual(rows['Thing::Sum']['natives'], 10)
        self.assertEqual(rows['Thing::Sum']['incl'], 50)
        self.assertEqual(rows['Thing::Twice [body]']['excl'], 5)
        self.assertAlmostEqual(rows['Thing::Twice']['exclMs'], 51.0 * 36 / 51)
        n = p.by_native()[0]
        self.assertEqual((n['native'], n['calls'], n['callers'][0]['method']), ('SE_ArraySize', 2000, 'Thing::Sum'))
        m = p.by_msci()
        self.assertEqual(m['lines'][0], {'script': '!job.test', 'line': 3, 'samples': 5, 'ms': 5.0})
        self.assertEqual(m['commands'][0]['command'], 1406)
        self.assertEqual(p.by_task()[0]['runs'], 400)
        self.assertEqual(p.by_cxx()[0]['kind'], 'body')
        f = p.frames()[0]
        self.assertEqual((f['frame'], f['ms'], f['topNatives'][0]['native']), (7, 50.0, 'SE_ArraySize'))
        for by in ('method', 'native', 'msci', 'task', 'body'):
            self.assertTrue(dbgprof.text_report(p, by, 10))
        self.assertTrue(dbgprof.text_frames(p, 5))

    def test_flame(self):
        p = self.profile()
        folded = p.folded()
        self.assertIn('Thing::Sum;Thing::Twice 30', folded)
        self.assertIn('Thing::Sum;msci !job.test:3;Thing::Twice 5', folded)
        self.assertIn('Thing::Sum;native SE_ArraySize 10', folded)
        doc = p.speedscope()
        self.assertEqual(dbgprof.check_speedscope(doc), [])
        self.assertAlmostEqual(doc['profiles'][0]['endValue'], 51.0)
        doc['profiles'][0]['samples'][0] = [99]
        self.assertTrue(dbgprof.check_speedscope(doc))


if __name__ == '__main__':
    unittest.main()
