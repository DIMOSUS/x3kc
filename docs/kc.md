# KC: the language, the object format and how Egosoft's compiler worked

The game logic of X3: Terran Conflict and Albion Prelude is written in Egosoft's internal language KC and
compiled into `STOR` object files: `L/x3story.obj`, `L/x3intro.obj`, `L/x3galedit.obj`, `L/x3config.obj`.
Everything here was derived from the bytecode and checked by the full round trip: object → source → object gives
the same file byte for byte for every object of every TC/AP version.

## Source language
C-like syntax, dynamic values: int, double, string, array, table, object.

**A source file** (`x3story.kc`) is a sequence of declarations in code order:
- directives: `#object <version> <build>`, `#sections …` (section order), `#pool`, `#patch`, `#stamp` (default
  build stamp of fields and classes);
- `var gN = …;` — global variables (fields of class 0, section VARS);
- `function F(arg1, …) { … }` — global functions (section SYMB);
- `class C150 : C100 { var m5 = 0; … function Init(arg1) { … } }` — classes (CLAS). A field's number is its
  position: own fields are numbered after the parents' fields. The default name `mN` is the absolute field index.
  Any class can be named: `class Client = 150 : C100`.
- Field initialisers: a number, `"string"`, `{}` (array), `{:}` (table), nothing (empty), or
  `__init(type, value)`. For example `__init(2, 0)` is the string at pool offset 0, which the VM reads as
  `"SB_String(0)"`, not as `""`.
- `@0x…` after a class or field — the build stamp of the build that added it (see «Mods and savegames»).

**Variables.** Parameters are `arg1…`, locals `v1…`, fields `mN`, globals `gN` (until annotations name them).
A local lives until the end of its `{ }` block; every `case` section is a block.

**Expressions** (C precedence): `= += -= …`, `?:`, `||`, `&&`, `|`, `^`, `&`, `== !=`, `< > <= >=`,
`<< >>`, `+ -`, `* / %`, unary `! - ~ new delete`, `++`/`--`, `a[i]`, calls.
- `&&` and `||` always give 0 or 1.
- Division by 0 gives 0.
- `+` with a string concatenates.
- Literals: `{a, b}` is an array (element 0 first), `{k: v, …}` a table. Integers are 32-bit:
  `0xFFFFFFFF` is -1, larger numbers are an error.
- The value of an assignment is the new value.

**Calls** (the form selects the opcode):

| Form | Opcode | Meaning |
|---|---|---|
| `F(a)` | `calls` / `callg` / `native` | a method of the own class or its ancestors (static); else a global function; else a native |
| `::F(a)` | `callg` | the global function explicitly |
| `C1000::F(a)` | `calls` | a method of the given class on the current object (like `Base::f()` in C++) |
| `this->F(a)` | `calln` | virtual call by name on the current object |
| `x.F(a)` | `callo` | call by name on object `x` (id or reference), resolved at run time |
| `C150.F(a)` | `callc` | static call of a class-150 method on the class object 150 ("singletons") |
| `x.C150::F(a)` | `callc` | static call of C150::F on object `x` |
| `new C661(a)` | `new` + `callo Create` | create an object and call `Create(a)` |
| `native::SE_X(a)` | `native` | a native whose name is hidden by a method |

Calls by name (`callo`, `calln`) pad missing arguments with zeros.

**Statements:** `var`, `if/else`, `while`, `for (init; cond; step)`, `do … while`, `switch`, `return [e]`,
`break`, `continue`, `fork { … }`, blocks.
- `fork { … }` runs the body in a child task with a copy of the frame; the parent continues. `break` and
  `continue` cannot leave a fork body.
- `switch` takes int and string cases. A dense table (opcode 0x78) is chosen when the value range is at most
  20 × the number of cases, else 0x79. `: dense`, `: sparse` and `: dense(lo, hi)` select the table explicitly.
  Duplicate cases are an error.

**Fallback form** — `function F(arg1) frame N asm { … }` with labels `L0012:` and the mnemonics of `kcisa.py`.
`x3kc decompile --asm-all` writes every method this way. It was never needed for the stock objects.

## How Egosoft's compiler generated code
- **Frame at entry**, bottom up: `argN … arg1, argc, return address, context`.
  `ldl n` / `stl n` count from the stack top (n = 1 is the top), so a local's number depends on the stack depth.
- **A local** is a value left on the stack: `var x = e;` compiles to `e`, `var x;` to `push0`. Block end: `pop`
  or `popn n`.
- **Call:** arguments pushed last first, then `argc` as a constant, then (for `callo`/`callc`) the object.
  Array elements and table pairs are pushed last first too; in a pair the value comes before the key.
- **Logic and branches:**
  - `a && b`: `a jf F; b jf F; push1; jmp E; F: push0; E:`; `||` the same with `jt` and swapped constants.
  - `c ? a : b`: `c jf F; a; jmp E; F: b; E:`.
  - `if`: `c jf L1; then; [jmp L2; L1: else]; L2:`.
- **Loops:**
  - `while`: `S: c jf X; body; jmp S; X:`;
  - `for`: `init; S: c jf X; jmp B; I: step; jmp S; B: body; jmp I; X:`;
  - `do`: `S: body; c; jt S`.
- **`switch`:** `e; jmp D; sections; jmp End; D: table; End:` — the table follows the bodies.
- **`fork`:** `fork; jt L; body; exit; L:`.
- **Returns:** `return e`: `e; stl k+1; pop/popn k; ret` (k = live locals); `return;`: `pop/popn k; push0; ret`;
  a function: `frame N; body; drop locals; push0; ret`. Dead code after `return` was kept.
- **`frame N`** = the maximal stack depth above the frame; the epilogue `push0; ret` and the `push0` of
  `return;` do not count; `calls`/`calln` count one extra cell for a moment, `callg`, `callo`, `callc` and natives
  do not. This rule holds for every method of every object; the "locals" field of a method row equals it.
- **Constants** always in the shortest form: `push0..3`, `pushb`, `pushw`, `pushd`; no `neg` of literals.

## Object format (`STOR`)
- Header `STOR`, version, build; sections `CODE`, `SYMB`, `VARS`, `CLAS`, `STRG` (or `STRU`), optionally `PTCH`;
  trailer `/STO`.
- **Method row (16 bytes):** code offset, name (pool offset), frame, parameter count.
- **Field row:** index (position in its class), value, type, build stamp. Types: 0 empty, 1 int, 2 string (value =
  pool offset), 8 array, 9 table.
- **Class record:** id, parent, build stamp, methods, fields. The object holds **no names** of classes, fields
  or locals at all — only method names.
- **Order:** the methods of a class lie in the code one after another, in row order; the class order in CLAS
  follows the code; global functions come in pieces between classes (source file boundaries).
- **STRG** — the string pool, append-only: savegames store string offsets, so it keeps strings of old versions
  that no code uses any more. The compiler takes the pool from `.pool` and appends new strings.
  `""` in code is the second empty string of the pool, never offset 0.
- **PTCH** — address maps for savegames of older builds: records of build, number of pairs, pairs `(old, new)`.

## Mods and savegames (`x3kc compile --base`)
How the game uses it (`Script::LoadVmState`, X3AP.exe 0x004a0880):
- a savegame stores the build of the object;
- when it differs, the PTCH record of the savegame's build is used; without one **the savegame does not load**;
- a record is a piecewise shift: for a pc the last pair with `old ≤ pc` applies and pc becomes `pc + (new − old)`;
  `new = 0` means "gone"; pcs below the first pair stay;
- this remaps the pc of every task and every return address on its stack;
- a field whose stamp is newer than the savegame's build is not read from it and keeps its initial value — **with
  one exception** (a bug of the game): the fields of an object are read level by level from the root class down,
  and the walk stops after the last level that still had saved fields. New fields on the levels below stay null.
  Example: `Ship : Thing` had no fields of its own in the base, the mod adds `Ship.s0 = 7`: `Ship` objects from
  old savegames get `s0 = null`. Static fields are not affected;
- an object of a class the new object lacks crashes the load; static fields of such a class make the game refuse
  the savegame;
- after the remap a task pc must follow a native call and every return address a call, or the load fails.

What `compile --base base.obj` does:
1. Gives the object a new build: the current time, but above every build the base knows. `--build` or `#object`
   can set it; the base build and builds the base already has PTCH records for are refused.
2. Stamps added fields, globals and classes with the new build, unless the source gives a stamp.
3. Maps base addresses to new ones: methods by name, instructions inside a method aligned with `difflib`. A saved
   pc is always the address after an instruction, so it moves with the instruction before it; when that one
   changed, the address is marked gone.
4. Re-targets every PTCH record **of the base** through this map and adds a record for the base build, so a mod
   can be rebuilt on top of its previous version (`--base mod_v1.obj`) and both stock and v1 savegames load.
5. Checks that every return address of the base lands after the same call (same native, same method) inside a
   method with the same frame size: an added local would shift every local of a suspended call. Only calls a task
   can be suspended in are return addresses a savegame can hold: natives that may wait and methods that reach one,
   transitively (a call by name counts if any method of that name can). Which natives always answer at once is in
   `x3kc/sync_natives.txt`: flow analysis of the native dispatchers in X3AP.exe for the groups B3D, INS, SA, X2
   and SE (every path of a handler resumes the task before the dispatcher returns), plus the SE/TI handler specs.
   Natives of the other groups count as possibly waiting. So a frame change in a method that only draws or
   computes (B3D, SE, most SA calls) does not stop the build; one in a method that waits (TI_Delay, flight
   commands, MSCI commands) does.
6. When something breaks savegames — lost fields, a removed class, a changed parent, wrong return addresses —
   nothing is written and the exit code is 3; `--force` writes anyway.

Rules for a mod: add fields **only at the end** of a class; never remove or reorder fields; changing an initial
value does not reach old savegames (the compiler warns); do not add fields to a class that had none of its own in
the base (the compiler warns with `B19`): put them into an ancestor that already has fields, or set them in code;
never remove or renumber a class whose objects old savegames may hold.

The emulator repeats the load: `vm.migrate(new_object)` moves objects level by level the way the game does,
B19 included, checks task pcs and return addresses, and raises `VMError` where the game would refuse or crash.

A rebuilt x3story in `addon/05.cat` loads and runs in the game. **Not verified in the game:** loading an old
savegame with it. The signature in `addon/x3files.xml` (a signed `<data>` block with the hash of x3story.obj) will
not match a changed file, so the game marks itself "modified".

## Annotations
The object has no names except method names, so `x3kc decompile` prints `C150`, `m12`, `v3`, `arg1`. Names and
comments live outside the source, in JSON, and are applied on every decompilation:

```
x3kc decompile x3story.obj src --names auto           the bundled names (x3kc/data)
x3kc decompile x3story.obj src --names mine.json      your own
```

Names never change the bytes: a named source compiles to the same object. Names that would not resolve back
(keywords, duplicates, shadowing) get a suffix, and every such fix is printed as a `names:` line.

**Two forms:**
- *Working form* — comments keyed by code offset, as `x3kc decompile --pcs` prints them (`// @0012fb`). This is
  what you write while annotating; it fits one build only.
  ```
  {"classes": {"150": {"name": "Client", "doc": "…",
                       "fields": {"3": {"name": "fastForward", "doc": "…"}},
                       "methods": {"Init": {"doc": "…", "params": ["sectorName"], "locals": {"v1": "ship"},
                                            "comments": {"0x012fb3": "…"}}}}},
   "globals": {"fields": {"33": {"name": "gPatchTable"}}, "methods": {…}}}
  ```
  Field keys are the class's own field index (0 = its first own field).
- *Portable form* (`x3kc names pack <obj> <working.json> <out.json>`) — adds the field layout of each class, a
  hash of each method body and a window of instruction hashes around each comment. `decompile` resolves it for
  any build: field names apply while the layout matches, params need the same count, locals the same body,
  comments are re-found by their window. `x3kc names unpack` gives the working form for a build back.

The bundled names were made for AP 3.3 (build 1510658513) by LLM agents from the code, the game texts and
the dobj names of X3_Customizer. They have not been proof-read by a human; treat documentation that says
"probably" as a guess. Applied to AP 1.0 they keep 1 698 field names and 6 583 of 6 667 comments.
