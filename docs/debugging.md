# Debugging KC objects

`x3kc compile` writes the debug info of every object it writes: `<name>.kcdbg` beside `<name>.obj` (another output
name: `<out>.kcdbg`; `--debug-info FILE` names it). The object's bytes do not depend on it: savegames and the x3_src
engine's native-body hashes see the same object as without it. The x3_src engine logs raw script locations;
`x3kc symbolize` maps them back to the source with the sidecar. The live debugger (stage 2) reads the same files
through the same module, `x3kc/kcdbg.py`.

## The sidecar (`.kcdbg`, format version 1)

One JSON object, UTF-8, one method per line (grep-able; about 2x the object's size, x3story 4 MB: not compressed).

```
{"format": "x3kc-kcdbg", "version": 1,
 "object": {"name": "x3story", "build": 1801462941, "size": 2088552, "crc32": "8 hex", "sha256": "..."},
 "files": ["Cheats.kc", "@header.kc", ...],          the source files (a split project's parts)
 "globals": ["g1", "gPlayer", ...],                  global variable names by slot (ldg / stg operand)
 "classes": [{"id": 2004, "name": "Ship", "parent": 2000 | null,
              "fields": [...]}],                     field names by slot, inherited fields first (ldm operand)
 "constants": {"<class id>": {"NAME": value}},       const and enum members (0: global)
 "methods": [
  {"class": 203, "name": "CheatEquip", "entry": 1, "end": 353, "file": 0, "line": 17, "frame": 8,
   "params": ["ship"],
   "lines": [[pc, line, col], ...],                  each statement's first pc (col 1-based); [pc, line, col, file]
                                                     when a statement is in another file than its method
   "vars": [[name, kind, cell, start, end], ...]}]}  kind param | local | frame; cell: see Frames; a local holds
                                                     its value for pc in [start, end), params and frame cells always
```

- `object` is the identity of the exact `.obj` bytes. A client refuses a sidecar whose size / crc32 (or sha256) are not
  the loaded object's: the engine logs size and crc32 at every load (below).
- `version` changes when a reader of version 1 would misread the file.
- A pc is an offset into the object's CODE section, the engine's `Task::pc` and return offsets.
- The line table: the location of a pc is the last entry at or before it. Loops also map their condition, step and
  back jump to the loop's own line; a method's final `push0; ret` maps to its closing brace.

## Frames

The KC stack grows down. A frame, R = its return-offset cell: the arguments at R + 2 .. R + 1 + argc (the first
argument at R + 2), argc at R + 1, the return offset at R (0: the task's root frame), the caller's saved context at
R - 1, the locals from R - 2 down (the first declared at R - 2), the operands below them. A variable's `cell` is its
offset from R. A native body or native case (x3_src) keeps this frame; a call the engine answers without a frame is not
on the stack.

## Raw locations and resume pcs

The engine writes `kc[<object> 0x<pc> <class id>:<method>]`; an MSCI frame (702::__runScript with its native case
bound) adds ` msci=<script>:<code index>` (the 0-based index of the line in the script's code array; `x3kc msci dis`
shows the editor's lines). The pc is a resume pc: the address after the last instruction the frame ran (a caller's
return address, a native call's next instruction). Its line is the line of pc - 1 (pc itself at a method's entry: a
task that has not run yet). `DebugInfo.location(pc)` does that; `location(pc, resume=False)` looks the pc itself up.

The load line, once per object load:

    kc object <name> loaded: build <n>, <size> bytes, crc32 <8 hex>[, file <path of a -kc object>]

## x3kc symbolize

    x3kc symbolize [<log> | -] [--dbg FILE|DIR]... [-o OUT] [--raw]

Reads a log (x3rw.log in the mod folder, or stdin) and rewrites every raw location to
`File.kc:line:col Class::Method` (`(msci=...)` kept after it; `--raw` keeps the raw location too). The sidecars: the
load lines' `file` (the `.kcdbg` beside it), then the `--dbg` folders and files: `<name>.kcdbg`, `<name>.<build>.kcdbg`
and their gzipped `.kcdbg.gz`, the one whose identity (first line) is the loaded object's. A sidecar whose identity
differs from the last load line of its object is stale: its locations stay raw and one warning goes to stderr. With no
load line for an object (a cut log, a pasted line), its first sidecar is used and one warning says that its identity
was not checked.

A release keeps its sidecar: x3_src `tools/kcrelease.py` stores `kc/<edition>/base/<name>.<build>.kcdbg[.gz]` beside the
released object, so a player's log is symbolized with `--dbg kc/<edition>/base` without a rebuild.

## The engine's log lines (x3_src, x3rw.log beside x3rw.ini)

- `script dprintf <location>: <text>` / `script dmprintf ...` - SE_Dprintf, SE_DMprintf (debugPrintMask applies),
  one line per line of the text; a line over 1000 characters is cut and ends with ` [cut]`. An entry is one write.
- `script assert <location>: assertion failed[: <message>]` and `script assert   at <location>` per caller -
  `SE_Assert(condition[, format, args...])` with a false condition.
- `kc error: <reason> in task <id> at <location>`, then `kc error   at <location>` per caller - a task the VM kills or
  answers 0 for because of the script (an unknown opcode, the stack limit, a method not found by name, a native the
  exe lacks, a return into a deleted object's frame). The same error at the same place again is counted: its 10th,
  100th ... occurrence gives one line `kc error (N times): ...`; the counts start again at every object load, and the
  places the count table has no room for share one overflow count.
- `kc: 702::__runScript of x3story build <n> is not a known build ...` - once per load: MSCI locations are not given
  (the engine trusts the method's cells only for the builds whose hash it knows).
- The crash log's story call stack (X3Crash.log) and the hitch log's slowest script runs use the same raw locations.

## Module API (`x3kc/kcdbg.py`)

- `DebugInfo.load(path)`: `name`, `build`, `size`, `crc32`, `sha256`, `files`, `globals`, `classes`, `constants`,
  `methods` (sorted by entry); `matches(size=, crc32=, data=)`; `method_at(pc)`; `location(pc, resume=True)` ->
  `Location(file, line, col, method)`; `frame_vars(method, pc, resume=True)` -> `[(name, kind, cell)]`;
  `class_name(cid)`, `field_name(cid, slot)`, `global_name(slot)`.
- `Symbolizer(dirs, warn=, raw=)`: `line(text)` rewrites one log line (and notes load lines); `info(name)`,
  `location(name, pc)`.
- `find(name, dirs, size=, crc32=)` (the sidecar of that identity), `find_all(name, dirs)`, `head(path)` (the
  identity from the first line), `sidecar_path(obj)`, `identity(bytes)`; `collect` / `write` (the compiler's side).
  `DebugInfo.load` reads plain and gzipped files.
