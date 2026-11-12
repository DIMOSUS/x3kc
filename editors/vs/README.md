# Visual Studio extension: KC language client

`x3kc-kc.vsix` is a classic in-process extension. It holds:
- a MEF `ILanguageClient` for the content type `kc`, which is mapped to `.kc`;
- the TextMate grammar.

The client starts the KC language server (`python -m editors.kclsp`, see `editors/kclsp/`) when the first `.kc`
file opens. Visual Studio then shows:
- the server's diagnostics as squiggles and in the Error List;
- the outline in the navigation bar / Document Outline;
- Go To Definition (F12) through the server.

It is built without the "Visual Studio extension development" workload: `csc` and the reference assemblies come from
the Visual Studio installation, and a Python script writes and zips the package.

## Build

```
python -X utf8 -B editors/vs/build_vsix.py --check
```

- This writes `editors/vs/dist/x3kc-kc.vsix`.
- `--check` loads the client DLL on .NET Framework, as devenv does, with the VS folders as probing path. It loads every
  referenced assembly, resolves the types, prints the MEF export attributes and creates the client object. It also
  compares the package files with an installed extension's (manifest keys, pkgdef keys, assets).
- `build/` and `dist/` are not committed, because `kclsp.json` inside the package holds this machine's paths.

## Install (the user, by hand)

1. Remove the per-user grammar if you installed it earlier: `python editors/install.py vs --uninstall`. The VSIX
   carries the same grammar, and two copies of `source.kc` would compete.
2. Close Visual Studio. Double-click `editors/vs/dist/x3kc-kc.vsix`, or run
   `"C:/Program Files/Microsoft Visual Studio/18/Professional/Common7/IDE/VSIXInstaller.exe" editors/vs/dist/x3kc-kc.vsix`.
3. Start Visual Studio and open a `.kc` file of a split project (`x3kc split` / `decompile --split`) or a single `.kc`.

Uninstall: Extensions > Manage Extensions > Installed > "KC language (x3kc)" > Uninstall. Or run
`VSIXInstaller.exe /uninstall:x3kc.KcLanguage`.

## How the client finds Python and x3kc

The client reads `kclsp.json` next to its DLL. `build_vsix.py` writes it with:
- `python`: the interpreter that ran the build, by default `sys.executable`; `--python` sets another;
- `x3kc_root`: the checkout the build ran from.

The server is started as `<python> -X utf8 -B -m editors.kclsp`, with that checkout as working directory and
`PYTHONPATH`. Environment variables override the file:
- `KCLSP_PYTHON`
- `X3KC_ROOT`
- `KCLSP_LOG` (a log file of every LSP message)

When the checkout moves, rebuild the VSIX or set `X3KC_ROOT`. The server needs only the stdlib and the x3kc package.

## Grammar and content type

- The VSIX defines the content type `kc` on top of `code-languageserver-preview`
  (`CodeRemoteContentDefinition.CodeRemoteContentTypeName` in VS 18.8) and maps `.kc` to it. The language client is
  attached to that content type.
- The colours still come from the TextMate grammar: `kc.pkgdef` registers the `Grammars` folder of the extension under
  `TextMate\Repositories`, the documented way for LSP extensions. It also maps `source.kc` and the content type `kc`
  to `language-configuration.json` (comment toggling, brackets), with the same keys as VS's own `TMLanguage.pkgdef`.
- So with the VSIX, the grammar is loaded from the extension instead of `%USERPROFILE%/.vs/Extensions`.
- Without the VSIX, the per-user grammar alone still gives the colours (no diagnostics).

## Not verified here (only in the IDE)

- That VSIXInstaller accepts the hand-written `manifest.json` / `catalog.json`. They follow the installed extensions'
  layout, but the VSSDK normally writes them.
- That VS picks up the grammar through `TextMate\Repositories` for the `kc` content type. The key is the documented
  one; the DLLs only show a `CustomRepositoryService`.
- That the LSP client activates for `.kc` files, the squiggles, the Error List entries, the navigation bar outline and
  F12 targets in other part files.
- How fast the editor feels on the 1.5 MB `ScriptObject.kc` and on a single 8 MB `x3story.kc`.
