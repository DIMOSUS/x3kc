"""x3kc md: Mission Director XML <-> .xmd text. The stock round trip needs the director folders (X3KC_MD_AP,
X3KC_MD_FL: game/ap/addon/director and game/fl/addon2/director of x3_src by default; skipped when absent)."""
import os
import unittest

from x3kc import md

AP = os.environ.get('X3KC_MD_AP', 'D:/GAMES/x3_src/game/ap/addon/director')
FL = os.environ.get('X3KC_MD_FL', 'D:/GAMES/x3_src/game/fl/addon2/director')

TEXT = '''#director demo
cue Demo(check: cancel) {
  condition {
    check_value({player.age} >= 10s);
  }
  action {
    this.Count = 0;
    this.Count++;
    this.Count += 2, chance: 50; /// two more, sometimes
    this.Pick = random(1, 5);
    if ($this.Count == 3) {
      cancel_cue(Demo);
    } else if ($this.Count in [1, 2]) {
      //few
      reset_cue(Demo);
    } else {
      cancel_cue(Missing);
    }
    if ($this.Pick != 1);
  }
  cue "Demo Lib"(ref: Lib) {
    params {
      Cue = Demo;
      ID = @ID;
    }
  }
}
'''

XML = '''<?xml version="1.0" encoding="ISO-8859-1" ?>
<?xml-stylesheet href="director.xsl" type="text/xsl" ?>
<director name="demo" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="director.xsd">
  <cues>
    <cue name="Demo" check="cancel">
      <condition>
        <check_value value="{player.age}" min="10s"/>
      </condition>
      <action>
        <do_all>
          <set_value name="this.Count" exact="0"/>
          <set_value name="this.Count" operation="add"/>
          <set_value name="this.Count" operation="add" exact="2" chance="50" comment="two more, sometimes"/>
          <set_value name="this.Pick" min="1" max="5"/>
          <do_choose>
            <do_when value="{value@this.Count}" exact="3">
              <cancel_cue cue="Demo"/>
            </do_when>
            <do_when value="{value@this.Count}" min="1" max="2">
              <!--few-->
              <reset_cue cue="Demo"/>
            </do_when>
            <do_otherwise>
              <cancel_cue cue="Missing"/>
            </do_otherwise>
          </do_choose>
          <do_if value="{value@this.Pick}" exact="1" negate="1"/>
        </do_all>
      </action>
      <cues>
        <cue name="Demo Lib" ref="Lib">
          <params>
            <param name="Cue" value="Demo"/>
            <param name="ID" value="{param@ID}"/>
          </params>
        </cue>
      </cues>
    </cue>
  </cues>
</director>
'''


def stock(d):
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.lower().endswith('.xml') and md.is_director(os.path.join(d, f))]


class Syntax(unittest.TestCase):
    def test_asm(self):
        xml, warns = md.asm_text(TEXT, 't.xmd')
        self.assertEqual(xml, XML.encode('latin-1'))
        self.assertEqual(warns, ["t.xmd:17: warning: cancel_cue refers to cue 'Missing', which this file does not "
                                 "define"])

    def test_dis(self):
        self.assertEqual(md.dis_bytes(XML.encode('latin-1'), 't.xml'), TEXT)

    def test_odd_bytes_kept(self):
        # CRLF, a tab-indented line, a blank line, spaces in a tag, single quotes, a comment on the line of a tag,
        # an element on the line of its parent, a multi-line comment, a literal action with two children, no BOM
        # change, utf-8 text
        x = ('<?xml version="1.0" encoding="utf-8"?>\r\n<director name="x">\r\n  <cues>\r\n'
             '    <cue name="A" >\r\n\t\t\t<action>\r\n        <do_all><!--c-->\r\n'
             "\r\n          <set_value  name='this.V' exact=\"1\" /><set_value name=\"this.\u00e9\" exact=\"2\"/>\r\n"
             '          <!--two\r\nlines-->\r\n        </do_all>\r\n      </action>\r\n'
             '      <action><cancel_cue cue="A"/><cancel_cue cue="A"/></action >\r\n'
             '    </cue>\r\n  </cues>\r\n</director>').encode('utf-8')
        text = md.dis_bytes(x, 'x.xml')
        self.assertIn('@action', text)
        self.assertIn('~>" "', text)
        self.assertEqual(md.asm_text(text, 'x.xmd')[0], x)

    def errors(self, body):
        with self.assertRaises(md.MdError) as cm:
            md.asm_text('#director t\n' + body, 'e.xmd')
        return str(cm.exception)

    def test_errors(self):
        self.assertIn('e.xmd:4: error: unknown element cancel_cu',
                      self.errors('cue A {\n  action {\n    cancel_cu(A);\n  }\n}\n'))
        self.assertIn('e.xmd:4: error: cancel_cue has no attribute bogus',
                      self.errors('cue A {\n  action {\n    cancel_cue(A, bogus: 1);\n  }\n}\n'))
        self.assertIn('missing } for the block opened at line 2',
                      self.errors('cue A {\n  action {\n    cancel_cue(A);\n\n}\n'))
        self.assertIn("e.xmd:2: error: unbalanced '}'", self.errors('}\n'))
        self.assertIn("'else' without 'if'", self.errors('cue A {\n  action {\n    else {\n    }\n  }\n}\n'))
        self.assertIn('positional argument not allowed',
                      self.errors('cue A {\n  action {\n    cancel_cue(A, B);\n  }\n}\n'))
        self.assertIn('expected', self.errors('cue A {\n  action {\n    cancel_cue(A)\n  }\n}\n'))
        self.assertIn('missing #director', md_error('cue A;\n'))

    def action(self, body, enc=None):
        """asm of one cue whose action holds `body` -> the XML between <action> and </action>."""
        head = '#director t\n' + ('#encoding %s\n' % enc if enc else '')
        xml = md.asm_text(head + 'cue A {\n  action {\n%s\n  }\n}\n' % body, 'e.xmd')[0]
        xml = xml.decode('latin-1')
        return xml[xml.index('<action>') + 8:xml.index('</action>')].strip()

    def test_review_fixes(self):
        # 1: compound assignment without spaces, never a renamed target
        self.assertEqual(self.action('    this.N+=1;'), '<set_value name="this.N" operation="add" exact="1"/>')
        self.assertIn("ends with '+'", self.errors('cue A {\n  action {\n    this.N+ = 1;\n  }\n}\n'))
        # 2: a comment XML cannot hold
        self.assertIn("e.xmd:4: error: an XML comment cannot contain '--'",
                      self.errors('cue A {\n  action {\n    // a --> b\n    cancel_cue(A);\n  }\n}\n'))
        self.assertIn("end with '-'", self.errors('//x-\ncue A;\n'))
        # 3: one <cues> per cue
        self.assertIn('two <cues>', self.errors('cue A {\n  cues {\n    cue B;\n  }\n  cue C;\n}\n'))
        self.assertIn('two <cues>', self.errors('cues {\n  cue B;\n}\ncue C;\n'))
        # 4: /// and comment: together
        self.assertIn('a /// comment and comment:',
                      self.errors('cue A {\n  action {\n    cancel_cue(A, comment: x); /// y\n  }\n}\n'))
        # 5: characters the encoding lacks: a reference in attributes, an error in comments
        self.assertEqual(self.action('    this.T = "a—b";'), '<set_value name="this.T" exact="a&#x2014;b"/>')
        self.assertEqual(self.action('    this.T = "a—b";', 'utf-8').encode('latin-1').decode('utf-8'),
                         '<set_value name="this.T" exact="a—b"/>')
        self.assertIn("e.xmd:4: error: '—' (U+2014) cannot be written in ISO-8859-1 in a comment",
                      self.errors('cue A {\n  action {\n    //a—b\n    cancel_cue(A);\n  }\n}\n'))
        # 6: element text is escaped (r"..." is raw XML); text only where the game has it
        xml = md.asm_text('#director t\nsignature "a<b&c";\n', 'e.xmd')[0]
        self.assertIn(b'<signature>a&lt;b&amp;c</signature>', xml)
        self.assertEqual(md.dis_bytes(xml, 'e.xml'), '#director t\nsignature "a<b&c";\n')
        self.assertIn('text is allowed only in signature', self.errors('cue A {\n  "stray";\n}\n'))
        self.assertIn('text is allowed only in signature', self.errors('cue A {\n  action {\n    cancel_cue(A) "x";\n  }\n}\n'))
        # 7: layout marks are whitespace
        self.assertIn('the ~ mark takes whitespace only',
                      self.errors('cue A {\n  action {\n    cancel_cue(A ~"bar");\n  }\n}\n'))
        self.assertIn('the ~~ mark takes whitespace only', self.errors('~~"x" cue A;\n'))
        self.assertIn('#end takes whitespace only', md_error('#end "x"\n#director t\ncue A;\n'))
        # 8: comparisons only after a positional value
        self.assertIn('a comparison follows only a positional value (here: name)',
                      self.errors('cue A {\n  action {\n    set_value(this.X == 3);\n  }\n}\n'))
        # 9: comments do not make an implicit <do_all>
        self.assertEqual(self.action('    //c\n    cancel_cue(A);'), '<!--c-->\n        <cancel_cue cue="A"/>')
        # 10: a missing ';' is reported at its statement
        self.assertIn("e.xmd:4: error: expected ';' or '{' after cancel_cue",
                      self.errors('cue A {\n  action {\n    cancel_cue(A)\n\n    reset_cue(A);\n  }\n}\n'))
        # 11: exact before operation reads as the operation
        x = self.action('    this.N += 2 ~swap, chance: 5;')
        self.assertEqual(x, '<set_value name="this.N" exact="2" operation="add" chance="5"/>')
        x = ('<?xml version="1.0" encoding="ISO-8859-1" ?>\n<director name="t">\n  <cues>\n    <cue name="A">\n'
             '      <action>\n        <set_value name="this.N" exact="2" operation="add"/>\n      </action>\n'
             '    </cue>\n  </cues>\n</director>\n').encode('latin-1')
        self.assertIn('this.N += 2 ~swap;', md.dis_bytes(x, 'x.xml'))


def md_error(text):
    try:
        md.asm_text(text, 'e.xmd')
    except md.MdError as e:
        return str(e)
    return ''


class Stock(unittest.TestCase):
    def check(self, d):
        if not os.path.isdir(d):
            self.skipTest('no director folder %s' % d)
        files = stock(d)
        bad = []
        for fp in files:
            with open(fp, 'rb') as f:
                data = f.read()
            try:
                md.dis_bytes(data, fp)          # dis checks that asm rebuilds the bytes
            except md.MdError as e:
                bad.append(str(e)[:200])
        self.assertEqual(bad, [], '%d/%d identical' % (len(files) - len(bad), len(files)))

    def test_ap(self):
        self.check(AP)

    def test_fl(self):
        self.check(FL)


if __name__ == '__main__':
    unittest.main()
