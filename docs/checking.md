# Checking a mod without the game

Trying every feature in the game takes hours, so x3kc checks whatever it can before the game is started: a static
verifier of the bytecode, and an emulator of the game's script VM that runs the object's own code.

## Static checks
- `x3kc verify obj [-v]` — the bytecode verifier. `x3kc compile` runs it after every build and writes nothing
  when it finds an error (exit code 4; `--force` writes anyway). On every path through every method it checks:
  - `frame N` matches the method row, the stack never grows past the N + 4 cells the VM reserves, the depth is
    the same on every path into an instruction and never goes below the frame, `ret` leaves exactly the value;
  - jumps and `switch` targets are instruction starts inside the method, there are no unknown opcodes, no path
    runs past the end of the method;
  - locals stay inside the frame, writes never hit the argc / return address / context cells;
  - fields and globals exist, direct calls go to method entries with the right number of arguments, the argc of
    every call is a known constant;
  - (warnings) names of calls by name and of natives exist; a missing one is not a crash — the VM answers 0.

  - per object: every class comes after its parent in CLAS, no class has parent 0, class ids are unique. The
    game's loader (`Script::LoadObject`) counts a class's inherited fields from the parent records read before
    it, while the static cells are filled along the whole chain later: a class above its parent in the source is
    a heap overflow when the game loads the object, so `compile` refuses it.

  Every object of every TC and AP version passes without errors.
- `x3kc methoddiff old.obj new.obj` — methods whose code differs (pure moves do not count).
- `x3kc savecheck src.kc base.obj` — which savegame return addresses a change breaks, and why (the call after
  the address differs, or the frame size of the method changed). Only calls a task can wait in count (see
  [kc.md](kc.md#mods-and-savegames-x3kc-compile---base)): a frame change in a method that never waits is fine.
- `x3kc bugscan obj [--names auto] -o out.jsonl` — a scan for typical bug patterns
  (a switch section falling through, a getter result thrown away, `!x & n`, an off-by-one size comparison, a
  copy-pasted getter …): candidates to review by hand, not findings.

## The emulator
`x3kc/kcvm.py` runs bytecode the way the game's `Script::Interpret` does:
- every opcode, frames, calls (calls by name pad missing arguments with 0; a missing object or method gives 0),
  `fork` (the child runs first), tasks waiting on a virtual clock, an instruction budget (running out raises
  `Hang` — in the game such a loop freezes the frame, since the scheduler has no time slice);
- cell rules as in the exe: an empty cell is true; a missing table key and an array read out of range give int 0;
  an array write out of range does nothing; `<` / `>` of non-numbers are false (so `<=` / `>=` are true); strings
  compare by content, containers by identity; `int + string` concatenates;
- script tables work like the exe's (4 buckets, doubling, the same rehash and iteration order), because which
  object a script finds first depends on it;
- `INT_MIN / -1` and `INT_MIN % -1` raise `VMError`: the game crashes there (integer division fault).

`x3kc/kcnatives.py` implements 117 SE_ / TI_ natives (strings, arrays, tables, maths, text pages, timers, the MSCI
expression evaluator) from their code in the exe; their behaviour is described in
[natives/spec_0.jsonl](natives/spec_0.jsonl), [spec_1](natives/spec_1.jsonl), [spec_2](natives/spec_2.jsonl),
traps included (`SE_ToInt` of an int is 0, `%s` of a non-string prints `<invalid>`, `SE_DivFix` overflow
crashes …). Where the exe would crash, the emulator raises `VMError`. Engine natives (SA_, X2_, INS_, sound, 3D)
answer 0 and are logged; a test supplies the ones it needs (`vm.natives[name] = fn`), and KC methods can be
replaced too (`vm.overrides[name] = fn`) to cut the world off.

```python
from x3kc import kcobj, kcvm, kcmsci
vm = kcvm.VM(kcobj.load('x3story.obj'))
vm.load_names('auto:x3story')            # field names for vm.get / vm.set
hq = vm.new(2046)                         # an instance of class 2046
vm.set(hq, 'recycleQueue', kcvm.KArray([]))
t = vm.call(hq, 'Recycle')                # runs until the task waits, returns or fails
vm.run(60000)                             # advance the virtual clock by 60 s
```

**MSCI.** `x3kc/kcmsci.py` runs an MSCI script through the game's own interpreter: text → the assembler of
`x3kc msci asm` → codearray → `ScriptObject::__runScript` of the object.

```python
m = kcmsci.Msci(vm, db)                   # db = msci_db.json from x3kc msci-setup
r = m.run_text(['$a = 5', '$b = $a * 3 + 1', 'return $b'])
r.type, r.value                           # 4, 16
```

**Savegames.** `vm.migrate(new_obj)` "saves" in the middle of a wait and loads into another build through its
PTCH record, as `Script::LoadVmState` does: the pc of every task and every return address on its stack move, and
the tasks go on running on the new code.

**Smoke run.** `x3kc smoke old.obj new.obj` calls **every** method of both builds with zero arguments and
compares what happens (returned, waiting, failed, hung). Differences outside the methods you changed are worth a
look.

**MSCI cost.** `x3kc msci cost x3story.obj msci_db.json [line ...]` measures what an MSCI line costs in KC
instructions (run through the game's interpreter on the emulator, the empty loop subtracted) and which natives it
calls; `x3kc msci usage msci_db.json scripts/` counts which commands the scripts use, also weighted by loop depth.
An expression line (`$c = $a + $b * 2`) costs about 40 instructions per token, a simple command 70-120.

## What the tests cover
`python -m unittest discover tests`; with `X3_GAME` set, also the tests on the game's objects.

| File | What |
|---|---|
| `test_vm.py` | the language on the emulator: arithmetic, strings, containers, loops, `switch`, objects, class objects, `fork`, timers, hangs |
| `test_fuzz_compiler.py` | ~900 random expressions and programs: `compile` + emulator against a reference evaluator in Python |
| `test_verify.py` | the verifier finds 8 kinds of damaged bytecode |
| `test_savesites.py` | only calls a task can wait in are savegame return sites (directly or through a callee) |
| `test_emulator_game.py` | every game object verifies; MSCI scripts through the game's interpreter; a mod adds an MSCI command (build with `--base`, verified, savegames map, the command runs, the stock object neither crashes nor hangs on it); a task saved mid-wait on the stock object finishes on the modded build |

## What the emulator does not know
The engine's world (flight, combat in 3D, collisions) — everything behind the SA_ / X2_ natives; frame timing (a
wait wakes on the next "frame" at once); the small random extra TI adds to waits (`vm.jitter`). Testing in the
game stays the last step, but logic errors, VM crashes, hangs and broken savegames are caught before it.
