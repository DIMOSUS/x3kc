# Editor support for KC sources

Syntax highlighting of `.kc` files (the sources `x3kc decompile` writes and `x3kc compile` reads) for Visual Studio
and VS Code, from one TextMate grammar. Nothing here depends on how the sources are split into files: every rule
works inside one file and on any part of a file.

```
editors/
  kc-textmate/                       a VS Code folder extension; Visual Studio uses the grammar from it
    package.json
    language-configuration.json      comments, brackets, auto-closing pairs, indentation
    syntaxes/kc.tmLanguage.json      the grammar (generated, do not edit by hand)
  install.py                         copies it into the per-user folder of VS or VS Code, or removes it
  tools/gen_grammar.py               writes the grammar from the rules below + x3kc/natives.txt + x3kc/kcisa.py
  tools/check_grammar.py             checks the grammar without an editor
  kclsp/                             the KC language server (Python stdlib, LSP over stdio)
  vs/                                the Visual Studio extension: language client + grammar (vs/README.md)
  kc-textmate/syntaxes/msci.tmLanguage.json, msci-language-configuration.json
                                     MSCI scripts in their text form (.msci)
  tools/gen_msci_grammar.py          writes the MSCI grammar from docs/msci_commands_ap.json + _fl.json
```

## MSCI grammar (.msci)

The text form is what `x3kc msci dis` prints and `x3kc msci new` reads. What it highlights:

| Construct | Scope |
|---|---|
| Header lines: `// name (description)`, `// version: N`, `// engine: N`, `// command: N`, `//   $arg: Type "desc"` | `meta.header.*` |
| `*` comment lines | `comment.line.asterisk` |
| `@` (interruptible) | `storage.modifier.interruptible` |
| Labels `name:`, and `gosub name` / `goto label name` | `entity.name.label` |
| Flow words (`if`, `else if`, `skip if`, `do if`, `while`, `end`, `break`, `continue`, `return`, `endsub`, `START`) | `keyword.control` |
| Command words: every literal fragment of the command templates (2 900 fragments, matched as one word trie) | `keyword.other.command` |
| Script names after `call script`, `script=` ... | `entity.name.function.script` |
| `$variables` | `variable.other` |
| `[constants]`, `null` | `constant.language` |
| Numbers | `constant.numeric` |
| `'strings'` | `string.quoted.single` |
| `{page,id}`, and `pageid=N textid=M` | `constant.other.text-reference` |
| Operators | `keyword.operator.*` |

Check on 100 stock AP scripts (7 841 lines, `msci dis` output):
- `check_grammar.py --grammar .../msci.tmLanguage.json`: 99.96 % of the non-blank characters scoped, 93.5 % with a
  specific scope, no `invalid.*`;
- all 26 regexes compile in Python's `re` and in VS's `Oniguruma.dll`;
- the Python interpreter took 3.3 s.

Installing:
- per-user grammar for Visual Studio: `python editors/install.py vs-msci` (its own folder `x3kc-msci`, because of
  `language-configuration.json`);
- VS Code: `python editors/install.py vscode`;
- the VSIX (0.2.0) carries both grammars and the content type `msci`.

## MSCI in the language server (editors/kclsp/msci_ls.py)

The server process is the same; it treats a file by its extension (`.msci`).

- **Diagnostics** come from x3kc's assembler (`msci_asm.Assembler.line`), fed line by line, so every bad line gets
  its own diagnostic: an unknown or mistyped command, a bad value or expression, block errors (`end` without `if`,
  `else` after `else` ...). On top of that: blocks left open (at the opening line), undefined labels (at the line
  that jumps), header errors (no header, unknown argument type, script name ≠ file name).
  - The assembler has no notion of "a variable used before assignment", so that is not reported.
  - The arguments of `call script X : desc=value` are checked against the header of `X.msci` in the same folder.
  - The assembler needs the game's `msci_db.json` (`x3kc msci-setup`). It is looked for in this order:
    `KCLSP_MSCI_DB_AP` / `_FL`; `KCLSP_MSCI_DB`; `msci_db.json` in the script's folder or above;
    `<repo>/build/msci/<ed>/`; `<repo>/build/tmp/queue/msci/<ed>/`. Without one, only the header is checked, and an
    information diagnostic says so.
- **Hover**:
  - on a command line: the command from `docs/msci_commands_<ed>.json` (syntax, group, what it does, parameter
    types, result, effects, edge cases, bugs, whether it waits, the KC natives its handler calls, how often stock
    scripts use it);
  - on `{page,id}` or `pageid=N textid=M`: the text in every language, as for KC.
- **Completion**: every command's syntax line, ordered by how often stock scripts use it. Parameters stay as
  `<Type>` text; no snippets.
- **Go to definition**:
  - `gosub X` / `goto label X` go to `X:`;
  - `call script X` / `script=X` go to `X.msci` in the same folder (or `../scripts/`);
  - text references go to the `<t>` entries.
- **Document symbols**: the script with its arguments, and the labels.
- **Edition**: `fl` when a path component is `fl` or `addon2`, else `ap`; `KCLSP_MSCI_EDITION` overrides.

## Mission Director files (XML)

**What Visual Studio's XML editor already gives.** It validates an XML file against the schema its root names with
`xsi:noNamespaceSchemaLocation` (a path relative to the file), and completes elements and attributes from that
schema. The stock files already have what it needs:
- every stock MD file names `director.xsd`: AP 88 of 89 `.xml` files in `game/ap/addon/director/`, FL 85 of 86 in
  `game/fl/addon2/director/` (the exception is `mdfiles.xml`, a file list, not a `<director>`);
- `director.xsd` lies next to them and includes `dirobjdb.xsd` from the same folder;
- `md_schema.py check` resolves every reference, and all four `.xsd` files parse as XML.

So in the repo layout, IntelliSense and validation for stock and edited stock MD files need no wiring.

**Elsewhere.** A director file outside that folder needs the relative path to director.xsd:

```
python editors/tools/md_schema.py check <file or dir>...
python editors/tools/md_schema.py fix <file or dir>... [--edition ap|fl] [--schema PATH] [--write]
```

`fix` points `xsi:noNamespaceSchemaLocation` (adding `xmlns:xsi` when missing) at
`<repo>/game/<ed>/addon[2]/director/director.xsd`. It changes nothing else in the file, and writes only with
`--write`.

**What the extension cannot add.**
- A schema catalog shipped by the extension cannot help: VS's catalog associates schemas by namespace, and MD files
  have none, or by file extension, which here is `.xml` for everything.
- The language client is not attached to XML. That would start the server for every XML file VS opens, and the XML
  editor already owns the content type.
- So the `{page,id}` text references in MD strings (1 306 in the AP files) get no hover in VS yet. The server's text
  index would serve them as soon as a client sends MD files.

**Only the user can verify:**
- that VS validates and completes a stock MD file (squiggles on an unknown element, completion of `<cue>`
  attributes);
- that the ISO-8859-1 files and the included `dirobjdb.xsd` load;
- that a file fixed with `md_schema.py` gets the same.

## Language server (editors/kclsp)

```
python -X utf8 -B -m editors.kclsp          from the x3kc checkout; speaks LSP on stdin/stdout
set KCLSP_LOG=<file>                        logs every message
```

- **Projects.** It opens the split project whose `.kcs` index lists the edited file. It looks in the file's directory
  and up to three levels above. A `.kc` that no index lists is a project of its own (a single-file source).
- **How it reads a split project.** Each part goes through pass 1 alone. The declarations of all parts are merged in
  index order into one symbol table. Pass 2 and codegen of a part then run against that table, so an edit re-reads
  one part.
- **When a declaration changes** (a class, field or method added, renamed, or its arity changed), the other open
  parts are re-checked.
- **On save** the whole project is checked, and diagnostics are published for every part.
- **Diagnostics**, with line and column ranges:
  - lexer and parser errors (both passes; one per function body, the others still checked);
  - duplicate classes, globals or functions across parts, and unknown parent classes;
  - codegen errors (`break` outside a loop, duplicate cases, ...), at the function name;
  - warnings for natives X3AP.exe lacks and for static or global calls whose argument count differs from the
    declaration. The stock AP x3story has none of either.
- **Document symbols**: global variables, global functions, classes with their fields and methods.
- **Go to definition**:
  - class names (`C150`, `Ship`, `new Ship(`, `: Parent`, `Ship::F`);
  - static and inherited methods (`F(`, `C::F(`, `C.F(`, `x.C::F(`);
  - `this->F` (nearest in the class chain, else every method of that name);
  - `x.F(` (every method of that name: KC calls by name at run time);
  - global functions (`F(`, `::F(`);
  - fields of the class and its parents, globals, locals, parameters;
  - natives: their entry in `docs/natives/spec_*.jsonl` when documented, else their line in `x3kc/natives.txt`.
- **Hover on a native** shows its entry of `docs/natives/spec_*.jsonl` (arguments, result, whether it waits, what it
  does).
- **Text references** (`editors/kclsp/texts.py`, `textrefs.py`): hover and go to definition on a call that reads a
  text page entry with literal arguments.
  - Which calls: `SE_ReadText` / `SE_ExistsText` with 1, 2 or 3 arguments (id / page, id / page, language, id), and
    every KC function that hands its own parameters (or a fixed page) on to one of them, found on the tokens,
    wrappers of wrappers included. AP x3story has 17 such wrappers, e.g. `GetTextLookup(id)` with page 35, and 80
    calls of them with literal arguments.
  - Hover shows the text in every language found, Russian and English first, each with its file and line. It notes
    the raw page id when the file uses a prefixed one, and the entries a later file replaces.
  - Go to definition jumps to the `<t>` entries.
  - Pages are read from `<repo>/game/<edition>/[addon*/]t/*.xml` (the content import) and `<repo>/kc/<edition>/t/`
    (the project's own pages). `<repo>` is the first folder above the source that holds `kc/` or `game/`;
    `<edition>` is the folder after `kc/`. `KCLSP_TEXT_DIRS` adds folders (separated by `;`).
  - Page ids are normalised as the exe's loader does (`normalizeXmlPageNumber` in x3_src `src/script/vm.cpp`):
    - AP: 380001..399999, 350001..379999 and 300001..349999 drop the prefix;
    - FL: also 390001..399999.

    A later file replaces earlier entries of the same page and id. The order assumed: stock folders, then the
    project's own; file names sorted; `NNNN.xml` after `NNNN-LXXX.xml`.
  - The index reads the AP pages (23 files, 30 MB) in 0.9 s on first use and re-reads only changed files.
  - Find references from a `<t>` entry back to its uses is not done: the client serves `.kc` files only.
- **Text sync** is incremental. After a change, analysis runs after 0.3 s of quiet. A request (definition, symbols,
  hover) analyses at once if needed.

Measured on AP x3story split into 466 parts:

| What | Time |
|---|---|
| Open the first class (pass 1 of all parts + merge) | ~1.4 s |
| Re-check `Ship.kc` after an edit | ~0.4 s |
| `ScriptObject.kc` (1.5 MB) after an edit: pass 1 + pass 2 + codegen | 0.21 + 0.58 s |
| The interpreter function `ScriptObject::__runScript` (168 000 tokens, 30 700 lines) alone | 0.32 s pass 2 + 0.06 s codegen |
| Single 8 MB `x3story.kc` (slow path) per change | 1.9 s pass 1 + 4.1 s pass 2 / codegen |
| Whole-project check on save | ~3.5 s, in the background |

Tests: `python -m unittest discover tests -p test_kclsp.py` starts the server as a subprocess and drives it like an
editor. They need no game files; with `D:/GAMES/x3_src/kc/ap/x3story.kc` (or `X3KC_STORY_KC`) present, one more test
splits it into a temp dir and opens `Ship.kc`.

VS Code: the folder extension `kc-textmate` gives highlighting only. An LSP client for VS Code needs the npm module
`vscode-languageclient` (a download) or a hand-written client, so it was left out.

## Install

Visual Studio 2022 / 18 (no extension to build: the editor's TextMate support loads user grammars):

```
python editors/install.py vs                 -> %USERPROFILE%/.vs/Extensions/x3kc-kc/Syntaxes/
python editors/install.py vs --uninstall
```

Restart Visual Studio, open a `.kc` file. If it still opens as plain text: *Tools > Options > Text Editor > File
Extension*, extension `kc`, editor "Common Language Editor Supporting TextMate Bundles" (the editor VS registers for
`*` in `CommonExtensions/Microsoft/TextMate/TMLanguage.pkgdef`).

VS Code:

```
python editors/install.py vscode             -> %USERPROFILE%/.vscode/extensions/x3kc.x3kc-kc-0.1.0/
python editors/install.py vscode --uninstall
```

`--dry-run` prints what would be copied. The script writes a marker file into the folder it creates and refuses to
remove a folder without it.

How Visual Studio finds the grammar (read from the installed files of VS 18.8, `Common7/IDE/CommonExtensions/
Microsoft/TextMate`): `Microsoft.VisualStudio.TextMate.VSWindows.dll` scans `Starterkit\Extensions` of the
installation and `.vs\Extensions` of the user profile; the stock grammars there are `.tmLanguage` / `.plist` (XML
plist) and `.json` / `.tmLanguage.json` (e.g. `rust/syntaxes/rust.json`, `cpp/syntaxes/cpp.tmLanguage.json`), so both
formats load; `Microsoft.VisualStudio.TextMate.Core.dll` maps files to a grammar by its `fileTypes`
(`["kc"]` here) and knows the file name `language-configuration.json`, which is why the installer puts that file next
to the grammar. The regex engine is `runtimes/win-x64/native/Oniguruma.dll`.

## What is highlighted

The token set is the one `x3kc/kcparse.py` reads (lexer `TOKEN`, the words `Parser` / `BodyParser` test for).

| Construct | Scope |
|---|---|
| `// …`, `/* … */` | `comment.line.double-slash`, `comment.block` |
| `#object 801 …`, `#sections CODE …`, `#pool x.pool`, `#patch`, `#stamp`, any `#word` | `meta.preprocessor.directive`, `keyword.control.directive`; numbers, section names (`support.constant.section`), file names (`string.unquoted.path`); a `//` comment after it |
| `"…"` | `string.quoted.double`; escapes `\n \r \t \\ \" \xHH` → `constant.character.escape`, any other escape → `invalid.illegal.unknown-escape`; printf conversions `%d %s %5.2f %!1s %%` → `constant.other.placeholder` |
| `0x1F`, `12`, `1.5`, `1.5e3`, `2e-3` | `constant.numeric.hex` / `.decimal` / `.float` |
| `class Name = 150 : Parent` | `storage.type.class`, `entity.name.type.class`, `constant.numeric.class-id`, `entity.other.inherited-class` |
| `function Name(a, b) frame 5 asm {` | `storage.type.function`, `entity.name.function`, `variable.parameter`, `storage.modifier.frame` + number, `storage.modifier.asm` |
| `var name` (globals, fields, locals) | `storage.type.var`, `variable.other.declaration` |
| `@0x4ee77ad2` (build stamps) | `storage.modifier.stamp`, `constant.numeric.stamp` |
| `if else while for do switch case default return break continue fork` | `keyword.control` |
| `switch (x) : dense(8, 16)`, `: sparse` | `storage.modifier.switch-table` |
| `new`, `delete` | `keyword.operator.new` |
| `this`, `this->F(` | `variable.language.this`, `entity.name.function.member` |
| `__m __g __f64 __and __or __opland __oplor __init` | `support.function.intrinsic` |
| `__argc __retpc __ctx` | `variable.language.frame` |
| `C1000::F(`, `x.C150::F(`, `::F(`, `__global::F(` | `entity.name.type.class`, `entity.name.function.call` |
| `native::SE_X(`, natives `SE_SPrintf(` … | `keyword.other.qualifier`, `support.function.native` (the 987 names of `x3kc/natives.txt`) |
| a group prefix with an unknown name (`SA_Bogus`) | `support.function.native.unlisted` (kcc warns about these calls) |
| `new Ship(` | `entity.name.type.class` |
| `x.F(` | `entity.name.function.member` |
| `F(` | `entity.name.function.call` |
| `C150` | `entity.name.type.class` |
| default names `arg3`, `v2`, `m12`, `g40` | `variable.parameter`, `variable.other.local`, `variable.other.member`, `variable.other.global` |
| other identifiers | `variable.other` |
| operators, `; , . ( ) [ ] { }` | `keyword.operator.*`, `punctuation.*` |
| a character the lexer refuses (`$`, `'`, `` ` ``, `\`) | `invalid.illegal.character` |
| asm bodies (`asm { … }`): labels `L0012:`, mnemonics of `kcisa.py`, `C150::F`, sswitch tables | `meta.asm`, `entity.name.label`, `keyword.other.mnemonic` |

Not highlighted as such (a grammar cannot know them): named classes used without `::`, `new` or `class`
(`Ship.F()` gets `variable.other`), field and global names other than the default `mN` / `gN`, and errors beyond
single tokens. That needs the language server (see the study in the x3_src queue).

## Speed

The grammar has to stay fast on `x3story.kc` (8 MB, 190 000 lines):
- every pattern begins with a literal, a word boundary or a fixed character class; there are no nested or unbounded
  lookaheads; the only lookarounds are the one-character lookbehind `(?<=\))` (frame, switch table) and
  `(?!\s*::)` / `(?=\s*\()` after an identifier;
- all rules are line-local except the block comment, the parameter list of a function header and an asm body, so
  an edit re-tokenises from the changed line on and stops as soon as the rule stack matches the old one;
- the 987 native names are one alternation grouped by prefix (`B3D_(?:…)|SA_(?:…)|…`), so an ordinary word fails
  on its first letters.

## Checks

`python -X utf8 -B editors/tools/check_grammar.py [file.kc] [--lines N]`:
1. the grammar is valid JSON and every `#include` resolves;
2. every regex compiles in Python's `re` and in Oniguruma - the `Oniguruma.dll` of the Visual Studio installation,
   called through its exported `CreateRegEx` (a broken probe pattern is fed first to prove that errors are reported);
3. a small TextMate interpreter over Python's `re` tokenises the first N lines (default 2000) and reports the share
   of non-blank characters with a scope, the share with a specific one, `invalid.*` hits and untouched lines.
   The patterns use only constructs that mean the same in `re` and Oniguruma.

Result on the first 2000 lines of AP `x3story.kc`: 49 929 non-blank characters, 100 % scoped, 82.1 % with a
specific scope (the rest are plain identifiers), no `invalid.*`, no untouched line. The whole file (`--lines 0`,
189 815 lines, 4.7 M non-blank characters): 100 % scoped, 76.2 % specific, no `invalid.*`, 42 s in this Python
interpreter (the editors' native engines are much faster); the slowest line is the 97 933-character `chartSpec`
literal at 1.07 s - linear, no backtracking blow-up. VS Code stops tokenising lines longer than
`editor.maxTokenizationLineLength` (20 000 by default), so that one line stays plain there.

Not verified without the IDE: that Visual Studio picks the grammar up from `.vs/Extensions` (the folder layout
follows the stock Starterkit and the strings in the VS DLLs), the colours the VS theme gives to each scope, and the
editor's speed on the whole file.
