"""Compile cache (`x3kc compile --cache DIR`): results that do not change between builds, kept outside the sources.

- Bodies: the AST of every function body (pass 2 of the parser), keyed by a hash of the function's tokens and
  header together with the global signature - every class (id, name, parent, field names, method names and
  parameter counts), the global variables and functions. A body is parsed against exactly those names, so the
  same tokens under the same signature give the same AST. An edit inside one body re-parses that body; a new or
  renamed class, field, method or global changes the signature and re-parses all of them.
- Base: what `--base` derives from the base object alone (the instruction keys of its methods, its return sites
  that a savegame can hold), keyed by the base object's sha256.
Every entry also depends on the x3kc code: the cache is keyed by a hash of the package's modules and data files,
so another x3kc version starts cold. A missing, stale or unreadable cache only costs time; results are the same.
"""
import hashlib
import os
import pickle

FORMAT = 1
_code_version = None


def code_version():
    global _code_version
    if _code_version is None:
        h = hashlib.sha256(b'kccache %d' % FORMAT)
        d = os.path.dirname(os.path.abspath(__file__))
        for f in sorted(os.listdir(d)):
            if f.endswith(('.py', '.txt')):
                with open(os.path.join(d, f), 'rb') as fh:
                    h.update(f.encode() + b'\0' + fh.read())
        _code_version = h.hexdigest()[:16]
    return _code_version


def signature(syms):
    """Everything a body is resolved against (kcparse.BodyParser): class ids, names, parents, field names, method
    names and parameter counts; class 0 holds the global variables and functions."""
    out = []
    for cid in sorted(syms.classes):
        k = syms.classes[cid]
        out.append((cid, k.name, k.parent, tuple(f.name for f in k.fields),
                    tuple((m.name, m.nparams) for m in k.methods)))
    return repr(out).encode('utf-8')


def body_key(sig_hash, toks, decl):
    h = hashlib.sha1(sig_hash)
    h.update(repr((decl.cls, decl.name, decl.params, decl.frame, decl.asm)).encode('utf-8'))
    h.update('\x00'.join(t.kind[0] + t.text for t in toks[decl.start:decl.end]).encode('latin1', 'replace'))
    return h.digest()


class Cache:
    def __init__(self, path):
        self.dir = path
        os.makedirs(path, exist_ok=True)

    def _file(self, name):
        return os.path.join(self.dir, '%s.%s.pickle' % (name, code_version()))

    def load(self, name):
        try:
            with open(self._file(name), 'rb') as f:
                return pickle.load(f)
        except Exception:           # missing, from an interrupted write, or unreadable: start cold
            return None

    def save(self, name, data):
        p = self._file(name)
        tmp = p + '.%d.tmp' % os.getpid()
        try:
            with open(tmp, 'wb') as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, p)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # entries of other x3kc versions for this name are dead weight
        prefix, mine = name + '.', os.path.basename(p)
        for f in os.listdir(self.dir):
            if f.startswith(prefix) and f.endswith('.pickle') and f != mine and f.count('.') == mine.count('.'):
                try:
                    os.remove(os.path.join(self.dir, f))
                except OSError:
                    pass
