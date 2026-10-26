"""Decompiler: KC bytecode of one method -> AST (kcast), the inverse of kcgen's emission rules.

The code is read linearly in emission order (dead code included), with a symbolic stack. A value left on the stack
becomes a local variable (`var x = e;`) only when something proves it: `ldl`/`stl` address it, a statement follows it,
or a block end pops it. Control flow is recognised from the jump layout of the one-pass compiler (see kcgen).
Anything that does not fit raises DecompileError; the caller then keeps the method as raw instructions.
"""
from . import kcast as A
from . import kcisa

BIN = {k for k in A.BINOPS}
UN = set(A.UNOPS)
PUSHC = {0x01: 0, 0x02: 1, 0x03: 2, 0x04: 3}


class DecompileError(Exception):
    pass


class Ent:
    __slots__ = ('e', 'var', 'anchor', 'pc0')

    def __init__(self, e=None, var=None, anchor=None, pc0=None):
        self.e, self.var, self.anchor, self.pc0 = e, var, anchor, pc0


class FuncDec:
    def __init__(self, code, entry, end, nparams, methods, pool_text, force=()):
        self.force = set(force)         # pops to read as jump/return cleanup (retry hints)
        self.ambig = []                 # pops read as block ends although a cleanup reading was possible
        self.bare = []                  # `push0 ret` without locals read as `return;` (or `return 0;`)
        self.code = code
        self.entry, self.end = entry, end
        self.ins = list(kcisa.walk(code, entry, end))
        self.at = {i.pc: i for i in self.ins}
        self.pos = {i.pc: k for k, i in enumerate(self.ins)}
        self.n = nparams
        self.methods = methods          # addr -> (cls, name)
        self.text = pool_text           # offset -> str
        self.stack = []
        self.ctx = []
        self.labels = {}                # switch case labels: pc -> [Case/Default]
        self.extra = {}                 # extra params / frame cells: key -> Var
        self.params = [A.Var('param', 'arg%d' % (i + 1), i + 1) for i in range(nparams)]
        for p in reversed(self.params):
            self.stack.append(Ent(var=p))
        for k in ('argc', 'retpc', 'ctx'):
            v = A.Var('frame', '__' + k, k)
            self.extra[k] = v
            self.stack.append(Ent(var=v))
        self.frame_len = len(self.stack)
        self.back = {}
        for i in self.ins:
            if 0x32 <= i.op <= 0x34 and i.arg <= i.pc:
                t = i.arg
                if True:
                    self.back.setdefault(t, []).append((i.pc, i.op))
        self.nvars = 0
        self.spc = {}                   # id(statement) -> code offset of its first instruction

    # ---- helpers -----------------------------------------------------------------------------
    def insn(self, pc):
        i = self.at.get(pc)
        if i is None:
            raise DecompileError('no instruction at %06x' % pc)
        return i

    def push(self, e, out, pc0, anchor=None):
        if anchor is None:
            anchor = (out, out[-1] if out else None)
        self.stack.append(Ent(e=e, anchor=anchor, pc0=pc0))

    def pop(self):
        if len(self.stack) <= self.frame_len:
            raise DecompileError('stack underflow')
        en = self.stack.pop()
        if en.e is None:
            raise DecompileError('expression expected, local %s found' % en.var.name)
        return en

    def promote(self, upto):
        """Entries 0..upto (absolute indexes) that are pending expressions become locals, bottom-up."""
        for k in range(upto + 1):
            en = self.stack[k]
            if en.e is None:
                continue
            self.nvars += 1
            v = A.Var('local', 'v%d' % self.nvars)
            d = A.VarDecl(v, en.e)
            v.decl = d
            lst, marker = en.anchor
            pos = 0
            if marker is not None:
                for j in range(len(lst) - 1, -1, -1):
                    if lst[j] is marker:
                        pos = j + 1
                        break
                else:
                    raise DecompileError('anchor lost')
            # never before the declaration of the local just below on the stack (same list)
            for below in reversed(self.stack[:k]):
                bd = below.var.decl if below.var is not None else None
                if bd is not None:
                    for j in range(len(lst) - 1, -1, -1):
                        if lst[j] is bd:
                            pos = max(pos, j + 1)
                            break
                    break
            lst.insert(pos, d)
            self.spc[id(d)] = en.pc0
            for other in self.stack[k + 1:]:
                if other.e is not None and other.anchor[0] is lst and other.anchor[1] is marker:
                    other.anchor = (lst, d)
            en.e, en.var, en.anchor = None, v, None

    def promote_all(self):
        self.promote(len(self.stack) - 1)

    def emit(self, out, s, pc=None):
        self.promote_all()
        out.append(s)
        if pc is not None:
            self.spc[id(s)] = pc

    def slot(self, n):
        """The stack entry addressed by ldl/stl n (Var), creating extra params below the frame."""
        k = len(self.stack) - n
        if k < 0:
            idx = self.n - k          # param number beyond the declared ones
            v = self.extra.get(idx)
            if v is None:
                v = self.extra[idx] = A.Var('param', 'arg%d' % idx, idx)
            return v
        en = self.stack[k]
        if en.e is not None:
            self.promote(k)
        return en.var

    def ctx_for(self, target):
        """('break'|'continue', ctx) when a jump to target leaves the innermost loop/switch."""
        for c in reversed(self.ctx):
            if c['brk'] == target:
                return 'break', c
            if c['kind'] != 'switch':
                if c['cont'] == target:
                    return 'continue', c
                if c['kind'] == 'do' and c['cont'] is None and c['lo'] < target < c['hi']:
                    c['cont'] = target
                    return 'continue', c
            if c['kind'] != 'switch':
                # a jump past an enclosing loop is not a plain break/continue
                pass
        return None, None

    # ---- body --------------------------------------------------------------------------------
    def function(self):
        if not self.ins or self.ins[0].op != 0x6e:
            raise DecompileError('no frame')
        last = self.ins[-2:]
        if len(last) < 2 or last[0].op != 0x01 or last[1].op != 0x83:
            raise DecompileError('no epilogue')
        body = []
        self.seq(self.ins[1].pc, last[0].pc, body)
        if len(self.stack) != self.frame_len:
            raise DecompileError('stack not balanced at end (%d)' % (len(self.stack) - self.frame_len))
        fn = A.Function(None, None, self.params, body, frame=self.ins[0].arg)
        fn.frame_vars = {k: v for k, v in self.extra.items()}
        fn.stmt_pc = self.spc
        return fn

    def seq(self, pc, stop, out):
        base = len(self.stack)
        while pc < stop:
            if self.back.get(pc):
                pc = self.loop(pc, stop, out)
                continue
            pc = self.step(pc, stop, out, base)
        if pc != stop:
            raise DecompileError('overran range at %06x (stop %06x)' % (pc, stop))

    def seq_expr(self, pc, stop):
        tmp = []
        d0 = len(self.stack)
        self.seq(pc, stop, tmp)
        if tmp or len(self.stack) != d0 + 1:
            raise DecompileError('expression expected in %06x..%06x' % (pc, stop))
        return self.pop()

    def close_block(self, k, out, pc_next, stop):
        """Block end popping k locals declared in this statement list."""
        top = len(self.stack)
        self.promote(top - 1)
        vs = [en.var for en in self.stack[top - k:]]
        del self.stack[top - k:]
        if pc_next == stop:
            return                       # natural end of this list's scope
        pos = None
        for v in vs:
            if v.decl is not None and v.decl in out:
                j = next(j for j, s in enumerate(out) if s is v.decl)
                pos = j if pos is None else min(pos, j)
        if pos is None:
            raise DecompileError('block end without declarations')
        out[pos:] = [A.Block(out[pos:])]

    def step(self, pc, stop, out, base):
        i = self.insn(pc)
        op = i.op
        nxt = i.end
        if op in PUSHC:
            self.push(A.Int(PUSHC[op]), out, pc)
        elif op in (0x05, 0x06, 0x07):
            self.push(A.Int(i.arg), out, pc)
        elif op == 0x0a:
            self.push(A.Dbl(i.arg), out, pc)
        elif op == 0x0b:
            self.push(A.Str(self.text(i.arg), i.arg), out, pc)
        elif op == 0x0d:
            self.push(A.Ref(self.slot(i.arg)), out, pc)
        elif op == 0x0e:
            self.push(A.Glob(i.arg), out, pc)
        elif op == 0x0f:
            self.push(A.Mem(i.arg), out, pc)
        elif op == 0x2e:
            self.push(A.This(), out, pc)
        elif op == 0x14:
            r = self.try_return(i, out)
            if r is not None:
                return r
            v = self.pop()
            t = self.slot(i.arg - 1)
            self.push(A.Assign(A.Ref(t), v.e), out, v.pc0, v.anchor)
        elif op in (0x15, 0x16):
            v = self.pop()
            self.push(A.Assign((A.Glob if op == 0x15 else A.Mem)(i.arg), v.e), out, v.pc0, v.anchor)
        elif op == 0x10:
            c = self.pop()
            ix = self.pop()
            self.push(A.Index(c.e, ix.e), out, ix.pc0, ix.anchor)
        elif op == 0x17:
            c = self.pop()
            k = self.pop()
            v = self.pop()
            self.push(A.SetIdx(c.e, k.e, v.e), out, v.pc0, v.anchor)
        elif op in BIN:
            b = self.pop()
            a = self.pop()
            self.push(A.Bin(A.BINOPS[op], a.e, b.e), out, a.pc0, a.anchor)
        elif op in UN:
            a = self.pop()
            self.push(A.Un(A.UNOPS[op], a.e), out, a.pc0, a.anchor)
        elif op == 0x28:
            items = [self.pop() for _ in range(i.arg)]
            first = items[-1] if items else None
            self.push(A.MkArr([x.e for x in items]), out, first.pc0 if first else pc, first.anchor if first else None)
        elif op == 0x29:
            pairs = []
            for _ in range(i.arg):
                k = self.pop()
                v = self.pop()
                pairs.append((k, v))
            first = pairs[-1][1] if pairs else None
            self.push(A.MkTab([(k.e, v.e) for k, v in pairs]), out, first.pc0 if first else pc,
                      first.anchor if first else None)
        elif op in (0x82, 0x84, 0x85, 0x86, 0x87, 0x88):
            obj = self.pop() if op in (0x85, 0x86) else None
            argc = self.pop()
            if type(argc.e) is not A.Int or argc.e.v < 0:
                raise DecompileError('argc is not a constant')
            args = [self.pop() for _ in range(argc.e.v)]
            deep = args[-1] if args else argc
            ea = [x.e for x in args]
            if op == 0x82:
                e = A.Native(self.text(i.arg), ea)
            elif op in (0x85, 0x87):
                e = A.Call('o' if op == 0x85 else 'n', self.text(i.arg), ea, obj.e if obj else None)
            else:
                tgt = self.methods.get(i.arg)
                if tgt is None:
                    raise DecompileError('call to non-entry %06x' % i.arg)
                e = A.Call({0x84: 'g', 0x86: 'c', 0x88: 's'}[op], tgt, ea, obj.e if obj else None)
            self.push(e, out, deep.pc0, deep.anchor)
        elif op == 0x24 or op == 0x23:
            k = 1 if op == 0x24 else i.arg
            j = self.at.get(nxt)
            pending_top = op == 0x24 and self.stack and self.stack[-1].e is not None

            def cleanup(after):
                # pops before a jump/return clean up locals (stack unchanged) when they reach below this block,
                # or when the same block end follows as dead code; otherwise they end a block first
                if len(self.stack) - k < base or pc in self.force:
                    return True
                d = self.at.get(after)
                if d is not None and d.op == op and (op == 0x24 or d.arg == k) and d.end == stop:
                    return True
                self.ambig.append(pc)
                return False
            if (not pending_top and j is not None and j.op == 0x01 and k == len(self.stack) - self.frame_len
                    and self.code[j.end] == 0x83 and j.end + 1 <= stop and cleanup(j.end + 1)):
                self.emit(out, A.Return(None), pc)
                return j.end + 1
            if j is not None and j.op == 0x32 and nxt < stop and not pending_top:
                kind, c = self.ctx_for(j.arg)
                if kind and len(self.stack) - c['depth'] == k and cleanup(j.end):
                    self.emit(out, A.Break() if kind == 'break' else A.Continue(), pc)
                    return j.end
            if op == 0x24 and self.stack and self.stack[-1].e is not None:
                en = self.pop()
                self.emit(out, A.ExprStmt(en.e), en.pc0)
            else:
                if len(self.stack) - k < base:
                    raise DecompileError('pop below block base at %06x' % pc)
                self.close_block(k, out, nxt, stop)
        elif op == 0x83:
            if len(self.stack) != self.frame_len + 1:
                raise DecompileError('ret with locals at %06x' % pc)
            e = self.pop()
            void = type(e.e) is A.Int and e.e.v == 0 and e.pc0 == pc - 1
            r = A.Return(None if void else e.e)
            if void:
                self.bare.append(r)
            self.emit(out, r, e.pc0)
        elif op == 0x34:
            return self.jf(i, stop, out)
        elif op == 0x33:
            return self.jt(i, stop, out)
        elif op == 0x32:
            return self.jmp(i, stop, out)
        elif op == 0x6f:
            j = self.at.get(nxt)
            if j is None or j.op != 0x33 or j.arg <= j.pc:
                raise DecompileError('fork without jt')
            L = j.arg
            ex = self.at.get(L - 1)
            if ex is None or ex.op != 0x70:
                raise DecompileError('fork body without exit')
            self.promote_all()
            body = []
            self.seq(j.end, L - 1, body)
            out.append(A.Fork(body))
            self.spc[id(out[-1])] = pc
            return L
        else:
            raise DecompileError('unexpected %s at %06x' % (kcisa.MN.get(op, hex(op)), pc))
        return nxt

    def try_return(self, i, out):
        nloc = len(self.stack) - 1 - self.frame_len
        if nloc < 1 or i.arg != nloc + 1:
            return None
        j = self.at.get(i.end)
        if j is None:
            return None
        if not ((nloc == 1 and j.op == 0x24) or (nloc > 1 and j.op == 0x23 and j.arg == nloc)):
            return None
        r = self.at.get(j.end)
        if r is None or r.op != 0x83:
            return None
        e = self.pop()
        self.emit(out, A.Return(e.e), e.pc0)
        return r.end

    # ---- control flow ------------------------------------------------------------------------
    def chain(self, i, out, jop):
        """a && b && ... (jop jf) / a || b || ... (jop jt) ending in push1/push0 jmp E; F: push0/push1; E:"""
        X = i.arg
        first = self.pop()
        items = [first.e]
        p = i.end
        while p < X - 6:
            q = p
            while True:
                j = self.insn(q)
                if j.op == jop and j.arg == X:
                    break
                q = j.end
                if q >= X - 6:
                    raise DecompileError('broken logic chain at %06x' % i.pc)
            items.append(self.seq_expr(p, q).e)
            p = q + 5
        if p != X - 6:
            raise DecompileError('logic chain misaligned at %06x' % i.pc)
        if jop == 0x34:
            self.push(A.Logic('&&', items) if len(items) > 1 else A.Cond(items[0], A.Int(1), A.Int(0)), out,
                      first.pc0, first.anchor)
        else:
            self.push(A.Logic('||', items), out, first.pc0, first.anchor)
        return X + 1

    def is_chain_end(self, X, a, b):
        c = self.code
        if X - 6 < self.entry or X + 1 > self.end:
            return False
        j = self.at.get(X - 5)
        return (c[X] == b and j is not None and j.op == 0x32 and j.arg == X + 1 and c[X - 6] == a
                and (X - 6) in self.at)

    def jf(self, i, stop, out):
        X = i.arg
        if X <= i.pc:
            raise DecompileError('backward jf')
        if self.is_chain_end(X, 0x02, 0x01):
            return self.chain(i, out, 0x34)
        c = self.pop()
        j = self.at.get(X - 5)
        inner_to_x = any(x.op in (0x32, 0x33, 0x34) and x.arg == X for x in self.ins_range(i.end, X - 5))
        if (j is not None and j.op == 0x32 and j.arg >= X and j.arg <= stop and self.ctx_for_peek(j.arg) is None
                and not inner_to_x):
            E = j.arg
            d0 = len(self.stack)
            then = []
            self.seq(i.end, X - 5, then)
            if len(self.stack) == d0 + 1:
                if then:
                    raise DecompileError('statements in a conditional expression')
                a = self.pop()
                b = self.seq_expr(X, E)
                self.push(A.Cond(c.e, a.e, b.e), out, c.pc0, c.anchor)
                return E
            if len(self.stack) != d0:
                raise DecompileError('unbalanced then at %06x' % i.pc)
            els = []
            self.seq(X, E, els)
            if len(self.stack) != d0:
                raise DecompileError('unbalanced else at %06x' % i.pc)
            self.emit(out, A.If(c.e, then, els), c.pc0)
            return E
        if X > stop:
            raise DecompileError('if jumps out of range at %06x' % i.pc)
        d0 = len(self.stack)
        then = []
        self.promote_all()
        self.seq(i.end, X, then)
        if len(self.stack) != d0:
            raise DecompileError('unbalanced if at %06x' % i.pc)
        self.emit(out, A.If(c.e, then), c.pc0)
        return X

    def ins_range(self, a, b):
        k = self.pos.get(a, len(self.ins))
        while k < len(self.ins) and self.ins[k].pc < b:
            yield self.ins[k]
            k += 1

    def ctx_for_peek(self, target):
        for c in reversed(self.ctx):
            if c['brk'] == target or (c['kind'] != 'switch' and c['cont'] == target):
                return c
        return None

    def jt(self, i, stop, out):
        X = i.arg
        if X > i.pc and self.is_chain_end(X, 0x01, 0x02):
            return self.chain(i, out, 0x33)
        raise DecompileError('unexpected jt at %06x' % i.pc)

    def jmp(self, i, stop, out):
        T = i.arg
        kind, c = self.ctx_for(T)
        if kind:
            if len(self.stack) != c['depth']:
                raise DecompileError('%s with unexpected depth at %06x' % (kind, i.pc))
            self.emit(out, A.Break() if kind == 'break' else A.Continue(), i.pc)
            return i.end
        sw = self.at.get(T)
        if T > i.pc and sw is not None and sw.op in (0x78, 0x79):
            return self.switch(i, sw, out)
        raise DecompileError('goto %06x at %06x' % (T, i.pc))

    def switch(self, i, sw, out):
        e = self.pop()
        self.promote_all()
        D = sw.pc
        End = sw.end
        close = self.at.get(D - 5)
        if close is None or close.op != 0x32 or close.arg != End:
            raise DecompileError('switch without closing jmp')
        span = None
        if sw.op == 0x78:
            base, default, tg = sw.cases
            explicit = [(base + k, t) for k, t in enumerate(tg) if t != default]
            if not explicit or explicit[0][0] != base or explicit[-1][0] != base + len(tg) - 1:
                span = (base, base + len(tg) - 1)
            cases = [(A.Int(v), t) for v, t in explicit]
        else:
            default, ints, strs = sw.cases
            cases = [(A.Int(v), t) for v, t in ints] + [(A.Str(self.text(o), o), t) for o, t in strs]
        lab = {}
        for v, t in cases:
            if not (i.end <= t <= D - 5):
                raise DecompileError('case target outside the switch body')
            lab.setdefault(t, []).append(A.Case(v))
        if default != End:
            if not (i.end <= default <= D - 5):
                raise DecompileError('default outside the switch body')
            lab.setdefault(default, []).append(A.Default())
        bounds = sorted(set(lab) | {i.end, D - 5})
        self.ctx.append({'kind': 'switch', 'brk': End, 'cont': None, 'depth': len(self.stack)})
        body = []
        for a, b in zip(bounds, bounds[1:] + [None]):
            if b is None:
                if a in lab:
                    body.append((lab[a], []))
                break
            stmts = []
            self.seq(a, b, stmts)
            body.append((lab.get(a, []), stmts))
        self.ctx.pop()
        out.append(A.Switch(e.e, body, sw.op == 0x78, span))
        self.spc[id(out[-1])] = e.pc0
        return End

    def loop(self, S, stop, out):
        self.promote_all()
        closers = self.back[S]
        J, jop = max(closers)
        closers.remove((J, jop))
        if J >= stop:
            raise DecompileError('loop closer outside range at %06x' % S)
        if jop == 0x33:
            X = J + 5
            c = {'kind': 'do', 'brk': X, 'cont': None, 'depth': len(self.stack), 'lo': S, 'hi': J}
            self.ctx.append(c)
            d0 = len(self.stack)
            body = []
            self.seq(S, J, body)
            self.ctx.pop()
            if len(self.stack) != d0 + 1:
                raise DecompileError('do-while without condition at %06x' % S)
            cond = self.pop()
            if c['cont'] is not None and c['cont'] != cond.pc0:
                raise DecompileError('do-while continue target mismatch')
            out.append(A.DoWhile(body, cond.e))
            self.spc[id(out[-1])] = cond.pc0     # S is the first body statement's offset
            return X
        if jop != 0x32:
            raise DecompileError('loop closed by %s' % kcisa.MN[jop])
        self.back.pop(S, None)          # the rest are continues
        # find the jf that ends the condition
        q = S
        found = None
        while q < J:
            j = self.insn(q)
            if j.op == 0x34 and j.arg > J:
                T = j.arg
                if T == J + 5:
                    found = ('while', j)
                    break
                nj = self.at.get(j.end)
                cl = self.at.get(T - 5)
                if (nj is not None and nj.op == 0x32 and nj.arg == J + 5 and cl is not None and cl.op == 0x32
                        and cl.arg == nj.end):
                    found = ('for', j)
                    break
            q = j.end
        if found is None:
            raise DecompileError('loop without condition at %06x' % S)
        kind, j = found
        cond = self.seq_expr(S, j.pc)
        d0 = len(self.stack)
        if kind == 'while':
            X = j.arg
            self.ctx.append({'kind': 'loop', 'brk': X, 'cont': S, 'depth': d0})
            body = []
            self.seq(j.end, J, body)
            self.ctx.pop()
            if len(self.stack) != d0:
                raise DecompileError('unbalanced while body at %06x' % S)
            out.append(A.While(cond.e, body))
            self.spc[id(out[-1])] = cond.pc0     # S is the first body statement's offset
            return X
        X = j.arg
        Li = j.end + 5
        Lb = J + 5
        self.back.pop(Li, None)          # body closer and continues
        stepl = []
        self.seq(Li, J, stepl)
        if len(self.stack) != d0:
            raise DecompileError('unbalanced for step at %06x' % S)
        self.ctx.append({'kind': 'loop', 'brk': X, 'cont': Li, 'depth': d0})
        body = []
        self.seq(Lb, X - 5, body)
        self.ctx.pop()
        if len(self.stack) != d0:
            raise DecompileError('unbalanced for body at %06x' % S)
        out.append(A.For([], cond.e, stepl, body))
        self.spc[id(out[-1])] = S
        return X


def decompile(code, entry, end, nparams, methods, pool_text, tries=8):
    """FuncDec with retries: when a reading fails, pops that were read as block ends are re-read as
    cleanups before a jump/return (latest first)."""
    force = set()
    err = None
    for _ in range(tries):
        d = FuncDec(code, entry, end, nparams, methods, pool_text, force)
        try:
            fn = d.function()
            if d.bare:
                from . import kcgen
                if kcgen.frame_of(fn) != fn.frame:
                    for r in d.bare:            # `return 0;` counts its push in the frame, `return;` does not
                        r.e = A.Int(0)
            return fn
        except DecompileError as x:
            err = x
            cand = [p for p in reversed(d.ambig) if p not in force]
            if not cand:
                break
            force.add(cand[0])
    raise err
