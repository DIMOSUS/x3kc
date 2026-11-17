# Текстовая форма Mission Director (.xmd)

Файлы Mission Director (`addon/director/*.xml`, для Farnham's Legacy `addon2/director/*.xml`) — XML: глубокий,
многословный, дорогой для чтения людьми и агентами. `x3kc md` превращает director-файл в C-подобный текст и обратно:

```
x3kc md dis <file.xml | dir>... [-o out]     XML -> текст (.xmd); проверяет, что текст собирает файл байт в байт
x3kc md asm <file.xmd | dir>... [-o out]     текст -> XML; проверяются имена, скобки, ссылки на cue
x3kc md check <file.xml | dir>... [-v]       dis + asm в памяти, сравнение, печать N/N identical (ничего не пишет)
x3kc md schema <out.json> <director dir>...  пересобрать x3kc/data/md_schema.json (см. «Схема»)
```
Каталог берёт все director-`.xml` (`dis`, `check`) или все `.xmd` (`asm`) в нём; при нескольких входах `-o` —
каталог (по умолчанию рядом с каждым входом). `dis --no-verify` пропускает проверку сборки, `asm --no-warn` —
предупреждения.

**Круг без потерь**: `asm(dis(x)) == x` байт в байт для каждого штатного файла — AP 88/88, FL 85/85 — вместе с
прологом, кодировкой и BOM, концами строк, отступами, пустыми строками, комментариями, порядком, кавычками и
пробелами атрибутов и `<signature>`. Что не выводится из структуры, несёт маленькая пометка в тексте (ниже);
штатному набору их нужно немного.

## Пример

XML (FL `2.183 Dual Convoy.xml`):
```xml
        <cue name="L2M183 Offer">
          <condition>
            <cue_completed cue="L2M183"/>
          </condition>
          <action>
            <do_choose>
              <do_when value="{object.exists@L2M183.StartStation1}*{object.exists@L2M183.EndStation2}" exact="0">
                <set_group_invincible group="L2M183.StationGroup" invincible="0"/>
                <cancel_cue cue="L2M183"/>
              </do_when>
              <do_otherwise>
                <set_value name="this.SectorChanges" exact="0"/>
                <set_value name="L2M183 Offer.PageID" exact="3383"/>
                <create_actor name="L2M183 Offer.Guide" race="{value@L2M183.OfferRace}" voiceflags="32" object="L2M183.OfferStation" location="crew"/>
                <create_offer actor="L2M183 Offer.Guide" conversation="L2OfferNormal" discipline="XFXX " cue="L2M183 Offer"/>
              </do_otherwise>
            </do_choose>
          </action>
          <cues>
            <cue name="L2M183 Briefing">
```
Текст:
```c
  cue "L2M183 Offer" {
    condition {
      cue_completed(L2M183);
    }
    action {
      if ({object.exists@L2M183.StartStation1}*{object.exists@L2M183.EndStation2} == 0) {
        set_group_invincible(L2M183.StationGroup, invincible: 0);
        cancel_cue(L2M183);
      } else {
        this.SectorChanges = 0;
        "L2M183 Offer.PageID" = 3383;
        create_actor("L2M183 Offer.Guide", race: $L2M183.OfferRace, voiceflags: 32, object: L2M183.OfferStation, location: crew);
        create_offer("L2M183 Offer.Guide", conversation: L2OfferNormal, discipline: "XFXX ", cue: "L2M183 Offer");
      }
    }
    cue "L2M183 Briefing" {
```
(Значение `<do_when>` здесь укорочено; настоящие строки длиннее в обеих формах.)

**Размер на штатных наборах** (все файлы, замерено через `dis`):

| | символов XML | символов текста | | токенов, оценка | | строк |
|---|---|---|---|---|---|---|
| AP, 88 файлов | 5 287 417 | 3 269 825 | **1,62×** | 1 118 095 → 778 663 | **1,44×** | 70 970 → 62 307 |
| FL, 85 файлов | 3 219 913 | 2 178 627 | **1,48×** | 848 313 → 598 031 | **1,42×** | 51 591 → 45 858 |

Токены оценены как серии букв, серии цифр и отдельные знаки препинания (токенизатора под рукой не было); пробелы
не считаются. Пол задаёт само содержимое: в FL одни значения атрибутов, комментарии и подписи — около 1,07 млн
символов, треть XML, и текст хранит их все. Оставшаяся структура (имена элементов, имена непозиционных атрибутов) —
то, что читателю нужно. Отступы — около пятой части символов текста, но в токенах почти ничего не стоят.

## Устройство файла

```
#eol crlf                    концы строк CR LF (по умолчанию LF)
#bom                         файл начинается с UTF-8 BOM
#encoding utf-8              кодировка (по умолчанию ISO-8859-1, как написано в XML-декларации)
#prolog "<?xml ...?>\n"      всё до <director>, если это не обычные две строки
#indent "\t"                 шаг отступа XML (по умолчанию два пробела; dis выбирает лучший)
#end ""                      текст после </director> (по умолчанию один конец строки)
#close "..."                 пробелы перед </director>, если необычные
#director test               <director name="test" xmlns:xsi=... xsi:noNamespaceSchemaLocation="director.xsd">
#root director(name: x)      любой другой корневой тег, атрибуты аргументами
```
Директивы идут первыми, по одной на строку; dis пишет только отличные от умолчания. Дальше — содержимое
`<director>`: `documentation { ... }`, cue, `signature "...";`. Обычный пролог —
`<?xml version="1.0" encoding="ENC" ?>` и `<?xml-stylesheet href="director.xsl" type="text/xsl" ?>`.

Текст в UTF-8; `asm` кодирует XML в кодировке файла. Отступы в тексте свободные (dis ставит два пробела), значат
только скобки. Пустые строки сохраняются: пустая строка в тексте — пустая строка в XML.

## Операторы

**Общий элемент** — так работает любой элемент схемы:
```c
name;                                   <name/>
name(args);                             <name a="..." b="..."/>
name(args) { операторы }                <name ...> дети </name>
name(args) {}                           <name ...></name>
name(args) "text";                      <name ...>text</name>   (только signature "...";)
```
Аргументы сохраняют порядок атрибутов XML. `key: value` называет атрибут; *позиционный* атрибут элемента можно
писать первым без имени, если он первый в XML (`cancel_cue(L2M183)` — это `cue="L2M183"`,
`check_value({player.age} >= 10s)` — `value=...`). Позиционный атрибут элемента — тот, что стоит первым не меньше чем
в 80 % штатных использований (exact/min/max/list/comment/chance — никогда): `name` у `cue`, `set_value`, `find_*`,
`create_actor`, `param`; `value` у `check_value`, `do_if`, `do_when`; `cue` у `cancel_cue`, `reset_cue`,
`cue_completed`; `object`, `group`, `actor`, `sector`, `race` у команд над ними; и так далее —
`x3kc/data/md_schema.json`, поле `pos`.

**Сравнения** после позиционного `value`:

| текст | атрибуты |
|---|---|
| `X == v` | `value="X" exact="v"` |
| `X != v` | `value="X" exact="v" negate="1"` |
| `X >= a` / `X <= b` | `min="a"` / `max="b"` |
| `X in [a, b]` | `min="a" max="b"` |

Идут в порядке атрибутов (`X >= a, list: ...`) и смешиваются с именованными аргументами. После любого другого
позиционного атрибута сравнение — ошибка (иначе `set_value(X == 3)` присвоил бы `X`).

**Сахар**

| текст | XML |
|---|---|
| `cue NAME(args) { ... }` | `<cue name="NAME" ...>` (name первым; иначе `cue(args)`) |
| cue прямо в теле cue (или на верхнем уровне) | обёртка `<cues>`; cue должны стоять подряд, а если в теле есть явный блок `cues { }`, cue пишутся в него |
| `action { a; b; }` (два элемента и больше; комментарии не считаются) | `<action><do_all> a b </do_all></action>` |
| `action { a; }`, `action { //c a; }` | `<action> a </action>` |
| `@action { a; b; }` | `<action> a b </action>` (без неявного `<do_all>`) |
| `if (args) { ... }` / `if (args);` | `<do_if args>` |
| `if (...) {} else if (...) {} else {}` | `<do_choose><do_when>...<do_when>...<do_otherwise>` |
| `N = V;` | `<set_value name="N" exact="V"/>` |
| `N += V;` `-=` `*=` `/=` `:=` | `<set_value name="N" operation="add|subtract|multiply|divide|set" exact="V"/>` (пробелы не обязательны: `N+=1;`) |
| `N += V ~swap;` | то же, но в XML `exact` стоит перед `operation` |
| `N++;` / `N--;` | `<set_value name="N" operation="add|subtract"/>` |
| `N = random(a, b);` | `<set_value name="N" min="a" max="b"/>` |
| `N = V, chance: 50;` | дальнейшие атрибуты после присваивания |
| `N = V;` внутри `params { }` | `<param name="N" value="V"/>` |
| `stmt; /// text` (или `{ /// text`) | последний атрибут `comment="text"` |
| `$X` внутри значения | `{value@X}` |
| `@X` внутри значения (не после буквы, цифры, `_` или `}`) | `{param@X}` |

`do_choose` с одним `do_when` и больше ничем остаётся `do_choose { do_when(...) {...} }` (иначе читался бы как
`do_if`). `N` может быть строкой: `"L2M183 Offer.PageID" = 3383;`.

**Значения.** Значение без пробелов и знаков остаётся голым: буквы, цифры и `_ . { } @ | + - * / % ? $`
(`{player.sector}`, `this.X`, `{player.age}+600`, `default|terran`). Остальное — строка: `"..."` с C-экранами
`\" \\ \n \r \t \xHH`. Общий синтаксис выражений `{...}` не меняется; сокращаются только `$X` и `@X`, и внутри строк
тоже. `r"..."` — сырая строка: без раскрытия `$`/`@` (dis ставит её, когда в значении есть буквальный `$` или
`@имя`, не являющееся параметром). Сущности XML в тексте раскрыты и записываются обратно как `&amp; &lt; &quot;`;
символ, которого нет в кодировке файла (длинное тире в файле ISO-8859-1), пишется ссылкой `&#x2014;`.

**Текст элемента** (он есть только у `<signature>`) — тоже строка: `"..."` — раскрытый текст, записывается обратно
с `&amp; &lt;`; `r"..."` — текст XML дословно. Строка-оператор в любом другом месте — ошибка.

**Комментарии.** `//text` — `<!--text-->` (текст как есть, пробел не добавляется); `/*text*/` — комментарий на
несколько строк; `//~"..."` — комментарий экранированной строкой (если его концы строк не такие, как у файла).
`//`-комментарий на строке предыдущего оператора — комментарий на строке предыдущего тега в XML. `///` — атрибут
`comment` (выше). XML запрещает `--` внутри комментария и `-` в конце, а символ, которого нет в кодировке файла,
комментарий хранить не может: asm сообщает об этом ошибкой.

## Пометки раскладки

Редкие, `dis` пишет их только там, где структура не предсказывает байты. Отступ узла предсказывается как у
предыдущего соседа, у первого ребёнка — как у родителя плюс шаг (таб, если родитель отбит табом).

| пометка | смысл |
|---|---|
| `~"\t\t\t" stmt` | отступ узла не совпал с предсказанием (следующие соседи идут за ним) |
| `~~"..." stmt` | точный текст перед узлом (пробелы в конце строки, нет переноса) |
| `~"..." }` / `~~"..." }` | то же для закрывающего тега |
| `~>" " }` | пробелы внутри закрывающего тега: `</cue >` |
| `name(a: 1, ~"  " b: 2)` | пробелы перед атрибутом (два пробела, перенос строки...) |
| `name(a: 1 ~" ")` | пробелы перед `>` или `/>` |
| `` name(`  a = 'x'`) `` | атрибут дословно (одинарные кавычки, пробелы вокруг `=`) |
| `N += V ~swap` | `exact` стоит перед `operation` |

Пометки (и `#indent`, `#end`, `#close`) принимают только пробельные символы; иное — ошибка.

На штатных наборах пометки в основном в файлах, правленных вручную табами (FL: около 1 100 `~"..."` и 50
`~~"..."` на 85 файлов).

## Проверки asm

Ошибки (`file:line: error: ...`, этот файл не пишется):
- `unknown element X`, `X has no attribute Y`, `attribute Y given twice` — по таблице элементов;
- `missing } for the block opened at line N`, `unbalanced '}'`, `expected ';' or '{' after X` (на строке
  оператора, где её нет),
  `expected ',' or ')' in the arguments of X`;
- `X: positional argument not allowed here (use name: value)` — второй безымянный аргумент или после именованного;
- `'else' without 'if'`, `expected '{' after else`;
- `the sub-cues of a cue must stand together: X at line N is between them`, `cue has two <cues>: ...` (cue и в
  блоке `cues { }`, и прямо в теле);
- `a comparison follows only a positional value (here: name)`, `a /// comment and comment: on one element`,
  `the target X+ ends with '+': write the operator with =`;
- `an XML comment cannot contain '--' or end with '-'`, `'—' (U+2014) cannot be written in ISO-8859-1 in a
  comment`, `text is allowed only in signature`, `the ~ mark takes whitespace only`;
- `unknown escape`, `unterminated string`, `unknown directive #X`, `missing #director line`.

Предупреждения (`file:line: warning: ...`, XML пишется):
- `X refers to cue 'Y', which this file does not define` — `cue="..."` называет cue, которого в файле нет (значения с
  `{...}`, `this...`, `parent...` пропускаются). Cue других файлов тоже дают его: на штатных наборах 41 (AP) и 25
  (FL); 8 и 7 из них называют cue, которого нет ни в одном штатном файле издания (`L0M81 Cleanup Actor`,
  `L0M82 Cleanup Actor`, `L2M145 Mission Reward`, `Destruction And Removal`, `L0M15`, в AP ещё
  `L2M023.A5 Armistice Goner Cease Fire Offer`) — дальше не проверялось, кандидаты на разбор.

## Схема

`x3kc/data/md_schema.json` перечисляет все элементы с атрибутами и позиционным атрибутом. Её строит
`x3kc md schema` из `director.xsd` + `dirobjdb.xsd` AP и FL (атрибуты, группы атрибутов, расширения типов
раскрыты) вместе с тем, что используют штатные файлы: 446 элементов. Дети по схеме не проверяются.

## Подпись

`<signature>` хранится дословно (`signature "...";`). Пересчитать её нельзя: загрузчик режиссёра (KC
`CueDirector.LoadFile` в x3story) проверяет её через `VerifyData(signature, внутренний текст <cues> + имя файла)` —
подпись RSA-2048 PKCS#1 v1.5 над MD5 (Windows CryptoAPI) против открытого ключа, зашитого в игру (`GetPubKey`);
закрытый ключ у Egosoft. Значит, подпись верна, пока байты внутри `<cues>` и имя файла не менялись — что
`md dis`/`md asm` без правок гарантируют. Если правленый файл проверку не прошёл: уже «модифицированная» игра грузит
его всё равно; немодифицированная грузит обязательный файл (из `mdfiles`) и помечает себя модифицированной, а
необязательный пропускает.
