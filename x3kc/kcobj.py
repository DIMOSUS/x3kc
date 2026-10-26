"""Lossless reader/writer of compiled KC objects (L/x3story.obj and friends, magic 'STOR').

Loader: Script::LoadObject 0x0049d030. All integers are big-endian.

    'STOR' u32 version (801) u32 build
    sections in any order, each: tag[4] u32 size payload[size]; then '/STO'
      CODE  bytecode
      CLAS  u32 n; n x { u32 id, u32 parent, u32 name, u32 nm, nm x Row, u32 nv, nv x Row }
      SYMB  u32 n; n x Row        methods of class 0 (global functions)
      VARS  u32 n; n x Row        fields of class 0 (global variables)
      STRG  string pool, obfuscated: plain[k] = ~enc[k] - enc[k-1]  (STRU: plain)
      PTCH  u32 n; n x { u32 a, u32 m, m x (u32, u32) }
Row = 4 x u32. Method row: code offset, name offset, locals, argc. Field row: init value, type, name offset, 4th word.
"""
import struct

ROW = struct.Struct('>4I')


def decode_strg(src):
    s = bytearray(len(src))
    if src:
        s[0] = ~src[0] & 255
    for i in range(1, len(src)):
        s[i] = (~src[i] - src[i - 1]) & 255
    return bytes(s)


def encode_strg(plain):
    e = bytearray(len(plain))
    for i, c in enumerate(plain):
        # plain = ~e - prev  ->  ~e = plain + prev  ->  e = ~(plain + prev)
        prev = e[i - 1] if i else 0
        e[i] = ~((c + prev) & 255) & 255
    return bytes(e)


class Reader:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def u32(self):
        v = struct.unpack_from('>I', self.b, self.p)[0]
        self.p += 4
        return v

    def row(self):
        v = ROW.unpack_from(self.b, self.p)
        self.p += 16
        return list(v)


class KcClass:
    def __init__(self, cid, parent, name, methods, fields):
        self.id, self.parent, self.name = cid, parent, name
        self.methods = methods      # [ [code, name, locals, argc] ]
        self.fields = fields        # [ [init, type, name, w3] ]


class KcObj:
    """Parsed object. `order` keeps section order; unknown sections are kept raw."""

    def __init__(self, raw=None):
        self.version, self.build = 801, 0
        self.order = []
        self.code = b''
        self.classes = []
        self.symb = []
        self.vars = []
        self.strings = b'\0'
        self.strg_plain = False      # True -> STRU
        self.ptch = []
        self.raw_sections = {}
        self.trailer = b''
        if raw is not None:
            self.parse(raw)

    # ---- parse ---------------------------------------------------------------------------------
    def parse(self, raw):
        assert raw[:4] == b'STOR', 'not a STOR object'
        self.version, self.build = struct.unpack_from('>II', raw, 4)
        p = 12
        while raw[p:p + 4] != b'/STO':
            tag = raw[p:p + 4].decode('latin1')
            n = struct.unpack_from('>I', raw, p + 4)[0]
            body = raw[p + 8:p + 8 + n]
            self.order.append(tag)
            getattr(self, '_parse_' + tag, lambda b, t=tag: self.raw_sections.__setitem__(t, b))(body)
            p += 8 + n
        self.trailer = raw[p + 4:]

    def _parse_CODE(self, b):
        self.code = bytes(b)

    def _parse_STRG(self, b):
        self.strings = decode_strg(b)

    def _parse_STRU(self, b):
        self.strings = bytes(b)
        self.strg_plain = True

    def _parse_SYMB(self, b):
        r = Reader(b)
        self.symb = [r.row() for _ in range(r.u32())]
        assert r.p == len(b)

    def _parse_VARS(self, b):
        r = Reader(b)
        self.vars = [r.row() for _ in range(r.u32())]
        assert r.p == len(b)

    def _parse_CLAS(self, b):
        r = Reader(b)
        for _ in range(r.u32()):
            cid, par, name = r.u32(), r.u32(), r.u32()
            ms = [r.row() for _ in range(r.u32())]
            fs = [r.row() for _ in range(r.u32())]
            self.classes.append(KcClass(cid, par, name, ms, fs))
        assert r.p == len(b)

    def _parse_PTCH(self, b):
        r = Reader(b)
        for _ in range(r.u32()):
            a, m = r.u32(), r.u32()
            self.ptch.append((a, [(r.u32(), r.u32()) for _ in range(m)]))
        assert r.p == len(b)

    # ---- write ---------------------------------------------------------------------------------
    def section(self, tag):
        u = lambda v: struct.pack('>I', v & 0xffffffff)
        rows = lambda rs: b''.join(ROW.pack(*[x & 0xffffffff for x in r]) for r in rs)
        if tag == 'CODE':
            return self.code
        if tag == 'STRG':
            return encode_strg(self.strings)
        if tag == 'STRU':
            return self.strings
        if tag == 'SYMB':
            return u(len(self.symb)) + rows(self.symb)
        if tag == 'VARS':
            return u(len(self.vars)) + rows(self.vars)
        if tag == 'CLAS':
            out = [u(len(self.classes))]
            for c in self.classes:
                out += [u(c.id), u(c.parent), u(c.name), u(len(c.methods)), rows(c.methods),
                        u(len(c.fields)), rows(c.fields)]
            return b''.join(out)
        if tag == 'PTCH':
            out = [u(len(self.ptch))]
            for a, ps in self.ptch:
                out += [u(a), u(len(ps))] + [u(x) + u(y) for x, y in ps]
            return b''.join(out)
        return self.raw_sections[tag]

    def serialize(self):
        out = [b'STOR', struct.pack('>II', self.version, self.build)]
        for tag in self.order:
            body = self.section(tag)
            out += [tag.encode('latin1'), struct.pack('>I', len(body)), body]
        out += [b'/STO', self.trailer]
        return b''.join(out)

    # ---- helpers -------------------------------------------------------------------------------
    def string(self, off):
        """The NUL-terminated string at pool offset off (None if off is not a string start)."""
        s = self.strings
        if not (0 <= off < len(s)) or (off > 0 and s[off - 1] != 0):
            return None
        e = s.find(b'\0', off)
        return s[off:e if e >= 0 else len(s)].decode('latin1')

    def string_table(self):
        """{offset: string} for every string in the pool."""
        out, p, s = {}, 0, self.strings
        while p < len(s):
            e = s.find(b'\0', p)
            if e < 0:
                e = len(s)
            out[p] = s[p:e].decode('latin1')
            p = e + 1
        return out


def load(path):
    return KcObj(open(path, 'rb').read())


if __name__ == '__main__':
    import sys
    raw = open(sys.argv[1], 'rb').read()
    o = KcObj(raw)
    print('version', o.version, 'build', o.build, 'sections', o.order, 'trailer', len(o.trailer))
    print('code', len(o.code), 'classes', len(o.classes), 'symb', len(o.symb), 'vars', len(o.vars),
          'strings', len(o.strings), 'ptch', len(o.ptch))
    print('roundtrip', o.serialize() == raw)
