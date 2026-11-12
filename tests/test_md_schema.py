"""editors/tools/md_schema.py: Mission Director files point Visual Studio's XML editor at director.xsd."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'editors', 'tools'))
import md_schema    # noqa: E402

STOCK = '''<?xml version="1.0" encoding="ISO-8859-1" ?>
<?xml-stylesheet href="director.xsl" type="text/xsl" ?>
<director name="test" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="director.xsd">
  <cues/>
</director>
'''
MINE = '<?xml version="1.0" encoding="ISO-8859-1" ?>\n<director name="mine">\n  <cues/>\n</director>\n'


class MdSchema(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='mdschema')
        self.d = os.path.join(self.dir, 'game', 'ap', 'addon', 'director')
        os.makedirs(self.d)
        with open(os.path.join(self.d, 'director.xsd'), 'w') as f:
            f.write('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>\n')
        with open(os.path.join(self.d, '2.024 Terran Plot.xml'), 'w', encoding='latin1') as f:
            f.write(STOCK)
        self.mine = os.path.join(self.dir, 'mods', 'ap', 'director', 'my plot.xml')
        os.makedirs(os.path.dirname(self.mine))
        with open(self.mine, 'w', encoding='latin1', newline='\n') as f:
            f.write(MINE)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_stock_resolves(self):
        is_md, ref, res = md_schema.schema_ref(os.path.join(self.d, '2.024 Terran Plot.xml'))
        self.assertEqual((is_md, ref), (True, 'director.xsd'))
        self.assertTrue(os.path.isfile(res))
        self.assertEqual(md_schema.main(['check', self.d]), 0)

    def test_fix_own_file(self):
        self.assertEqual(md_schema.main(['check', self.mine]), 1)
        before = open(self.mine, 'rb').read()
        md_schema.main(['fix', self.mine])                  # dry run: nothing written
        self.assertEqual(open(self.mine, 'rb').read(), before)
        md_schema.main(['fix', self.mine, '--write'])
        after = open(self.mine, 'rb').read()
        self.assertIn(b'xsi:noNamespaceSchemaLocation="../../../game/ap/addon/director/director.xsd"', after)
        self.assertIn(b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"', after)
        self.assertEqual(after.replace(after[after.index(b'<director'):after.index(b'>', after.index(b'<director')) + 1],
                                       b''), before.replace(b'<director name="mine">', b''))
        self.assertEqual(md_schema.main(['check', self.mine]), 0)


if __name__ == '__main__':
    unittest.main()
