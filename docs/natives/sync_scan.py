"""Which natives always answer synchronously: flow analysis of the group dispatchers in X3AP.exe.

    python sync_scan.py > sync.json          (needs the Ghidra project x3ap through rt)

A native suspends the calling task unless its handler calls Script::ResumeTask* before the dispatcher returns
(Script::Interpret then pops the arguments and goes on; otherwise the task waits in state 2 and its pc is saved in a
savegame). For every case of a dispatcher's jump table the instructions are walked from the case entry, following
jumps inside the dispatcher, up to every RET; the native is "sync" when every path to a RET passes a call to a
Script::ResumeTask* function (or Script::ResumeTask). A path that leaves the dispatcher by a jump, an indirect jump or a
return without a resume makes the native "may wait". Calls to other functions are assumed not to resume (a helper
that answers for the handler makes the result conservative, never wrong).
"""
import json
import re
import subprocess
import sys

RT = ['D:/GAMES/RETools/.venv/Scripts/python.exe', 'D:/GAMES/RETools/rt.py', '-p', 'x3ap']
GROUPS = {                      # group: (dispatcher, name table)
    'B3D': (0x00493b40, 0x0057a420),
    'INS': (0x0042d340, 0x0057aef0),
    'SA': (0x00460630, 0x0057a7f0),
    'X2': (0x00406de0, 0x0057c8f0),
    'SE': (0x004ab880, 0x00579fe0),
}
LINE = re.compile(r'\s*(0x[0-9a-f]{8})\s+[0-9a-f]+\s+(\S+)\s*([^;]*?)\s*(?:;\s*(.*))?$')


def rt(*a):
    return subprocess.run(RT + list(a), capture_output=True, text=True, cwd='D:/GAMES/RETools').stdout


def listing(entry, size):
    out = rt('dis', hex(entry), '-n', str(size))
    insns = {}
    order = []
    for l in out.splitlines():
        m = LINE.match(l)
        if m:
            a = int(m.group(1), 16)
            insns[a] = (m.group(2).upper(), m.group(3), m.group(4) or '')
            order.append(a)
    nxt = {a: b for a, b in zip(order, order[1:])}
    return insns, nxt


def cases(entry, table):
    out = rt('cases', hex(entry), '--names', hex(table))
    res = {}
    for l in out.splitlines():
        m = re.match(r'\s*(\d+)\s+(0x[0-9a-f]+)\s+(\S+)', l)
        if m and m.group(3).startswith(('B3D_', 'INS_', 'SA_', 'X2_', 'SE_', 'STA_')):
            res[int(m.group(1))] = (int(m.group(2), 16), m.group(3))
    return res


def target(ops):
    m = re.match(r'(0x[0-9a-f]+)$', ops.strip())
    return int(m.group(1), 16) if m else None


def is_sync(start, insns, nxt):
    """True when every path from start to a RET calls a ResumeTask* first."""
    seen = set()
    stack = [(start, False)]
    while stack:
        a, resumed = stack.pop()
        if (a, resumed) in seen:
            continue
        seen.add((a, resumed))
        if a not in insns:
            return False                          # left the dispatcher
        mn, ops, cm = insns[a]
        if mn == 'CALL':
            if 'ResumeTask' in cm:
                resumed = True
            stack.append((nxt.get(a), resumed))
        elif mn == 'RET':
            if not resumed:
                return False
        elif mn == 'JMP':
            t = target(ops)
            if t is None:
                return False                      # indirect jump (another switch): not followed
            stack.append((t, resumed))
        elif mn.startswith('J'):
            t = target(ops)
            if t is None:
                return False
            stack.append((t, resumed))
            stack.append((nxt.get(a), resumed))
        else:
            stack.append((nxt.get(a), resumed))
    return True


def main():
    result = {}
    for g, (entry, table) in GROUPS.items():
        insns, nxt = listing(entry, 12000)
        cs = cases(entry, table)
        for k, (addr, name) in sorted(cs.items()):
            result[name] = 'sync' if is_sync(addr, insns, nxt) else 'may wait'
        n = sum(1 for k, (a, nm) in cs.items() if result[nm] == 'sync')
        print('%s: %d cases, %d sync' % (g, len(cs), n), file=sys.stderr)
    json.dump(dict(sorted(result.items())), sys.stdout, indent=1)
    print()


if __name__ == '__main__':
    main()
