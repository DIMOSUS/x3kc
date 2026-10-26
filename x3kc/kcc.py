"""Compile a KC source project (<name>.kc + .pool + .ptch) into an object file.

    x3kc compile <file.kc> -o <out.obj> [--check <original.obj>]

--check compares the result with an object byte for byte and names the first differing methods.
"""
import argparse
import re
import os
import struct
import sys

from . import kcast as A
from . import kcgen
from . import kcobj
from . import kcparse
from .kcsym import NO_PARENT


class CompileError(Exception):
    pass


def read_pool(path):
    text = open(path, encoding='latin1').read()
    data = bytearray()
    try:
        for t in kcparse.lex(text):
            if t.kind == 'eof':
                break
            if t.kind != 'str':
                raise CompileError('%s:%d: string expected' % (path, t.line))
            data += kcparse.unescape(t.text).encode('latin1') + b'\0'
    except kcparse.ParseError as x:
        raise CompileError('%s: %s' % (path, x))
    lines = text.splitlines()
    if data and lines and re.fullmatch(r'\s*//\s*no-terminator\s*', lines[-1]):
        data = data[:-1]
    return bytes(data)


def read_ptch(path):
    out = []
    cur = None
    for n, line in enumerate(open(path, encoding='latin1'), 1):
        line = line.split('//')[0].strip()
        if not line:
            continue
        w = line.split()
        try:
            if w[0] == 'build' and len(w) in (2, 3):        # build <id> [<number of pairs>]
                cur = (int(w[1], 0), [])
                out.append(cur)
            elif cur is not None and len(w) == 2:
                cur[1].append((int(w[0], 16), int(w[1], 16)))
            else:
                raise ValueError
        except ValueError:
            raise CompileError('%s:%d: expected "build <id>" or a pair "<old> <new>" after it' % (path, n))
    return out


def known_natives():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'natives.txt')
    if not os.path.exists(p):
        return None
    return {l.strip() for l in open(p) if l.strip() and not l.startswith('#')}


def natives_of(node):
    """Names of the natives called anywhere in an AST (functions, statements, expressions)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (list, tuple)):
            stack.extend(n)
        elif isinstance(n, A.Native):
            yield n.name
            stack.extend(n.args)
        elif isinstance(n, (A.Node, A.Function)):
            stack.extend(getattr(n, k) for k in n.__slots__ if k != 'asm')


def compile_source(path):
    with open(path, encoding='latin1') as f:
        src = f.read()
    base = os.path.dirname(os.path.abspath(path))
    P = kcparse.Parser(src, os.path.basename(path))
    u = P.unit()
    dirs = dict(u.directives)
    o = kcobj.KcObj()
    try:
        ver, build = dirs.get('object', '801 0').split()
        o.version, o.build = int(ver, 0), int(build, 0)
    except ValueError:
        raise CompileError('#object takes two numbers: version and build id (e.g. #object 801 0)')
    o.order = dirs.get('sections', 'CODE SYMB VARS CLAS STRG').split()
    default_stamp = int(dirs.get('stamp', '0'), 0)
    pool = kcgen.Pool(read_pool(os.path.join(base, dirs['pool'])) if 'pool' in dirs else b'\0')
    if 'patch' in dirs:
        o.ptch = read_ptch(os.path.join(base, dirs['patch']))
    S = u.syms

    # pass 2: bodies
    fns = []
    for f in u.funcs:
        bp = kcparse.BodyParser(P.toks, S, f, os.path.basename(path))
        f.ast = bp.function()
        fns.append(f)

    # a native the exe does not have resolves to group 0 at load and silently returns 0: warn
    known = known_natives()
    if known:
        for f in fns:
            for name in sorted(set(natives_of(f.ast))):
                if name not in known:
                    print('warning: %s (%s): native %s is unknown to X3AP.exe (it would return 0)'
                          % (os.path.basename(path), f.name, name))

    # layout in source order; addresses of calls are fixed up afterwards
    env = kcgen.Env(pool, {})
    code = bytearray(b'\0')
    gens = []
    for f in fns:
        g = kcgen.FuncGen(f.ast, len(code), env)
        try:
            g.function()
        except kcgen.GenError as x:
            raise CompileError('%s: %s' % (f.name, x))
        env.methods[(f.cls, f.name)] = len(code)
        f.entry = len(code)
        f.frame_size = g.frame
        code += g.out
        gens.append((f, g))
    for f, g in gens:
        try:
            b = g.resolve()
        except kcgen.GenError as x:
            raise CompileError('%s: %s' % (f.name, x))
        code[f.entry:f.entry + len(b)] = b
    o.code = bytes(code)

    def rows_of(k):
        out = []
        for m in k.methods:
            f = m.fn
            out.append([f.entry, pool.offset(m.name), f.frame_size, len(f.params)])
        return out

    def field_rows(k):
        out = []
        for j, fd in enumerate(k.fields):
            if fd.type == 2:
                v = pool.offset(fd.value) if isinstance(fd.value, str) else fd.value
            else:
                v = fd.value
            out.append([j, v, fd.type, default_stamp if fd.stamp is None else fd.stamp])
        return out

    o.symb = rows_of(u.globals)
    o.vars = field_rows(u.globals)
    for kind, k in u.items:
        if kind == 'class':
            o.classes.append(kcobj.KcClass(k.id, k.parent, default_stamp if k.stamp is None else k.stamp,
                                           rows_of(k), field_rows(k)))
    o.strings = bytes(pool.data)
    o.unit = u
    return o, pool, fns


def compare(o, ref, fns):
    a, b = o.serialize(), ref.serialize()
    if a == b:
        return True
    for tag in ref.order:
        if o.section(tag) != ref.section(tag):
            print('section %s differs (%d vs %d bytes)' % (tag, len(o.section(tag)), len(ref.section(tag))))
    if o.code != ref.code:
        n = 0
        for f in fns:
            end = f.entry + len(f.ast and b'' or b'')
        for k, f in enumerate(fns):
            end = fns[k + 1].entry if k + 1 < len(fns) else len(o.code)
            if o.code[f.entry:end] != ref.code[f.entry:end]:
                print('  first differing method: %s (class %d) at %06x' % (f.name, f.cls, f.entry))
                n += 1
                if n >= 5:
                    break
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('-o', '--out')
    ap.add_argument('--check')
    ap.add_argument('--base', help='object the existing saves were made with: makes the output load them '
                                   '(new build id, stamps of added fields, PTCH remap)')
    ap.add_argument('--build', type=lambda x: int(x, 0), help='build id with --base (default: now)')
    ap.add_argument('--force', action='store_true', help='write even when old saves would break (--base) or kcverify finds errors')
    a = ap.parse_args()
    try:
        o, pool, fns = compile_source(a.src)
    except (CompileError, kcparse.ParseError) as x:
        print('error:', x)
        return 1
    if a.base:
        from . import kcsave
        base = kcobj.load(a.base)
        if o.serialize() == base.serialize():
            print('identical to the base object: nothing to do')
        else:
            # saves exist for the base build and for every build it can already load: the new build must differ
            known = {base.build} | {b for b, _ in base.ptch}
            if a.build is not None:
                build = a.build
            elif o.build not in known and o.build:
                build = o.build
            else:
                build = kcsave.new_build_id(max(known))
            if build in known:
                print('error: build %d is the base build or already has a PTCH record in the base' % build)
                return 3
            u = o.unit

            def explicit(kind, cid, j):
                k = u.syms.classes.get(cid)
                if k is None:
                    return False
                return (k.stamp is not None) if kind == 'class' else (k.fields[j].stamp is not None)
            broken = False
            for line in kcsave.make_compatible(o, base, build, explicit):
                print(line)
                broken |= 'will not load' in line
            segs = kcsave.code_map(base, o)
            ok, gone, bad = kcsave.check_return_sites(base, o, segs)
            print('base return sites (calls a task can wait in): %d mapped, %d gone, %d WRONG; new build %d'
                  % (ok, gone, bad, build))
            if (broken or bad) and not a.force:
                print('error: old saves would break (see above); nothing written. --force writes anyway')
                return 3
    if pool.added:
        print('%d new strings appended to the pool' % len(pool.added))
    from . import kcverify
    V = kcverify.Verifier(o, kcverify.load_natives())
    V.run()
    for m, lines in sorted(V.warnings.items()):
        for l in lines:
            print('verify warning: %s %s' % (m, l))
    if V.errors:
        for m, lines in sorted(V.errors.items()):
            for l in lines:
                print('verify ERROR: %s %s' % (m, l))
        if not a.force:
            print('error: the bytecode would break tasks in the game; nothing written. --force writes anyway')
            return 4
    if a.out:
        open(a.out, 'wb').write(o.serialize())
        print('wrote %s (%d bytes, %d methods)' % (a.out, len(o.serialize()), len(fns)))
    if a.check:
        ref = kcobj.load(a.check)
        ok = compare(o, ref, fns)
        print('identical to %s' % a.check if ok else 'DIFFERENT from %s' % a.check)
        return 0 if ok else 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
