"""The emulator runs code compiled by kcc and answers like the language reference (docs/kc.md)."""
import os
import tempfile
import unittest

from x3kc import kcc, kcvm


def compile_src(src):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 't.kc')
    with open(p, 'w', encoding='latin1') as f:
        f.write(src)
    o, _, _ = kcc.compile_source(p)
    return o


def run(src, fn='Main', args=()):
    vm = kcvm.VM(compile_src(src))
    return vm.call(0, fn, list(args)), vm


class Language(unittest.TestCase):
    def ev(self, expr, decls=''):
        r, _ = run('%s\nfunction Main() { return %s; }\n' % (decls, expr))
        return r

    def test_arithmetic(self):
        self.assertEqual(self.ev('1 + 2 * 3 - 4'), 3)
        self.assertEqual(self.ev('7 / 2'), 3)
        self.assertEqual(self.ev('-7 / 2'), -3)
        self.assertEqual(self.ev('-7 % 3'), -1)
        self.assertEqual(self.ev('5 / 0'), 0)
        self.assertEqual(self.ev('0x7fffffff + 1'), -2147483648)
        self.assertEqual(self.ev('1 << 4 | 3'), 19)
        self.assertEqual(self.ev('~0'), -1)
        self.assertEqual(self.ev('-(5)'), -5)

    def test_logic(self):
        self.assertEqual(self.ev('3 && 4'), 1)
        self.assertEqual(self.ev('0 || ""'), 1)       # a pool string other than offset 0 is true
        self.assertEqual(self.ev('!0'), 1)
        self.assertEqual(self.ev('1 ? 10 : 20'), 10)
        self.assertEqual(self.ev('"a" < "b"'), 0)     # non-numbers are never less ...
        self.assertEqual(self.ev('"a" <= "b"'), 1)    # ... so <= is "not greater"

    def test_strings(self):
        self.assertEqual(self.ev('"x" + 5'), 'x5')
        self.assertEqual(self.ev('5 + "x"'), '5x')
        self.assertEqual(self.ev('"ab" == "ab"'), 1)
        self.assertEqual(self.ev('"abc"[1]'), ord('b'))
        self.assertEqual(self.ev('SE_SPrintf("%d-%s", 7, "z")'), '7-z')

    def test_containers(self):
        self.assertEqual(self.ev('{10, 20, 30}[2]'), 30)
        self.assertEqual(self.ev('{10, 20}[5]'), 0)
        self.assertEqual(self.ev('{"k": 4}["k"] + {"k": 4}["missing"]'), 4)
        r, _ = run('function Main() { var a = {1, 2}; a[5] = 9; a[0] = 7; return SE_ArraySize(a) * 100 + a[0]; }')
        self.assertEqual(r, 207)
        r, _ = run('function Main() { var t = {:}; t["a"] = 1; t[3] = 2; return SE_TableSize(t); }')
        self.assertEqual(r, 2)

    def test_loops_and_switch(self):
        src = '''
function Main() {
    var s = 0;
    var i;
    for (i = 0; i < 10; i++) {
        if (i == 3) { continue; }
        if (i == 8) { break; }
        s += i;
    }
    var k = 0;
    do { k++; } while (k < 5);
    var w = 0;
    while (w < 3) { w = w + 1; }
    return s * 100 + k * 10 + w;
}'''
        self.assertEqual(run(src)[0], 25 * 100 + 50 + 3)
        sw = '''
function F(x) {
    switch (x) {
    case 1: return 10;
    case 2:
    case 3: return 23;
    case "s": return 99;
    default: return -1;
    }
}
function G(x) {
    switch (x) : dense {
    case 0: return 100;
    case 1: return 101;
    case 2: return 102;
    }
    return 7;
}
function Main() { return F(1) + F(3) + F("s") + F(9) + G(2) + G(5); }'''
        self.assertEqual(run(sw)[0], 10 + 23 + 99 - 1 + 102 + 7)

    def test_objects_and_calls(self):
        src = '''
var gCount = 0;
class Base = 10 {
    var m0 = 5;
    function Create(a) { m0 = a; gCount++; return this; }
    function Get() { return m0; }
    function Kind() { return 1; }
    function Twice() { return this->Kind() * 2; }
}
class Child = 11 : Base {
    var m1 = 3;
    function Kind() { return 4; }
    function Sum(a, b) { return a + b + m1 + Get(); }
}
function Main() {
    var b = new Base(7);
    var c = new Child(2);
    return b.Get() * 1000 + c.Twice() * 100 + c.Sum(1) * 10 + gCount + (b.Missing() == 0);
}'''
        # c.Sum(1): b padded with 0 -> 1 + 0 + 3 + 2 = 6
        self.assertEqual(run(src)[0], 7 * 1000 + 8 * 100 + 6 * 10 + 2 + 1)

    def test_singleton_class_object(self):
        src = '''
class Counter = 20 {
    var m0 = 0;
    function Inc() { m0++; return m0; }
}
function Main() { Counter.Inc(); Counter.Inc(); return Counter.Inc(); }'''
        self.assertEqual(run(src)[0], 3)

    def test_fork_and_timers(self):
        src = '''
var gLog = 0;
function Main() {
    fork {
        TI_Delay(1000);
        gLog = gLog * 10 + 2;
    }
    gLog = gLog * 10 + 1;
    TI_Delay(2000);
    gLog = gLog * 10 + 3;
    return gLog;
}'''
        vm = kcvm.VM(compile_src(src))
        t = vm.call(0, 'Main')
        self.assertIsInstance(t, kcvm.Task)
        vm.run(5000)
        self.assertEqual(t.result, 123)

    def test_hang_detected(self):
        vm = kcvm.VM(compile_src('function Main() { while (1) { } return 0; }'))
        vm.budget = 10000
        with self.assertRaises(kcvm.Hang):
            vm.call(0, 'Main')

    def test_unknown_native_answers_zero(self):
        r, vm = run('function Main() { return SA_NotThere(1, 2) + 5; }')
        self.assertEqual(r, 5)
        self.assertEqual(vm.unknown_natives, ['SA_NotThere'])


if __name__ == '__main__':
    unittest.main()
