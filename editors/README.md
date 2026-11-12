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
```

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
