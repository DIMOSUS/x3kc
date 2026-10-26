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

MSCI:
```
x3kc msci-setup "C:/Games/X3 Terran Conflict" work           texts, ware names, command database, scripts
x3kc msci dis work/msci_db.json work/scripts/plugin.foo.xml > foo.txt
# edit foo.txt
x3kc msci asm work/msci_db.json work/scripts/plugin.foo.xml foo.txt > plugin.foo.xml
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

## Documentation
- [docs/kc.md](docs/kc.md) — the KC language, how Egosoft's compiler generated code, the `STOR` object format,
  mods and savegames, annotations.
- [docs/msci.md](docs/msci.md) — MSCI script format, command and condition encoding, the interpreter.
- [docs/checking.md](docs/checking.md) — checking a mod without the game: the verifier, the emulator, savegame
  checks, what the tests cover.
- [docs/screen.md](docs/screen.md) — drawing on screen from KC: scenes, bodies, writable textures, pixel
  coordinates, the per-frame hook, what does not work; the FPS counter.

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
