"""The live debugger's stage 2b client parts without a game (docs/debugging.md "Debugger code", "Hot reload",
"Snapshots"): snippets compiled against a sidecar with their relocations, the reload plan (changed methods found,
relocatable, layout changes refused), the snapshot diff."""
import io
import os
import shutil
import struct
import tempfile
import unittest
from contextlib import redirect_stdout

from x3kc import dbg, kcdbg, kcisa, kcreload, kcsnip
from x3kc.__main__ import main as cli

SOURCE = '''#object 801 100
var gCount = 0;

class Thing = 10 {
    var m0 = 0;
    function Sum(n) {
        var total = 0;
        var i;
        for (i = 0; i < n; i += 1) {
            total += i;
        }
        return total;
    }
    function Twice(x) {
        return x * 2;
    }
    function Slow() {
        TI_Delay(1);
        return Twice(1);
    }
    function Calm() {
        return Twice(2);
    }
}
'''


class Dbg2b(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kc')
        self.src = os.path.join(self.dir, 'story.kc')
        self.write(SOURCE)
        self.obj = os.path.join(self.dir, 'story.obj')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(['compile', self.src, '-o', self.obj]), 0)
        self.info = kcdbg.DebugInfo.load(os.path.join(self.dir, 'story.kcdbg'))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.src, 'w', encoding='latin1', newline='\n') as f:
            f.write(text)

    def test_snippet_reads_frame_cells_below_its_frame(self):
        m = [x for x in self.info.methods if x.name == 'Sum'][0]
        pc = m.lines[-2][0]
        outer = {n: c for n, k, c in self.info.frame_vars(m, pc, resume=False) if k in ('param', 'local')}
        s = kcsnip.compile_snippet(self.info, 'total + n * m0 + gCount', cls=10, outer=outer)
        self.assertEqual(s['low'], min(outer[n] for n in ('total', 'n')))
        self.assertEqual(s['high'], max(outer[n] for n in ('total', 'n')))
        ops = [i for i in kcisa.walk(bytes.fromhex(s['code']))]
        self.assertEqual(ops[0].op, 0x6e)               # a method of its own: the frame instruction first
        loads = [i.arg for i in ops if i.op == 0x0d]
        # (the first read: total, at cell c -> ldl 3 + 1 + c - low past the entry's argc / retpc / ctx)
        self.assertEqual(loads[0], 4 + outer['total'] - s['low'])

    def test_snippet_relocations_and_heap_strings(self):
        s = kcsnip.compile_snippet(self.info, 'var i; var t = ""; for (i = 0; i < 3; i++) { t = t + "x"; } '
                                   'return {t, Twice(4), SE_Random(2)};', block=True, cls=10)
        code = bytes.fromhex(s['code'])
        texts = sorted(t for _, t in s['strings'])
        self.assertIn('x', texts)
        self.assertIn('SE_Random', texts)
        twice = [x for x in self.info.methods if x.name == 'Twice'][0]
        calls = [i.arg for i in kcisa.walk(code) if i.op in (0x84, 0x86, 0x88)]
        self.assertEqual(calls, [twice.entry])        # a method of the class: called by its loaded entry
        for p in s['jumps']:                            # relative targets, inside the block
            self.assertLess(struct.unpack_from('>I', code, p)[0], len(code))
        self.assertEqual(s['natives'], ['SE_Random'])
        with self.assertRaises(kcsnip.SnipError):
            kcsnip.compile_snippet(self.info, 'fork { gCount = 1; } return 0;', block=True)
        with self.assertRaises(kcsnip.SnipError):
            kcsnip.compile_snippet(self.info, 'n +', cls=10)

    def test_reload_plan(self):
        obj = {'name': 'story', 'size': self.info.size, 'crc32': self.info.crc32}
        self.write(SOURCE.replace('return x * 2;', 'var y = x * 3;\n        return y;'))
        p = kcreload.plan(self.info, obj, self.src, None, self.obj)
        self.assertEqual([m['name'] for m in p['methods']], ['Thing::Twice'])
        m = p['methods'][0]
        old = [x for x in self.info.methods if x.name == 'Twice'][0]
        self.assertEqual((m['entry'], m['end']), (old.entry, old.end))
        # a literal the loaded pool lacks: compiled as a heap string (pushs "zz"; pushs ""; add)
        self.write(SOURCE.replace('return x * 2;', 'SE_Dprintf("zz %d", x);\n        return x * 2;'))
        m = kcreload.plan(self.info, obj, self.src, None, self.obj)['methods'][0]
        ops = [i.op for i in kcisa.walk(bytes.fromhex(m['code']))]
        k = ops.index(0x0b)
        self.assertEqual(ops[k:k + 3], [0x0b, 0x0b, 0x46])
        self.assertIn('SE_Dprintf', m['body'][0])       # (the call graph of later snippets sees the new body)
        self.write(SOURCE)                              # the same source again: nothing changed
        self.assertEqual(kcreload.plan(self.info, obj, self.src, None, self.obj)['methods'], [])
        self.write(SOURCE.replace('var m0 = 0;', 'var m0 = 0;\n    var m1 = 0;'))
        with self.assertRaises(kcreload.ReloadError):
            kcreload.plan(self.info, obj, self.src, None, self.obj)

    def test_written_cells_and_the_callees_checked(self):
        from x3kc import targets
        m = [x for x in self.info.methods if x.name == 'Sum'][0]
        pc = m.lines[-2][0]
        outer = {n: c for n, k, c in self.info.frame_vars(m, pc, resume=False) if k in ('param', 'local')}
        s = kcsnip.compile_snippet(self.info, 'total += 5; return n;', block=True, cls=10, outer=outer)
        self.assertEqual(s['written'], [outer['total']])
        g = kcsnip.CallGraph(self.info, self.obj)
        sync, known = targets.sync('ap'), targets.known('ap')
        self.assertIsNone(g.refusal(kcsnip.compile_snippet(self.info, 'Calm()', cls=10), sync, known))
        why = g.refusal(kcsnip.compile_snippet(self.info, 'Slow()', cls=10), sync, known)
        self.assertIn('TI_Delay', why)

    def test_snapshot_diff(self):
        a = {'globals': {'g': 1}, 'statics': {}, 'objects': {'-1': {'class': 'Thing', 'fields': {'m0': 0}}},
             'arrays': {'5': [1, 2]}, 'tables': {}, 'tasks': {'7': {'state': 'waiting', 'location': 'a'}}}
        b = {'globals': {'g': 2}, 'statics': {}, 'objects': {'-1': {'class': 'Thing', 'fields': {'m0': {'s': 'x'}}},
                                                             '-2': {'class': 'Thing', 'fields': {}}},
             'arrays': {'5': [1, 3]}, 'tables': {}, 'tasks': {}}
        text = '\n'.join(dbg.snapshot_diff(a, b))
        self.assertIn('g: 1 -> 2', text)
        self.assertIn('#-1 Thing.m0: 0 -> "x"', text)
        self.assertIn('#-2 Thing', text)
        self.assertIn('[1] 2 -> 3', text)
        self.assertIn('task 7 ended', text)


if __name__ == '__main__':
    unittest.main()
