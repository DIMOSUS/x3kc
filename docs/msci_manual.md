# Writing MSCI scripts

A manual for writing working MSCI scripts (the "script editor" scripts, `addon/scripts/*.xml`) for *X3: Terran
Conflict*, *X3: Albion Prelude* (AP 3.3) and *X3: Farnham's Legacy* (FL 1.3), written so that a person or a language
model can produce a correct script from it alone. Everything marked **(code)** was read from the game's own
interpreter (`ScriptObject::__runScript` in `x3story.obj`) and the script loader, and most of it was run through that
interpreter in x3kc's emulator. Nothing here was taken on trust from older guides; where they disagree, the
difference is listed in [Older guides](#what-older-guides-get-wrong).

[Русская версия](ru/msci_manual.md) · Command reference: [AP](msci_commands_ap.md), [FL](msci_commands_fl.md) ·
File format and encoding: [msci.md](msci.md)

**Contents.** [Rules in brief](#rules-in-brief) · [Worked example](#worked-example) · [Games and versions](#games-and-versions)
· [Files and loading](#files-and-loading) · [The text form](#the-text-form) · [Language](#language) ·
[Objects](#objects-and-this) · [Texts](#texts-t-files) · [Commands, signals, tasks, AL, saves](#commands-signals-tasks-al-saves)
· [Command reference](#reading-the-command-reference) · [Pitfalls](#pitfalls) · [Testing](#testing-without-the-game)
· [Checklist](#checklist-before-shipping) · [Older guides](#what-older-guides-get-wrong) · [Limitations](#limitations)

## Rules in brief
1. A script is a loose file `addon/scripts/<name>.xml` (AP; FL: `addon2/scripts`); `<name>` inside must equal the
   file name. Scripts in catalogues are never loaded, and a `<name>.pck` beside it wins over your `.xml`. The game
   runs only the `<codearray>`; write the script as editor text and build it with `x3kc msci new`.
2. **An unsigned script loads only in a "modified" game** (code). A name starting with `!` loads anyway and makes
   the game modified. In practice: enable the script editor (`Thereshallbewings`) and reload a save.
3. `setup.*` runs on every new game and every load; `init.*` and `!init.plugin.*` run when scripts are (re)loaded.
   Order between scripts of one kind is not defined.
4. There is no time slicing: a loop without a waiting command (`@` in the reference: `wait`, flight commands,
   `call script`) freezes the whole game until it ends. Put `@ = wait N ms` in every long loop.
5. Integers are 32-bit and wrap silently; `<`/`>` on numbers more than 2³¹ apart give the wrong answer; division
   and `mod` by zero give `null`; `-2147483648 / -1` crashes the original game. A false comparison stores `null`,
   not 0.
6. Test objects with `-> exists`. `if $ship` is true for a destroyed ship, `$ship == null` is false for it.
7. A value of the wrong type is `null` without an error: `'a' - 1`, `$array[99]`, a command on a dead object.
   Unknown commands (an FL command on AP) are skipped, their result variable is not written.
8. `call script` runs in the caller's task and waits for it; `START` makes a new task, and on a ship or station
   it **replaces the object's current command** (slot 0).
9. Write texts in a language file `t/NNNN-L044.xml`, load it with `load text: id=NNNN` in your setup script (the
   game loads only its own files by itself) and read them with `read text` / `sprintf: pageid=…`.
10. Before shipping: build, `dis` it back, run it through the interpreter (`x3kc.kcmsci`), check the
    [checklist](#checklist-before-shipping).

## Worked example
A hotkey that greets the player with name, money (with thousands separators) and the number of ships in the sector.
Sources: [docs/examples/msci_hello](examples/msci_hello).

| File | Role |
|---|---|
| `lib.x3kc.hello.thousands.txt` | library: `1234567` → `'1,234,567'`; loops, `mod`, `gosub`, string building |
| `plugin.x3kc.hello.txt` | hotkey target: player data, `call script` of the library, `sprintf` from a text page, subtitle |
| `setup.x3kc.hello.txt` | runs on every new game and load: loads the texts, registers the hotkey |
| `8870-L044.xml` | English texts, page 8870 |

The library, as written (the `//` lines are the header: name, description, version, arguments):
```
// lib.x3kc.hello.thousands  (x3kc example: a number with thousands separators, 1234567 -> '1,234,567')
// version: 1
//   $value: Var/Number  "Value"
* Library script: no [THIS], no waiting, returns a string.
$neg = $value < 0
if $neg
  $value = -$value
end
$out = ''
$first = [TRUE]
while $first OR $value > 0
  $first = [FALSE]
  $part = $value mod 1000
  $value = $value / 1000
  if $value > 0
    gosub pad3
  else
    $part.text = convert number $part to string
  end
  if $out == ''
    $out = $part.text
  else
    $out = $part.text + ',' + $out
  end
end
if $neg
  $out = '-' + $out
end
return $out

* $part.text = $part as three digits
pad3:
$part.text = convert number $part to string
skip if $part >= 100
  $part.text = '0' + $part.text
skip if $part >= 10
  $part.text = '0' + $part.text
endsub
```
`convert number … to string` makes the type explicit; `'' + $part` would give the same string (`+` with a string on
either side concatenates). The script is wrong for exactly one input, `-2147483648`, and the test keeps it that way
as a demonstration: `-$value` of the smallest integer is itself, so the result is `'--648'`.

The hotkey target and the setup script:
```
// plugin.x3kc.hello  (x3kc example: the hotkey target, greets the player)
// version: 1
* Runs as a global script when the hotkey is pressed.
$name = get player name
$money = get player money
$money.text = [THIS] -> call script lib.x3kc.hello.thousands :  Value=$money
$ship = get player ship
$count = 0
$sector = $ship -> get sector
if $sector -> exists
  $ships = $sector -> get ship array from sector/ship/station
  $count = size of array $ships
end
$text = sprintf: pageid=8870 textid=2, $name, $money.text, $count, null, null
display subtitle text: text=$text duration=5000 ms
return null
```
```
// setup.x3kc.hello  (x3kc example: loads the texts, registers the hotkey; runs on every new game and every load)
// version: 1
* The game loads only t/0001, 0003 and 0004 by itself (FL: 0001, 0003, 0004, 0005 ... while they exist):
* load our own file t/8870-L044.xml. Ids 1..9999; the language variant -L<lang> is preferred.
load text: id=8870
* Registering the same script again reuses its hotkey id, so this is safe on every load.
$hotkey = register hotkey: page=8870 id=1, to call script plugin.x3kc.hello
set global variable: name='x3kc.hello.hotkey' value=$hotkey
return null
```
The texts (`\(` is a real parenthesis; an unescaped `(…)` would be dropped as a comment):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<language id="44">
<page id="8870" title="x3kc MSCI example" descr="texts of the x3kc hello example">
<t id="1">x3kc: Hello</t>
<t id="2">Hello, %s! You have %s credits; %s ships are in this sector. \(x3kc example\)</t>
</page>
</language>
```

**Build** (one command database per game; `msci-setup` reads it from your installation):
```
x3kc msci-setup "C:/Games/X3 Terran Conflict" work                  AP: work/msci_db.json
x3kc msci new work/msci_db.json lib.x3kc.hello.thousands.txt -o out/lib.x3kc.hello.thousands.xml
x3kc msci new work/msci_db.json plugin.x3kc.hello.txt -o out/plugin.x3kc.hello.xml
x3kc msci new work/msci_db.json setup.x3kc.hello.txt -o out/setup.x3kc.hello.xml
x3kc msci dis work/msci_db.json out/*.xml                            must print the sources back
```
Build the library first: a `call script` line names the callee's arguments by their descriptions (`Value=$money`),
and `new` reads them from the XML files in the output directory (or `--scripts DIR`). For FL use
`x3kc msci-setup "<TC dir>" work-fl --addon addon2` and that database.

**Check without the game** — what `tests/test_msci_examples.py` does (it passes on AP and FL):
```python
from x3kc import kcmsci, kcobj, kcvm, msci
vm = kcvm.VM(kcobj.load('work/x3story.obj'))       # the game's own script code
m = kcmsci.Msci(vm, db)                            # db = json of work/msci_db.json
lib, _ = msci.read_script('out/lib.x3kc.hello.thousands.xml')
r = m.run(lib, args=kcmsci.to_cells([4, 1234567])) # 4 = int
assert (r.type, r.value) == (5, '1,234,567')       # 5 = string
```

**Install:** copy the three XML files to `addon/scripts/` (FL: `addon2/scripts/`) and `8870-L044.xml` to `addon/t/`
(FL: `addon2/t/`). Make the game modified (type `Thereshallbewings` as the pilot name, then save and load), bind
"x3kc: Hello" in the controls menu (Interface → extensions), press it. **Not yet run in the game.**

## Games and versions
| | TC | AP 3.3 | FL 1.3 |
|---|---|---|---|
| exe | `X3TC.exe` | `X3AP.exe` | `X3FL.exe` (in the TC directory) |
| data, scripts | game dir, `scripts/` | `addon/`, `addon/scripts/` | `addon2/`, `addon2/scripts/` |
| commands | — (not measured) | 828 | 2 507 (1 679 new, 61 with other parameters) |
| `script engine version` | — | 54 | 73 |
| x3kc | `msci-setup <dir>` | `msci-setup <dir>` | `msci-setup <TC dir> --addon addon2` |

- The loader accepts any `<engineversion>` ≥ 40 (code); `x3kc msci new` writes 40 unless `// engine: N`.
- **Ids are shared, parameter lists are not always.** FL keeps every AP id, but 61 commands read different
  parameters (`write to player logbook: printf` and `send incoming message` gained a message type; array reads and
  writes also take tables). A script built against one game's database is not portable for those ids: rebuild the
  text with the other game's database. The FL reference marks them `sig: AP had …`.
- **FL editor texts.** FL ships editor texts for only ~800 of its 2 507 commands. `msci-setup` learns the rest from
  the text the shipped FL scripts saved (`msci learn`): 681 commands, 270 constant, class and command names; the
  decoder then matches 99.7 % of the lines FL saved, the assembler rebuilds 853 of 1 020 FL scripts from that text (all 1 020 from `dis` output).
- **Synthetic texts.** The 1 000 FL commands no shipped script uses get a text made from their signature:
  result, object, then `fl.<id>` and the parameters, e.g. `$x = $hq -> fl.1789: $type`. They are not the game
  editor's texts, but `dis` prints them and `new`/`asm` read them, so every FL command can be written. With
  `msci-setup … --xscript x3fl.xml` (your copy of [XScriptCompiler](https://github.com/Cycrowuk/XScriptCompiler)'s
  command list) the name is XScriptCompiler's function name instead (`$hq -> hqObjRemoveBlueprint: $type`). The
  reference tags them `synthetic`; what each one does is in its `Does` line.

## Files and loading
**File.** XML with the script array in `<codearray>`; see [msci.md](msci.md) for every cell. What matters when
writing one:
- `<name>` and `codearray[0]` equal the file name without extension; otherwise the script is not loaded (code).
- `codearray[1]` (engine version) ≥ 40, `codearray[4] & 1` clear; else not loaded (code).
- `<sourceplaintext>`/`<sourcetext>` are for people and the editor; the game ignores them. `<signature>` is
  checked against the code.
- `.pck` is a gzip-packed `.xml` (`x3kc unpck`). How a script file is found (code: `LoadAllScripts`, exe
  `SE_GetDir`, `SE_ReadFile`, `archive::resolveName`):
  1. the list of scripts is the **directory listing on disk** of `addon/scripts` (`_findfirst`): `*.xml` and
     `*.pck`, extension cut off. **A script that exists only inside a catalogue is never loaded.**
  2. each name is read as `addon/scripts/<name>.xml`, which the file system resolves with the extension list
     `pck xml`: loose files first (any loose match beats every catalogue), a language variant `<name>-L044.*` before
     a plain one, then the earlier extension — **`<name>.pck` beats `<name>.xml`**. To replace a stock script, rename
     its `.pck` away (e.g. `.pck.bak`) and put your `.xml` there.
  3. only when no loose file matches are the catalogues searched, newest first (so a catalogue copy is used only for
     a name that is listed on disk but has no loose file of a wanted extension — in practice never).

**Signature and "modified"** (`ScriptDatabase.LoadScript`, same in FL):

| script | game not modified | game modified |
|---|---|---|
| signed | loaded | loaded |
| unsigned, name without `!` | **not loaded** | loaded |
| unsigned or missing, name with `!` | loaded, game becomes modified | loaded |

The game becomes modified by: typing `Thereshallbewings` (sets the script-editor feature, marks modified, reloads
the scripts), loading a save while the script editor is on (`_Restart`), a failed check of the game files
(`x3files.xml`), an unsigned `!` script, or (for Mission Director files) `addon/director/modified.txt`. If a file
`modified.txt` exists in the game's directory, every reason is appended to it — the quickest way to see why a
game is modified.

**When scripts run** (code, `ScriptDatabase` and `_Restart`; FL the same):

| event | what runs |
|---|---|
| client start | all scripts loaded; the command maps rebuilt by `!init.{ship,station,wing}.*`; `init.*`, `!init.plugin.*` run |
| new game, after the universe is built | `setup.*`, then `!setup.*`; then `al.plugin.*`, `!al.plugin.*` started as tasks, AL `init` events |
| loading a save | all scripts reloaded from disk; command maps rebuilt; `init.*`, `!init.plugin.*`; later `setup.*`, `!setup.*`; `al.plugin.*` started again, AL `reinit` events |
| `Thereshallbewings` typed | all scripts reloaded (no init/setup run) |
| editor "reinit script caches" | command caches, `init.*`, then `setup.*` |

- `init.*` and `setup.*` are run synchronously, one after another, in the order of the script table (a hash
  order, not alphabetical). Do not depend on another init script having run.
- Running `setup.*` on every load is normal: write setup scripts so that a second run does no harm (re-registering
  the same hotkey or command reuses it; keep a version in a global variable when you must run something once).
- `al.plugin.*` and `!al.plugin.*` are started (as separate tasks, not synchronously) by `AlPlugins.Init` /
  `Reinit` after the setup scripts (code); such a loader script should only `al engine: register script=…`.

## The text form
x3kc reads and writes the text the in-game editor shows (`<sourceplaintext>`): one command per line.
- **Indentation** is ignored. `@` at the start marks an interruptible command; it is printed by `dis` and ignored
  by the assembler (whether a command is interruptible comes from the database).
- **Comments:** `* text` is a comment line; `*` before a command is a commented-out command (kept, not run).
  An empty line is kept as an empty editor line.
- **Header** (`//` lines, read by `msci new`, skipped by `msci asm`):
  `// <name>  (<description>)`, `// version: N`, `// engine: N`, `// command: N` (the script command, a
  number or a quoted name; optional), and one `//   $<arg>: <type>  "<description>"`
  per argument, in order. Types are names from text page 2000: `Var/Number`, `Var/String`, `Var/Ship`,
  `Var/Station`, `Var/Ship/Station`, `Var/Sector`, `Var/Ware`, `Var/Race`, `Var/Class`, `Var/Array`, `Var/Boolean`,
  `Value` (anything) … The description is what `call script` lines show (`Value=$money`).
- **Result:** `$x = <command>` stores the result; `= <command>` discards it (the editor shows `=` for "no
  result"); a command with a `RetVar/IF` result may stand after `if`, `if not`, `else if`, `else if not`,
  `while`, `while not`, `skip if`, `do if` instead of a variable.
- **Values:**

| write | is |
|---|---|
| `42`, `-7` | integer |
| `'text'` | string (no escapes; `'` cannot occur inside — use a text page) |
| `null` | null |
| `$name` | variable: letters, digits, `.`, `_` |
| `[THIS]`, `[PLAYERSHIP]`, `[TRUE]`, `[FALSE]`, `[SECTOR]`, `[OWNER]`, `[HOMEBASE]`, `[ENVIRONMENT]`, `[DOCKEDAT]`, `[MAX]`, `[Find.Nearest]`… | constant (page 2002) |
| `Argon Mercury`, `Energy Cells` | ware, ship or station type: its name in the database's language |
| `Argon`, `Player` | race |
| `Ship`, `Fighter`, `Station` (page 2006) | object class |
| `Friend`, `Neutral`, `Foe` | relation |
| `COMMAND_ATTACK`, `SIGNAL_ATTACKED` (page 2008) | object command / signal |
| `Var/Number` (page 2013) | data type (for `is datatype[…] ==`) |

  Names are matched against the database built for one language (`msci-setup --lang 044` = English). If a name is
  both a ware and a class, the parameter type decides; ambiguous names are listed in [Limitations](#limitations).
- **Script names** are written bare: `call script lib.x3kc.hello.thousands`, `to call script plugin.x3kc.hello`.
- **Call arguments** are `<description>=<value>` separated by spaces, in the callee's order; trailing ones may be
  left out (they arrive as `null`). For an unknown callee the editor writes `???=value` separated by two spaces.
- **Labels:** `name:` defines one, `goto label name`, `gosub name`, `endsub`.
- **Expressions** (a line `$x = <expression>` or `if <expression>`): operators separated by spaces, as the editor
  prints them: `$a + 1`, `( $a + 1 ) * 2`, `$a == 3 AND $b < 5`, `not` is written `!` or `if not`. An operand
  must be followed by a space or `)`; `$a+1` does not parse.

## Language

### Variables and scope
- `$x` variables belong to one run of one script: a called script starts with all variables `null` except its
  arguments (code: a fresh variable array per call). There are no globals among them.
- **Arguments** are the first variables; the caller's values are copied in, extra ones dropped, missing ones `null`.
- **Object variables:** `<obj> -> set local variable: name='key' value=…`, `<obj> -> get local variable: name='key'`.
  **Global variables:** `set global variable: name='key' value=…`, `get global variable: name='key'`. Both are
  kept in KC objects and therefore in the savegame; setting `null` deletes the key. Prefix keys with your mod name.
- Arrays are references: `$b = $a` shares the array, arguments pass the reference, a global variable can hold one.
  `clone array` copies.

### Data types
A variable holds a pair (data type, value). The common types: `null` (0), integer (4), string (5), ship (65542),
station (65543), sector (65544), ware (9), race (10), object class (12), relation (14), object command (18),
data type (20), array (21); other objects have their own types (65553 generic object, 65559 wing …). `datatype[
$x ]` returns the type, `is datatype[ $x ] == Var/Number` tests it. There are no floating-point numbers in
scripts; some commands use fixed-point (`fixed sin` …).

### Expressions
Computed by the native `SE_ScriptComputeExpression` (behaviour as in the exe, checked in the emulator):

| precedence (low → high) | operators |
|---|---|
| 1 | `OR` |
| 2 | `AND` |
| 3 | `\|` |
| 4 | `^` |
| 5 | `&` |
| 6 | `==` `!=` |
| 7 | `<` `>` `<=` `>=` |
| 8 | `+` `-` |
| 9 | `*` `/` `mod` |
| 10 | unary `-` `~` `!` |
| 11 | `( )` |

- **Results:** a true comparison or logical operator gives integer 1, **a false one gives `null`** (type 0), not
  0: after `$c = 1 > 2`, `$c == 0` is false. Test results with `if $c` / `if not $c`.
- **Integers** are 32-bit signed and wrap: `2147483647 + 1` = `-2147483648`.
- **Comparisons** subtract and test the sign of the 32-bit difference: `2000000000 > -2000000000` is **false**.
  Compare values that can be far apart with care (subtract and test ranges yourself).
- **Different types** are never equal: `null == 0` is false, `$s == null` is false when `$s` is `''`; only `!=`
  is true for them. Objects compare only with `==`/`!=` (`<`, `>` are false).
- **Strings** compare byte by byte, case-sensitive (`'abc' == 'ABC'` is false; `'abc' < 'abd'` is true).
- `+` adds two integers; anything else is concatenated as printed text by the KC method
  `ScriptObject.__scriptOpAddString` (`'abc' + 5` = `'abc5'`, `'' + 5` = `'5'`, `null` prints as the game's text for
  null, so `'a' + $unset` is `'anull'`, and even `null + 1` is a string). Build strings from strings and numbers you
  know are set.
- `-`, `*`, `/`, `mod`, `&`, `|`, `^`, `~` and unary `-` work on integers only; on anything else the result is
  `null`.
- `/` truncates toward zero (`-7 / 2` = `-3`); `mod` keeps the sign of the left operand (`-7 mod 3` = `-1`);
  **division or `mod` by zero gives `null`**; `-2147483648 / -1` and `-2147483648 mod -1` **crash the original
  game** (unguarded `IDIV`, bugs.md B9; the rebuilt exe of x3_src no longer crashes).
- `AND`, `OR` give 1 or `null`; `AND` with `null` on either side is false. Both sides are always evaluated.
- `return` takes one value, not an expression: compute into a variable first (`$r = $a + 1`, `return $r`).
- **Truth** inside an expression: `null` and integer 0 are false, an object is true only if it exists, every
  string (also `''`) and array is true. See [Pitfalls](#pitfalls) for `if $obj` without an operator.
- An expression has room for 50 values on its stack and 50 operands; longer ones crash the exe. Split them.

### Control flow
```
if <cond>            else if <cond>        else        end
if not <cond>        else if not <cond>
while <cond>         while not <cond>      break       continue     end
skip if <cond>       (skips the next line when true)
do if <cond>         (runs the next line only when true; old name "skip if not")
name:                goto label name       gosub name  endsub
```
- A condition is an expression or any command whose result is `RetVar/IF` (marked `condition` in the reference):
  `if $ship -> exists`, `while not $done`, `skip if [THIS] -> is docked`.
- **A command used as a condition is false when its result is `null` or integer 0, true otherwise** (code). An
  object result is true even if the object no longer exists.
- `gosub` pushes the return line, `endsub` returns; there are no arguments or local variables. At 500 nested
  `gosub` the interpreter kills the task. An `endsub` without `gosub` does nothing.
- There is no `for`; use `while` and `inc $i =` / `dec $i =`.

### Calling scripts
`$r = <obj> -> call script <name> : <args>` (`<obj>` is `[THIS]`, `null` or a variable):
- **Synchronous, same task** (code): the caller waits; the callee gets a new frame with fresh variables,
  `[THIS]` = `<obj>`, the caller's priority. `return <value>` gives the value back; a script that ends without
  `return` … always end with `return` (the last line runs into the end of the array otherwise).
- The call is made only if `<obj>` is `null`, or an **existing ship or station** (code). On any other object
  (sector, asteroid, wing …) or a destroyed one, the script is not called and the result is `null`. An unknown
  script name also gives `null`.
- `START <obj> -> call script …` starts a **new task** and does not wait: on a ship or station it runs in task
  slot 0 and **stops the object's current command** (code: `StopThisScriptID(0)`), on other objects or `null` it
  is a separate global task.
- Calls nest without a fixed limit (frames grow); each level keeps its own variables and `gosub` stack.

### Waiting, tasks and the scheduler
- The script VM has **no time slices and no instruction limit** (code; `SE_SetMaxOpCount` is a no-op in the exe).
  A task runs until it waits (`wait`, flight commands, an interruptible command, a `call script` that waits) or
  ends. A busy loop freezes the game. Commands marked `@`/interruptible in the reference may suspend the task.
- `infinite loop detection enabled=[TRUE]` (command 153) turns on a watchdog for that task: every 10 000 commands
  it checks the game clock and, if no time passed, prints "Infinite loop detected" and ends the script. It is off by
  default.
- `wait N ms` and `wait randomly from A to B ms` count **game time** and end on the frame tick after it is due, so
  the shortest wait is one frame (exe `TI_WaitForInterrupt`, `TiTick`). `$r = wait N ms` gives 0 when the time
  ran out and the **remaining milliseconds** when it was cut short. A wait is keyed to the object (`[THIS]`), and
  any `interrupt` of that object ends the waits of **all** its tasks early — wait again for the remaining
  milliseconds when the exact duration matters.
- `get task ID`, `get script priority`, `set script priority to N`.

### Interrupts and signals
- `<obj> -> interrupt with script <name> and prio N` posts a script into the object's command task; it is
  accepted only if **N is greater than the running script's priority** and interrupt handling is on
  (`enable signal/interrupt handling: [TRUE]`). The waiting task is woken (a running `wait` ends early); the
  interrupt runs as a call at the next command after the interruptible one, then the script continues.
- Signals (`SIGNAL_ATTACKED`, `SIGNAL_KILLED` …) are object commands delivered the same way; who handles them is
  set with `global script map: set: key=SIGNAL_…, class=…, race=…, script=…, prio=…` (stock: the
  `!init.*.globalscriptmap*` scripts) or per object with `connect ship command/signal … to script … with prio …`.

### Errors
There is no exception mechanism. What happens instead (code, emulator):
- an unknown command id: skipped, its result variable keeps its old value (`default: break` in the interpreter);
- a command on `null` or a destroyed object: the method call answers 0, the result is `null`;
- reading an array past its end or at a negative index: `null`; writing there: nothing happens;
- an expression ending in an operator, a too long expression, `-2147483648 / -1`: the exe crashes or reads
  garbage — `x3kc msci new` refuses an expression that ends in an operator;
- a VM error (stack overflow, bad opcode): the task is removed silently.

## Objects and [THIS]
- `[THIS]` is the object the script runs on: the ship or station for a command or signal script, `null` for a
  global script (hotkey targets, init/setup, AL plugins). `[PLAYERSHIP]`, `[SECTOR]` (of `[THIS]`), `[OWNER]`,
  `[HOMEBASE]`, `[ENVIRONMENT]`, `[DOCKEDAT]` are computed when read.
- An object reference stays in a variable after the object is destroyed. Before using an object you did not just
  get, test `if $obj -> exists` (or `skip if not …`). Do not use `if $obj` or `$obj == null` for this.
- Commands that return objects (`find ship: …`, `get ship array …`) return `null` or an empty array when nothing is
  found; `Find.Multiple` searches return arrays.

## Texts (t files)
- Language files `t/NNNN-L044.xml` (`L044` English, `L049` German, `L007` Russian …) in the addon's `t/`
  directory, UTF-8, `<language id="44"><page id="P"><t id="I">text</t></page></language>`. Use a page number no
  one else uses (check `x3kc texts <game> 044 all.tsv`).
- **The game loads only its own text files** (code, `UiTextFonts.Set`): AP `t/0001`, `0003`, `0004`; FL `0001`,
  `0003`, then `0004`, `0005` … as long as they exist. The stock plugins' texts are inside `0001`. **Load your file
  with `load text: id=NNNN`** (ids 1…9999) in a `setup.*` script: it reads `addon/t/NNNN.xml`, which resolves to
  the loose or catalogue `NNNN-L<language>.xml|pck` first. Loaded files are reloaded by the game after a save load
  (`RestoreTexts`), and setup runs again anyway.
- Page numbers 300001–349999, 350000–379999 and 380000–399999 (FL also 390001–399999) are folded to their range
  start at import (301903 → 1903), so a page in a mod file can replace a stock page (exe `parsePageXml`).
- `$t = read text: page=P id=I`; `$s = sprintf: pageid=P textid=I, a, b, c, d, e` (up to five values);
  `sprintf: fmt=$t, …` with a format string. `%s` prints any value, `%d` a number.
- In a text, an unescaped `(…)` is a comment and is not shown; write `\(` and `\)`. `{P,I}` inserts another text.
- `write to player logbook …`, `send incoming message …`, `display subtitle text …` show texts. In FL the logbook
  and message commands take a message type first (see the FL reference).

## Commands, signals, tasks, AL, saves
Read in the KC code (AP; class and method names of the bundled annotations), checked against FL where noted.

### Task slots
A ship or station runs scripts in numbered **slots** (`ScriptObject.scriptTasks`); a slot holds one task with its
call stack. Only ships, stations and class 1700 objects own slots.

| slot | used by |
|---|---|
| 0 | the ship's main command (console commands, `START $ship -> call script`, most signals) |
| 1 … turrets−1 | turret commands |
| 10, 11 | "additional ship commands" (console, commands 1200–1263) |
| 10 … 19 | station console commands (1100–1163) |
| 16 | player ship: sector-change handler while slot 0 is busy |
| 17 / 21 | command preload scripts on ships / stations |
| 18 | secondary signal handlers (stopped and reused for every one) |
| 23 | all station signals |
| ≥ 30 | free: `get next available task: starting=N` returns the first free slot from max(N, 30) |

- `start task N with script S and prio P` (1017) stops whatever runs in slot N and starts S there;
  `interrupt task N …` (1016) interrupts it; `is task N in use` (1073). Use slots from 30 up for your own
  background tasks; slots below 30 are the game's.
- **Stopping a task runs no MSCI code**: the task and all its `call` frames are killed (`StopThisScriptID`); for
  slot 0 the ship stops, for turrets the turret. Scripts the task STARTed keep running. When the object is
  destroyed, all its tasks die without callbacks.
- **Global tasks** (`START null -> call script`, hotkeys, AL events) have no slot, `[THIS]` = null, and cannot be
  interrupted by slot interrupts; no MSCI command stops them.

### Ship and station commands
- **Lookup** (`Ship.GetCommandScript`): first the ship's own binding (`connect ship command/signal … to script …
  with prio …`, 113; `ignore ship command/signal` stores "ignored" and ends the search; `set … to global default
  behaviour` removes the binding); then the global map (`global script map: set` 118 / `ignore` 127 / `remove` 119)
  for the ship's class, its parent class and so on up to `Ship`: at each class a race-specific entry beats an
  any-race one, and a more derived class beats a less derived one whatever the race. Stations: global map only —
  113/114/115 do nothing on a station. A script name that is not found in 118 **removes** the entry.
- **What the player sees** (`CommandScriptRegistry.HasCommandUpgrade`): for player-owned objects a command appears
  only if `set script command upgrade` (144, or 154 with a check script) is set for it **and** a script is bound
  and (the upgrade is `[TRUE]` or the ship carries that ware). For other owners the bound script is enough.
- **Check script** (154): run synchronously as a global script every time the menu is built, with arg 1 = the ship
  or station and arg 2 = 1 or the upgrade ware; it must not wait. Its return value: low four bits 0 = hide,
  2 = show greyed, other = show; bits 16 and up are kept as entry flags. The `[CmdConCheck.*]` constants: Available
  1, Disabled 2, OneTime 16, Infinite 32, NeedHomeStation 64, NeedHomeBase 128, NoAudioConfirmation 256,
  WingAllShips 512, NoVoice 1024 (no spoken confirmation), NoTempRemove 2048 (the ship is not taken out of its
  wing); the AP code tests only 1, 2, 512, 1024, 2048.
- **Free ids** for mod commands (ranges the console lists and no stock code handles): ship 200–263 (navigation),
  300–363 (combat), 400–463 (trade), 500–563 (special), 600–663 (piracy), 700–763 (custom), 800–863 (general),
  1300–1363 (fleet), 1200–1263 (additional slots 10/11), 900–963 (turrets); station 1100–1163; any id ≥ 1400 that a
  script registers as a custom command (shown under Custom). Stock `!init.*` scripts already use some ids in these
  ranges — check `x3kc msci dis` of `!init.ship.globalscriptmap*.xml` and page 2010 before choosing.
- **Texts:** the menu shows page 2010 (name), 2011 (short name, in the info line) and 2022 (description) with the
  command id; page 2008 is the `COMMAND_TYPE_…` name the editor shows. Put these pages in your `t` file.
- **Running:** the console asks the player for each declared argument of the script in order (picker by its type)
  and starts the script in slot 0 (turret: its slot; station: its console slot) with the priority given in the
  binding, `[THIS]` = the ship. There is no implicit "target": it is argument 1 if you declare one. The ship's
  previous command is stopped when the script starts and again when it ends.
- **Preload** (`set ship command preload script`, 1466): runs synchronously before the argument pickers with arg 1
  = the object; return an **array** of values to pre-fill the first arguments, or `null` to cancel the command.

### Signals
Signals are command ids 1000–1019 bound like commands (`global script map: set: key=SIGNAL_…`, or per ship). Each
has its own rule (`Ship.Signal_*`):

| signal | id | arguments | how the handler runs |
|---|---|---|---|
| ATTACKED | 1000 | attacker, mode | slot 0 busy → interrupt with the bound priority; idle → started in slot 0 |
| KILLED | 1001 | killer, current command and its parameters | synchronous in slot 0, interrupts off; the object still exists |
| CAPTURED | 1004 | as KILLED | interrupt if slot 0 is busy, else synchronous |
| DOCKED | 1012 | station | player ship: synchronous in slot 0; others: interrupt |
| CHANGESECTOR | 1018 | sector | busy → player: synchronous in slot 16, others: interrupt; idle → synchronous in slot 0 |
| FORMATIONLEADERCHANGED, LEADERNEEDSHELP, FOLLOWERNEEDSHELP, CALLFORHELP, WINGREMOVE | 1003, 1005, 1006, 1008, 1017 | (leader/follower/caller, attacker …) | interrupt only: nothing happens when slot 0 is idle or the priority is too low |
| REQUESTUNDOCK, DAMAGED, SCANNED, BUY, SELL, LOAD, UNLOAD | 1009–1011, 1013–1016 | (partner, ware, count …) | interrupt when the ship has a command, else started in slot 0 |

- Stations run every signal in slot 23 (KILLED synchronously). `SIGNAL_ABORTED` (1007) is never raised and
  `SIGNAL_COLLISIONWARN` (1002) has no sender in the code.
- **A synchronous handler in slot 0 kills the command script that was running there.** An interrupt handler runs
  as a call: when it returns, the interrupted script continues at the line **after** the interrupted command (a
  flight command is not resumed; the interrupt stopped the ship).
- A second interrupt before the first one has run replaces it (one pending slot).
- **Secondary signals** (`add secondary signal: signal=… script=… prio=… name=…` per object or with `null` for all
  objects; `global secondary signal map: add …` by race and class): many handlers per signal, one per name; they run
  synchronously in slot 18, before the primary handler, with interrupts off, and do not disturb slot 0. Use them to
  observe signals without replacing the stock handler.
- `<obj> -> send signal …` (515) delivers 1 ms later; ships accept 1000, 1003, 1005, 1006, 1010–1018, stations
  1000, 1010, 1013–1016, 1019; others are ignored.

### Hotkeys
`register hotkey …` stores the hotkey **by script name** (ids from 1536; the same name gets the same id). Pressing
it starts a new global task of the script found by that name, `[THIS]` = null, argument 1 = the hotkey id, priority
0 (`InputMapper.TriggerCustomHotkey`). FL can pass a second string argument: `pressed`, `double`, `released`,
`short`, `long`.

### AL plugins
- Loader `al.plugin.<name>` (started automatically, see [When scripts run](#files-and-loading)):
  `al engine: register script=<controller>`; `al engine: set plugin <controller> description to …`, `… timer
  interval to N s`.
- The controller is started as a new global task with argument 1 = its own name and argument 2 = the event:
  `init` (new game), `reinit` (each load), `start` / `stop` (the player toggles it in the AL menu), `timer` (every N
  seconds of game age; checked once a second, so 1 s is the minimum; a new task each time, runs may overlap), and
  `isenabled` — run **synchronously** for the menu: return non-zero for "on" and do not wait.
- The engine keeps no on/off state: keep it in a global variable. Plugins whose script is gone are dropped on load.

### Savegames
- A running script is saved as a KC task with **its own copy of the code**, variables, line and call stack, and
  continues after loading with that old code even if the file changed or was removed (inferred from the code and
  the VM's save format; not checked with a save).
- Everything looked up by name at the moment it is used gets the new file: `call script`, `START … call script`,
  hotkeys, AL events, console commands. A `call` to a script that no longer exists does nothing.
- Per-ship bindings (113) and secondary signal handlers store the script itself: they keep the old code after a
  load until registered again.
- The global command map and the command upgrades are cleared and rebuilt on every load from the init/setup
  scripts: **register commands only in `init.*`/`setup.*`**, never in a script that runs once.
- Global variables, object variables, hotkeys and AL registrations are saved with the game.

## Reading the command reference
[msci_commands_ap.md](msci_commands_ap.md) and [msci_commands_fl.md](msci_commands_fl.md) are generated by `x3kc msci
ref` from the command database, the editor's command menu, the interpreter and the shipped scripts. An entry:
```
- **703** `$ret =|if <RefObj> -> get sector`
  params: %0 RefObj, %1 RetVar/IF; condition; used 287× (!fight.attack.object.bomber); calls: `.GetSector`
```
- the id and the syntax with placeholders: `$ret =` a result variable (write `$x =` or `=`); `$ret =|if` a result
  that may also be a condition; `$ret =|if/START` also `START`; `<RefObj> ->` the object (`[THIS] ->`, `$ship ->`);
  `<Var/Number>` etc. a value of that parameter type; `<arguments…>` `call script` arguments;
- `params` — parameter types in signature order (the order of the cells in the code line, not of the text);
- `not in the text: %n` — parameters the editor text does not show; they are written as `null`;
- `@ interruptible` — the command can suspend the task; `condition` — usable with `if`/`while`/`skip if`;
- `used N× (script)` — how many lines of the shipped scripts use it, and one script to read as an example
  (`x3kc msci dis work/msci_db.json work/scripts/<script>.xml`);
- `calls` — KC methods (`.Name`) and natives (`SE_…`) the interpreter calls: a hint of what it really does;
- `Does (confidence)` and the lines under it — what the command does, its result (`Returns`), parameter notes,
  effects on the game and edge cases, from reading its handler ([x3kc/data/msci_semantics_ap.json],
  `…_fl.json`); `high` = the called methods were read too, `medium` = the handler only, `low` = inferred. A
  command that is the same in FL and AP shows the AP description in the FL reference;
- `Engine bug (S…)` — the handler does not do what the text says, checked by hand
  ([x3kc/data/msci_notes.json](../x3kc/data/msci_notes.json)); `Suspected (not verified)` — a possible bug the
  reading wave noted, not checked;
- FL only: `new` (not in AP), `sig: AP had […]` (parameters changed), `text learned` (syntax recovered from the
  shipped scripts), `synthetic text` (a text made from the signature: `fl.<id>`).

[x3kc/data/msci_semantics_ap.json]: ../x3kc/data/msci_semantics_ap.json

**Finding a command** (for agents and people). The hierarchy is the in-game editor's command menu: groups and
subgroups (`General Commands / Arrays`, `Fly Commands / Wings`, `Trade Commands / for Stations` …), listed with
their size at the top of each file. Every command is one line starting with `- **<id>**` and its syntax, so:
- search a word of what you want in the syntax: `grep -n "sector" msci_commands_ap.md`, then read the two lines
  under the hit; or open the group in the index;
- [msci_commands_ap.json](msci_commands_ap.json) / [msci_commands_fl.json](msci_commands_fl.json) hold the same as
  data, one command per line: `groups` (the menu tree with command ids) and `commands`
  (`id, group, syntax, text, params[{n, type, type_id}], result, condition, interruptible, hidden, not_in_text,
  learned, new, changed_from_other, used, example_script, calls, bug`). Filter by `group`, by a parameter type
  (`Var/Station`), by `condition`, prefer high `used`;
- then read the `example_script` with `x3kc msci dis` to see the command in real use.

Regenerate after a game update (and for TC, which has no generated reference here):
```
x3kc msci-setup "<game>" work [--addon addon2]
x3kc decompile work/x3story.obj src                         unnamed source, for msci map
x3kc msci map src/x3story.kc work/texts_L044.tsv work/map.md --json work/map.json
x3kc msci ref work/msci_db.json msci_commands.md --game "AP 3.3" --map work/map.json --scripts work/scripts \
    --json msci_commands.json                                 FL: also --other <AP db> --other-name AP
```

## Pitfalls
| pitfall | do instead |
|---|---|
| a loop without a waiting command freezes the game | `@ = wait 100 ms` (or more) in each pass of a long loop |
| `if $ship` is true for a destroyed ship; `$ship == null` false | `if $ship -> exists` |
| `null == 0` is false, `'' == null` is false | compare with the type you stored; `is datatype[…] ==` |
| a false comparison stores `null`: `$c = $a > $b` then `$c == 0` is false | test `if $c` / `if not $c` |
| `null` inside `+` turns the result into text (`'a' + $unset` = `'anull'`, `null + 1` is a string) | set variables before using them |
| `return $a + 1` does not parse | `$r = $a + 1`, `return $r` |
| your `x.xml` is ignored | a stock `x.pck` is beside it (`.pck` wins): rename the `.pck` away |
| a script inside a `.cat` is never loaded | ship scripts as loose files |
| texts of your `t` file show as `ReadText…` | `load text: id=NNNN` in your setup script |
| a `wait` ends early | an interrupt of the object ends all its waits; check the result (remaining ms) |
| `2000000000 > -2000000000` is false | keep compared values within ±1 073 741 823 or compare differences |
| integer overflow wraps (money, distances squared) | check ranges; fixed-point commands for fractions |
| `/ 0`, `mod 0` give `null`, then `null` spreads | test the divisor |
| `-2147483648 / -1` crashes the original game | avoid the minimum int |
| a command on a destroyed object returns `null` silently | test `-> exists` first |
| `call script` on a sector, asteroid or dead object does nothing | call on `null` and pass the object as an argument |
| `START $ship -> call script` stops the ship's command | `call script` without `START`, or another task slot (`start task N …`) |
| unsigned script not loaded | make the game modified (`Thereshallbewings`, reload) |
| two `init`/`setup` scripts depend on each other | one setup script that calls the others in order |
| `setup.*` runs on every load | make it idempotent; store a version in a global variable |
| command ids from FL on AP | skipped silently on AP; check `script engine version` (FL 73, AP 54) |
| AP-built script on FL for the 61 changed commands | rebuild from text with the FL database |
| a ware or class name in another language | build the database with `--lang` of the names you write |
| `(` in a text disappears | `\(` … `\)` |
| interrupt with a priority ≤ the running one is ignored | use a higher priority |

## Testing without the game
`x3kc.kcmsci` runs a script through the game's interpreter (`ScriptObject::__runScript` of the real
`x3story.obj`) in x3kc's VM emulator; natives behave as in the exe (`docs/natives`). See
[checking.md](checking.md) for the emulator itself.
```python
import json
from x3kc import kcmsci, kcobj, kcvm, msci
vm = kcvm.VM(kcobj.load('work/x3story.obj'))
vm.load_names('auto:x3story')                    # optional: method names for overrides and messages
db = json.load(open('work/msci_db.json', encoding='utf-8'))
m = kcmsci.Msci(vm, db)
r = m.run_text(['$a = 5', '$b = $a * 3 + 1', 'return $b'])        # text straight in
code, _ = msci.read_script('out/my.script.xml')
vm.call(700, 'AddScript', [kcmsci.to_cells(code)])               # make it callable by name
ship = vm.new(2004)                                              # an object of class Ship
vm.overrides['GetSector'] = lambda vm, t, obj, args: 0           # replace a KC method by name
r = m.run(code, this=ship, this_type=65542, args=kcmsci.to_cells([4, 10]))
print(r.type, r.value, vm.called('GetSector'))
vm.texts[(8870, 2)] = 'Hello, %s!'                               # texts for read text / sprintf
```
- `run` advances a virtual clock through `wait`s; a script that never returns stops after 1 000 steps.
- The emulator has no universe: player, sectors and ships are absent unless you create objects or override the
  methods a command calls (the reference's `calls` tells which). It checks logic, control flow, expressions, calls,
  texts and that every command line is well formed — not game effects.
- `x3kc msci cost <x3story.obj> <db> 'line' …` measures how many KC instructions a line costs.

## Checklist before shipping
1. Every file name equals its `<name>`; names have no spaces; your prefix (`plugin.mymod.*`, `lib.mymod.*`).
2. `x3kc msci new` builds without error; `x3kc msci dis` prints the source back.
3. Every loop that may run long has an interruptible command in each pass.
4. Every object not created in the same line is tested with `-> exists` before use.
5. No arithmetic can overflow; divisors are tested; compared numbers are within ±2³⁰.
6. `setup`/`init` scripts can run twice without harm; their order does not matter.
7. Player-visible texts come from your `t` file; the page number is free in AP and FL; `(` is escaped.
8. Commands exist in the target game (reference for AP or FL); for both games, test `script engine version` and
   build one XML per game when you use any of the 61 changed commands.
9. The logic ran through `kcmsci` with the edge cases you can think of (`null`, 0, negative, empty arrays).
10. Tell users the game must be modified (or sign/rename accordingly).

## What older guides get wrong
The community material (X2-era MSCI guides, X3 Reunion modding guides) is mostly right about the editor and the
patterns. Where it disagrees with the code of AP/FL:

| guide says | the code does |
|---|---|
| a run of more than ~200 ms between two `@` commands hangs the game for good | there is no time limit at all: any loop without a waiting command blocks the frame until it ends |
| the infinite-loop detector "rarely fires" | it is off unless the script turns it on (command 153), then it fires after 10 000 commands in one moment |
| any value > 0 is true; null is false | `null` and 0 are false; also negative numbers, every string (even `''`) and every array are true; inside expressions objects are true only if they exist |
| `init.*` runs before the galaxy exists | true for the client start; on a save load `init.*` runs with the universe loaded |
| unsigned `init`/`setup` run only in a modified game | true, and it holds for every unsigned script whose name does not start with `!` |
| grouping with `[ ]` | AP/FL expressions use `( )`; `[ ]` are constants |
| an unpacked `.xml` next to the stock `.pck` is used instead | the `.pck` wins (extension order `pck xml`); move the `.pck` away |
## Limitations
What is not known or not done — read before relying on the manual:
- **Not run in the game.** The worked example and every claim marked (code) were checked in the emulator with the
  game's interpreter, not in the running game. No script built by `x3kc msci new`/`asm` has been loaded by the
  game yet.
- **What each command does** (the reference's `Does` lines) comes from a reading wave over every command's
  handler (AP 828, FL 1 740 new or changed): confidence high where the called methods were read, medium where only
  the handler was (most), low where inferred. Two of the suspected bugs were confirmed by hand and recorded (S52,
  S53); the others are shown as "Suspected (not verified)". Effects deep inside the engine (AI, economy) are
  summarised, not traced.
- **Round trip.** `dis` → `new` rebuilds the `<codearray>` of 697 of 697 AP and 1 020 of 1 020 FL stock scripts.
  Where the text alone is ambiguous (106/110 `random value`, a ware and a class with one name, cells the text
  does not show such as 1185's, values without a text) `dis` appends a pin `//= k: [values]` (docs/msci.md,
  «Round trip»); delete the pin when you change that line. From the game's own saved text (`asm` / `msci
  verify`, no pins) 653 of 697 AP and 853 of 1 020 FL scripts rebuild. A script you write yourself avoids the
  ambiguities (use variables or constants for ambiguous values).
- **FL:** commands with synthetic texts are written in a form the game's own editor would not show; some FL names
  print wrongly (`COMMAND_AGENT_*` and `role=` values share one text); FL's new menu subgroups have no names in the
  texts (`Group 107` …); FL's own bugs S48–S51, S54, S55 are in the reference.
- **TC** is not covered by a generated reference (the tools work on a TC directory as well).
- Commands, signals, task slots, hotkeys and the AL engine were read in the KC code; hotkeys (AP), task slots,
  interrupt priorities, waits cut short across slots, AL events and the command lookup also run through the
  game's KC in the emulator (`tests/test_msci_engine.py`). Signal delivery from the engine (attacks, docking) and
  the console menus are read only. What a save keeps of a running script is inferred from the code and the VM's
  save format, not checked with a savegame. The in-game editor's own behaviour is not covered.
- `<signature>` cannot be produced: the game's signature check is not reproduced.
