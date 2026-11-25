"""The debug info sidecar (x3kc/kcdbg.py, docs/debugging.md): written by compile beside the object, its line table,
variables, classes and globals, and symbolize's mapping of raw locations (no game files needed)."""
import io
import json
import os
import shutil
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout

from x3kc import kcc, kcdbg, kcisa
from x3kc.__main__ import main as cli

SOURCE = '''#object 801 100
var gCount = 0;
var gName = "n";

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
}

class Ship = 11 : Thing {
    var m2 = "ship";
    function Run(x) {
        while (x > 0) {
            x--;
            gCount++;
        }
        var s = Sum(x);
        SE_Random(s);
        return s;
    }
}
'''


class Sidecar(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kc')
        self.src = os.path.join(self.dir, 'story.kc')
        with open(self.src, 'w', encoding='latin1', newline='\n') as f:
            f.write(SOURCE)
        self.obj = os.path.join(self.dir, 'story.obj')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(['compile', self.src, '-o', self.obj]), 0)
        self.dbg = os.path.join(self.dir, 'story.kcdbg')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_obj(self):
        with open(self.obj, 'rb') as h:
            return h.read()

    def test_written_beside_the_object_and_the_object_unchanged(self):
        self.assertTrue(os.path.isfile(self.dbg))
        data = self.read_obj()
        o, _, _ = kcc.compile_source(self.src, quiet=True)
        self.assertEqual(o.serialize(), data)       # the sidecar is no input of the object
        info = kcdbg.DebugInfo.load(self.dbg)
        self.assertEqual(info.name, 'story')
        self.assertEqual(info.size, len(data))
        self.assertEqual(info.crc32, '%08x' % (zlib.crc32(data) & 0xffffffff))
        self.assertTrue(info.matches(data=data))
        self.assertFalse(info.matches(size=len(data) + 1))
        with open(self.dbg, encoding='utf-8') as h:
            raw = json.load(h)
        self.assertEqual((raw['format'], raw['version']), (kcdbg.FORMAT, kcdbg.VERSION))

    def test_classes_fields_globals(self):
        info = kcdbg.DebugInfo.load(self.dbg)
        self.assertEqual(info.globals, ['gCount', 'gName'])
        self.assertEqual(info.class_name(11), 'Ship')
        self.assertEqual(info.classes[11]['parent'], 10)
        self.assertEqual(info.field_name(11, 0), 'm0')     # inherited fields first
        self.assertEqual(info.field_name(11, 1), 'm2')
        self.assertIsNone(info.classes[10]['parent'])

    def test_every_statement_maps_to_its_line(self):
        info = kcdbg.DebugInfo.load(self.dbg)
        lines = SOURCE.split('\n')
        m = [x for x in info.methods if x.name == 'Run'][0]
        self.assertEqual(m.qualname, 'Ship::Run')
        self.assertEqual(m.file, 'story.kc')
        from x3kc import kcobj
        o = kcobj.load(self.obj)
        # the native call SE_Random: its resume pc (after the instruction) is on the line that calls it
        natives = [i for i in kcisa.walk(o.code, m.entry, m.end) if i.op == 0x82]
        self.assertEqual(len(natives), 1)
        for i in natives:
                loc = info.location(i.end)
                self.assertIn('SE_Random', lines[loc.line - 1])
                self.assertEqual(lines[loc.line - 1].index('SE_Random') + 1, loc.col)
        # the while loop's back jump belongs to the loop's line, the epilogue to the closing brace
        self.assertTrue(lines[info.location(m.entry, resume=False).line - 1].strip().startswith('function Run'))
        last = info.location(m.end - 1, resume=False)
        self.assertEqual(lines[last.line - 1].strip(), '}')
        self.assertEqual(str(info.location(m.lines[0][0] + 1)).split(' ')[1], 'Ship::Run')

    def test_variables_cells_and_live_ranges(self):
        info = kcdbg.DebugInfo.load(self.dbg)
        m = [x for x in info.methods if x.name == 'Sum'][0]
        names = {v[0]: v for v in m.vars}
        self.assertEqual(names['n'][1:3], ('param', 2))         # the first argument: R + 2
        self.assertEqual(names['total'][1:3], ('local', -2))    # the first local: R - 2
        self.assertEqual(names['i'][2], -3)
        live = [n for n, _, _ in info.frame_vars(m, m.entry + 1)]
        self.assertEqual(live, ['n'])                            # before the locals are declared
        live = [n for n, _, _ in info.frame_vars(m, m.end - 1)]
        self.assertIn('n', live)

    def test_symbolize_and_stale(self):
        info = kcdbg.DebugInfo.load(self.dbg)
        m = [x for x in info.methods if x.name == 'Run'][0]
        data = self.read_obj()
        pc = m.lines[1][0] + 1      # a resume pc inside the second statement
        load = 'kc object story loaded: build 100, %d bytes, crc32 %s, file %s' % (len(data), info.crc32, self.obj)
        warnings = []
        S = kcdbg.Symbolizer(warn=warnings.append)
        S.line(load)
        out = S.line('x kc[story 0x%x 11:Run] y' % pc)
        self.assertRegex(out, r'^x story\.kc:\d+:\d+ Ship::Run y$')
        S = kcdbg.Symbolizer(warn=warnings.append)
        S.line(load.replace(info.crc32, '00000000'))
        out = S.line('kc[story 0x%x 11:Run msci=plugin.x:3]' % pc)
        self.assertEqual(out, 'kc[story 0x%x 11:Run msci=plugin.x:3]' % pc)     # stale: stays raw
        self.assertTrue(any('stale' in w for w in warnings))
        S = kcdbg.Symbolizer([self.dir], warn=warnings.append)
        out = S.line('kc[story 0x%x 11:Run msci=plugin.x:3]' % pc)
        self.assertTrue(out.endswith('Ship::Run (msci=plugin.x:3)'), out)
        self.assertTrue(any('no load line' in w for w in warnings))

    def test_release_sidecar_gzipped_found_by_identity(self):
        import gzip
        info = kcdbg.DebugInfo.load(self.dbg)
        rel = os.path.join(self.dir, 'base')
        os.makedirs(rel)
        with open(self.dbg, 'rb') as src, gzip.open(os.path.join(rel, 'story.100.kcdbg.gz'), 'wb') as z:
            z.write(src.read())
        with open(os.path.join(rel, 'story.99.kcdbg'), 'w', encoding='utf-8') as h:     # another build's
            h.write('{"format":"x3kc-kcdbg","version":1,"object":{"name":"story","size":1,"crc32":"00000000"},'
                    '"methods":[\n]}\n')
        m = [x for x in info.methods if x.name == 'Run'][0]
        S = kcdbg.Symbolizer([rel], warn=lambda t: None)
        S.line('kc object story loaded: build 100, %d bytes, crc32 %s' % (info.size, info.crc32))
        self.assertIn('Ship::Run', S.line('kc[story 0x%x 11:Run]' % (m.lines[1][0] + 1)))

    def test_foreach_back_jump_is_the_loop_line(self):
        src = os.path.join(self.dir, 'fe.kc')
        text = '#object 801 100\nfunction F(a) {\n    var t = 0;\n    for (var x in a) {\n        t += x;\n    }\n' \
               '    return t;\n}\n'
        with open(src, 'w', encoding='latin1', newline='\n') as f:
            f.write(text)
        obj = os.path.join(self.dir, 'fe.obj')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(['compile', src, '-o', obj]), 0)
        info = kcdbg.DebugInfo.load(os.path.join(self.dir, 'fe.kcdbg'))
        from x3kc import kcobj
        o = kcobj.load(obj)
        m = info.methods[0]
        jumps = [i for i in kcisa.walk(o.code, m.entry, m.end) if i.op == 0x32]
        self.assertEqual(info.location(jumps[-1].pc, resume=False).line, 4)    # the loop's back jump: `for`


if __name__ == '__main__':
    unittest.main()
