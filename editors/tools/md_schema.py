"""Mission Director files and their schema: Visual Studio's XML editor validates and completes an MD file when its
<director> root names the schema with xsi:noNamespaceSchemaLocation (a path relative to the file). The stock files
do (director.xsd, next to them in game/<edition>/addon[2]/director/, which includes dirobjdb.xsd). A director file
elsewhere needs the relative path to that director.xsd; this tool checks and, with --write, sets it.

    python editors/tools/md_schema.py check <file or dir>...          report each <director> file's schema reference
    python editors/tools/md_schema.py fix <file or dir>... [--edition ap|fl] [--schema PATH] [--write]

`fix` points the reference at --schema, else at <repo>/game/<edition>/addon[2]/director/director.xsd (<repo> = the
first folder above the file that holds game/; edition from the path, else --edition). The file's bytes are kept
except the root tag's attributes; without --write it only prints what it would change.
"""
import argparse
import os
import re
import sys

ROOT_RX = re.compile(rb'<director\b[^>]*>', re.S)
LOC_RX = re.compile(rb'\bxsi:noNamespaceSchemaLocation\s*=\s*"([^"]*)"')
XSI = b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def md_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for d, _, fs in os.walk(p):
                for f in sorted(fs):
                    if f.lower().endswith('.xml'):
                        yield os.path.join(d, f)
        else:
            yield p


def root_tag(data):
    m = ROOT_RX.search(data[:4096])
    return m


def schema_ref(path):
    """(is a director file, the reference or None, the resolved path or None)."""
    with open(path, 'rb') as f:
        data = f.read()
    m = root_tag(data)
    if not m:
        return False, None, None
    lm = LOC_RX.search(m.group(0))
    if not lm:
        return True, None, None
    ref = lm.group(1).decode('latin1')
    return True, ref, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), ref.replace('%20', ' ')))


def default_schema(path, edition=None):
    parts = os.path.abspath(path).replace('\\', '/').split('/')
    for i in range(len(parts) - 1, 0, -1):
        d = '/'.join(parts[:i])
        if os.path.isdir(d + '/game'):
            ed = edition
            low = [x.lower() for x in parts[i:]]
            if ed is None:
                ed = 'fl' if 'fl' in low else 'ap' if 'ap' in low else None
            if ed is None:
                return None
            for sub in ('addon2', 'addon') if ed == 'fl' else ('addon',):
                x = os.path.join(d, 'game', ed, sub, 'director', 'director.xsd')
                if os.path.isfile(x):
                    return x
            return None
    return None


def fix(path, schema, write):
    with open(path, 'rb') as f:
        data = f.read()
    m = root_tag(data)
    if not m:
        return None
    rel = os.path.relpath(schema, os.path.dirname(os.path.abspath(path))).replace('\\', '/').replace(' ', '%20')
    tag = m.group(0)
    if LOC_RX.search(tag):
        new = LOC_RX.sub(b'xsi:noNamespaceSchemaLocation="' + rel.encode('latin1') + b'"', tag, count=1)
    else:
        add = (b'' if b'xmlns:xsi' in tag else b' ' + XSI) + b' xsi:noNamespaceSchemaLocation="' + \
            rel.encode('latin1') + b'"'
        new = tag[:-1].rstrip(b'/') + add + (b'/>' if tag.endswith(b'/>') else b'>')
    if new == tag:
        return False
    if write:
        with open(path, 'wb') as f:
            f.write(data[:m.start()] + new + data[m.end():])
    return rel


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('check', 'fix'))
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--edition', choices=('ap', 'fl'))
    ap.add_argument('--schema')
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args(argv)
    bad = 0
    for p in md_files(a.paths):
        is_md, ref, res = schema_ref(p)
        if not is_md:
            continue
        if a.cmd == 'check':
            if ref is None:
                print('%s: no xsi:noNamespaceSchemaLocation' % p)
                bad += 1
            elif not os.path.isfile(res):
                print('%s: %s does not resolve (%s)' % (p, ref, res))
                bad += 1
            continue
        schema = a.schema or default_schema(p, a.edition)
        if not schema:
            print('%s: no director.xsd found (pass --schema or --edition)' % p)
            bad += 1
            continue
        if ref is not None and res and os.path.isfile(res) and not a.schema:
            continue
        r = fix(p, schema, a.write)
        if r:
            print('%s: %s xsi:noNamespaceSchemaLocation="%s"' % (p, 'set' if a.write else 'would set', r))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
