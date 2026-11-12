"""KC language server (editors/kclsp): a scripted LSP client over stdio, no editor.

The synthetic split project needs no game files. With D:/GAMES/x3_src/kc/ap/x3story.kc (or X3KC_STORY_KC) present,
the real x3story is split into a temp directory and one class is opened.
"""
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import unquote

from x3kc import kcsplit

from test_kc import SOURCE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORY = os.environ.get('X3KC_STORY_KC', 'D:/GAMES/x3_src/kc/ap/x3story.kc')
MSCI_DB = os.environ.get('X3KC_MSCI_DB', 'D:/GAMES/x3_src/build/tmp/queue/msci/ap/msci_db.json')


def uri(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


class Client:
    def __init__(self, extra_env=None):
        env = dict(os.environ)
        for k in ('KCLSP_TEXT_DIRS', 'KCLSP_MSCI_DB', 'KCLSP_MSCI_DB_AP', 'KCLSP_MSCI_DB_FL', 'KCLSP_MSCI_EDITION'):
            env.pop(k, None)
        env.update(extra_env or {})
        self.p = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-m', 'editors.kclsp'], cwd=ROOT, env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q = queue.Queue()
        self.n = 0
        self.diags = {}
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        f = self.p.stdout
        while True:
            n = None
            while True:
                line = f.readline()
                if not line:
                    self.q.put(None)
                    return
                line = line.strip()
                if not line:
                    break
                k, _, v = line.decode().partition(':')
                if k.lower() == 'content-length':
                    n = int(v)
            self.q.put(json.loads(f.read(n).decode('utf-8')))

    def send(self, msg):
        msg['jsonrpc'] = '2.0'
        b = json.dumps(msg).encode('utf-8')
        self.p.stdin.write(b'Content-Length: %d\r\n\r\n' % len(b) + b)
        self.p.stdin.flush()

    def notify(self, method, params):
        self.send({'method': method, 'params': params})

    def pump(self, until, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                m = self.q.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            if m is None:
                raise AssertionError('server exited: %s' % self.p.stderr.read().decode(errors='replace'))
            if m.get('method') == 'textDocument/publishDiagnostics':
                self.diags[m['params']['uri']] = m['params']['diagnostics']
            r = until(m)
            if r is not None:
                return r
        raise AssertionError('timeout')

    def request(self, method, params):
        self.n += 1
        mid = self.n
        self.send({'id': mid, 'method': method, 'params': params})
        m = self.pump(lambda m: m if m.get('id') == mid else None)
        if 'error' in m:
            raise AssertionError(m['error'])
        return m['result']

    def wait_diags(self, u, pred, timeout=30):
        def ok(m):
            if m.get('method') == 'textDocument/publishDiagnostics' and m['params']['uri'] == u \
                    and pred(m['params']['diagnostics']):
                return m['params']['diagnostics']
            return None
        if u in self.diags and pred(self.diags[u]):
            return self.diags[u]
        return self.pump(ok, timeout)

    def open(self, path, text=None):
        if text is None:
            with open(path, encoding='latin1') as f:
                text = f.read()
        self.notify('textDocument/didOpen', {'textDocument': {'uri': uri(path), 'languageId': 'kc', 'version': 1,
                                                              'text': text}})
        return text

    def close(self):
        try:
            self.request('shutdown', None)
            self.notify('exit', None)
            self.p.wait(10)
        finally:
            if self.p.poll() is None:
                self.p.kill()
            for s in (self.p.stdin, self.p.stdout, self.p.stderr):
                s.close()


def pos_of(text, needle, k=0):
    i = text.index(needle) + k
    line = text.count('\n', 0, i)
    return line, i - (text.rfind('\n', 0, i) + 1)


class LanguageServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kclsp')
        self.c = Client({'KCLSP_MSCI_DB': MSCI_DB} if self._testMethodName == 'test_msci_with_database' else None)
        r = self.c.request('initialize', {'processId': None, 'rootUri': None, 'capabilities': {}})
        self.assertTrue(r['capabilities']['definitionProvider'])
        self.c.notify('initialized', {})

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def split(self, text, name='t'):
        d = os.path.join(self.dir, name)
        kcsplit.write_project(d, name, text)
        return d

    def test_synthetic_project(self):
        d = self.split(SOURCE)
        ship = os.path.join(d, 'Ship.kc')
        u = uri(ship)
        text = self.c.open(ship)
        self.assertEqual(self.c.wait_diags(u, lambda ds: True), [])

        # outline
        syms = self.c.request('textDocument/documentSymbol', {'textDocument': {'uri': u}})
        self.assertEqual([s['name'] for s in syms], ['Ship = 11'])
        kids = [k['name'] for k in syms[0]['children']]
        self.assertIn('m2', kids)
        self.assertIn('Describe(x)', kids)

        # definitions: inherited method, global function, global, native, this->, a class name
        def defn(needle, k=0):
            line, col = pos_of(text, needle, k)
            return self.c.request('textDocument/definition', {'textDocument': {'uri': u},
                                                               'position': {'line': line, 'character': col}})
        r = defn('Sum(x)')
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]['uri'].endswith('/Thing.kc'), r)
        with open(os.path.join(d, 'Thing.kc'), encoding='latin1') as f:
            thing = f.read()
        self.assertEqual((r[0]['range']['start']['line'], r[0]['range']['start']['character']),
                         pos_of(thing, 'Sum(n)'))
        r = defn('Helper(x)')
        self.assertTrue(unquote(r[0]['uri']).endswith('/@Helper.kc'), r)
        r = defn('gCount++')
        self.assertTrue(unquote(r[0]['uri']).endswith('/@header.kc'), r)
        r = defn('SE_Random')
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]['uri'].endswith(('.jsonl', 'natives.txt')), r)
        r = defn('this->Describe', 6)
        self.assertEqual((r[0]['uri'], r[0]['range']['start']['line']), (u, pos_of(text, 'Describe(x)')[0]))
        r = defn(': Thing', 2)
        self.assertTrue(r[0]['uri'].endswith('/Thing.kc'), r)

        # an unknown name: a diagnostic at the name, incremental change
        line, col = pos_of(text, 'gCount++')
        self.c.notify('textDocument/didChange', {
            'textDocument': {'uri': u, 'version': 2},
            'contentChanges': [{'range': {'start': {'line': line, 'character': col},
                                          'end': {'line': line, 'character': col + 6}}, 'text': 'noSuchName'}]})
        ds = self.c.wait_diags(u, lambda ds: len(ds) > 0)
        self.assertEqual(len(ds), 1)
        self.assertIn('unknown name noSuchName', ds[0]['message'])
        self.assertEqual(ds[0]['range'], {'start': {'line': line, 'character': col},
                                          'end': {'line': line, 'character': col + 10}})
        # a lexer error: a bad character, full sync
        bad = text.replace('gCount++', 'gCount++ $')
        self.c.notify('textDocument/didChange', {'textDocument': {'uri': u, 'version': 3},
                                                 'contentChanges': [{'text': bad}]})
        ds = self.c.wait_diags(u, lambda ds: len(ds) == 1 and 'bad character' in ds[0]['message'])
        self.assertEqual(ds[0]['range']['start'], dict(zip(('line', 'character'), pos_of(bad, '$'))))
        # a codegen error and a wrong argument count
        wrong = text.replace('gCount++;', 'gCount++; break; Sum(1, 2);', 1).replace('while (x > 0)', 'if (x > 0)')
        self.c.notify('textDocument/didChange', {'textDocument': {'uri': u, 'version': 4},
                                                 'contentChanges': [{'text': wrong}]})
        ds = self.c.wait_diags(u, lambda ds: len(ds) == 2)
        msgs = sorted(x['message'] for x in ds)
        self.assertIn('break/continue outside a loop', msgs[0])
        self.assertIn('Sum takes 1 argument, called with 2', msgs[1])
        # fixed again: diagnostics clear
        self.c.notify('textDocument/didChange', {'textDocument': {'uri': u, 'version': 5},
                                                 'contentChanges': [{'text': text}]})
        self.c.wait_diags(u, lambda ds: ds == [])

    def test_declaration_change_reaches_other_parts(self):
        d = self.split(SOURCE)
        ship, thing = os.path.join(d, 'Ship.kc'), os.path.join(d, 'Thing.kc')
        self.c.open(ship)
        t = self.c.open(thing)
        self.c.wait_diags(uri(ship), lambda ds: ds == [])
        # rename Thing.Sum: Ship's call to Sum stops resolving (it becomes a native call: a warning)
        self.c.notify('textDocument/didChange', {'textDocument': {'uri': uri(thing), 'version': 2},
                                                 'contentChanges': [{'text': t.replace('Sum(n)', 'Total(n)')}]})
        ds = self.c.wait_diags(uri(ship), lambda ds: len(ds) > 0)
        self.assertIn('native Sum is unknown', ds[0]['message'])

    def test_single_file_and_pass1_error(self):
        p = os.path.join(self.dir, 'one.kc')
        with open(p, 'w', encoding='latin1', newline='\n') as f:
            f.write(SOURCE)
        bad = SOURCE.replace('class Ship = 11 : Thing {', 'class Ship = 11 : Thing {\n    junk;')
        self.c.open(p, bad)
        ds = self.c.wait_diags(uri(p), lambda ds: len(ds) > 0)
        self.assertIn('field or function expected', ds[0]['message'])
        self.assertEqual(ds[0]['range']['start'], dict(zip(('line', 'character'), pos_of(bad, 'junk'))))

    def msci_project(self):
        """The two x3kc example scripts as .msci files, a text page 8870 next to them (repo layout)."""
        repo = os.path.join(self.dir, 'repo')
        d = os.path.join(repo, 'scripts', 'ap', 'scripts')
        os.makedirs(d)
        ex = os.path.join(ROOT, 'docs', 'examples', 'msci_hello')
        for n in ('plugin.x3kc.hello', 'lib.x3kc.hello.thousands'):
            shutil.copyfile(os.path.join(ex, n + '.txt'), os.path.join(d, n + '.msci'))
        t = os.path.join(repo, 'kc', 'ap', 't')
        os.makedirs(t)
        shutil.copyfile(os.path.join(ex, '8870-L044.xml'), os.path.join(t, '8870-L044.xml'))
        return d

    def test_msci_without_database(self):
        """No msci_db.json: header check, outline, labels / script calls, command completion and text hover."""
        d = self.msci_project()
        p = os.path.join(d, 'plugin.x3kc.hello.msci')
        u = uri(p)
        text = self.c.open(p)
        ds = self.c.wait_diags(u, lambda ds: len(ds) > 0)
        self.assertIn('no msci_db.json', ds[0]['message'])
        self.assertEqual(ds[0]['severity'], 3)
        lib = os.path.join(d, 'lib.x3kc.hello.thousands.msci')
        lt = self.c.open(lib)
        syms = self.c.request('textDocument/documentSymbol', {'textDocument': {'uri': uri(lib)}})
        self.assertEqual([s['name'] for s in syms], ['lib.x3kc.hello.thousands', 'pad3'])
        self.assertEqual(syms[0]['children'][0]['name'], '$value: Var/Number')
        line, col = pos_of(lt, 'gosub pad3', 7)
        r = self.c.request('textDocument/definition', {'textDocument': {'uri': uri(lib)},
                                                        'position': {'line': line, 'character': col}})
        self.assertEqual(r[0]['range']['start'], dict(zip(('line', 'character'), pos_of(lt, 'pad3:'))))
        line, col = pos_of(text, 'lib.x3kc.hello.thousands', 3)
        r = self.c.request('textDocument/definition', {'textDocument': {'uri': u},
                                                        'position': {'line': line, 'character': col}})
        self.assertTrue(unquote(r[0]['uri']).endswith('/lib.x3kc.hello.thousands.msci'), r)
        c = self.c.request('textDocument/completion', {'textDocument': {'uri': u},
                                                        'position': {'line': 3, 'character': 0}})
        labels = [i['label'] for i in c['items']]
        self.assertGreater(len(labels), 700)
        self.assertIn('$ret = get player name', labels)
        line, col = pos_of(text, 'pageid=8870', 8)
        h = self.c.request('textDocument/hover', {'textDocument': {'uri': u},
                                                   'position': {'line': line, 'character': col}})
        self.assertIn('Text {8870,2}', h['contents']['value'])
        self.assertIn('English', h['contents']['value'])

    @unittest.skipUnless(os.path.isfile(MSCI_DB), 'msci_db.json not found')
    def test_msci_with_database(self):
        d = self.msci_project()
        p = os.path.join(d, 'plugin.x3kc.hello.msci')
        u = uri(p)
        text = self.c.open(p)
        self.assertEqual(self.c.wait_diags(u, lambda ds: True, 60), [])
        bad = text.replace('$ship = get player ship', '$ship = get playr ship') \
                  .replace('return null', 'gosub nowhere\nif $count\nreturn null')
        self.c.notify('textDocument/didChange', {'textDocument': {'uri': u, 'version': 2},
                                                 'contentChanges': [{'text': bad}]})
        ds = self.c.wait_diags(u, lambda ds: len(ds) == 3)
        got = sorted((x['range']['start']['line'], x['message'].split(' ')[0]) for x in ds)
        lines = bad.split('\n')
        self.assertEqual(got, sorted([(lines.index('$ship = get playr ship'), 'cannot'),
                                      (lines.index('gosub nowhere'), 'undefined'),
                                      (lines.index('if $count'), 'if')]))
        line, col = pos_of(text, 'get player name', 4)
        h = self.c.request('textDocument/hover', {'textDocument': {'uri': u},
                                                   'position': {'line': line, 'character': col}})
        self.assertIn('command 1070', h['contents']['value'])

    def test_text_references(self):
        """Hover / definition on SE_ReadText and on a KC wrapper with literal arguments, pages found through the
        repository layout (game/<ed>/addon/t, kc/<ed>/t), the AP page prefix 35xxxx normalised."""
        repo = os.path.join(self.dir, 'repo')
        t = os.path.join(repo, 'game', 'ap', 'addon', 't')
        own = os.path.join(repo, 'kc', 'ap', 't')
        os.makedirs(t)
        os.makedirs(own)
        page = '<?xml version="1.0" encoding="UTF-8"?>\n<language id="%d">\n<page id="350035" title="x">\n' \
               '<t id="951">%s</t>\n<t id="952">%s</t>\n</page>\n</language>\n'
        for lang, a, b in ((44, 'Autosaving Game ...', 'Saving &amp; more'), (7, 'Автосохранение игры...', 'Сохр'),
                           (49, 'Spielstand wird automatisch gespeichert...', 'Speichern')):
            with open(os.path.join(t, '0001-L%03d.xml' % lang), 'w', encoding='utf-8', newline='\n') as f:
                f.write(page % (lang, a, b))
        with open(os.path.join(own, '9500.xml'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<language id="44">\n<page id="9500">\n'
                    '<t id="1">FPS counter</t>\n</page>\n</language>\n')
        src = ('#object 801 100\nvar gX = 0;\n\nclass Menu = 10 {\n'
               '    function Label(id) {\n        return SE_ReadText(35, id);\n    }\n'
               '    function Show() {\n        var a = SE_ReadText(35, 951);\n        var b = Label(952);\n'
               '        var c = SE_ReadText(9500, 1);\n        return a + b + c + SE_ReadText(35, 999);\n    }\n}\n')
        d = os.path.join(repo, 'kc', 'ap', 'menu')
        kcsplit.write_project(d, 'menu', src)
        menu = os.path.join(d, 'Menu.kc')
        u = uri(menu)
        text = self.c.open(menu)
        self.c.wait_diags(u, lambda ds: ds == [])

        def hover(needle, k):
            line, col = pos_of(text, needle, k)
            r = self.c.request('textDocument/hover', {'textDocument': {'uri': u},
                                                       'position': {'line': line, 'character': col}})
            return r and r['contents']['value']

        h = hover('SE_ReadText(35, 951)', 17)          # on the id argument
        self.assertIn('Text {35,951}', h)
        self.assertLess(h.index('Russian'), h.index('English'))
        self.assertLess(h.index('English'), h.index('German'))
        self.assertIn('Автосохранение игры...', h)
        self.assertIn('page 350035', h)
        h = hover('Label(952)', 0)                      # a KC wrapper: page 35 fixed inside it
        self.assertIn('Text {35,952}', h)
        self.assertIn('Saving & more', h)
        h = hover('SE_ReadText(9500, 1)', 0)            # the project's own page
        self.assertIn('FPS counter', h)
        self.assertIn('9500.xml', h)
        h = hover('SE_ReadText(35, 999)', 0)
        self.assertIn('not found', h)
        h = hover('SE_ReadText(35, id)', 0)             # not literal: the native's description instead
        self.assertIn('SE_ReadText', h)
        self.assertNotIn('Text {', h)
        line, col = pos_of(text, 'SE_ReadText(35, 951)')
        r = self.c.request('textDocument/definition', {'textDocument': {'uri': u},
                                                        'position': {'line': line, 'character': col}})
        self.assertEqual(len(r), 3)
        self.assertTrue(r[0]['uri'].endswith('/0001-L007.xml'), r)
        self.assertEqual(r[0]['range']['start'], {'line': 3, 'character': 0})

    @unittest.skipUnless(os.path.isfile(STORY), 'x3story.kc not found')
    def test_real_class(self):
        with open(STORY, encoding='latin1') as f:
            text = f.read()
        d = self.split(text, 'x3story')
        ship = os.path.join(d, 'Ship.kc')
        u = uri(ship)
        t0 = time.monotonic()
        src = self.c.open(ship)
        self.assertEqual(self.c.wait_diags(u, lambda ds: True, 120), [])
        first = time.monotonic() - t0
        syms = self.c.request('textDocument/documentSymbol', {'textDocument': {'uri': u}})
        self.assertEqual(syms[0]['name'].split(' ')[0], 'Ship')
        self.assertGreater(len(syms[0]['children']), 100)
        # break one statement: the diagnostic arrives within a second of the change
        i = src.index('return ', src.index('function '))
        line, col = pos_of(src, 'return ', i - src.index('return '))
        self.c.notify('textDocument/didChange', {
            'textDocument': {'uri': u, 'version': 2},
            'contentChanges': [{'range': {'start': {'line': line, 'character': col},
                                          'end': {'line': line, 'character': col}}, 'text': 'noSuchName; '}]})
        t0 = time.monotonic()
        ds = self.c.wait_diags(u, lambda ds: len(ds) > 0)
        dt = time.monotonic() - t0
        self.assertEqual(ds[0]['range']['start'], {'line': line, 'character': col})
        self.assertLess(dt, 0.3 + 1.5)
        # a definition into another part
        line, col = pos_of(src, 'SetPlayerShip') if 'SetPlayerShip' in src else pos_of(src, 'SE_')
        r = self.c.request('textDocument/definition', {'textDocument': {'uri': u},
                                                        'position': {'line': line, 'character': col}})
        self.assertTrue(r)
        print('\nreal Ship.kc: first open %.2f s, diagnostic %.2f s after the change' % (first, dt))


if __name__ == '__main__':
    unittest.main()
