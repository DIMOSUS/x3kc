"""Compile-only sugar of the KC compiler: foreach, type hints, conditional compilation (#if / -D).

None of it reaches the object except plain code: foreach lowers to the ordinary for loop over hidden locals, hints
only produce warnings, #if only selects lines. No game files needed.
"""
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from x3kc import kccache, kcc, kcparse, kcpre, kcvm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FOREACH = r'''
function Elems(a) {
    var s = 0;
    for (var x in a) {
        if (x == 3) continue;       // continue goes to the step: no endless loop
        if (x == 9) break;
        s += x;
    }
    return s;
}
function Indexed(a) {
    var s = 0;
    for (var i, x in a) {
        s += i * x;
        i = 100;                    // the loop variables are copies: the loop goes on
    }
    return s;
}
function Pairs(t) {
    var s = 0;
    for (var k, v in table t) {
        s += k * v;
    }
    return s;
}
function Keys(t) {
    var s = 0;
    for (var k in table t) {
        SE_TableRemove(t, k);       // a snapshot of the keys: removing while iterating is safe
        s += k;
    }
    return s + SE_TableSize(t);
}
function Nested(a) {
    var n = 0;
    for (var x in a) {
        for (var y in a) {
            if (y > x) break;
            n++;
        }
    }
    return n;
}
function Early(a) {
    for (var x in a) {
        var z = x * 2;
        if (z > 5) return z;
    }
    return -1;
}
function NotArray() {
    var n = 0;
    for (var x in 5) n++;           // SE_ArrayClone of a non-array is an empty array
    for (var k in table 0) n++;
    return n;
}
function RemoveDead(a) {
    var seen = "";
    for (var i, x in a) {
        if (x < 0) {
            SE_ArrayRemove(a, SE_ArrayFind(a, x));     // the snapshot still has every element
        }
        seen = seen + x + ",";
    }
    return seen + SE_ArraySize(a);
}
function Hand(a) {
    var s = 0;
    var i;
    for (i = 0; i < SE_ArraySize(a); i++) {
        s += a[i];
    }
    return s;
}
'''


def write(d, name, text):
    p = os.path.join(d, name)
    with open(p, 'w', encoding='latin1', newline='\n') as f:
        f.write(text)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kcsugar')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, text, defines=None, name='t.kc'):
        p = write(self.dir, name, text)
        out = io.StringIO()
        with redirect_stdout(out):
            o, _, fns = kcc.compile_source(p, defines=defines)
        return o, fns

    def error(self, text, defines=None):
        p = write(self.dir, 't.kc', text)
        with self.assertRaises((kcc.CompileError, kcparse.ParseError)) as c:
            with redirect_stdout(io.StringIO()):
                kcc.compile_source(p, defines=defines)
        return str(c.exception)


class Foreach(Base):
    def test_runs(self):
        o, fns = self.build(FOREACH)
        vm = kcvm.VM(o)

        def call(name, *args):
            return vm.call(0, name, list(args))
        A, T = kcvm.KArray, kcvm.KTable
        self.assertEqual(call('Elems', A([1, 2, 3, 4, 9, 100])), 7)
        self.assertEqual(call('Indexed', A([5, 6, 7])), 20)
        t = T({2: 10, 3: 100})
        self.assertEqual(call('Pairs', t), 320)
        self.assertEqual(call('Keys', t), 5)
        self.assertEqual(call('Nested', A([1, 2, 3])), 6)
        self.assertEqual(call('Early', A([1, 2, 3, 4])), 6)
        self.assertEqual(call('Early', A([])), -1)
        self.assertEqual(call('NotArray'), 0)
        # removing elements in the body: every element of the start is visited once, no phantom zeros
        self.assertEqual(call('RemoveDead', A([1, -2, -3, 4])), '1,-2,-3,4,2')
        self.assertEqual([f.foreach_lines for f in fns if f.name == 'Elems'], [[4]])
        # hidden locals: container, size, index - two cells more than the loop written by hand (s, i)
        frames = {f.name: f.frame_size for f in fns}
        self.assertEqual((frames['Elems'], frames['Hand']), (7, 5))
        from x3kc import kcverify, targets
        V = kcverify.Verifier(o, targets.known())
        V.run()
        self.assertEqual(V.errors, {})

    def test_errors(self):
        e = self.error('function F(a) {\n    for (var x, y, z in a) {}\n}\n')
        self.assertIn('t.kc:2: foreach takes one or two variables', e)
        e = self.error('function F(a) {\n    var n = 0;\n    for (var x of a) {}\n}\n')
        self.assertIn("t.kc:3: expected 'in'", e)
        e = self.error('function F(a) {\n    for (var x in a) {}\n    return x;\n}\n')
        self.assertIn('t.kc:3: unknown name x', e)
        e = self.error('function F(a) {\n    fork {\n        for (var x in a) { break; }\n    }\n    break;\n}\n')
        self.assertIn('break/continue outside a loop', e)

    def test_table_is_a_variable_name(self):
        # `table` followed by `)` or an operator is the variable, not the marker
        o, _ = self.build('function F() {\n    var table = {4, 5};\n    var s = 0;\n'
                          '    for (var x in table) s += x;\n    for (var x in table[0] + 1) s += 1;\n'
                          '    return s;\n}\n')
        self.assertEqual(kcvm.VM(o).call(0, 'F', []), 9)

    def test_savegame_note(self):
        base_src = '#object 801 100\nfunction W(a) {\n    var s = 0;\n    TI_Delay(1);\n    return s;\n}\n'
        new_src = ('#object 801 100\nfunction W(a) {\n    var s = 0;\n    TI_Delay(1);\n'
                   '    for (var x in a) s += x;\n    return s;\n}\n')
        base, _ = self.build(base_src, name='b.kc')
        o, fns = self.build(new_src)
        notes = kcc.foreach_frame_notes(o, fns, base)
        self.assertEqual(len(notes), 1)
        self.assertIn('t.kc:2: W: frame 3 -> 7; the foreach at line 5 keeps', notes[0])


HINTS = r'''
class Thing = 10 {
    function Create() { return this; }
    function Hit(dmg, who) { return dmg; }
    function Any() { return __argc; }
}
class Ship = 11 : Thing {
    var target: Thing;
    function Fly(speed): Ship {
        target.Fly(1);
        return this;
    }
}
class Rock = 12 {
    function Roll() { return 0; }
}
var gShip: Ship;
function Make(): Ship { return gShip; }
function Typed(s: Ship, n) : Ship {
    s.Fly(1, 2);
    s.Hit(1);
    s.Nope();
    var t: Thing = s;
    t.Thing::Hit(1, 2);
    t.Ship::Fly(1);
    var bad: Ship = t;
    Typed(t, 1);
    Make().Roll();
    gShip.Any(1, 2);
    gShip.Any();
    var r: Rock = new Rock();
    for (var x: Ship in n) { x.Roll(); }
    this.Whatever();
    return t;
}
'''


class Hints(Base):
    def test_warnings(self):
        o, _ = self.build(HINTS)
        self.assertEqual(o.warnings, [
            't.kc:10: Thing has no method Fly (a call by name answers 0)',
            't.kc:20: 2 arguments to Ship::Fly, which takes 1',
            't.kc:21: 1 argument to Thing::Hit, which takes 2 (a call by name passes 0 for the rest)',
            't.kc:22: Ship has no method Nope (a call by name answers 0)',
            't.kc:25: Ship::Fly is called on a Thing, which is not a Ship',
            't.kc:26: the value of bad is a Thing, not a Ship',
            't.kc:27: argument 1 of Typed is a Thing, not a Ship',
            't.kc:28: Ship has no method Roll (a call by name answers 0)',
            't.kc:29: 2 arguments to Thing::Any, which takes 0',
            't.kc:32: Ship has no method Roll (a call by name answers 0)',
            't.kc:34: the returned value is a Thing, not a Ship',
        ])

    def test_code_unchanged(self):
        plain = re.sub(r'([A-Za-z_)])\s*:\s*(Thing|Ship|Rock)\b', r'\1', HINTS)
        self.assertIn('class Ship = 11 : Thing {', plain)
        self.assertNotIn('s: Ship', plain)
        a, _ = self.build(HINTS)
        b, _ = self.build(plain, name='u.kc')
        self.assertEqual(a.serialize(), b.serialize())

    def test_unknown_class(self):
        self.assertIn('t.kc:2: unknown class Shp in a type hint',
                      self.error('class Ship = 11 {}\nfunction F(s: Shp) {\n}\n'))
        self.assertIn('t.kc:3: unknown class Shp in a type hint',
                      self.error('class Ship = 11 {}\nfunction F() {\n    var s: Shp;\n}\n'))
        self.assertIn('t.kc:1: unknown class Nothing in a type hint', self.error('var gX: Nothing;\n'))


class Conditional(Base):
    SRC = ('const LEVEL = 2;\n'
           'function F() {\n'
           '#ifdef DEBUG\n'
           '    return 1;\n'
           '#elif LEVEL > 1 && MODE == "fast"\n'
           '    return 2;\n'
           '#elif LEVEL > 1\n'
           '#  if defined(EXTRA)\n'
           '    return 30 + EXTRA_UNUSED;\n'
           '#  else\n'
           '    return 3;\n'
           '#  endif\n'
           '#else\n'
           '    return 4;\n'
           '#endif\n'
           '}\n'
           'function G() { return x; }\n')

    def value(self, defines):
        o, _ = self.build(self.SRC.replace('function G() { return x; }\n', ''), defines)
        return kcvm.VM(o).call(0, 'F', [])

    def test_branches(self):
        self.assertEqual(self.value({'DEBUG': 1, 'MODE': 0}), 1)
        self.assertEqual(self.value({'MODE': 'fast'}), 2)
        self.assertEqual(self.value({'MODE': 'slow'}), 3)
        self.assertEqual(kcpre.parse_defines(['A', 'B=0x10', 'C="x y"', 'D=word']),
                         {'A': 1, 'B': 16, 'C': 'x y', 'D': 'word'})

    def test_errors_and_lines(self):
        # line numbers of the code after a directive stay the file's: G is on line 17
        p = write(self.dir, 't.kc', self.SRC)
        with self.assertRaises(kcparse.ParseError) as c:
            with redirect_stdout(io.StringIO()):
                kcc.compile_source(p, defines={'MODE': 1})
        self.assertIn('t.kc:17: unknown name x', str(c.exception))
        self.assertIn('t.kc:2: #if: unknown name NOPE',
                      self.error('function F() {\n#if NOPE\n#endif\n}\n'))
        self.assertIn('t.kc:2: #if without #endif', self.error('function F() {\n#if 1\n}\n'))
        self.assertIn('t.kc:1: #endif without #if', self.error('#endif\n'))
        self.assertIn('t.kc:3: #elif after #else', self.error('#if 0\n#else\n#elif 1\n#endif\n'))

    def test_short_circuit(self):
        src = ('function F() {\n#if defined(X) && X > 1\n    return 1;\n#elif !defined(X) || X == 0\n    return 2;\n'
               '#else\n    return 3;\n#endif\n}\n')
        for defs, want in (({}, 2), ({'X': 5}, 1), ({'X': 0}, 2), ({'X': 1}, 3)):
            o, _ = self.build(src, defs)
            self.assertEqual(kcvm.VM(o).call(0, 'F', []), want, defs)
        o, _ = self.build('function F() {\n#if 0 && 1 / 0\n    return 1;\n#endif\n    return 2;\n}\n')
        self.assertEqual(kcvm.VM(o).call(0, 'F', []), 2)

    def test_comments(self):
        # a '#' line inside /* */ is no directive; a "/*" in a string opens no comment; /* */ after a directive
        src = ('/*\n#if 1\n*/\nfunction F() {\n    var s = "/*";\n#ifdef X /* the switch */\n    return 1;\n'
               '#endif /* X */\n    return s;\n}\n')
        o, _ = self.build(src)
        self.assertEqual(kcvm.VM(o).call(0, 'F', []), '/*')
        o, _ = self.build(src, {'X': 1})
        self.assertEqual(kcvm.VM(o).call(0, 'F', []), 1)
        self.assertIn('t.kc:1: a /* comment after a directive must close on its line',
                      self.error('#if 1 /* open\n */\n#endif\n'))

    def test_line_breaks(self):
        # latin1 \x85 / \x0c inside a comment are no line breaks for the lexer: the #if error stays on line 2
        self.assertIn('t.kc:2: #if: unknown name NOPE',
                      self.error('// note\x85 more\x0c\n#if NOPE\n#endif\n'))
        # a trailing -D without a name is a usage error, not a traceback
        from x3kc import savecheck
        argv = sys.argv
        try:
            sys.argv = ['savecheck', os.path.join(self.dir, 'p.kc'), 'b.obj', '-D']
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(savecheck.main(), 2)
            self.assertIn('-D needs a name', out.getvalue())
        finally:
            sys.argv = argv

    def test_split_project(self):
        d = os.path.join(self.dir, 'p')
        os.makedirs(d)
        write(d, 'p.kcs', '@header.kc\nA.kc\n')
        write(d, '@header.kc', '#object 801 100\nconst ON = 1;\n')
        write(d, 'A.kc', 'class A = 10 {\n#if ON && defined(X)\n    function F() { return 1; }\n#else\n'
                         '    function F() { return 2; }\n#endif\n}\n')
        with redirect_stdout(io.StringIO()):
            o, _, _ = kcc.compile_source(d, defines={'X': 1})
            self.assertEqual(kcvm.VM(o).call(10, 'F', []), 1)
        write(d, 'A.kc', 'class A = 10 {\n#if ON\n    function G() { return y; }\n#endif\n}\n')
        with self.assertRaises(kcparse.ParseError) as c:
            with redirect_stdout(io.StringIO()):
                kcc.compile_source(d)
        self.assertIn('A.kc:3: unknown name y', str(c.exception))
        write(d, 'A.kc', 'class A = 10 {\n#if ON\n}\n')
        with self.assertRaises(kcc.CompileError) as c:
            kcc.compile_source(d)
        self.assertIn('A.kc:2: #if without #endif', str(c.exception))

    def test_cache_follows_defines(self):
        p = write(self.dir, 't.kc', self.SRC.replace('function G() { return x; }\n', ''))
        cache = kccache.Cache(os.path.join(self.dir, 'cache'))
        out = []
        with redirect_stdout(io.StringIO()):
            for defs in ({'MODE': 1}, {'DEBUG': 1, 'MODE': 1}, {'MODE': 1}, {'DEBUG': 1, 'MODE': 1}):
                o, _, _ = kcc.compile_source(p, cache, defs)
                out.append(kcvm.VM(o).call(0, 'F', []))
        self.assertEqual(out, [3, 1, 3, 1])


class Lsp(Base):
    def test_diagnostics(self):
        sys.path.insert(0, ROOT)
        from editors.kclsp import project
        p = write(self.dir, 't.kc', HINTS.replace('function Make', '#ifdef NOTSET\nnonsense here\n#endif\n'
                                                                  'function Make'))
        pr = project.Project(single=p)
        pr.refresh()
        part = pr.parts[0]
        pr.check(part)
        ds = pr.diagnostics(part)
        self.assertTrue(any(d.line == 23 and d.severity == 2 and 'has no method Fly' not in d.msg and
                            '2 arguments to Ship::Fly' in d.msg for d in ds), ds)
        self.assertFalse([d for d in ds if d.severity == 1], ds)
        # go to definition of a foreach variable: its use refers to the declaring token
        i = next(j for j, t in enumerate(part.toks) if t.text == 'x' and part.toks[j + 1].text == ':')
        use = next(j for j, t in enumerate(part.toks) if t.text == 'x' and j > i)
        self.assertEqual(part.refs[i], part.refs[use])
        self.assertEqual(part.var_tok[id(part.refs[use][1])], i)

    def test_unknown_field_hint_and_constants(self):
        sys.path.insert(0, ROOT)
        from editors.kclsp import project
        d = os.path.join(self.dir, 'p')
        os.makedirs(d)
        idx = write(d, 'p.kcs', '@header.kc\nA.kc\n')
        write(d, '@header.kc', 'const LEVEL = 1;\nvar gX: Nothing;\n')
        write(d, 'A.kc', 'class A = 10 {\n    function F() {\n#if LEVEL > 1\n        return y;\n#endif\n'
                         '        return 0;\n    }\n}\n')
        pr = project.Project(index=idx)
        pr.refresh()
        head, a = pr.parts
        self.assertEqual([x.msg for x in pr.diagnostics(head)], ['unknown class Nothing in a type hint'])
        pr.check(a)
        self.assertEqual(pr.diagnostics(a), [])
        # the constant changes in the other part: the #if of A.kc is read again
        head.set_text('const LEVEL = 2;\nvar gX;\n')
        pr.refresh()
        pr.check(a)
        self.assertEqual([x.msg for x in pr.diagnostics(a)], ['unknown name y'])


if __name__ == '__main__':
    unittest.main()
