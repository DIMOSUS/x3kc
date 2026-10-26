"""kcverify finds broken bytecode: a clean object passes, every kind of damage is reported."""
import os
import struct
import tempfile
import unittest

from x3kc import kcc, kcisa, kcverify


SRC = r'''
var gA = 0;
class Thing = 10 {
    var m0 = 0;
    function Get(a) {
        var x = a + m0;
        if (x > 2) {
            return x;
        }
        return Twice(x);
    }
    function Twice(v) {
        return v * 2;
    }
}
function Main() {
    var t = new Thing();
    return t.Get(5) + SE_Random(3);
}
'''


def build():
    d = tempfile.mkdtemp()
    p = os.path.join(d, 't.kc')
    open(p, 'w').write(SRC)
    o, _, _ = kcc.compile_source(p)
    return o


def method(o, cid, name):
    rows = o.symb if cid == 0 else next(c.methods for c in o.classes if c.id == cid)
    return next(r for r in rows if o.string(r[1]) == name)


def find(o, entry, op):
    return next(i for i in kcisa.walk(o.code, entry) if i.op == op)


def verify(o):
    V = kcverify.Verifier(o, kcverify.load_natives())
    V.run()
    return [l for ls in V.errors.values() for l in ls]


class Verify(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(verify(build()), [])

    def mutate(self, f):
        o = build()
        code = bytearray(o.code)
        f(o, code)
        o.code = bytes(code)
        return ' | '.join(verify(o))

    def test_frame_mismatch(self):
        def f(o, code):
            e = method(o, 10, 'Get')[0]
            struct.pack_into('>h', code, e + 1, 1)
        self.assertIn('frame 1 but the method row says', self.mutate(f))

    def test_missing_pop_breaks_the_stack(self):
        def f(o, code):
            e = method(o, 0, 'Main')[0]
            i = find(o, e, 0x82)                 # native call; replace its argc push by a nop
            prev = [x for x in kcisa.walk(o.code, e, i.pc)][-1]
            code[prev.pc] = 0x0c
        self.assertIn('argc', self.mutate(f))

    def test_jump_into_an_instruction(self):
        def f(o, code):
            e = method(o, 10, 'Get')[0]
            i = find(o, e, 0x34)
            struct.pack_into('>I', code, i.pc + 1, i.arg + 1)
        self.assertIn('not an instruction of the method', self.mutate(f))

    def test_member_out_of_range(self):
        def f(o, code):
            e = method(o, 10, 'Get')[0]
            i = find(o, e, 0x0f)
            struct.pack_into('>h', code, i.pc + 1, 7)
        self.assertIn('class 10 has 1 fields', self.mutate(f))

    def test_write_over_return_pc(self):
        def f(o, code):
            e = method(o, 10, 'Twice')[0]
            i = find(o, e, 0x14) if any(x.op == 0x14 for x in kcisa.walk(o.code, e)) else None
            if i is None:                         # `return v * 2` has no stl: put one in place of the nop-free mul
                i = find(o, e, 0x50)
                code[i.pc] = 0x14
                code[i.pc + 1:i.pc + 3] = b''
            struct.pack_into('>h', code, i.pc + 1, 3)
        self.assertTrue(self.mutate(f))

    def test_unknown_opcode(self):
        def f(o, code):
            e = method(o, 10, 'Twice')[0]
            i = find(o, e, 0x50)
            code[i.pc] = 0x60
        self.assertIn('kills the task', self.mutate(f))

    def test_call_to_non_entry(self):
        def f(o, code):
            e = method(o, 10, 'Get')[0]
            i = find(o, e, 0x88)
            struct.pack_into('>I', code, i.pc + 1, i.arg + 1)
        self.assertIn('not a method entry', self.mutate(f))

    def test_ret_with_extra_cell(self):
        def f(o, code):
            e = method(o, 10, 'Twice')[0]
            i = find(o, e, 0x83)
            prev = [x for x in kcisa.walk(o.code, e, i.pc)][-1]
            code[prev.pc] = 0x22                 # the last value op becomes a dup
        self.assertTrue(self.mutate(f))


if __name__ == '__main__':
    unittest.main()
