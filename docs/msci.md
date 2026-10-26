# MSCI scripts: how they are stored and executed

The ordinary X3 scripts (MSCI, the ones written in the in-game script editor, `addon/scripts/*.xml|pck`) are
neither machine code nor KC. They are data: XML with a `<codearray>` that an interpreter written in KC executes.
- The interpreter is `C702::__runScript` (`ScriptObject::__runScript` in the bundled names) in x3story.obj.
- The command database is `C700` (`ScriptDatabase`), the editor `C701` (`MsciScript`).

Everything below was read from the decompiled KC and checked with `x3kc msci`: the decoder reproduces the
`<sourceplaintext>` the game saved itself in **99.6 %** of 43 247 lines of all AP scripts; the assembler rebuilds
the `<codearray>` of 526 of 697 scripts exactly.

## File
```
<script>
  <name> <version> <engineversion> <description>
  <arguments><argument index name type desc/>…</arguments>
  <sourceplaintext>  (text for people; may be missing)
  <sourcetext>       (the same with markup; may be missing)
  <codearray>        (what runs)
  <signature> | <nosignature>
</script>
```
- **Loading** (`C700::LoadScript`): reads `addon/scripts/<name>.xml` (pck is unpacked before), takes only
  `<codearray>`, checks the signature (`VerifyDataXML("signature")`), requires `engineversion ≥ 40`.
- **Signature.** A script without a valid signature loads. When its name starts with `!` (a "required" Egosoft
  script), the game marks itself "modified" (`Client::SetModifiedByFile`); a missing or broken `!` script too.
- **Saving** (`C700::SaveScript`) writes `<nosignature>NNNN</nosignature>` with a random number.

## The script array (`<codearray>`)
Nested `<sval type="array|int|string">`; empty arrays are written as `int 0`.

| Index | Content |
|---|---|
| 0 | name (must match the file name) |
| 1 | engine version (≥ 40) |
| 2 | description |
| 3 | script version |
| 4 | flags: 1 do not load; 2 sector/object references encoded inside; 4 signature valid (set at load) |
| 5 | variable names, arguments first, then in order of first use |
| 6 | **code lines** |
| 7 | arguments `[[type (text page 2000), description], …]` |
| 8 | editor-only lines `[code line index, id, parameters…]`: 1 comment `* text`, 2 empty line, 3 commented-out command or keyword, 4 `end`, 5 `else`, 6 `continue`, 7 `break` |
| 9 | 0 |

## A code line
`[command id, parameters…]`.
- **Signatures** of all commands are in `C700::InitDatabase`, field `m0`: `id → [[parameter types], flags]`.
- **Parameter types** are ids of text page 2000; their flags are field `m3`, read with an offset of one:
  `GetParDefFlags(p) = m3[p+1]`, as the game does.
- **Command flags:** 0x10000 interruptible (the editor shows `@`); 0x20000 hidden (not shown by the editor, e.g.
  `goto` 112, from which loops, `break` and `continue` are built).

**Parameter encoding** (`C700::CmdToParStringArray`):
- type flag 0x10000 or type 4 — a pair `(data type, value)`;
- flag 0x20000 — the result variable, one cell:
  - `≥ 0` — variable index;
  - `0x80000001` — result not wanted (shown as `=`); `0x80000002` — `START`;
  - else a condition form: the low byte is the form from page 2007 (3 `if`, 4 `if not`, 5 `else if`,
    6 `else if not`, 7 `skip if`, 8 `do if`, 9 `while`, 10 `while not`), bits 8–23 the target line, bit 30
    "jump when true" (forms 4, 6, 7, 10), else "jump when false";
  - **these are all the `if`/`while`/`skip if`:** there are no separate branch commands;
- 1 — a label (the line index of its `define label`);
- 2, 6, 7, 8 — the value as it is (e.g. a script name); 3 — a number;
- 5 of `call script` (102) — the argument list: `n`, then `n` pairs;
- 30 of an expression (104) — `n` operand pairs in **reverse Polish order**, then `m` display items: `-i-1`
  means operand `i`, a positive number an operator code (page 2001). The expression is computed by the native
  `SE_ScriptComputeExpression`.

**Value data types** (`C700::DataTypeToString`):

| Type | Value | Shown as |
|---|---|---|
| 0 | null | `null` |
| 0x20002 (131074) | variable | `$name` |
| 0x20003 (131075) | constant | `[THIS]`, `[PLAYERSHIP]`… (page 2002) |
| 4 | int | number |
| 5 | string | `'text'` |
| 65542 / 65543 / 65544 | ship / station / sector | object name |
| 9 | ware `(main << 16) \| sub` | name from the T files (text 17:id) |
| 10 | race | `Player` or 1266:id |
| 12 | object class | 2006:id |
| 14 | relation | Friend / Neutral / Foe |
| 15 | operator | 2001:id |
| 18 / 19 / 25 | object command / flags / … | 2008 / 2009 / 2028 |
| 20 | data type | 2013:id |
| 21 | array | `ARRAY(…)` |

## How the editor builds control flow
- `if` / `else if`: the condition jumps to the next branch or the end; the end of every branch but the last has
  a hidden `goto` (112) to the end of the chain; `else` and `end` exist only in `[8]`.
- `while`: a hidden `goto` back to the `while` line at the end; the condition jumps past it.
- `break`: hidden `goto` to the loop end; `continue`: hidden `goto` to the `while` line.
- `skip if` / `do if`: the target is the line after the next one.
- Expressions are parsed by precedence: OR < AND < `|` < `^` < `&` < `== !=` < comparisons < `+ -` < `* / mod`
  < unary `~ - !` < parentheses.

## Execution (`C702::__runScript`)
An MSCI task is a KC task looping over: take `code[pc++]`; an expression (104) is computed; anything else goes
through a `switch` over the command id (about 830 cases); then the result is stored or the condition form jumps.
`goto` (100, 112) sets `pc`. `wait`, `call script` and flight commands suspend the KC task on timer natives or
on the object — MSCI has no scheduler of its own. `x3kc msci map` lists which KC methods and natives each
command calls.

## Texts
- Command syntax: page 2004 when it has the id, else page 2003; NOP (2) has empty text.
- Pages 300001–399999 are renumbered at load (301903 → 1903).
- In X3 texts an unescaped `(…)` is a comment and not shown; `\(` is a real parenthesis.
- `{page,id}` inside a text refers to another text.

## Tools
```
x3kc msci-setup "<game>" work                          texts, ware names, msci_db.json, work/scripts
x3kc msci dis work/msci_db.json work/scripts/X.xml     script text as the editor shows it
x3kc msci asm work/msci_db.json work/scripts/X.xml edited.txt > X.xml
x3kc msci check work/msci_db.json work/scripts         decoder vs <sourceplaintext>
x3kc msci verify work/msci_db.json work/scripts        assembler vs <codearray>
```
`asm` takes the header, description, version and arguments from the given XML and argument names of called
scripts from the XML files next to it. The `//` header lines of `dis` output are skipped, indentation does not
matter, a BOM is accepted. `<sourcetext>` and `<signature>` are not written.

These are errors (not silently wrong code): `if`/`while` without `end`; `end`/`else`/`else if` without their
block; `else if` after `else`; `break`/`continue` outside `while`; a jump to an undefined label; an expression
ending in an operator (the game editor lets it through, the VM then runs short of operands); a variable name
with characters other than letters, digits, `.` and `_`; an unterminated string; `]]>` in the text.

What the assembler cannot reproduce: hidden trailing parameters the text does not show (1185: `0,0` vs
`4,100`); a ware and a class with the same name (`709`); wares from T files of mods that are not in the
database; colour control codes `\x1b…` in texts. **A script built by `asm` has not been run in the game yet.**
