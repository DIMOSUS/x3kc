"""Run MSCI scripts in the emulator through the game's own interpreter (ScriptObject::__runScript in x3story).

    m = Msci(vm, db)                            # db = msci_db.json (x3kc msci-setup), for the assembler
    res = m.run_text(['$a = 5', '$b = $a * 3 + 1', 'return $b'])
    res.value, res.type, res.vars              # the returned value and type, the script's variables at the end

The text is assembled by msci_asm (the same assembler `x3kc msci asm` uses), the codearray goes to __runScript
exactly as the game passes it after loading the XML (nested arrays, strings as heap strings).
"""
from . import msci_asm
from .kcvm import KArray, KTable


def to_cells(v):
    """codearray (from msci.read_script or the assembler) -> script cells."""
    if isinstance(v, list):
        return KArray([to_cells(x) for x in v])
    if isinstance(v, str):
        return str(v)
    return v


class Result:
    def __init__(self, type_, value, task, vm):
        self.type, self.value, self.task, self.vm = type_, value, task, vm


class Msci:
    def __init__(self, vm, db=None):
        self.vm = vm
        self.db = db
        self.asm = msci_asm.Assembler(db) if db else None
        if not getattr(vm, 'msci_ready', False):
            # ScriptDatabase.Init: command tables, the flag array per command id, the expression buffer (100 cells);
            # script loading and the !init scripts find no files here and do nothing
            vm.call(700, 'Init')
            vm.msci_ready = True

    def codearray(self, lines, name='test.script', args=()):
        names, code, extra = self.asm.script(list(lines), list(args))
        return [name, 40, 'test', 0, 0, names or 0, code, [[0, a] for a in args] or 0, extra or 0, 0]

    def run(self, script, this=0, this_type=0, args=None, priority=0):
        """Start a script (codearray as a Python list or KArray) as RunThisScriptCall does; returns a Result once
        the script task returns (the virtual clock is advanced while it waits)."""
        vm = self.vm
        if isinstance(script, list):
            script = to_cells(script)
        task = KArray([0] * 13)
        it = task.items
        it[0], it[7], it[1], it[2], it[3] = 0, 0, priority, 0, 3
        it[4] = KArray([0, 0, 0])
        it[5] = KArray([0] * 36)
        it[6], it[8], it[12] = 0, args or 0, 1
        out = KArray([0, 0])
        t = vm.call(702, '__runScript', [task, script, this_type, this, args or 0, out], run=False)
        vm.execute(t)
        vm.run_ready()
        steps = 0
        while t.state == 'wait' and steps < 1000:
            vm.run(1000)
            steps += 1
        if t.error:
            raise t.error
        return Result(out.items[0], out.items[1], t, vm)

    def run_text(self, lines, **kw):
        return self.run(self.codearray(lines), **kw)
