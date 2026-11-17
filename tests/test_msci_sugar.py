"""MSCI sugar (.xms, x3kc/msci_sugar.py): preprocessor, functions, subs, foreach, compound assignments, type warnings.

The preprocessor and lowering tests need nothing. The build tests need a command database with x3story.obj and
scripts/ beside it (`x3kc msci-setup`): X3KC_MSCI_DB for Albion Prelude, X3KC_MSCI_DB_FL for Farnham's Legacy
(defaults: the x3_src work copies); they run the built scripts through the game's own interpreter in the emulator
and spot-check that stock text builds identically.
"""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from x3kc import msci, msci_asm, msci_sugar as S

MSCI_DB = os.environ.get('X3KC_MSCI_DB', 'D:/GAMES/x3_src/build/tmp/queue/msci/ap/msci_db.json')
MSCI_DB_FL = os.environ.get('X3KC_MSCI_DB_FL', 'D:/GAMES/x3_src/build/msci/fl/msci_db.json')
HEAD = '// lib.sugar.test  (sugar test)\n// version: 1\n'


class Files:
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='x3kcsugar')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return p

    def lower(self, text, defines=(), name='a.xms'):
        """-> (body lines of the lowered text, diags)"""
        r = S.build(self.write(name, HEAD + text), defines=defines)
        return [l for l in r.text.split('\n') if l and not l.startswith('//')], r.diags

    def body(self, text, defines=()):
        lines, diags = self.lower(text, defines)
        self.assertEqual([d for d in diags if d.severity == 'error'], [])
        return [l.strip() for l in lines]


class Preprocessor(Files, unittest.TestCase):
    def test_object_and_function_macros(self):
        b = self.body("#define N 5\n#define SQ(x) ( x ) * ( x )\n$a = SQ($b + N)\n$s = 'N stays'\n$N = N\n")
        self.assertEqual(b, ['$a = ( $b + 5 ) * ( $b + 5 )', "$s = 'N stays'", '$N = 5'])

    def test_multi_line_macro_and_undef(self):
        b = self.body('#define TWO(v) $x = v \\\n  $y = v\nTWO(3)\n#undef TWO\n#ifdef TWO\n$z = 1\n#endif\n')
        self.assertEqual(b, ['$x = 3', '$y = 3'])

    def test_conditions_and_command_line(self):
        src = ('#ifdef DEBUG\n$d = 1\n#endif\n#if LEVEL >= 2\n$l = 2\n#elseif LEVEL == 1\n$l = 1\n#else\n$l = 0\n'
               '#endif\n#ifndef LEVEL\n$n = 1\n#endif\n#if MODE == fast\n$m = 1\n#endif\n')
        self.assertEqual(self.body(src), ['$l = 0', '$n = 1'])
        self.assertEqual(self.body(src, ['DEBUG', 'LEVEL=1', 'MODE=fast']), ['$d = 1', '$l = 1', '$m = 1'])
        self.assertEqual(self.body(src, ['LEVEL=7']), ['$l = 2'])

    def test_include_relative_and_line_mapping(self):
        os.makedirs(os.path.join(self.dir, 'inc'))
        self.write('inc/defs.xmh', '#define K 9\n#include "more.xmh"\n')
        self.write('inc/more.xmh', '\n  end\n')
        lines, diags = self.lower('#include "inc/defs.xmh"\n$k = K\n')
        self.assertIn('$k = 9', [l.strip() for l in lines])
        self.assertEqual([(d.file.replace('\\', '/'), d.line, d.col, d.msg) for d in diags],
                         [(self.dir.replace('\\', '/') + '/inc/more.xmh', 2, 3, 'end without if/while')])

    def test_include_cycle(self):
        self.write('x.xmh', '#include "y.xmh"\n')
        self.write('y.xmh', '#include "x.xmh"\n')
        _, diags = self.lower('#include "x.xmh"\n')
        self.assertTrue(any('#include cycle' in d.msg and d.file.endswith('y.xmh') and d.line == 1 for d in diags),
                        diags)

    def test_if_tests_the_value_ifdef_the_presence(self):
        src = '#if DEBUG\n$a = 1\n#elseif LEVEL\n$a = 2\n#endif\n#ifdef DEBUG\n$d = 1\n#endif\n'
        self.assertEqual(self.body(src, ['DEBUG=0', 'LEVEL=0']), ['$d = 1'])
        self.assertEqual(self.body(src, ['DEBUG=0', 'LEVEL=3']), ['$a = 2', '$d = 1'])
        self.assertEqual(self.body(src, ['DEBUG']), ['$a = 1', '$d = 1'])

    def test_apostrophe_is_not_a_string(self):
        self.assertEqual(self.body("#define X 7\n$a = Kha'ak X\n$b = 'X' + X\n"), ["$a = Kha'ak 7", "$b = 'X' + 7"])

    def test_unbalanced_and_unknown_directives(self):
        _, diags = self.lower('#ifdef A\n#frobnicate\n')
        self.assertTrue(any('#if without #endif' in d.msg for d in diags))
        _, diags = self.lower('#frobnicate\n#else\n')
        msgs = [str(d) for d in diags]
        self.assertTrue(any(re.search(r'a\.xms:3:1: unknown directive #frobnicate', m) for m in msgs), msgs)
        self.assertTrue(any('#else without #ifdef' in m for m in msgs), msgs)


class Lowering(Files, unittest.TestCase):
    def test_function_private_scope_return_and_placement(self):
        b = self.body('$r = max($a, 3)\nreturn $r\n\nfunction max($x, $y) {\n  if $x > $y\n    return $x\n  end\n'
                      '  $t = $y\n  return $t\n}\n')
        self.assertEqual(b, ['$fn.max.x = $a', '$fn.max.y = 3', 'gosub fn.max', '$r = $fn.max', 'return $r',
                             'fn.max:', '$fn.max = null', 'if $fn.max.x > $fn.max.y', '$fn.max = $fn.max.x',
                             'goto label fn.max.exit', 'end', '$fn.max.t = $fn.max.y', '$fn.max = $fn.max.t',
                             'fn.max.exit:', 'endsub'])

    def test_forward_call_missing_args_local_labels(self):
        b = self.body('f(1)\nreturn null\nfunction f($a, $b) {\nagain:\n  $a++\n  skip if $a > 9\n'
                      '    goto label again\n}\n')
        self.assertEqual(b, ['$fn.f.a = 1', '$fn.f.b = null', 'gosub fn.f', 'return null', 'fn.f:', 'fn.f.again:',
                             'inc $fn.f.a =', 'skip if $fn.f.a > 9', 'goto label fn.f.again', 'endsub'])

    def test_sub_block_after_main_and_return_null_added(self):
        b = self.body('sub tick() {\n  $n = $n + 1\n}\n$n = 0\ntick()\ngosub tick\n')
        self.assertEqual(b, ['$n = 0', 'gosub tick', 'gosub tick', 'return null', 'tick:', '$n = $n + 1', 'endsub'])

    def test_foreach_increments_before_the_body(self):
        b = self.body('foreach $i, $v in $arr {\n  continue\n}\n')
        self.assertEqual(b, ['$_fe1.a = $arr', '$_fe1.n = size of array $_fe1.a', '$_fe1.i = 0',
                             'while $_fe1.i < $_fe1.n', '$v = $_fe1.a[$_fe1.i]', '$i = $_fe1.i', 'inc $_fe1.i =',
                             'continue', 'end'])

    def test_compound_increment_chained(self):
        b = self.body("$x += 1\n$x -= $y * 2\n$x *= -1\n$s += 'a b'\n$x /= ( $y )\n$x++\n@ $y--\n$a = $b = $c = 4\n")
        self.assertEqual(b, ['$x = $x + 1', '$x = $x - ( $y * 2 )', '$x = $x * -1', "$s = $s + 'a b'",
                             '$x = $x / ( $y )', 'inc $x =', '@ dec $y =', '$c = 4', '$b = $c', '$a = $b'])

    def test_errors_with_positions(self):
        cases = [
            ('foreach $v in $a {\n  end\n}\n', 4, 'end inside foreach'),
            ('if $a\n}\nend\n', 4, '} closes nothing: if at line 3'),
            ('$r = f(1, 2)\nfunction f($a) {\n}\n', 3, 'takes 1 argument(s), 2 given'),
            ('if f(1)\nend\nfunction f($a) {\n}\n', 3, 'cannot stand in a condition'),
            ('$x = g()\nsub g() {\n}\n', 3, 'sub g has no result'),
            ('f:\nfunction f() {\n}\n', 3, 'label f has the name of function f'),
            ('function f() {\n  function g() {\n  }\n}\n', 4, 'function inside function f'),
            ('goto label nowhere\n', 3, 'undefined label nowhere'),
            ('while $a\n', 3, 'while without end'),
        ]
        for src, line, msg in cases:
            _, diags = self.lower(src)
            self.assertTrue(any(d.line == line and msg in d.msg and d.severity == 'error' for d in diags),
                            (src, diags))

    def test_recursion_warns(self):
        _, diags = self.lower('f(1)\nfunction f($n) {\n  g($n)\n}\nfunction g($n) {\n  f($n)\n}\n')
        w = [d for d in diags if d.severity == 'warning']
        self.assertTrue(w and all('recursive' in d.msg for d in w), diags)
        self.assertFalse([d for d in diags if d.severity == 'error'])

    def test_guard_of_a_multi_line_statement_becomes_if(self):
        b = self.body('do if $a\n  $x = f(1)\nskip if $b\n  $c += 1\nskip if $b\n  $p = $q = 2\n'
                      'skip if not $e\n  foo()\nreturn $x\n'
                      'function f($v) {\n  do if $v\n    return 5\n  do if $v\n    return\n}\nsub foo() {\n}\n')
        self.assertEqual(b, ['if $a', '$fn.f.v = 1', 'gosub fn.f', '$x = $fn.f', 'end',
                             'skip if $b', '$c = $c + 1',
                             'if not $b', '$q = 2', '$p = $q', 'end',
                             'skip if not $e', 'gosub foo', 'return $x',
                             'fn.f:', '$fn.f = null', 'if $fn.f.v', '$fn.f = 5', 'goto label fn.f.exit', 'end',
                             'if $fn.f.v', 'end', 'fn.f.exit:', 'endsub', 'foo:', 'endsub'])

    def test_guarded_return_at_the_end_of_main_gets_return_null(self):
        b = self.body('do if $z\n  return 1\nfunction f() {\n}\n')
        self.assertEqual(b[:3], ['do if $z', 'return 1', 'return null'])

    def test_compound_operand_of_several_groups_is_wrapped(self):
        b = self.body('#define SQ(x) ( x ) * ( x )\n$x -= ( $a ) + ( $b )\n$x /= SQ($b)\n$x -= ( $a + $b )\n')
        self.assertEqual(b, ['$x = $x - ( ( $a ) + ( $b ) )', '$x = $x / ( ( $b ) * ( $b ) )',
                             '$x = $x - ( $a + $b )'])

    def test_function_name_extending_another_is_an_error(self):
        _, diags = self.lower('function vec() {\n  $len = 1\n}\nfunction vec.len() {\n}\n')
        self.assertTrue(any(d.line == 6 and 'extends function vec' in d.msg for d in diags), diags)

    def test_recursion_through_a_sub_warns(self):
        _, diags = self.lower('f(1)\nreturn null\nfunction f($n) {\n  s()\n}\nsub s() {\n  gosub t\n}\n'
                              'sub t() {\n  f(2)\n}\n')
        self.assertTrue(any(d.severity == 'warning' and 'function f is recursive' in d.msg for d in diags), diags)

    def test_stock_text_unchanged(self):
        src = '* comment\n  $a = 1\n@ = wait 100 ms\nif $a == 1\n  $b = $a\nelse if not $c\nend\n'
        lines, diags = self.lower(src)
        self.assertEqual(diags, [])
        self.assertEqual(lines, src.rstrip('\n').split('\n'))


class Build(Files):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(cls.db_path):
            raise unittest.SkipTest('set %s to an %s msci_db.json' % (cls.env, cls.addon.upper()))
        with open(cls.db_path, encoding='utf-8') as f:
            cls.db = json.load(f)
        cls.A = msci_asm.Assembler(cls.db)
        cls.work = os.path.dirname(cls.db_path)

    def build(self, text, **kw):
        return S.build(self.write('lib.sugar.test.xms', HEAD + text), self.db, self.A, **kw)

    def run_script(self, text):
        from x3kc import kcmsci, kcobj, kcvm
        r = self.build(text)
        self.assertEqual(r.errors, [], r.text)
        cls = type(self)
        if 'vm' not in cls.__dict__:
            cls.vm = kcvm.VM(kcobj.load(os.path.join(self.work, 'x3story.obj')))
        return kcmsci.Msci(cls.vm, self.db).run(r.code)

    def test_functions_run(self):
        res = self.run_script('$r = max(3, 8)\n$q = max(9, 2)\n$s = sum3(1, 2, 3)\n$t = $r * 100 + $q * 10 + $s\n'
                              'return $t\nfunction max($x, $y) {\n  if $x > $y\n    return $x\n  end\n  return $y\n}\n'
                              'function sum3($a, $b, $c) {\n  $t = max($a, $b)\n  $u = $a + $b + $c\n  return $u\n}\n')
        self.assertEqual((res.type, res.value), (4, 800 + 90 + 6))

    def test_foreach_continue_and_break_run(self):
        res = self.run_script('$arr = array alloc: size=6\n$k = 0\nwhile $k < 6\n  $t = $k + 1\n  $arr[$k] = $t\n  inc $k =\nend\n'
                              '$sum = 0\nforeach $i, $v in $arr {\n  if $v == 2\n    continue\n  end\n  skip if $v != 5\n'
                              '    break\n  $sum += $v * 10 + $i\n}\nreturn $sum\n')
        # 1 (i 0), 3 (i 2), 4 (i 3); 2 skipped by continue, 5 breaks: 10 + 30 + 40 + 0 + 2 + 3
        self.assertEqual((res.type, res.value), (4, 85))

    def test_guarded_return_runs(self):
        res = self.run_script('$a = g(1)\n$b = g(null)\n$r = $a * 10 + $b\nreturn $r\n'
                              'function g($v) {\n  do if $v\n    return 5\n  return 7\n}\n')
        self.assertEqual((res.type, res.value), (4, 57))

    def test_sub_and_compound_run(self):
        res = self.run_script('$n = 1\ndouble()\ndouble()\n$n -= 1\n$n++\n$a = $b = $n\nreturn $a\n'
                              'sub double() {\n  $n *= 2\n}\n')
        self.assertEqual((res.type, res.value), (4, 4))

    def test_assembler_error_points_at_the_source(self):
        r = self.build('#define BAD frobnicate the universe\n$a = 1\nBAD\n')
        self.assertTrue(r.errors and r.errors[0].line == 5 and r.errors[0].file.endswith('lib.sugar.test.xms'),
                        r.diags)

    def test_name_clash_with_a_command(self):
        r = self.build('wait(1)\nfunction wait($a) {\n}\n')
        self.assertTrue(any('first word of command' in d.msg for d in r.errors), r.diags)

    def test_type_warnings(self):
        r = self.build('$s = [PLAYERSHIP] -> get sector\n$w = get warearray for $s\n'
                       '$y = sec()\n$w = get warearray for $y\n'
                       'probe($s)\n'
                       '$ok = get warearray for [PLAYERSHIP]\n$u = $unknown\n$w = get warearray for $u\n'
                       '$n = 5\n$w = get warearray for $n\nreturn null\n'
                       'function sec() {\n  $x = [PLAYERSHIP] -> get sector\n  return $x\n}\n'
                       'function probe($o) {\n  $w = get warearray for $o\n}\n')
        self.assertEqual(r.errors, [])
        w = [(d.line, d.msg) for d in r.diags if d.severity == 'warning']
        self.assertEqual([x[0] for x in w], [4, 6, 12, 19], w)
        self.assertIn('$s, which holds a sector (set at', w[0][1])
        self.assertIn('$y, which holds a sector', w[1][1])      # through the function's result
        self.assertIn('$o, which holds a sector', w[3][1])
        self.assertEqual(self.build('$s = [PLAYERSHIP] -> get sector\n$w = get warearray for $s\n',
                                    types=False).diags, [])

    def test_parameters_that_take_more_than_their_type(self):
        src = '$c = Fighter\n$r = $c -> is of class Moveable Ship\n'
        if self.addon == 'fl':
            src += ("$t = 'terran'\n$g = convert gate: gate=null, type=$t, allow oos=[FALSE], build ship=null, "
                    "disabled cutscene=[FALSE]\n$a = create new array, arguments=Dock, Shipyard, null, null, null\n"
                    "$i = create input: id='s', user input=Var/Station, refobj=[PLAYERSHIP], description=null, "
                    "without sector=[FALSE], filterclass=$a, racemask=$a, flags=null, default=null\n"
                    "$n = add large number arrays: $a, 5, null, null, null\n")
        src += '$s = [PLAYERSHIP] -> get sector\n$w = get warearray for $s\n'      # still warns
        r = self.build(src)
        self.assertEqual(r.errors, [], r.diags)
        self.assertEqual([d.line for d in r.diags if d.severity == 'warning'], [src.count('\n') + 2], r.diags)

    def test_stock_scripts_build_identically(self):
        scripts = os.path.join(self.work, 'scripts')
        names = ['!config.add.wares.few.one', '!fight.attack.object.bomber', '!lib.msg.found.enemies',
                 'plugin.autotrade.checksector', '!job.trade.planetsupply']
        pr = msci.Printer(self.db)
        pr.scriptargs = {}
        A = msci_asm.Assembler(self.db)
        S.load_scriptargs(A, [scripts])
        done = 0
        for n in names:
            p = os.path.join(scripts, n + '.xml')
            if not os.path.isfile(p):
                continue
            code, _ = msci.read_script(p)
            text = '\n'.join(msci.dis_lines(pr, code, A)) + '\n'
            r = S.build(self.write(n + '.xms', text), self.db, A)
            self.assertEqual(r.errors, [], n)
            self.assertEqual(r.xml, msci_asm.new_script_text(A, self.db, text), n)
            self.assertEqual(r.code, code, n)
            done += 1
        self.assertGreater(done, 0)

    def test_language_server_diagnostics(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'editors'))
        from kclsp import msci_ls
        from unittest import mock
        doc = msci_ls.Doc(self.write('lib.sugar.test.xms', ''))
        with mock.patch.dict(os.environ, {'KCLSP_MSCI_DB': self.db_path}):
            doc.set_text(HEAD + '$s = [PLAYERSHIP] -> get sector\n$w = get warearray for $s\nforeach $v in $a {\n')
            ds = [(d.line, d.severity, d.msg) for d in msci_ls.diagnostics(doc)]
            self.assertEqual([(l, s) for l, s, _ in ds], [(5, 1)], ds)     # a lowering error stops the build
            self.assertIn('foreach without }', ds[0][2])
            doc.set_text(HEAD + '$s = [PLAYERSHIP] -> get sector\n$w = get warearray for $s\n')
            self.assertEqual([(d.line, d.severity) for d in msci_ls.diagnostics(doc)], [(4, 2)])

    def test_cli(self):
        from x3kc.__main__ import main as cli
        src = self.write('lib.sugar.test.xms', HEAD + '$x = 1\n$x += 2\nreturn $x\n')
        out = os.path.join(self.dir, 'lib.sugar.test.xml')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(['msci', 'build', self.db_path, src, '-o', out]), 0)
            self.assertEqual(cli(['msci', 'build', self.db_path, src, '--text', '-o', out + '.txt']), 0)
        code, _ = msci.read_script(out)
        self.assertEqual(code[0], 'lib.sugar.test')
        with open(out + '.txt', encoding='utf-8') as f:
            self.assertIn('$x = $x + 2', f.read())
        bad = self.write('bad.xms', HEAD + 'foreach $v in $a {\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli(['msci', 'build', self.db_path, bad, '-o', out]), 1)
        self.assertRegex(err.getvalue(), r'bad\.xms:3:1: foreach without \}')


class BuildAP(Build, unittest.TestCase):
    db_path, env, addon = MSCI_DB, 'X3KC_MSCI_DB', 'ap'


class BuildFL(Build, unittest.TestCase):
    db_path, env, addon = MSCI_DB_FL, 'X3KC_MSCI_DB_FL', 'fl'


if __name__ == '__main__':
    unittest.main()
