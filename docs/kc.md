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

**A split project** is the same text cut into files (`x3kc split` / `join`, `decompile --split`, `x3kc/kcsplit.py`):
the index `<name>.kcs` lists the parts in source order (`//` comment lines allowed); a part starts at the line after
the previous declaration's last line, so the comments above a class go with it. `compile` concatenates the parts
in index order and parses the result exactly as one file, so the object is the same; line numbers in messages are
mapped back to the part. Each part also parses alone (`kcsplit.parse_part`: pass 1, parents unresolved).
A `.kc` file in the project folder that the index does not list, or an index line whose file is missing, is an
error. `x3kc add` writes a new class or global function part and its index line (`x3kc/kcadd.py`).

**Class ids.** An id is any positive number no other class of the object has; 0 is the globals, and the game's
object ids of instances are negative. Ids are what savegames store for objects: never renumber a class of the
base. `compile --base` accepts an added class anywhere in the source order (its methods' code moves the later
methods, which the PTCH map covers) and stamps it with the new build; the class must stand after its parent
(the loader counts inherited fields only for a parent earlier in CLAS; the verifier refuses the other order).
`x3kc add` picks the next id above every id of the project and at least 9500, clear of the stock ids (AP and FL
x3story use 150..9003, x3intro 50..3001).

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

What each native does — its call forms, result, whether the task waits, the differences of Farnham's Legacy and the
x3_src engine, original bugs — is in the natives reference [natives/README.md](natives/README.md) (one page per
group, generated from `docs/natives/spec_<GROUP>.jsonl`); `x3kc natives --full <name>` prints the same at the command
line.

**Statements:** `var`, `if/else`, `while`, `for (init; cond; step)`, `do … while`, `switch`, `return [e]`,
`break`, `continue`, `fork { … }`, blocks.
- `fork { … }` runs the body in a child task with a copy of the frame; the parent continues. `break` and
  `continue` cannot leave a fork body.
- `switch` takes int and string cases. A dense table (opcode 0x78) is chosen when the value range is at most
  20 × the number of cases, else 0x79. `: dense`, `: sparse` and `: dense(lo, hi)` select the table explicitly.
  Duplicate cases are an error.

**Constants** (compiler only, `x3kc/kcconst.py`; the decompiler never writes them). Nothing of a constant reaches
the object: a use compiles to exactly the code of the number or string typed in its place.

```
const MAX_SECTORS = 24 * 22;             // top level: project-global, every part sees it, in any order
const NONE = -1;
const NAME = "player";                   // a string: a literal alone, no operators
enum MainType { MT_SECTOR = 1, MT_SHIP = 7, MT_LASER, MT_SHIELD }   // 1, 7, 8, 9; first without value: 0
class Ship = 2004 : ScriptObject {
    const LASER_KEY_BASE = 1;            // class-scoped: bare in Ship and its subclasses, Ship.LASER_KEY_BASE elsewhere
    enum Reason { R_KILLED, R_DOCKED }   // members are class constants too: R_DOCKED, Ship.R_DOCKED, Reason.R_DOCKED
    var m5 = LASER_KEY_BASE << 2;        // field initialiser: any constant expression
    ...
}
```

- **Value:** integer literals (decimal, hex), strings (alone), other constants (bare or `Q.NAME`), class names (a
  class name is its id: `Ship` is 2004, `C9002` is 9002), unary `- ~ +`, `* / % + - << >> & ^ |` with the
  expression precedence above, parentheses. 32-bit cells: results wrap; `/` and `%` round towards zero (the VM's
  rule), division by zero and shifts outside 0..31 are errors. A constant may use one declared later or in another
  part; a cycle is an error.
- **Scope:** top level = global. In a class body = that class: the bare name in its methods and those of its
  subclasses, `Class.NAME` from anywhere (also `Sub.NAME` for an inherited one). Enum members are constants of the
  enum's scope, used bare (`MT_SHIP`, like a C enum or `#define`) or qualified by the enum name (`MainType.MT_SHIP`)
  where the enum name is visible; the enum name itself is not a value. `Q.NAME` without `(` is a constant,
  `Q.NAME(...)` stays the static call it always was.
- **Use:** wherever an expression goes, including `case` labels, table / array literals and suspendable methods.
  `-NAME` is the negative literal (as `-5` is, not `-(5)`); `-Ship` likewise. `~NAME`, `NAME + 1` are the same
  expressions as `~5`, `5 + 1`.
- **Errors** (`part.kc:line: ...`): a name defined twice in one scope; a constant or enum named like a class, global
  variable, global function, or a field or method visible where it is (for a global constant: of any class; for a
  class constant: of its class chain or a subclass); a global and a class constant sharing a name, or two classes of
  one chain doing so (unrelated classes may reuse a name); a
  local or parameter named like a visible constant; assignment, `op=`, `++`/`--` on a constant; a value that is not
  a constant expression; a cycle.
- **Not usable** (a literal number is still required): class ids `class X = N`, stamps `@N`, `frame N`, `__m(N)` /
  `__g(N)` / `__init(t, v)`, `switch : dense(lo, hi)`, asm operands, directives; a `case` label must still be a
  single literal or constant (`case A + 1:` is refused, as `case 5 + 1:` is).
- **Cache:** the constants' scopes, names and values are part of the global signature of the compile cache
  (`kccache.signature`): a changed value re-parses every body, whichever part it is in.
- `split` keeps top-level constants and enums in `@header.kc` (or a `@globals.<NAME>.kc` part after a class or
  function, like a late `var`).

**foreach** (compiler only, like constants: the decompiler never writes it, the object gets the plain loop code).

```
for (var x in a) { … }                   // the elements of array a
for (var i, x in a) { … }                // index and element
for (var k in table t) { … }             // the keys of table t
for (var k, v in table t) { … }          // key and value
for (var s: Ship in ships) { … }         // a loop variable may carry a type hint
```

- `table` after `in` marks a table; without it the value is an array. (`table` stays usable as a variable name:
  it is the marker only when an expression follows it, so `in table)` and `in table[0]` read the variable.)
- Lowering, in a block of its own: `{ var $c = SE_ArrayClone(a); var $n = SE_ArraySize($c); var $i = 0;
  for (; $i < $n; $i = $i + 1) { var x = $c[$i]; …body } }`. A table: `var $t = t; var $c = SE_TableKeys($t);`,
  then the same loop over the keys, with `var v = $t[k];`.
- **A foreach walks a snapshot:** the elements of an array (a shallow copy, `SE_ArrayClone`) and the keys of a
  table as they were when the loop started. Adding, removing or replacing elements in the body does not move the
  loop: every element of the start is visited once, removed ones included, appended ones not (removing dead ships
  while walking the list is safe). For a table the value is read when its key is visited, so a key removed by
  then gives `v` = 0. The copy costs one array allocation per loop. Key 0 is visited, unlike the stock
  `while ((k = SE_TableNext(t, k)) != 0)` walk. A value that is not an array (table) runs the body zero times.
- The loop variables are fresh locals of each iteration: assigning them does not move the loop. `break` and
  `continue` work as in `for`; `continue` goes to the step.
- **Frame and savegames:** the hidden locals (container, size, index; the table too) stay on the stack for the whole
  loop, so a foreach usually makes the method's frame larger than a loop written by hand. That matters only in a
  method a task can wait in, under `compile --base` (see «Mods and savegames»): the build is refused there as for any
  changed frame, and `compile` / `savecheck` add a note naming the foreach. Write such a loop by hand with locals
  the method already has, or move it into a method that does not wait.

**Type hints** (compiler only; they never change the code, the decompiler does not write them).

```
var gPlayer: Ship;                       // a global or a field
class Ship = 2004 : ScriptObject {
    var target: Ship;
    function Attack(enemy: Ship, n): Ship { … }    // parameters and the return value
}
var s: Ship = FindShip();                // a local; also foreach variables
```

- A hint names a class of the project (`Ship` or `C2004`); an unknown class is an error at the hint.
- The compiler knows the class of a hinted variable, field, global or parameter, of a call to a function with a
  return hint, and of `this`. With it, it **warns** (`warning: part.kc:line: …`, the build goes on):
  - `x.F(a)` on a hinted `x`: the class chain has no method `F` (the VM looks the name up in the object's class
    chain; a missing method answers 0); more arguments than `F` takes; fewer (a call by name passes 0 for the
    missing ones) — unless `F` reads `__argc` or an `argN` past its parameters;
  - `x.C::F(a)` on a hinted `x` that is not a `C`;
  - a value of a known class that is not the hinted class or a subclass of it: `var s: Ship = t;`, `s = t;`, an
    argument for a hinted parameter (direct calls and calls on a hinted object), `return t;` in a function with a
    return hint.
- Calls by name on `this` (`this.F()`, `this->F()`) are not checked: a base class calling a method its subclasses
  define is the usual virtual call. Direct calls already have their argument count checked by the verifier.
- There is no cast: `var s: Sub = this;` in a method of a base class warns (`this` is the base class there).

**Conditional compilation** (compiler only, `x3kc/kcpre.py`): `#ifdef NAME`, `#ifndef NAME`, `#if expr`,
`#elif expr`, `#else`, `#endif`, and `x3kc compile -D NAME[=V]` (repeatable; `-D NAME` is 1; V is an integer or a
string). `savecheck` takes `-D` too.

```
#ifdef DEBUG
    gTicks++;
#endif
#if LEVEL >= 2 && MODE == "fast"
    …
#elif defined(OLD_SAVES)
    …
#else
    …
#endif
```

- A directive is a line starting (after blanks) with `#` and one of these words. The inactive lines and the
  directive lines become empty lines, so every other line keeps its number: messages point at the right line of
  the right part. Directives may stand anywhere, inside functions too; each part of a split project closes its
  own `#if`. A `#` line inside a `/* */` comment is not a directive (a `"/*"` inside a string opens no comment).
  A directive may end with `// …` or `/* … */`; a `/*` after a directive must close on the same line.
- `#if` expressions: integers, strings, names, `defined(NAME)` / `defined NAME`, parentheses, `! - ~ +`, and
  `* / % + - << >> < > <= >= == != & ^ | && ||` with C precedence; 32-bit arithmetic. `&&` and `||`
  short-circuit as in C: the side they do not need is not evaluated, so `#if defined(X) && X > 1` and
  `#if !defined(X) || X == 0` work without `X`.
- Names are the `-D` names, then the top-level `const` declarations of the project that stand outside every `#if`
  block (their values must be integer or string expressions of literals and such constants). An unknown name in
  `#if` is an error, not 0 as in C. `#ifdef NAME` and `defined(NAME)` are true for either kind. `-D` names are
  not visible to the code: declare a `const` (possibly inside `#if`) for a value the code reads.
- **Cache:** the `-D` values are part of the compile cache signature, and every set of them keeps its own entry, so
  alternating builds (debug / release) both stay warm.
- The language server sees no `-D` names: it shows the `#else` branches.

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
   the target's list (`--target`, README "Targets"): `x3kc/sync_natives_ap.txt` — flow analysis of the native
   dispatchers in X3AP.exe for the groups B3D, INS, SA, X2 and SE (every path of a handler resumes the task before
   the dispatcher returns, `docs/natives/sync_scan.py`), plus the SE/TI handler specs; `sync_natives_fl.txt` — the
   same analysis of X3FL.exe (`sync_scan.py fl`; the natives FL shares with AP come out the same, its two B3D
   quality natives answer at once), TI from the specs; for the x3_src targets also `sync_natives_x3src.txt` (each
   handler of the engine's own natives resumes the task at once). A name the target does not have answers at once.
   Natives of the other groups count as possibly waiting. So a frame change in a method that only draws or
   computes (B3D, SE, most SA calls) does not stop the build; one in a method that waits (TI_Delay, flight
   commands, MSCI commands) does. The natives reference gives a `waits` verdict per native
   ([natives/README.md](natives/README.md#waits-and-savegame-return-sites)); where it disagrees with these lists, and
   what following it would change in the x3_src objects' return sites: [natives/sync_review.md](natives/sync_review.md).
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
