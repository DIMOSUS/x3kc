"""Symbols shared by the printer and the compiler: classes, fields, methods, globals and their names."""

NO_PARENT = 0xffffffff
FIELD_TYPES = {0: None, 1: 'int', 2: 'string', 8: 'array', 9: 'table'}


class Field:
    __slots__ = ('name', 'type', 'value', 'stamp')

    def __init__(self, name, type_, value, stamp):
        self.name, self.type, self.value, self.stamp = name, type_, value, stamp


class Method:
    __slots__ = ('name', 'nparams', 'frame', 'fn', 'entry')

    def __init__(self, name, nparams, frame=None, fn=None, entry=None):
        self.name, self.nparams, self.frame, self.fn, self.entry = name, nparams, frame, fn, entry


class Klass:
    """Class 0 is the global context: its fields are the global variables (VARS), its methods SYMB."""

    def __init__(self, cid, name, parent, stamp):
        self.id, self.name, self.parent, self.stamp = cid, name, parent, stamp
        self.fields = []        # [Field], own fields in index order
        self.methods = []       # [Method], row order

    def method(self, name):
        for m in self.methods:
            if m.name == name:
                return m
        return None


class Symbols:
    def __init__(self):
        self.classes = {}       # id -> Klass (0 included)
        self.by_name = {}       # class name -> id

    def add(self, k):
        self.classes[k.id] = k
        if k.id != 0:
            self.by_name[k.name] = k.id

    def chain(self, cid):
        out = []
        seen = set()
        while cid in self.classes and cid not in seen and cid != 0:
            seen.add(cid)
            out.append(cid)
            p = self.classes[cid].parent
            if p == NO_PARENT:
                break
            cid = p
        return out

    def inherited(self, cid):
        """Number of fields of the parents (first own field index)."""
        return sum(len(self.classes[c].fields) for c in self.chain(cid)[1:])

    def member(self, cid, k):
        """(owner class, Field) for absolute member index k of an instance of cid, or None."""
        for c in self.chain(cid):
            base = self.inherited(c)
            fs = self.classes[c].fields
            if base <= k < base + len(fs):
                return c, fs[k - base]
        return None

    def member_by_name(self, cid, name):
        for c in self.chain(cid):
            base = self.inherited(c)
            for j, f in enumerate(self.classes[c].fields):
                if f.name == name:
                    return base + j
        return None

    def find_method(self, cid, name):
        """Owner class of the method `name` as seen from class cid (nearest in the chain), or None."""
        for c in self.chain(cid):
            if self.classes[c].method(name):
                return c
        return None

    def glob(self, k):
        g = self.classes.get(0)
        return g.fields[k] if g and 0 <= k < len(g.fields) else None

    def glob_by_name(self, name):
        g = self.classes.get(0)
        if g:
            for j, f in enumerate(g.fields):
                if f.name == name:
                    return j
        return None

    def global_function(self, name):
        g = self.classes.get(0)
        return g.method(name) if g else None

    def class_name(self, cid):
        k = self.classes.get(cid)
        return k.name if k else 'C%d' % cid
