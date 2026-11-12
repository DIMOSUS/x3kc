"""KC calls that read a text page entry: SE_ReadText / SE_ExistsText and every KC function that passes its own
parameters on to one of them (found on the tokens, transitively), with literal page and id arguments.

A wrapper is (page position or None, id position, language position or None) in the call's argument list.
SE_ReadText by argument count (Script::NativeDispatch, docs/natives/spec_0.jsonl): 1 = (id) with page 0,
2 = (page, id), 3 = (page, language, id).
"""
import bisect

NATIVES = ('SE_ReadText', 'SE_ExistsText')
NATIVE_FORMS = {1: (None, 0, None), 2: (0, 1, None), 3: (0, 2, 1)}
OPEN, CLOSE = '([{', ')]}'


def call_args(toks, i):
    """Token i is a name followed by '(' -> [(first, last) token index of each argument], or None."""
    if i + 1 >= len(toks) or toks[i + 1].text != '(':
        return None
    args, depth, start = [], 0, i + 2
    j = i + 1
    n = len(toks)
    while j < n:
        t = toks[j]
        if t.kind == 'op':
            if t.text in OPEN:
                depth += 1
            elif t.text in CLOSE:
                depth -= 1
                if depth == 0:
                    if j > start:
                        args.append((start, j - 1))
                    return args
            elif t.text == ',' and depth == 1:
                args.append((start, j - 1))
                start = j + 1
        elif t.kind == 'eof':
            return None
        j += 1
    return None


def literal(toks, a, b):
    if a == b and toks[a].kind == 'num' and not toks[a].text.lower().startswith('0x') and '.' not in toks[a].text:
        return int(toks[a].text)
    if a == b and toks[a].kind == 'num' and toks[a].text.lower().startswith('0x'):
        return int(toks[a].text, 16)
    if b == a + 1 and toks[a].text == '-' and toks[b].kind == 'num' and toks[b].text.isdigit():
        return -int(toks[b].text)
    return None


def form(wrappers, name, nargs):
    if name in NATIVES:
        return NATIVE_FORMS.get(nargs)
    return wrappers.get(name)


class Wrappers:
    """Per project: name -> form, recomputed when the symbols generation changes."""

    def __init__(self):
        self.gen = None
        self.map = {}

    def get(self, project):
        if self.gen == project.gen:
            return self.map
        found = {}
        bad = set()
        parts = [p for p in project.parts if p.unit is not None and p.toks]
        calls = {}              # part key -> {name: [token index]}
        for p in parts:
            d = {}
            toks = p.toks
            for i in range(len(toks) - 1):
                t = toks[i]
                if t.kind == 'id' and toks[i + 1].text == '(':
                    d.setdefault(t.text, []).append(i)
            calls[p.key] = d
        names = set(NATIVES)
        for _ in range(4):                      # wrappers of wrappers
            new = set()
            for p in parts:
                d = calls[p.key]
                funcs = sorted(p.unit.funcs, key=lambda f: f.start)
                starts = [f.start for f in funcs]
                for name in names:
                    for i in d.get(name, ()):
                        k = bisect.bisect_right(starts, i) - 1
                        if k < 0 or i >= funcs[k].end:
                            continue
                        f = funcs[k]
                        args = call_args(p.toks, i)
                        if args is None:
                            continue
                        fm = form(found, name, len(args))
                        if fm is None:
                            continue
                        pos = []
                        for k_slot, slot in enumerate(fm):
                            if slot is None or isinstance(slot, tuple):
                                pos.append(slot)
                                continue
                            if slot >= len(args):
                                pos = None
                                break
                            a, b = args[slot]
                            t = p.toks[a]
                            if a == b and t.kind == 'id' and t.text in f.params:
                                pos.append(f.params.index(t.text))
                                continue
                            v = literal(p.toks, a, b)
                            if k_slot != 1 and v is not None:      # a fixed page or language: GetSectorName(id)
                                pos.append(('c', v))
                                continue
                            pos = None
                            break
                        if pos is None or not isinstance(pos[1], int):
                            continue
                        cand = tuple(pos)
                        if f.name in NATIVES:
                            continue
                        old = found.get(f.name)
                        if old is not None and old != cand:
                            bad.add(f.name)
                        elif old is None:
                            found[f.name] = cand
                            new.add(f.name)
            if not new:
                break
            names = new
        for n in bad:
            found.pop(n, None)
        self.gen, self.map = project.gen, found
        return found


def text_ref(project, part, i, wrappers):
    """Token index i (a call name, or inside its argument list) -> (page, id, lang or None, name token index) for a
    call with literal arguments, else None."""
    toks = part.toks
    cands = []
    if toks[i].kind == 'id' and i + 1 < len(toks) and toks[i + 1].text == '(':
        cands.append(i)
    depth = 0
    j = i - 1
    while j >= 0 and i - j < 400:
        t = toks[j]
        if t.kind == 'op':
            if t.text in CLOSE:
                depth += 1
            elif t.text in OPEN:
                if depth == 0:
                    if t.text == '(' and j > 0 and toks[j - 1].kind == 'id':
                        cands.append(j - 1)
                    if t.text != '(':
                        pass
                    depth = 0
                else:
                    depth -= 1
            elif t.text in (';', '{', '}') and depth == 0:
                break
        j -= 1
    for c in cands:
        args = call_args(toks, c)
        if args is None:
            continue
        fm = form(wrappers, toks[c].text, len(args))
        if fm is None:
            continue
        vals = []
        for slot in fm:
            if slot is None:
                vals.append(None)
            elif isinstance(slot, tuple):
                vals.append(slot[1])
            elif slot >= len(args):
                vals = None
                break
            else:
                vals.append(literal(toks, *args[slot]))
        if vals is None or vals[1] is None or (fm[0] is not None and vals[0] is None):
            continue
        return (vals[0] if vals[0] is not None else 0, vals[1], vals[2], c)
    return None
