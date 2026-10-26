# Drawing on screen from KC

Everything the game shows in 2D — HUD panels, menus, tips, tooltips — is made by the KC scripts with a few B3D
natives, so a mod can add its own on-screen elements by changing the object alone. This page collects what is known
about it; the complete working example is the [FPS counter](#example-an-fps-counter) at the end.

## How 2D works

- **Scene and camera.** A 2D element lives in a scene (`B3D_SceneAlloc`) with a camera (`B3D_CameraAlloc`,
  `B3D_CameraSetViewPort(camera, 0, 65536, 0, 65536)` for the whole screen, `B3D_CameraSetFlags(camera, 0)` so it
  does not clear what is below). Scenes are drawn in camera priority order (`B3D_CameraSetPri`): the cockpit HUD
  overlay uses 61, subtitles 140, game tips 150 — a higher number is drawn later, on top.
- **Body.** What is drawn is an instance (`B3D_InstAlloc`, `B3D_InstSetBody`, `B3D_InstAddToScene`) of a body: a
  quad with the `gui2d` effect whose material names its texture. **The texture belongs to the body file**, not to
  the script: the weapons panel is the body `effects/menugfx/hud_weapons_2d`, its material uses texture 17, and the
  scripts draw the ammunition counts into texture 17.
- **Writable textures.** Textures a script can draw into are the rows of `types/Materials` marked
  `MPF_GENERATED | MPF_WRITEABLE`: blank surfaces of the size the row gives, created on first use. They are drawn
  with `B3D_TexRectFill`, `B3D_TexTextBlock`, `B3D_TexBltBlockAlpha` and friends; colours are ARGB, `0` is fully
  transparent.
- **Pixel coordinates.** `B3D_InstSetFlags2(inst, B3D_InstGetFlags2(inst) | flags)` with `0x200` makes the
  instance a pixel-space 2D element; the anchor bits choose the screen edge the position counts from: `0x1000` left,
  `0x2000` right, `0x4000` top, `0x8000` bottom. `B3D_InstSetPos(inst, x, y, 0)` then places the **centre** of the
  quad, in pixels, x to the right, y downwards. `0x5200` (top left) is what the game tip window uses:
  `x = B3D_ScreenGetWidth() - 32 - 128` puts its 256-pixel-wide quad 32 pixels from the right edge. The meaning of the
  edge bits is read from the HUD panels (radar `0x8000|0x2000` at (-142, -131), weapons `0x8000|0x1000`, side
  monitors `0x4000|...`), not from the exe.
- **Quad size.** A body file gives an "automatic body size": the number of pixels its largest vertex coordinate
  stands for. `GUI/tooltip`: vertices ±50000, size 128 → a 256 × 256 pixel quad; `GUI/gametip`: ±50000 × ±100000,
  size 256 → 256 × 512. The texture covers the whole quad (UV 0..1), so a quad of the texture's size shows it pixel
  for pixel.
- **Text.** `B3D_TexTextBlock(texture, text, font, x, y, width, colour, 129)` — `gHudFont` (Tahoma 13) and
  `gLargeFont` are the game's fonts (global names with `--names auto`), width 0 = no wrap, flags 129 the usual
  ones; `B3D_TexTextWidth(text, font, 129)` measures.

## Bodies and their textures

Bodies with a writable texture of their own (from `objects/effects/menugfx` in the catalogues), and who uses them:

| Body | Texture | Quad | Used by |
|---|---|---|---|
| `GUI/tooltip` | 1230 (256 × 256) | 256 × 256 | tooltips (`INS_ShowTooltip` from KC, drawn by the exe from the top left corner) |
| `GUI/gametip` | 9 (256 × 512) | 256 × 512 | `GameTips` (tip window at the right edge) |
| `GUI/infobox` | 6 (512 × 64) | 512 × 64 | `ScreenMessage` (short messages) |
| `GUI/messagebox` | 7 (512 × 256) | 512 × 256 | `MessageBox` |
| `GUI/contextmenu`, `GUI/contextsubmenu` | 10, 14 (256 × 1024) | 256 × 1024 | context menus |
| `GUI/sidebar` | 8 (64 × 2048) | 64 × 2048 | the sidebar |
| `hud_info_2d` | 16 (512 × 128) | 512 × 128 | `MonitorControl` info panel (bottom centre) |
| `hud_weapons_2d` | 17 (256 × 512) | 256 × 512 | `MonitorControl` weapons panel |
| `monitor_text_1/2`, `monitor_text_zoom` | 21, 22 (256 × 256), 19 (512 × 512) | 256 × 216, 512 × 432 | side monitors |
| `hud_radar_2d` | 66 (128 × 128) + atlas | 196 × 172 | the radar; **the exe clears and redraws texture 66 every frame** (`Hud::DrawGravidar` in `Cockpit::UpdateAll`, after every place where KC runs), so nothing a script draws into it reaches the screen |

Writable textures no body uses (e.g. 13, 256 × 256) exist too, but there is no way to show them: see below.

## Per frame, and after a load

- **`Client.Vbi`** runs once per frame: the engine calls `Client.Input` every frame (outside the pause loop), which
  calls `Vbi` whenever the real time (`TI_GetRealTime`, ms) moved on. A mod hooks in by one line at its start.
- **After loading a game** every B3D handle a class kept in its fields is stale (the numbers may belong to other
  objects now): forget them without freeing them. **`Client.Restart`** is called when a game is (re)loaded — the
  place to reset (the FPS counter below comes back after a load this way).

## What does not work

- **`B3D_InstSetAnimTextureCoords(inst, texture, u, v)`** (the native that sets a texture override on an instance)
  has no visible effect on a body whose material has a plain texture: the instance keeps showing its own texture.
  The exe uses the same override for icons of bodies with an animated texture (negative texture number in the
  material, e.g. `GUI/qm_icon`); whether it works there for a script was not tried.
- **Drawing into the radar** (texture 66): erased before it is shown, see above.
- **`SA_AllocTextInst(text, flags)`** builds 3D text from glyph models; the stock scripts never call it, and it was
  not tried.

So an own element must use the texture of an existing body, and share it with the game's own user of that body —
which is what the example does, in a way the game never notices.

## Example: an FPS counter

Frames per second in the top right corner. It uses the tooltip body and texture 1230: tooltips are drawn from the
top left corner of that texture, the counter takes its bottom strip (rows 232..255), and its quad is lifted above the
screen so that only that strip is visible — tooltips are neither erased nor shown twice.

Two hooks in `x3story.kc` (decompiled with `--names auto`):

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

and a new class:

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

Build and install it as any mod:

```
x3kc compile src/x3story.kc -o x3story.obj --base objs/x3story.obj
x3kc cat "C:/Games/X3 Terran Conflict/addon/05.cat" --pack L/x3story.obj=x3story.obj
```

`addon/05.cat` — the next free number after the game's own `addon/01..04.cat` (AP): the game mounts the catalogues
in order and the highest number wins. Tested on X3: Albion Prelude 3.3.

Before the game: the emulator runs the class like the game does (`vm.natives[...]` answers the B3D calls), see
[checking.md](checking.md).
