"""Split projects (kcsplit): split / join byte for byte, compile of a split project, part names, messages."""
import os

from x3kc import kcc, kcparse, kcsplit
from x3kc.__main__ import main as cli

from test_kc import SOURCE, Base

# hand-edited: comments everywhere, a trailing comment on a closing brace, two declarations on one line, a block
# comment between declarations, a global after a class, no newline at the end
EDITED = '''// my notes on top
#object 801 100   // the build
#pool t.pool
var gCount = 0;  // counter

/* a block comment
   before the first class */
// Thing: the root
class Thing = 10 {
    var m0 = 0;
    function Create(a, b) {
        m0 = a + b;   // sum
        return this;
    }
}  // end of Thing
var gLate = {:};
var gLater = 3;
class Ship = 11 : Thing { function Go() { return gLate; } } class C12 : Ship {
}

function Helper(v) {
    return new Thing(v, 2);
}
// trailing words'''


class Split(Base):
    def roundtrip(self, text, name='t'):
        raw = text.encode('latin1')
        p = os.path.join(self.dir, name + '.kc')
        with open(p, 'wb') as f:
            f.write(raw)
        d = os.path.join(self.dir, name + '_split')
        self.assertEqual(cli(['split', p, d]), 0)
        out = os.path.join(self.dir, name + '_joined.kc')
        self.assertEqual(cli(['join', d, out]), 0)
        self.assertEqual(open(out, 'rb').read(), raw)
        return p, d

    def test_join_split_is_identity(self):
        for k, t in enumerate((SOURCE, EDITED, EDITED.replace('\n', '\r\n'), '', '// only a comment\n',
                               SOURCE.replace('}\n\nclass Ship', '}\r\n\r\n/* x */ class Ship'))):
            self.roundtrip(t, 'r%d' % k)

    def test_parts_and_order(self):
        parts = kcsplit.split_text(EDITED)
        self.assertEqual([f for f, _ in parts], ['@header.kc', 'Thing.kc', '@globals.gLate.kc', 'Ship.kc',
                                                 'C12.kc', '@Helper.kc'])
        self.assertEqual(''.join(t for _, t in parts), EDITED)
        thing = dict(parts)['Thing.kc']
        self.assertTrue(thing.startswith('\n/* a block comment'))   # comments above a class go with it
        self.assertTrue(thing.endswith('}  // end of Thing\n'))
        self.assertTrue(dict(parts)['@Helper.kc'].endswith('// trailing words'))

    def test_name_collisions(self):
        src = ('class Foo = 10 {\n}\nclass foo = 11 {\n}\nclass Con = 12 {\n}\nclass C13 {\n}\n'
               'function f() {\n}\nfunction F() {\n}\nfunction nul() {\n}\n')
        names = [f for f, _ in kcsplit.split_text(src)]
        self.assertEqual(names, ['Foo.kc', 'foo~11.kc', 'Con~12.kc', 'C13.kc', '@f.kc', '@F~2.kc', '@nul.kc'])

    def test_compile_split_equals_single(self):
        for text in (SOURCE, EDITED):
            p = self.write('s.kc', text)
            self.write('t.pool', '""\n')
            d = os.path.join(self.dir, 's_split')
            self.assertEqual(cli(['split', p, d]), 0)
            a = kcc.compile_source(p)[0].serialize()
            self.assertEqual(kcc.compile_source(d)[0].serialize(), a)
            self.assertEqual(kcc.compile_source(os.path.join(d, 's.kcs'))[0].serialize(), a)

    def test_index_is_the_order(self):
        p, d = self.roundtrip(SOURCE)
        index = os.path.join(d, 't.kcs')
        lines = open(index, encoding='utf-8').read().splitlines()
        # file names mean nothing: rename a part, the index follows
        os.rename(os.path.join(d, 'Ship.kc'), os.path.join(d, 'Other.kc'))
        with open(index, 'w', encoding='utf-8') as f:
            f.write('\n'.join(l.replace('Ship.kc', 'Other.kc') for l in lines) + '\n')
        out = os.path.join(self.dir, 'j.kc')
        self.assertEqual(cli(['join', index, out]), 0)
        self.assertEqual(open(out, 'rb').read(), open(p, 'rb').read())
        # the index order is the source order: swapping two classes changes the object
        swapped = [l for l in lines]
        i, j = swapped.index('Thing.kc'), swapped.index('Ship.kc')
        swapped[i], swapped[j] = 'Other.kc', 'Thing.kc'
        with open(index, 'w', encoding='utf-8') as f:
            f.write('\n'.join(swapped) + '\n')
        self.assertNotEqual(kcc.compile_source(d)[0].serialize(), kcc.compile_source(p)[0].serialize())

    def test_part_parses_alone(self):
        _, d = self.roundtrip(SOURCE)
        u = kcsplit.parse_part(os.path.join(d, 'Ship.kc'))      # its parent Thing is in another part
        (kind, k), = u.items
        self.assertEqual((kind, k.name, k.id), ('class', 'Ship', 11))
        self.assertEqual(u.class_parent_names[11][0], 'Thing')
        with self.assertRaises(kcparse.ParseError):
            kcparse.Parser(open(os.path.join(d, 'Ship.kc'), encoding='latin1').read(), 'Ship.kc').unit()

    def test_messages_name_the_part(self):
        _, d = self.roundtrip(SOURCE)
        p = os.path.join(d, 'Ship.kc')
        lines = open(p, encoding='latin1').read().split('\n')
        n = next(k for k, l in enumerate(lines) if 'gCount++' in l)
        lines[n] = lines[n].replace('gCount++', 'noSuchName++')
        with open(p, 'w', encoding='latin1', newline='') as f:
            f.write('\n'.join(lines))
        with self.assertRaises(kcparse.ParseError) as c:
            kcc.compile_source(d)
        self.assertTrue(str(c.exception).startswith('Ship.kc:%d: unknown name noSuchName' % (n + 1)),
                        str(c.exception))
        lines[n] = lines[n].replace('noSuchName++', 'gCount++;$')
        with open(p, 'w', encoding='latin1', newline='') as f:
            f.write('\n'.join(lines))
        with self.assertRaises(kcparse.ParseError) as c:
            kcc.compile_source(d)
        self.assertTrue(str(c.exception).startswith('Ship.kc:%d: bad character' % (n + 1)), str(c.exception))

    def test_decompile_split(self):
        o = self.compile(SOURCE)
        obj = os.path.join(self.dir, 'a.obj')
        with open(obj, 'wb') as f:
            f.write(o.serialize())
        single, split = os.path.join(self.dir, 'one'), os.path.join(self.dir, 'many')
        self.assertEqual(cli(['decompile', obj, single, '--name', 't', '--no-offsets']), 0)
        self.assertEqual(cli(['decompile', obj, split, '--name', 't', '--no-offsets', '--split',
                              '--names-from', obj]), 0)
        self.assertFalse(os.path.exists(os.path.join(split, 't.kc')))
        self.assertEqual(cli(['compile', split, '--check', obj]), 0)
        joined = os.path.join(self.dir, 'j.kc')
        self.assertEqual(cli(['join', split, joined]), 0)
        a = open(os.path.join(single, 't.kc'), encoding='latin1').read()
        b = open(joined, encoding='latin1').read()
        self.assertEqual(b, a.replace('x3kc compile t.kc', 'x3kc compile t.kcs'))
        self.assertEqual(cli(['compile', joined, '--check', obj]), 0)

    def test_resplit_removes_stale_parts(self):
        p, d = self.roundtrip(SOURCE)
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(SOURCE.replace('class Ship = 11', 'class Boat = 11').replace('Ship', 'Boat'))
        self.assertEqual(cli(['split', p, d]), 0)
        self.assertFalse(os.path.exists(os.path.join(d, 'Ship.kc')))
        self.assertTrue(os.path.exists(os.path.join(d, 'Boat.kc')))

    def test_object_commands_refuse_sources(self):
        _, d = self.roundtrip(SOURCE)
        with self.assertRaises(SystemExit) as c:
            cli(['methoddiff', d, d])
        self.assertIn('source project', str(c.exception.code))
