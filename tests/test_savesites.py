"""Savegame return sites are only the calls a task can be suspended in: waiting natives and methods that reach one.
A frame change in a method that never waits does not break savegames; one in a method that waits does, also when
the wait is in a callee."""
import os
import shutil
import tempfile
import unittest

from x3kc import kcc, kcobj, kcsave

SOURCE = r'''
#object 801 100
class Thing = 10 {
    var m0 = 0;
    function Draw(x) {
        B3D_TexRectFill(13, 0, 0, x, 8, 0);
        return SE_Random(x);
    }
    function Wait(x) {
        TI_Delay(x);
        return x;
    }
    function Outer(x) {
        m0 = Wait(x);
        return m0;
    }
    function Uses(x) {
        return Draw(x) + Unknown_Native(x);
    }
}
'''


class SaveSites(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kcsites')
        self.base = self.compile(SOURCE, 'base.kc')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def compile(self, text, name):
        p = os.path.join(self.dir, name)
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(text)
        o, _, _ = kcc.compile_source(p)
        return kcobj.KcObj(o.serialize())

    def check(self, text):
        new = self.compile(text, 'new.kc')
        return kcsave.check_return_sites(self.base, new, kcsave.code_map(self.base, new))

    def methods_with_sites(self):
        live = kcsave.waiting_sites(self.base)
        spans = kcsave.method_spans(self.base)
        return sorted(name for (cid, name), (e, end) in spans.items() if any(e <= s <= end for s in live))

    def test_only_waiting_calls_are_sites(self):
        # Wait calls TI_Delay, Outer calls Wait; Draw only calls synchronous natives, Uses calls Draw and a name the
        # exe does not know (the VM answers int 0 at once)
        self.assertEqual(self.methods_with_sites(), ['Outer', 'Wait'])

    def test_frame_change_in_a_method_that_never_waits(self):
        text = SOURCE.replace('        B3D_TexRectFill(13, 0, 0, x, 8, 0);',
                              '        var w = x * 2;\n        B3D_TexRectFill(13, 0, 0, w, 8, 0);')
        ok, gone, bad = self.check(text)
        self.assertEqual(bad, 0)

    def test_frame_change_in_a_waiting_method(self):
        text = SOURCE.replace('        TI_Delay(x);', '        var d = x + 1;\n        TI_Delay(d);')
        ok, gone, bad = self.check(text)
        self.assertGreater(bad, 0)

    def test_frame_change_above_a_waiting_callee(self):
        text = SOURCE.replace('        m0 = Wait(x);', '        var y = x;\n        m0 = Wait(y);')
        ok, gone, bad = self.check(text)
        self.assertGreater(bad, 0)


if __name__ == '__main__':
    unittest.main()
