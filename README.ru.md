# x3kc

Декомпилятор и компилятор **скриптов KC** игр *X3: Terran Conflict* и *X3: Albion Prelude* — игровой логики в
`L/x3story.obj`, `x3galedit.obj`, `x3intro.obj` и `x3config.obj` — с именами и комментариями для классов, полей и
функций, а также декодер и сборщик обычных **MSCI**-скриптов.

[English version](README.md)

- **Точно.** Объект → Си-подобный исходник → объект даёт тот же файл байт в байт для каждого объекта каждой
  версии TC и AP (проверено 99 объектов). Все методы выходят исходником, ассемблерные вставки не нужны.
- **С именами.** В объектах нет имён, кроме имён методов. x3kc содержит имена и комментарии для AP 3.3: все 280
  классов x3story, 1 724 поля, ~6 500 описанных методов, ~6 700 комментариев. Они хранятся в переносимом виде и
  применяются и к другим сборкам — там, где код совпадает.
- **Для модов.** `compile --base` сохраняет загрузку сейвов исходного объекта (и прошлых версий вашего мода):
  новый build, метки добавленных полей, карты адресов для загрузчика сейвов и отказ, если правка сломает сейвы.
- **MSCI.** `msci dis` печатает скрипт так, как его показывает игровой редактор (99,6 % строк совпадают с
  текстом самой игры), `msci asm` собирает отредактированный текст обратно в XML скрипта.
- **Проверено до игры.** Каждая сборка проходит верификатор байткода, а эмулятор скриптовой VM игры исполняет
  код самого объекта — нативы как в exe, MSCI-скрипты через интерпретатор игры, перенос сейвов на новую
  сборку, — так что мод можно проверить, не запуская игру.
- **На экране.** Как скрипты рисуют HUD и как мод добавляет свой элемент: [docs/ru/screen.md](docs/ru/screen.md),
  с рабочим счётчиком FPS — примером ниже.

Нужна только стандартная библиотека Python (3.8+).

## Установка
```
pip install .            # или без установки: python -m x3kc ...
```

## Быстрый старт
```
x3kc extract "C:/Games/X3 Terran Conflict" objs              объекты, которые загружает игра
x3kc decompile objs/x3story.obj src --names auto             src/x3story.kc (+ .pool, .ptch)
x3kc compile src/x3story.kc -o check.obj --check objs/x3story.obj   до правок: «identical»
# правим src/x3story.kc
x3kc compile src/x3story.kc -o x3story.obj --base objs/x3story.obj  мод, с которым грузятся старые сейвы
x3kc smoke objs/x3story.obj x3story.obj                      прогнать все методы обеих сборок, сравнить
x3kc cat mod/08.cat --pack L/x3story.obj=x3story.obj         упаковать в каталог
```

MSCI:
```
x3kc msci-setup "C:/Games/X3 Terran Conflict" work           тексты, имена товаров, база команд, скрипты
x3kc msci dis work/msci_db.json work/scripts/plugin.foo.xml > foo.txt
# правим foo.txt
x3kc msci asm work/msci_db.json work/scripts/plugin.foo.xml foo.txt > plugin.foo.xml
```

`x3kc help` — список всех команд.

## Пример: счётчик FPS
Мод, написанный по декомпилированному `x3story.kc`: кадры в секунду в правом верхнем углу экрана. Кадры считаются
в покадровом обработчике `Client.Vbi`, число печатается в полосу текстуры тултипа, которую показывает только квад
счётчика. Подключается двумя строками:

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

и сам класс:

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

Сборка — `x3kc compile src/x3story.kc -o x3story.obj --base objs/x3story.obj`, упаковка в `addon/05.cat`; стоковые
сейвы продолжают загружаться. Зачем каждая строка — сцены, тела, рисуемые текстуры, пиксельные координаты, что не
работает — в [docs/ru/screen.md](docs/ru/screen.md).

## Документация
- [docs/ru/kc.md](docs/ru/kc.md) — язык KC, как компилировал Egosoft, формат объекта `STOR`, моды и сейвы,
  аннотации.
- [docs/ru/msci.md](docs/ru/msci.md) — формат MSCI-скриптов, кодирование команд и условий, интерпретатор.
- [docs/ru/checking.md](docs/ru/checking.md) — проверка мода без игры: верификатор, эмулятор, проверка сейвов,
  что покрывают тесты.
- [docs/ru/screen.md](docs/ru/screen.md) — вывод на экран из KC: сцены, тела, рисуемые текстуры, пиксельные
  координаты, покадровый обработчик, что не работает; счётчик FPS.

## Разметка
`x3kc decompile --pcs` помечает каждый оператор адресом в коде (`// @0012fb`). Имена и комментарии пишутся в
рабочем JSON ([docs/ru/kc.md](docs/ru/kc.md#аннотации)), проверяются через `decompile --names mine.json`, а
`x3kc names pack` делает их переносимыми. Правки встроенных имён приветствуются: их сделали LLM-агенты по коду и
текстам игры, человеком они не вычитаны.

## Состояние и оговорки
- Полный круг проверен, изменённые объекты проверяются верификатором и эмулятором; запуск **изменённого**
  объекта или собранного MSCI-скрипта в игре ещё не проверялся.
  Игра, скорее всего, пометит себя как «modified» (подписи не совпадут).
- Комментарии, которые агенты привязали к адресу чужого метода, печатаются как `names: comment not placed`.
- `for (;;)` не поддерживается (пишите `while (1)`).

## Тесты
```
python -m unittest discover tests                      без файлов игры
X3_GAME="C:/Games/X3 Terran Conflict" python -m unittest discover tests   плюс объекты игры: полный круг, эмулятор
```

## Права
X3: Terran Conflict и X3: Albion Prelude — © Egosoft. В репозитории нет файлов игры и декомпилированного кода
игры: всё читается из вашей установки. Встроенные аннотации (имена, описания, комментарии), список имён нативов
и описания поведения нативов (`docs/natives`) — собственная работа, описывающая интерфейсы игры.

Лицензия MIT, см. [LICENSE](LICENSE).
