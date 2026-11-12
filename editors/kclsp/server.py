"""KC language server: JSON-RPC 2.0 over stdio (LSP base protocol), Python stdlib only.

    python -X utf8 -B -m editors.kclsp            (from the x3kc checkout; or with it on PYTHONPATH)
    set KCLSP_LOG=<file> to log every message

Features: diagnostics (lexer / parser errors of the edited part at once, pass 2 + codegen + unknown natives +
argument counts of static calls after a 0.3 s pause; the whole project on save), document symbols, go to
definition. Text sync is incremental.
"""
import json
import os
import pathlib
import sys
import threading
import time
import traceback
import urllib.parse

from . import project as P

DEBOUNCE = 0.3


def uri_to_path(uri):
    u = urllib.parse.urlparse(uri)
    p = urllib.parse.unquote(u.path)
    if os.name == 'nt' and len(p) > 2 and p[0] == '/' and p[2] == ':':
        p = p[1:]
    if u.netloc and u.netloc != 'localhost':
        p = '//' + u.netloc + p
    return os.path.abspath(p)


def path_to_uri(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


def offset_of(text, line, char, starts):
    """LSP position (0-based line, UTF-16 units; KC text is latin1 / BMP, so units are characters) -> offset."""
    if line >= len(starts):
        return len(text)
    return min(starts[line] + char, starts[line + 1] - 1 if line + 1 < len(starts) else len(text))


def line_starts(text):
    out = [0]
    i = text.find('\n')
    while i >= 0:
        out.append(i + 1)
        i = text.find('\n', i + 1)
    return out


class Server:
    def __init__(self, rfile, wfile, log=None):
        self.r, self.w = rfile, wfile
        self.wlock = threading.Lock()
        self.lock = threading.RLock()       # project state
        self.cv = threading.Condition(self.lock)
        self.projects = {}                  # index key or file key -> Project
        self.docs = {}                      # file key -> (Project, Part)
        self.uris = {}                      # file key -> the client's uri for it
        self.pending = {}                   # file key -> due time
        self.full = set()                   # projects to check as a whole
        self.published = {}                 # file key -> number of diagnostics last sent
        self.log = log
        self.stop = False
        self.worker = threading.Thread(target=self.work, daemon=True)

    # ---- transport ---------------------------------------------------------------------------------------------
    def logf(self, *a):
        if self.log:
            self.log.write(time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a) + '\n')
            self.log.flush()

    def read(self):
        n = None
        while True:
            line = self.r.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            k, _, v = line.decode('ascii', 'replace').partition(':')
            if k.lower() == 'content-length':
                n = int(v.strip())
        if n is None:
            return {}
        body = self.r.read(n)
        if len(body) < n:
            return None
        return json.loads(body.decode('utf-8'))

    def send(self, msg):
        msg['jsonrpc'] = '2.0'
        b = json.dumps(msg, ensure_ascii=False).encode('utf-8')
        with self.wlock:
            self.w.write(b'Content-Length: %d\r\n\r\n' % len(b))
            self.w.write(b)
            self.w.flush()
        if self.log:
            self.logf('->', json.dumps(msg)[:400])

    def notify(self, method, params):
        self.send({'method': method, 'params': params})

    # ---- main loop ---------------------------------------------------------------------------------------------
    def run(self):
        self.worker.start()
        while True:
            msg = self.read()
            if msg is None:
                break
            if not msg:
                continue
            if self.log:
                self.logf('<-', json.dumps(msg)[:400])
            method = msg.get('method')
            mid = msg.get('id')
            if method == 'exit':
                break
            try:
                h = getattr(self, 'on_' + (method or '').replace('/', '_').replace('$', 'S'), None)
                if h is None:
                    if mid is not None and method:
                        self.send({'id': mid, 'error': {'code': -32601, 'message': 'method not found: %s' % method}})
                    continue
                res = h(msg.get('params') or {})
                if mid is not None:
                    self.send({'id': mid, 'result': res})
            except Exception as x:                  # a bug must not kill the session
                self.logf('error', traceback.format_exc())
                if mid is not None:
                    self.send({'id': mid, 'error': {'code': -32603, 'message': '%s: %s' % (type(x).__name__, x)}})
        with self.lock:
            self.stop = True
            self.cv.notify_all()

    # ---- lifecycle ---------------------------------------------------------------------------------------------
    def on_initialize(self, p):
        return {
            'capabilities': {
                'textDocumentSync': {'openClose': True, 'change': 2, 'save': {'includeText': False}},
                'definitionProvider': True,
                'documentSymbolProvider': True,
            },
            'serverInfo': {'name': 'x3kc-kclsp', 'version': '0.1'},
        }

    def on_initialized(self, p):
        return None

    def on_shutdown(self, p):
        return None

    def on_S_cancelRequest(self, p):
        return None

    # ---- documents ---------------------------------------------------------------------------------------------
    def doc(self, uri):
        """(Project, Part) for a uri, opening the project on first use."""
        path = uri_to_path(uri)
        key = P.norm(path)
        d = self.docs.get(key)
        if d:
            return d
        index, _ = P.find_project(path)
        pkey = P.norm(index) if index else key
        pr = self.projects.get(pkey)
        if pr is None:
            pr = P.Project(index=index) if index else P.Project(single=path)
            self.projects[pkey] = pr
        part = pr.by_key.get(key)
        if part is None:                    # listed nowhere: a project of its own
            pr = P.Project(single=path)
            self.projects[key] = pr
            part = pr.parts[0]
        self.docs[key] = (pr, part)
        return pr, part

    def on_textDocument_didOpen(self, p):
        td = p['textDocument']
        with self.lock:
            pr, part = self.doc(td['uri'])
            self.uris[part.key] = td['uri']
            part.open = True
            part.set_text(td['text'])
            self.analyse(pr, part, full=False)          # lexer / pass 1 at once
            self.schedule(part, 0.0)

    def on_textDocument_didChange(self, p):
        td = p['textDocument']
        with self.lock:
            pr, part = self.doc(td['uri'])
            text = part.source()
            for ch in p['contentChanges']:
                if 'range' not in ch:
                    text = ch['text']
                    continue
                st = line_starts(text)
                r = ch['range']
                a = offset_of(text, r['start']['line'], r['start']['character'], st)
                b = offset_of(text, r['end']['line'], r['end']['character'], st)
                text = text[:a] + ch['text'] + text[b:]
            part.set_text(text)
            self.schedule(part, DEBOUNCE)

    def on_textDocument_didSave(self, p):
        with self.lock:
            pr, part = self.doc(p['textDocument']['uri'])
            self.full.add(pr)
            self.schedule(part, 0.0)

    def on_textDocument_didClose(self, p):
        with self.lock:
            pr, part = self.doc(p['textDocument']['uri'])
            part.open = False
            part.text = None                # back to the file on disk
            part.version += 1
            self.pending.pop(part.key, None)

    def schedule(self, part, delay):
        self.pending[part.key] = time.monotonic() + delay
        self.cv.notify_all()

    # ---- analysis ----------------------------------------------------------------------------------------------
    def work(self):
        while True:
            with self.lock:
                while not self.stop:
                    now = time.monotonic()
                    due = [k for k, t in self.pending.items() if t <= now]
                    if due or (self.full and not self.pending):
                        break
                    wait = min(self.pending.values()) - now if self.pending else None
                    self.cv.wait(wait)
                if self.stop:
                    return
                try:
                    if due:
                        for k in due:
                            del self.pending[k]
                            pr, part = self.docs[k]
                            self.analyse(pr, part, full=True)
                    else:
                        pr = self.full.pop()
                        self.check_all(pr)
                except Exception:
                    self.logf('analysis error', traceback.format_exc())

    def analyse(self, pr, part, full):
        changed = pr.refresh()
        if full:
            pr.check(part)
            if changed:                     # declarations moved: the other open parts may resolve differently
                for k, (pr2, p2) in list(self.docs.items()):
                    if pr2 is pr and p2 is not part and p2.open:
                        pr2.check(p2)
                        self.publish(pr2, p2)
        self.publish(pr, part)

    def check_all(self, pr):
        t0 = time.perf_counter()
        pr.refresh()
        for part in pr.parts:
            pr.check(part)
            if pr.diagnostics(part) or self.published.get(part.key):
                self.publish(pr, part)
        self.logf('project check %s: %.2f s' % (pr.name(), time.perf_counter() - t0))

    def publish(self, pr, part):
        ds = pr.diagnostics(part)
        self.published[part.key] = len(ds)
        self.notify('textDocument/publishDiagnostics', {
            'uri': self.uris.get(part.key) or path_to_uri(part.path),
            'diagnostics': [{
                'range': {'start': {'line': d.line - 1, 'character': d.col},
                          'end': {'line': d.line - 1, 'character': d.end}},
                'severity': d.severity, 'source': 'kc', 'message': d.msg} for d in ds]})

    def fresh(self, uri):
        """(Project, Part) analysed up to the current text (requests may come before the debounce fires)."""
        pr, part = self.doc(uri)
        pr.refresh()
        pr.check(part)
        return pr, part

    # ---- requests ----------------------------------------------------------------------------------------------
    def on_textDocument_definition(self, p):
        with self.lock:
            pr, part = self.fresh(p['textDocument']['uri'])
            pos = p['position']
            locs = pr.definition(part, pos['line'] + 1, pos['character'])
            return [{'uri': self.uris.get(P.norm(path)) or path_to_uri(path),
                     'range': {'start': {'line': ln - 1, 'character': c},
                               'end': {'line': ln - 1, 'character': c + n}}} for path, ln, c, n in locs]

    def on_textDocument_documentSymbol(self, p):
        with self.lock:
            pr, part = self.doc(p['textDocument']['uri'])
            pr.refresh()

            def conv(s):
                name, kind, r, sel, kids = s
                return {'name': name, 'kind': kind,
                        'range': {'start': {'line': r[0] - 1, 'character': r[1]},
                                  'end': {'line': r[2] - 1, 'character': r[3]}},
                        'selectionRange': {'start': {'line': sel[0] - 1, 'character': sel[1]},
                                           'end': {'line': sel[2] - 1, 'character': sel[3]}},
                        'children': [conv(k) for k in kids]}
            return [conv(s) for s in pr.symbols(part)]


def main():
    log = None
    if os.environ.get('KCLSP_LOG'):
        log = open(os.environ['KCLSP_LOG'], 'a', encoding='utf-8')
    sys.setrecursionlimit(20000)
    threading.stack_size(64 * 1024 * 1024)      # deep expressions recurse in the parser
    t = threading.Thread(target=lambda: Server(sys.stdin.buffer, sys.stdout.buffer, log).run())
    t.start()
    t.join()
