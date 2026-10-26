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
  text), `msci asm` turns edited text back into a script XML.
- **Checked before the game.** Every build passes a bytecode verifier, and an emulator of the game's script VM
  runs the object's own code — natives as the exe implements them, MSCI scripts through the game's own
  interpreter, savegames moved to a new build — so a mod can be tested without starting the game.

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

MSCI:
```
x3kc msci-setup "C:/Games/X3 Terran Conflict" work           texts, ware names, command database, scripts
x3kc msci dis work/msci_db.json work/scripts/plugin.foo.xml > foo.txt
# edit foo.txt
x3kc msci asm work/msci_db.json work/scripts/plugin.foo.xml foo.txt > plugin.foo.xml
```

`x3kc help` lists all commands.

## Documentation
- [docs/kc.md](docs/kc.md) — the KC language, how Egosoft's compiler generated code, the `STOR` object format,
  mods and savegames, annotations.
- [docs/msci.md](docs/msci.md) — MSCI script format, command and condition encoding, the interpreter.
- [docs/checking.md](docs/checking.md) — checking a mod without the game: the verifier, the emulator, savegame
  checks, what the tests cover.

## Annotating
`x3kc decompile --pcs` marks every statement with its code offset (`// @0012fb`). Write names and comments in the
working JSON form ([docs/kc.md](docs/kc.md#annotations)), check them with `decompile --names mine.json`, then
`x3kc names pack` makes them portable. Contributions to the bundled names are welcome — they were made by LLM
agents from the code and the game texts and have not been proof-read by a human.

## Status and caveats
- Round trips are verified, changed objects are checked by the verifier and the emulator; running a **changed**
  object or an assembled MSCI script in the game has not been tested yet. Expect the game to mark itself
  "modified" (signatures will not match).
- Comments that the agents attached to an offset of another method are reported as `names: comment not placed`.
- `for (;;)` is not supported (write `while (1)`).

## Tests
```
python -m unittest discover tests                      no game files needed
X3_GAME="C:/Games/X3 Terran Conflict" python -m unittest discover tests   also the game's objects: round trips, emulator
```

## Legal
X3: Terran Conflict and X3: Albion Prelude are © Egosoft. This repository contains no game files and no
decompiled game code; it reads them from your own installation. The bundled annotations (names, descriptions,
comments), the list of native function names and the descriptions of native behaviour (`docs/natives`) are
original work describing the game's interfaces.

Licensed under the MIT license, see [LICENSE](LICENSE).
