# x3kc

Decompiler and compiler for the **KC scripts** of *X3: Terran Conflict* and *X3: Albion Prelude* — the game logic
in `L/x3story.obj`, `x3galedit.obj`, `x3intro.obj` and `x3config.obj` — with names and comments for its classes,
fields and functions, plus a decoder and assembler for ordinary **MSCI** scripts.

[Русская версия](README.ru.md)

- **Exact.** Object → C-like source → object gives the same file byte for byte, for every object of every TC and
  AP version (99 objects tested). Every method comes out as source; no inline assembly needed.
- **Named.** The objects carry no names except method names. x3kc ships names and comments for AP 3.3: all 280
  classes of x3story, 1 724 fields, ~6 500 documented methods, ~6 700 comments. They are stored in a portable
  form and also apply to other builds as far as the code still matches.
- **Mod-friendly.** `compile --base` keeps savegames of the original object (and of earlier versions of your mod)
  loading: new build id, stamps for added fields, address maps for the savegame loader, and a refusal when a
  change would break savegames.
- **MSCI.** `msci dis` prints a script as the in-game editor shows it (99.6 % of lines identical to the game's own
  text), `msci asm` turns edited text back into a script XML, `msci new` builds a new script from text alone;
  `dis` → `new` rebuilds the code of every stock script (AP 697 of 697, FL 1 020 of 1 020), so scripts can be
  kept as text.
  Albion Prelude and Farnham's Legacy (`--addon addon2`). A [manual for writing scripts](docs/msci_manual.md)
  and a command reference generated from the game's tables ([AP](docs/msci_commands_ap.md),
  [FL](docs/msci_commands_fl.md)). `msci build` adds a source layer on top (`.xms`, [MSCI
  sugar](docs/msci_manual.md#msci-sugar-xms)): a preprocessor (`#define`, `#ifdef`, `#include`), functions with
  private variables, `sub` blocks, `foreach`, `+=` / `++`, and type warnings; it lowers to plain editor text, so the
  stock game runs the result and stock text builds unchanged.
- **Mission Director.** `md dis` turns a director XML into a compact C-like text (`.xmd`: `cue X { condition {...}
  action { if (...) {...} else {...} } }`, `N = V;`, `$var`, `@param`), `md asm` builds it back; byte-identical
  for every stock file (AP 88/88, FL 85/85), about 1.5x fewer characters; unknown elements and attributes, braces
  and cue references are checked with file:line. [docs/md.md](docs/md.md).
- **Checked before the game.** Every build passes a bytecode verifier, and an emulator of the game's script VM
  runs the object's own code — natives as the exe implements them, MSCI scripts through the game's own
  interpreter, savegames moved to a new build — so a mod can be tested without starting the game.
- **On screen.** How the HUD is drawn from the scripts and how a mod adds its own element:
  [docs/screen.md](docs/screen.md), with a working FPS counter as the example below.

Only the Python standard library is needed (Python 3.8+).

## Install
```
pip install .            # or run in place: python -m x3kc ...
```

## Quick start
```
x3kc extract "C:/Games/X3 Terran Conflict" objs              the objects the game loads
x3kc decompile objs/x3story.obj src --names auto             src/x3story.kc (+ .pool, .ptch)
x3kc compile src/x3story.kc -o check.obj --check objs/x3story.obj   "identical" before you change anything
# edit src/x3story.kc
x3kc compile src/x3story.kc -o x3story.obj --base objs/x3story.obj  a mod that keeps savegames loading
x3kc smoke objs/x3story.obj x3story.obj                      run every method of both builds, compare
x3kc cat mod/08.cat --pack L/x3story.obj=x3story.obj         pack it into a catalogue
```
With `--names auto` the source reads like `class Station = 2016 : ScriptObject { … function Hit(shieldDamage,
hullDamage, directHullDamage, hitter) { … } }`, with a comment above most classes, methods and many statements;
without it, like `class C2016 : C702 { … function Hit(arg1, arg2, arg3, arg4) { … } }`.
Keeping the source in git? `decompile --no-offsets` leaves out the method's code offset (`// 131f2b`) after each
`function` line: otherwise any inserted code shifts the offsets of every later method and bloats the diff.
`compile` does not care either way. `decompile new.obj src --names auto --no-offsets --names-from old.obj` also
keeps the identifiers the previous build decompiles with — real names and numbered ones (`v12`, `m5`, `g7`) alike —
on the locals, fields and globals whose code still aligns (methods by name, instructions in order; fields and
globals by type and initial value); only new ones get new numbers, above the previous build's. Without it one
inserted local or global renumbers everything after it (Farnham's Legacy's community patch: 131 000 diff lines in
x3story with renumbering, 22 000 without).

**Split projects.** x3story.kc is 8–12 MB in one file. `decompile … --split` (it combines with `--names`,
`--no-offsets`, `--names-from`) writes a directory instead: `x3story.kcs`, the index that lists the parts in source
order (the only thing that decides the order), `@header.kc` (directives and global variables), one `<Class>.kc`
per class (`C2259.kc` when unnamed) and one `@<Function>.kc` per global function, with `.pool` and `.ptch`
unchanged. `x3kc split src/x3story.kc src/x3story` and `x3kc join src/x3story src/x3story.kc` convert an existing
project either way as plain text — your comments and formatting included, `join(split(x))` is `x` byte for byte.
`compile`, `savecheck` and `msci map` take the directory (or its `.kcs`) wherever they take a `.kc`, and give
errors as `Client.kc:123:`. One project per directory. A part file name only helps you find a class: on a name
that collides case-insensitively with an earlier one (or a Windows device name like `Con`) it becomes
`Name~<class id>.kc`; rename a class and the old file name still works, the next `split` renames the file.
A new class or global function: `x3kc add src/x3story class Radar --parent ScriptObject` (or `function
DrawRadar`) writes `Radar.kc` from a template and lists it in the index, after the last class (function) part or
`--after <part>`; without `--id` the class gets the next id above every id of the project, at least 9500 (the stock
objects use 50..9003); an existing name or id is refused. `x3kc remove src/x3story Radar` takes a part out again.
`compile` refuses a `.kc` file in the project folder that the index does not list (it would silently not be
compiled) and an index line whose file is missing.

**Rebuilds.** `compile --cache DIR` keeps what does not change between builds in DIR, outside the sources: the
parsed function bodies (an edit re-parses only the bodies it touches; a new or renamed class, field, method or
global re-parses all) and what `--base` derives from the base object. The object is byte for byte the one
`--no-cache` (or no `--cache`) builds; a missing or stale cache only costs time. Each set of `-D` values keeps its
own entry. AP x3story with `--base`:
7.5 s without the cache, 6.0 s with it (it was 108 s before `--base` stopped aligning unchanged methods with difflib).

**Targets.** `compile`, `verify`, `savecheck` and `natives` take `--target`, the exe the object is for: `ap`
(X3AP.exe, the default), `fl` (X3FL.exe), `x3_src-ap` / `x3_src-fl` (the x3_src engine, a rewrite of the exe, built
as AP / FL: the edition's natives plus its own). The target decides which natives exist — a call of another one
answers 0, so it is warned about: `native X exists only in the x3_src engine (the original exe answers 0)`,
`... only in Farnham's Legacy (the AP exe answers 0)`, `... only in Albion Prelude (the FL exe answers 0)`, else
`native X is unknown to X3AP.exe` — and which natives always answer at once (their calls are not savegame return
sites for `--base`). The lists: `x3kc/natives_ap.txt` and `natives_fl.txt` (the native names among each exe's
strings, `docs/natives/native_strings.py`; FL adds `B3D_InstGetQualityModifier` and
`B3D_InstSetQualityModifierRecursive` and has no `X2_GetModName`), `natives_x3src.txt` (the engine's own),
`sync_natives_ap.txt`, `_fl.txt`, `_x3src.txt` (step 5 of `--base` in [docs/kc.md](docs/kc.md)). The language server
takes the target from the environment variable `KCLSP_KC_TARGET`, else from a file `.x3kc-target` (one word) in the
project's folder or above, else `fl` for a path with a folder `fl` or `addon2`, else `ap`.
`x3kc natives [--target T] [name or prefix]` lists a target's natives, one line each: name, group, arguments ->
result, at once / may wait, which exes have it (AP+FL, AP only, FL only, x3_src only) and the summary of its entry
in `docs/natives/spec_<GROUP>.jsonl`; `--full` prints every field of the entry.

**Natives reference.** [docs/natives/README.md](docs/natives/README.md) describes every native of X3AP.exe, X3FL.exe
and the x3_src engine (998 entries, one page per group: SA, SE, TI, B3D, J3D, INS, X2, STA, P, MOV, SFX, GS): call
forms, result, whether the task waits (savegame return sites), the full behaviour, how FL and x3_src differ, original
bugs and the confidence of each entry. The pages are generated from the spec files (`x3kc natives-ref`; `--check`
validates them against the native lists). Where the reference's waits verdicts disagree with the at-once lists:
[docs/natives/sync_review.md](docs/natives/sync_review.md).

MSCI:
```
x3kc msci-setup "C:/Games/X3 Terran Conflict" work           texts, ware names, command database, scripts
x3kc msci dis work/msci_db.json work/scripts/plugin.foo.xml > foo.txt
# edit foo.txt
x3kc msci asm work/msci_db.json work/scripts/plugin.foo.xml foo.txt > plugin.foo.xml
x3kc msci new work/msci_db.json my.script.txt -o my.script.xml       a new script: header from `//` lines
x3kc msci build work/msci_db.json my.script.xms -o my.script.xml     the same from MSCI sugar (--text: lowered text)
```
Farnham's Legacy: `x3kc msci-setup "<TC dir>" work-fl --addon addon2` (and `extract … --addon addon2`).

Mission Director:
```
x3kc md dis "addon/director/2.183 Dual Convoy.xml" -o dc.xmd     XML -> text
# edit dc.xmd
x3kc md asm dc.xmd -o "2.183 Dual Convoy.xml"                    text -> XML
x3kc md check addon/director                                     round trip of a whole folder: N/N identical
```

`x3kc help` lists all commands.

## Example: an FPS counter
A mod written against the decompiled `x3story.kc`: frames per second in the top right corner of the screen,
counted in the per-frame hook `Client.Vbi` and printed into a strip of the tooltip texture that only this counter's
quad shows. Two lines hook it in:

```c
class Client = 150 {
    function Vbi() {
        FpsCounter.Vbi();
        ...
    }
    function Restart(arg1) {
        FpsCounter.Reset();
        ...
    }
}
```

and the class:

```c
class FpsCounter = 9500 {
    var scene = 0;  // B3D scene of the counter, 0 until created
    var camera = 0;  // camera of that scene
    var inst = 0;  // tooltip body instance
    var frames = 0;  // frames counted since startTime
    var startTime = 0;  // real time (ms) the count started

    function Vbi() {
        var now = TI_GetRealTime();
        if (!scene) {
            Create();
            frames = 0;
            startTime = now;
            return;
        }
        frames += 1;
        var elapsed = TI_CmpTime(now, startTime);
        if (elapsed >= 1000) {
            Draw(frames * 1000 / elapsed);
            frames = 0;
            startTime = now;
        }
    }

    function Create() {
        scene = B3D_SceneAlloc();
        if (!scene) {
            return;
        }
        B3D_SceneSetName(scene, "FPS scene");
        camera = B3D_CameraAlloc();
        if (camera) {
            B3D_CameraAddToScene(camera, scene);
            B3D_InstSetPos(camera, 0, 0, 0);
            B3D_InstSetRot(camera, 0, 0, 0);
            B3D_InstSetName(camera, "FPS camera");
            B3D_CameraSetViewPort(camera, 0, 65536, 0, 65536);
            B3D_CameraSetFlags(camera, 0);
            // above the HUD overlay (61), subtitles (140) and game tips (150)
            B3D_CameraSetPri(camera, 155);
        }
        inst = B3D_InstAlloc();
        if (inst) {
            B3D_InstSetBody(inst, B3D_GetBodyID("effects/menugfx/GUI/tooltip"));
            B3D_InstAddToScene(inst, scene);
            B3D_InstSetRot(inst, 0, 0, 0);
            // 0x200 pixel-space 2D, 0x1000 | 0x4000 position = centre of the quad from the top left corner
            B3D_InstSetFlags2(inst, B3D_InstGetFlags2(inst) | 0x5200);
            B3D_InstSetInvis(inst, 0);
        }
        B3D_SceneEnable(scene);
    }

    function Draw(fps) {
        var text = SE_SPrintf("%d FPS", fps);
        B3D_TexRectFill(1230, 0, 232, 256, 24, 0);
        B3D_TexTextBlock(1230, text, gHudFont, 256 - 6 - B3D_TexTextWidth(text, gHudFont, 129), 236, 0, 0xFFD2D2D2, 129);
        // centre 128 - 232 from the top: texture rows 232..255 are screen rows 0..23, the rest is above the screen;
        // set every second, as the screen width may not be known yet when the scene is made in the first frame
        if (inst) {
            B3D_InstSetPos(inst, B3D_ScreenGetWidth() - 128, -104, 0);
        }
    }

    // The handles of a saved game are stale after a load: forget them (never free them, they may belong to others now).
    function Reset() {
        scene = 0;
        camera = 0;
        inst = 0;
    }
}
```

Build it with `x3kc compile src/x3story.kc -o x3story.obj --base objs/x3story.obj` and pack it as
`addon/05.cat`; the stock savegames keep loading. Why every line is there — scenes, bodies, writable textures,
pixel coordinates, what does not work — is in [docs/screen.md](docs/screen.md).

## Constants
Hand-written sources can name their numbers: `const MAX_SECTORS = 24 * 22;` and `enum MainType { MT_SHIP = 7,
MT_LASER }` at the top level (project-global) or in a class body (`Ship.NAME` from outside, bare inside the class
and its subclasses). A use compiles to exactly the code of the typed literal; the object does not change. Rules,
errors and where a literal is still required: [docs/kc.md](docs/kc.md) "Constants". The decompiler never writes
constants.

## foreach, type hints, conditional compilation
More compile-only sugar for hand-written code; none of it changes the object beyond the plain code it stands for,
and the decompiler never writes it ([docs/kc.md](docs/kc.md) has the rules):
```
for (var x in ships) { … }                    for (var i, x in ships) { … }      an array: element / index, element
for (var k, v in table t) { … }               for (var k in table t) { … }       a table: key, value / key
var s: Ship = gPlayerShip;   function Attack(enemy: Ship, n): Ship { … }    hints: warnings for calls on s / enemy
#ifdef DEBUG … #elif LEVEL > 1 … #else … #endif                                     conditional lines
x3kc compile src -o out.obj -D DEBUG -D LEVEL=2 -D MODE=fast                        names for #if / #ifdef
```
foreach walks a snapshot of the array (the keys of the table) taken at the start, so removing elements in the body
is safe and removed ones are still visited. It lowers to a `for` loop over hidden locals (container, size, index):
a larger frame, which `--base` refuses
in a method a task can wait in (the note names the foreach). Hints warn when a method a call names does not exist
in the hinted class chain, the argument count does not fit, or a value of another class is assigned or passed.

## Documentation
- [docs/kc.md](docs/kc.md) — the KC language (constants and enums included), how Egosoft's compiler generated
  code, the `STOR` object format, mods and savegames, annotations.
- [docs/msci_manual.md](docs/msci_manual.md) — **writing MSCI scripts**: files and loading, the text form, the
  language as the interpreter runs it (expressions, calls, waiting, interrupts, errors), texts, hotkeys and
  commands, pitfalls, testing without the game, a checklist, a worked example
  ([docs/examples/msci_hello](docs/examples/msci_hello)); command reference for [AP](docs/msci_commands_ap.md) and
  [FL](docs/msci_commands_fl.md).
- [docs/msci.md](docs/msci.md) — MSCI script format, command and condition encoding, the interpreter.
- [docs/md.md](docs/md.md) — the Mission Director text form (`md dis` / `md asm`): syntax, sugar, layout marks,
  checks, the signature.
- [docs/checking.md](docs/checking.md) — checking a mod without the game: the verifier, the emulator, savegame
  checks, what the tests cover.
- [docs/debugging.md](docs/debugging.md) — the debug info `compile` writes beside every object (`.kcdbg`: pc ->
  file:line, variables, fields, globals) and `x3kc symbolize` (raw script locations of an x3_src log -> source lines).
- [docs/screen.md](docs/screen.md) — drawing on screen from KC: scenes, bodies, writable textures, pixel
  coordinates, the per-frame hook, what does not work; the FPS counter.

## Annotating
`x3kc decompile --pcs` marks every statement with its code offset (`// @0012fb`). Write names and comments in the
working JSON form ([docs/kc.md](docs/kc.md#annotations)), check them with `decompile --names mine.json`, then
`x3kc names pack` makes them portable. Contributions to the bundled names are welcome — they were made by LLM
agents from the code and the game texts and have not been proof-read by a human.

## Status and caveats
- Round trips are verified, changed objects are checked by the verifier and the emulator. Output of all three
  kinds has been run in the game: changed KC objects, assembled MSCI scripts and Mission Director files built
  from `.xmd`. The game marks itself "modified" (signatures do not match).
- Comments that the agents attached to an offset of another method are reported as `names: comment not placed`.
- `for (;;)` is not supported (write `while (1)`).

## Tests
```
python -m unittest discover tests                      no game files needed
X3_GAME="C:/Games/X3 Terran Conflict" python -m unittest discover tests   also the game's objects: round trips, emulator
X3_FL_GAME="C:/Games/X3 Terran Conflict" ...           Farnham's Legacy (addon2) for the MSCI example tests
X3KC_MD_AP=<dir> X3KC_MD_FL=<dir> ...                  stock director folders for the md round trip (default: the
                                                       x3_src game/ap/addon/director, game/fl/addon2/director)
```

## Legal
X3: Terran Conflict and X3: Albion Prelude are © Egosoft. This repository contains no game files and no
decompiled game code; it reads them from your own installation. The bundled annotations (names, descriptions,
comments), the list of native function names and the descriptions of native behaviour (`docs/natives`) are
original work describing the game's interfaces. The MSCI command references (`docs/msci_commands_*.md`) are
generated from the games' command tables and list each command's editor syntax line, as an interface description.

Licensed under the MIT license, see [LICENSE](LICENSE).
