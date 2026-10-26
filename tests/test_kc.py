"""Round trips and diagnostics on small hand-written sources (no game files needed)."""
import json
import os
import shutil
import tempfile
import unittest

from x3kc import annotations, kcc, kcobj, kcparse
from x3kc.__main__ import main as cli

SOURCE = r'''
#object 801 100
var gCount = 0;
var gName = "player";
var gList = {};

class Thing = 10 {
    var m0 = 0;
    var m1 = {:};
    function Create(a, b) {
        m0 = a + b * 2;
        m1["k"] = b;
        return this;
    }
    function Sum(n) {
        var total = 0;
        var i;
        for (i = 0; i < n; i += 1) {
            if (i % 2 == 0 && i != 4) {
                total += i;
            } else if (i == 5) {
                continue;
            } else {
                total -= 1;
            }
        }
        return total;
    }
}

class Ship = 11 : Thing {
    var m2 = "ship";
    function Describe(x) {
        switch (x) {
        case 1:
            return "one";
        case "two":
            return 2;
        default:
            break;
        }
        while (x > 0) {
            x--;
            gCount++;
        }
        fork {
            SE_Random(10);
        }
        return Sum(x) + ::Helper(x) + this->Describe(0) + 0xFFFFFFFF;
    }
}

function Helper(v) {
    var t = new Thing(v, 2);
    do {
        v = v - 1;
    } while (v > 0);
    return t.Sum(v) ? v : -v;
}
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kc')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(text)
        return p

    def compile(self, text, name='t.kc'):
        o, _, _ = kcc.compile_source(self.write(name, text))
        return o


class RoundTrip(Base):
    def test_source_object_source_object(self):
        o = self.compile(SOURCE)
        obj = os.path.join(self.dir, 'a.obj')
        with open(obj, 'wb') as f:
            f.write(o.serialize())
        out = os.path.join(self.dir, 'dec')
        self.assertEqual(cli(['decompile', obj, out, '--name', 't']), 0)
        rebuilt = os.path.join(self.dir, 'b.obj')
        self.assertEqual(cli(['compile', os.path.join(out, 't.kc'), '-o', rebuilt, '--check', obj]), 0)
        self.assertEqual(open(rebuilt, 'rb').read(), open(obj, 'rb').read())

    def test_names_do_not_change_bytes(self):
        o = self.compile(SOURCE)
        obj = os.path.join(self.dir, 'a.obj')
        with open(obj, 'wb') as f:
            f.write(o.serialize())
        ann = {'classes': {'10': {'name': 'Item', 'fields': {'0': {'name': 'value'}},
                                  'methods': {'Sum': {'params': ['count'], 'locals': {'v1': 'acc'}}}},
                           '11': {'name': 'Item'}},          # duplicate: gets a suffix
               'globals': {'fields': {'0': {'name': 'gTotal'}}}}
        names = self.write('n.json', json.dumps(ann))
        out = os.path.join(self.dir, 'dec')
        self.assertEqual(cli(['decompile', obj, out, '--name', 't', '--names', names]), 0)
        src = open(os.path.join(out, 't.kc'), encoding='latin1').read()
        for s in ('class Item = 10', 'class Item11 = 11 : Item', 'var value', 'function Sum(count)', 'var acc',
                  'var gTotal'):
            self.assertIn(s, src)
        self.assertEqual(cli(['compile', os.path.join(out, 't.kc'), '--check', obj]), 0)

    def test_portable_names_follow_code_changes(self):
        o1 = self.compile(SOURCE, 'one.kc')
        changed = SOURCE.replace('        m0 = a + b * 2;\n', '        SE_Random(1);\n        m0 = a + b * 2;\n')
        o2 = self.compile(changed, 'two.kc')
        work = {'classes': {'10': {'methods': {'Create': {'comments': {}}}}}}
        e = next(r[0] for c in o1.classes if c.id == 10 for r in c.methods if o1.string(r[1]) == 'Create')
        from x3kc import kcisa
        pcs = [i.pc for i in kcisa.walk(o1.code, e, len(o1.code))][:4]
        work['classes']['10']['methods']['Create']['comments']['0x%06x' % pcs[1]] = 'sum of a and b'
        port, lost = annotations.pack(o1, work)
        self.assertEqual(lost, [])
        res = annotations.resolve(o2, port)
        got = res['classes']['10']['methods']['Create']['comments']
        self.assertEqual(list(got.values()), ['sum of a and b'])
        pc2 = int(next(iter(got)), 16)
        e2 = next(r[0] for c in o2.classes if c.id == 10 for r in c.methods if o2.string(r[1]) == 'Create')
        ins2 = [i for i in kcisa.walk(o2.code, e2, len(o2.code))]
        self.assertEqual(kcisa.MN[next(i for i in ins2 if i.pc == pc2).op],
                         kcisa.MN[next(i for i in kcisa.walk(o1.code, e, len(o1.code)) if i.pc == pcs[1]).op])


class Diagnostics(Base):
    def fails(self, text, msg):
        with self.assertRaises((kcc.CompileError, kcparse.ParseError)) as cm:
            self.compile(text)
        self.assertIn(msg, str(cm.exception))

    def test_errors(self):
        self.fails('function f() { return 0x1FFFFFFFF; }', 'does not fit in 32 bits')
        self.fails('function f() { return "\\q"; }', 'unknown escape')
        self.fails('function f() { return "a\\x00b"; }', 'NUL')
        self.fails('function f() { return 1; }\nfunction f() { return 2; }', 'defined twice')
        self.fails('function f(a) { switch (a) { case 1: return 1; case 1: return 2; } return 0; }', 'twice')
        self.fails('function f(a) { switch (a) : dense(0, 3) { case 9: return 1; } return 0; }', 'outside')
        self.fails('function f(a) { while (a) { fork { break; } } return 0; }', 'fork')
        self.fails('function f() asm { push1 ret }', 'frame N')
        self.fails('#object 801\nfunction f() { return 1; }', '#object')

    def test_int_wrap_and_empty_string(self):
        o = self.compile('function f() { return 0xFFFFFFFF; }\nfunction g() { return ""; }')
        self.assertIn(bytes.fromhex('05ff83'), o.code)          # pushb -1; ret
        self.assertNotIn(bytes.fromhex('0b0000000083'), o.code)  # "" never at pool offset 0


class SaveCompat(Base):
    def test_base_insert_and_added_local(self):
        base = self.compile(SOURCE, 'base.kc')
        bp = os.path.join(self.dir, 'base.obj')
        with open(bp, 'wb') as f:
            f.write(base.serialize())
        out = os.path.join(self.dir, 'mod.obj')
        ins = SOURCE.replace('        while (x > 0) {', '        SE_Random(1);\n        while (x > 0) {')
        self.assertEqual(cli(['compile', self.write('mod.kc', ins), '-o', out, '--base', bp]), 0)
        mod = kcobj.load(out)
        self.assertNotEqual(mod.build, base.build)
        self.assertEqual([b for b, _ in mod.ptch], [base.build])
        loc = SOURCE.replace('        while (x > 0) {', '        var extra = SE_Random(1);\n        while (x > 0) {')
        self.assertEqual(cli(['compile', self.write('mod2.kc', loc), '-o', out + '2', '--base', bp]), 3)
        self.assertFalse(os.path.exists(out + '2'))


if __name__ == '__main__':
    unittest.main()
