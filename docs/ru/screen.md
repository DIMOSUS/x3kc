# Вывод на экран из KC

Всё, что игра показывает в 2D, — панели HUD, меню, подсказки, тултипы — делают скрипты KC несколькими нативами B3D,
поэтому мод может добавить свой элемент на экран, меняя только объект. Здесь собрано, что об этом известно; полный
рабочий пример — [счётчик FPS](#пример-счётчик-fps) в конце.

## Как устроено 2D

- **Сцена и камера.** 2D-элемент живёт в сцене (`B3D_SceneAlloc`) со своей камерой (`B3D_CameraAlloc`,
  `B3D_CameraSetViewPort(camera, 0, 65536, 0, 65536)` — весь экран, `B3D_CameraSetFlags(camera, 0)` — не стирать то,
  что под ней). Сцены рисуются по приоритету камеры (`B3D_CameraSetPri`): оверлей HUD кокпита — 61, субтитры — 140,
  подсказки — 150; чем больше число, тем позже и выше.
- **Тело.** Рисуется экземпляр (`B3D_InstAlloc`, `B3D_InstSetBody`, `B3D_InstAddToScene`) тела — квада с эффектом
  `gui2d`, материал которого называет текстуру. **Текстура принадлежит файлу тела**, а не скрипту: панель оружия —
  это тело `effects/menugfx/hud_weapons_2d`, его материал берёт текстуру 17, а скрипты пишут в текстуру 17 число
  боеприпасов.
- **Текстуры для рисования** — строки `types/Materials` с флагами `MPF_GENERATED | MPF_WRITEABLE`: пустые
  поверхности указанного в строке размера, создаются при первом обращении. Рисуют в них `B3D_TexRectFill`,
  `B3D_TexTextBlock`, `B3D_TexBltBlockAlpha` и родственные нативы; цвета ARGB, `0` — полностью прозрачный.
- **Пиксельные координаты.** `B3D_InstSetFlags2(inst, B3D_InstGetFlags2(inst) | флаги)` с `0x200` делает
  экземпляр пиксельным 2D-элементом; биты привязки выбирают край экрана, от которого считается позиция: `0x1000`
  левый, `0x2000` правый, `0x4000` верхний, `0x8000` нижний. `B3D_InstSetPos(inst, x, y, 0)` ставит **центр** квада,
  в пикселях, x вправо, y вниз. `0x5200` (от левого верхнего угла) использует окно подсказок:
  `x = B3D_ScreenGetWidth() - 32 - 128` ставит его квад шириной 256 в 32 пикселях от правого края. Смысл битов краёв
  выведен из панелей HUD (радар `0x8000|0x2000` в (-142, -131), оружие `0x8000|0x1000`, боковые мониторы
  `0x4000|…`), а не из exe.
- **Размер квада.** В файле тела есть «автоматический размер тела»: сколько пикселей соответствует наибольшей
  координате вершины. `GUI/tooltip`: вершины ±50000, размер 128 → квад 256 × 256 пикселей; `GUI/gametip`:
  ±50000 × ±100000, размер 256 → 256 × 512. Текстура покрывает весь квад (UV 0..1), так что квад размера текстуры
  показывает её пиксель в пиксель.
- **Текст.** `B3D_TexTextBlock(текстура, текст, шрифт, x, y, ширина, цвет, 129)` — `gHudFont` (Tahoma 13) и
  `gLargeFont` — шрифты игры (имена глобальных с `--names auto`), ширина 0 — без переноса, флаги 129 обычные;
  `B3D_TexTextWidth(текст, шрифт, 129)` измеряет.

## Тела и их текстуры

Тела со своей рисуемой текстурой (`objects/effects/menugfx` в каталогах) и кто ими пользуется:

| Тело | Текстура | Квад | Кто пользуется |
|---|---|---|---|
| `GUI/tooltip` | 1230 (256 × 256) | 256 × 256 | тултипы (`INS_ShowTooltip` из KC, рисует exe от левого верхнего угла) |
| `GUI/gametip` | 9 (256 × 512) | 256 × 512 | `GameTips` (окно подсказок у правого края) |
| `GUI/infobox` | 6 (512 × 64) | 512 × 64 | `ScreenMessage` (короткие сообщения) |
| `GUI/messagebox` | 7 (512 × 256) | 512 × 256 | `MessageBox` |
| `GUI/contextmenu`, `GUI/contextsubmenu` | 10, 14 (256 × 1024) | 256 × 1024 | контекстные меню |
| `GUI/sidebar` | 8 (64 × 2048) | 64 × 2048 | боковая панель |
| `hud_info_2d` | 16 (512 × 128) | 512 × 128 | инфопанель `MonitorControl` (внизу по центру) |
| `hud_weapons_2d` | 17 (256 × 512) | 256 × 512 | панель оружия `MonitorControl` |
| `monitor_text_1/2`, `monitor_text_zoom` | 21, 22 (256 × 256), 19 (512 × 512) | 256 × 216, 512 × 432 | боковые мониторы |
| `hud_radar_2d` | 66 (128 × 128) + атлас | 196 × 172 | радар; **exe каждый кадр стирает и перерисовывает текстуру 66** (`Hud::DrawGravidar` в `Cockpit::UpdateAll`, после всех мест, где работает KC), поэтому нарисованное скриптом на экран не попадает |

Рисуемые текстуры без тела (например 13, 256 × 256) тоже есть, но показать их нечем, см. ниже.

## Каждый кадр и после загрузки

- **`Client.Vbi`** вызывается раз в кадр: движок каждый кадр (вне цикла паузы) вызывает `Client.Input`, а тот —
  `Vbi`, если сдвинулось реальное время (`TI_GetRealTime`, мс). Мод встраивается одной строкой в его начало.
- **После загрузки игры** все дескрипторы B3D в полях класса устарели (номера могут принадлежать уже другим
  объектам): забыть их, не освобождая. **`Client.Restart`** вызывается при (пере)загрузке игры — место для сброса
  (так счётчик FPS ниже возвращается после загрузки).

## Что не работает

- **`B3D_InstSetAnimTextureCoords(inst, текстура, u, v)`** (натив, ставящий экземпляру подмену текстуры) не даёт
  видимого эффекта на теле с обычной текстурой в материале: экземпляр показывает свою текстуру. Exe использует ту
  же подмену для иконок тел с анимированной текстурой (отрицательный номер в материале, например `GUI/qm_icon`);
  работает ли она там из скрипта, не проверялось.
- **Рисовать в радар** (текстура 66): стирается до показа, см. выше.
- **`SA_AllocTextInst(текст, флаги)`** строит 3D-текст из моделей глифов; стоковые скрипты его не вызывают, не
  проверялся.

Поэтому свой элемент приходится показывать на текстуре существующего тела и делить её с тем, кто в игре этим телом
пользуется, — как в примере, так, что игра этого не замечает.

## Пример: счётчик FPS

Кадры в секунду в правом верхнем углу. Используются тело и текстура тултипа 1230: тултипы рисуются от левого
верхнего угла этой текстуры, счётчик занимает её нижнюю полосу (строки 232..255), а его квад поднят за край экрана
так, что видна только эта полоса, — тултипы не стираются и не показываются дважды.

Две врезки в `x3story.kc` (декомпилированный с `--names auto`):

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

и новый класс:

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

Сборка и установка — как у любого мода:

```
x3kc compile src/x3story.kc -o x3story.obj --base objs/x3story.obj
x3kc cat "C:/Games/X3 Terran Conflict/addon/05.cat" --pack L/x3story.obj=x3story.obj
```

`addon/05.cat` — следующий свободный номер после собственных `addon/01..04.cat` игры (AP): игра подключает каталоги по
порядку, и побеждает больший номер. Проверено на X3: Albion Prelude 3.3.

До игры: эмулятор исполняет класс так же, как игра (`vm.natives[...]` отвечает за вызовы B3D), см.
[checking.md](checking.md).
