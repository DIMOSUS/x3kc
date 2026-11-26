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

    x3kc symbolize [<log>... | -] [--dbg FILE|DIR]... [-o OUT] [--raw]

Reads the logs in their order (`x3rw_previous.log x3rw.log`: the load lines of an earlier log give the identities
for the later one; nothing or `-`: stdin) and rewrites every raw location to
`File.kc:line:col Class::Method` (`(msci=...)` kept after it; `--raw` keeps the raw location too). The sidecars: the
load lines' `file` (the `.kcdbg` beside it), then the `--dbg` folders and files: `<name>.kcdbg`, `<name>.<build>.kcdbg`
and their gzipped `.kcdbg.gz`, the one whose identity (first line) is the loaded object's. A sidecar whose identity
differs from the last load line of its object is stale: its locations stay raw and one warning goes to stderr. With no
load line for an object (a cut log, a pasted line), its first sidecar is used and one warning says that its identity
was not checked.

A release keeps its sidecar: x3_src `tools/kcrelease.py` stores `kc/<edition>/base/<name>.<build>.kcdbg[.gz]` beside the
released object, so a player's log is symbolized with `--dbg kc/<edition>/base` without a rebuild.

## The engine's log lines (x3_src, x3rw.log)

The game log is `x3rw.log` in the mod folder (`addon_mod`, FL `addon2_mod`, or the `-mod` folder; when that folder is
not writable: the saves folder in Documents; a stand run: `x3rw_<pid>.log` in its TEMP). The previous run's log is
`x3rw_previous.log` beside it, a log rotates there at 8 MB, and every line starts with `hh:mm:ss.mmm `.

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
- `script debugger: listening on \\.\pipe\x3dbg-<pid>` - a ScriptDebug build (below) at its first frame.

## Live debugger (x3_src ScriptDebug build, protocol 1)

Only the x3_src ScriptDebug build has it (`-p:X3ScriptDebug=1`, the configurations `AP ScriptDebug` / `FL ScriptDebug`;
x3_src `docs/script_debugging.md` is the agent guide: building, launching, a worked session). The game serves a named
pipe `\\.\pipe\x3dbg-<pid>` on its game thread: once per frame while it runs, continuously while it is stopped. One
client at a time; `x3kc dbg` connects per command (and `wait` per poll, so other commands get in between), so the
breakpoints and the stop live in the game. A second client waits until the first one closes the pipe.

### Transport

Byte stream, UTF-8, one JSON object per line (`\n`). Every request `{"id": N, "cmd": "...", ...}` gets exactly one reply
`{"id": N, "ok": true, ...}` or `{"id": N, "ok": false, "error": "..."}`, in order; several requests may be sent at once
(all lines that arrived are served in the same frame). Nothing is pushed: stops and tracepoint logs wait in an event
queue (1 MB, the oldest dropped and counted) that `events` takes. A request line is at most 1 MB, a reply 16 MB.

The server knows no names. The client resolves them with the loaded object's sidecar (its identity from `hello`):
source lines -> code offsets (pcs), variables -> stack cells relative to the frame's return cell R ("Frames"), fields
-> slots of the object's class chain, globals -> slots of class 0.

### Paths (what `read`, `if` and log `paths` evaluate)

A path is an array of steps; the first step is a start:

    ["frame", F, CELL]   the cell R + CELL of frame F of the task (0: the innermost)
    ["this", F]          frame F's receiver
    ["global", SLOT]     a global
    ["object", ID]       an object by id (the VM's object table)
    ["class", CID]       a class's own object (its static fields)

then any of `["field", SLOT]` (an object's field), `["index", N]` (an array element, or a table's integer key),
`["key", "TEXT"]` (a table's string key). Frame starts need a task: the stopped one, or `"task": ID` (its saved state:
a waiting or ready task).

### Values

`{"t": "int", "v": 5}`, `"double"`, `"int64"`, `{"t": "str", "v": "...", "length": N}` (length when cut at `chars`),
`{"t": "empty"}`, `{"t": "ret", "v": pc}`, `{"t": "obj", "id": ID, "class": CID, "size": FIELDS, "fields": [...]}`
(`"static": true` a class object, `"deleted": true`, `"null": true`), `{"t": "array", "id", "size", "items": [...]}`,
`{"t": "table", "id", "size", "items": [[key, value], ...]}`, `{"error": "..."}` for a path that does not resolve.
Containers are expanded `depth` levels (default 1, at most 8), at most `items` elements (50, at most 100000), strings
to `chars` (2000, at most 1 MB); one reply writes at most a million values, the rest as `{"t": "more"}`.

### Frames (`stack`, `stop`)

`{"pc", "resume", "method" (its entry), "class", "name", "this" (id | null), "depth" (stackEnd - R, larger = deeper),
"argc", "nativeBody", "nativeCase", "msci": {"script", "index"}}`. `resume: false`: pc is the instruction itself (the
stop's frame 0), `true`: a resume pc (callers, a waiting task): its line is pc - 1's ("Raw locations").

### Requests

| cmd | arguments | reply |
|---|---|---|
| `hello` | | `protocol`, `pid`, `edition`, `state` (running / stopped), `stopTimeout`, `objects` [{name, build, size, crc32, file, main}], `msciHead` (the MSCI line loop's head pc, 0: unknown build), `breakpoints` |
| `config` | `stopTimeout` (s), `syncNatives` [names] (the session's at-once natives, kept until detach) | `stopTimeout`, `syncNatives` (count) |
| `break` | `object`, `size`, `crc32` (must be the loaded object's), `pc`; options `log`, `paths`, `labels`, `depth`, `if` [{path, op (== != < <= > >=), value (number, string, null)}], `count`, `once`, `this`, `tag` | `bp` (id), `applied`, `unbound` [the native bindings unbound for it] |
| `clear` | `bp` or `all` | `cleared` |
| `list` | | `breakpoints` [...], `patched` (pcs), `unboundBindings` |
| `events` | `max` (16) | `events` [...], `dropped`, `state`, `stop` (when stopped) |
| `tasks` | `max` | `object`, `tasks` [{task, state (ready / waiting / stopped), object, frames, + the top frame}], `count` |
| `stack` | `task` | `frames` |
| `read` | `paths`, `depth`, `items`, `chars`, `task` | `values` (one per path) |
| `continue` | | (stopped only) |
| `step` | `targets` [{pc, depth: "le" / "lt" / "any"}], `into` | `targets` (set) |
| `pause` | | stops the next task that enters a method |
| `detach` | | `cleared`: every breakpoint removed, the game goes on |
| `eval` | a compiled block (below), `low`, `high`, `frame` N (at a stop) or `on` ID (an object; a class id: its statics) or neither (the globals), `write` [cells it assigns], `budget` (instructions, 10 M), `ms` (2000), `depth`, `items`, `chars` | `value`, `instructions`; a failed run: `failed` (why), `pc` (in the block) |
| `watch` | `object`, `on` ID + `slot` (a field) or `global` SLOT, `change`, the breakpoint options (`log`, `labels`, `ifCode`, `logCode`, `count`, `once`, `tag`) | as `break` (`watch`, `on`, `slot`) |
| `snapshot` | `classes` [ids] (only their objects, no containers), `chars` | `object`, `state`, `f` (globals), `classes` [{id, f}], `objects` [{id, c, f}], `arrays` [{id, v}], `tables` [{id, v: [[k, v]]}], `tasks` (as `tasks`); values: a number, null, {"s"}, {"d"}, {"l"}, {"o"}, {"a"}, {"m"}, {"r"} |
| `reload` | `object`, `size`, `crc32`, `methods` [{entry, end (the loaded method), frame, a compiled block, revert (its bytes are the loaded ones: undone)}], `sidecar` | `placed` [{entry, placement, size, inPlace}], `reloaded`, `serial` |

Stage 2b additions: `hello` gives `stage` 2, `syncNatives` (0: the client sends them with `config`) and per object
`codeSize`, `reloaded`, `sidecar` (the reloaded layout's sidecar the client wrote), `reloadSerial`, `reloads` [{entry,
placement, size}] (the game's whole reload table); `break` takes `ifCode` (a compiled condition, with the path ones) and
`logCode` (a block whose value, an array, is logged: the `log` event's `value`); `list` adds `codeBlocks`, `codeBlockBytes`,
`poolTailBytes`, `reloads`, `retiredBindings`; events: `stop` with `reason` `watch` and `old` / `new`, `log` with
`old` / `new` for a watchpoint, `{"event": "error", "bp", "error"}` (a compiled condition failed: once per breakpoint,
it then counts as false), `dropped` with `tag` (a reload replaced the breakpoint's method).

### Debugger code (compiled blocks)

A compiled block is `{"code": hex, "jumps": [offsets], "strings": [[offset, text]], "low", "high"}`: one method in the
object file's layout compiled at offset 0 (`x3kc/kcsnip.py`; a reload's methods `x3kc/kcreload.py`). The game puts it
into a block of its code tail (1 MB past the object's code), adds the block's offset to the 4-byte targets at `jumps`,
writes the pool offset of each text at its offset (a text the object's pool lacks goes into the pool's tail once,
256 KB, given back when no block and no reload is left) and runs the loader's passes over it. Calls of the object's methods are their loaded
entries. A run (eval, condition, log) is a scratch task: the cells R + low .. R + high of the frame (R its return
cell, the sidecar's cell numbers) copied below a frame whose `this` is the frame's receiver; `write` copies the cells
named back after the run. Natives: only the session's at-once list (`config syncNatives`); another, a fork, a kill of the
held task (the stopped or hit one: it stays alive) or a delete of an object it runs in ends the run. The client refuses
before sending when the snippet, or any method it can reach in the loaded `.obj` (calls by name: every method of that
name), forks or calls a native that may wait (`kcsnip.CallGraph`, cached in `%TEMP%/x3kc_dbg`); what remains is a run the
budget ends inside a game method, whose work stays half done. The snippet's names: the frame's params and locals, `this` and its fields (bare names), globals, constants,
methods, natives; a string literal is a heap string at run time (`"x" + ""`: the tail's strings can then be given back,
and a save never stores a pool offset the original object lacks): no pool string of the tail reaches the
game's state.

### Hot reload

`x3kc dbg reload [File.kc | Class::Method] --src PROJECT` compiles the project (target x3_src-<edition>, the compile
cache in `%TEMP%/x3kc_dbg/cache`), compares each method with the loaded object's `.obj` (its `-kc` file, or the `.obj`
beside its sidecar) and refuses a new layout: classes, parents, every class's field slots, globals, the methods' set
and parameter counts. A changed method goes relocatable (jumps relative, calls the loaded entries, strings by text; a
literal the loaded pool lacks compiled as a heap string); a method the game runs reloaded is sent again even when it is
back to the loaded bytes (a revert). The game refuses a method a task has a frame in. The reloaded layout's sidecar
(the new compile's lines and variables at the pcs the game runs: unchanged methods at their entries, reloaded ones at
their placement; `origin` the loaded entry, `reloaded`; the object's `reload` the serial) goes to
`%TEMP%/x3kc_dbg/<object>-<pid>-r<serial>.kcdbg`, and the game names it in `hello`: every later command reads it. It is
built from the game's whole reload table: a method reloaded before keeps its previous entry, one the game runs as loaded
gets the new compile's lines when its bytes are the loaded ones, else the loaded sidecar's. A revert (the source back,
reloaded again) puts the loaded bytes back and the bindings the reload unbound. The game refuses to save while a
reload is active (a load drops every reload: saved pcs of reloaded code would resume nowhere): revert first. `x3kc symbolize` switches an object's
sidecar at the log's `script debugger: reload N of <object>` line (by the log's process id and N, in `%TEMP%/x3kc_dbg`
or a `--dbg` folder).

Breakpoint options: `count N` - it fires from its Nth passing hit on; `once` - removed after it fired; `this ID` - only
when the frame's receiver is #ID; `if` - every condition must hold (a path with no value equals null only; an object
compares by its id). `log` makes a tracepoint: the `paths` (with their `labels`) go into a `log` event, no stop. The
conditions are the server's pluggable piece (a kind per condition; stage 2b adds compiled KC expressions).

`step`: the stopped task goes on until it reaches one of the targets with its frame depth relation to the stop's frame
(`le`: the same frame or a caller, `lt`: a caller, `any`); `into` also stops at the first statement of the next method
the task enters. The client computes the targets (below). Any breakpoint's stop ends a step; so does `continue`.

### Events

    {"event": "stop", "seq", "reason": "breakpoint" | "step" | "pause", "bp", "tag", "task", "object", <frame 0>, "frames"}
    {"event": "log", "seq", "bp", "tag", "task", "object", <frame 0>, "labels": [...], "values": [...]}
    {"event": "resumed", "seq", "reason": "timeout"}     the client was silent for stopTimeout at a stop
    {"event": "dropped", "seq", "bp", "reason"}          another build of the object was loaded
    {"event": "stepEnded", "seq", "reason": "task gone"} the stepping task ended before a target

### The stop

The game thread stays in the breakpoint: the world does not advance, the window's messages are pumped (keyboard and
mouse dropped, the mouse released), the pipe is served. No request for `stopTimeout` seconds (default 120; `config`,
the environment variable `X3_DBG_STOP_TIMEOUT`): every breakpoint is cleared and the game goes on (a `resumed` event).

### Mechanism (engine side)

A breakpoint replaces the first byte of its instruction by `0xb4` (an opcode the VM does not have); the interpreter's
case for it asks the debugger and runs the replaced opcode. A native body or native case whose C++ runs in place of the
breakpoint's method (a body bound at the method, a body or case that relies on its bytes - FindShip's in-line callees
among them -, the case bound inside it) is unbound while a breakpoint is there (reported in `unbound`); the last
breakpoint gone writes the bytes back exactly (the byte, then the binding). A pc that is not the start of an
instruction (the method walked from its entry) is refused. A load of the object drops the patches with the code and,
at once (in a save's load: once its tasks were checked against the new code), writes them into it when its identity is
the breakpoint's. Breakpoints are at statement starts (the sidecar's line table); a statement that is only a jump
(`break`, `continue`, an `else` jump) may be passed by: the loader threads jumps through such statements.

## x3kc dbg (the client)

    x3kc dbg [--pid N] [--json] [--dbg DIR]... <command> [args]

The game: `--pid`, else the only pipe `x3dbg-*`. A reply is awaited `--deadline S` (default 30, `X3KC_DBG_DEADLINE`): a loading screen or a hung game ends the command with "no reply" (wait: its `--timeout`, exit 2). The sidecars: beside a `-kc` object (hello's `file`), else `--dbg`.
`--json`: one JSON object per line (the replies, events with a `location` added). Exit 0, 1 an error, 2 wait's timeout.

| command | does |
|---|---|
| `games` | the running ScriptDebug games |
| `hello` | the game's state and objects |
| `break LOC [--if PATH OP VALUE]... [--log PATHS] [--depth N] [--count N] [--once] [--this ID] [--object NAME]` | LOC: `File.kc:LINE` (one source file of the sidecar: a folder prefix tells equal names apart; the line's first statement, or the next line with code within 20), `Class::Method` / `Function` (its first statement), `0xPC` |
| `msci break SCRIPT LINE [...]` | an MSCI breakpoint: script name + the editor's line (as ScriptDatabase counts lines: its code array, the commands with flag 131072, the comments), the script loaded in ScriptDatabase.scripts |
| `msci vars [--frame N]` / `msci stack` | the MSCI script's variables (`$name = value (type)`) / the scripts of the stack's `__runScript` frames |
| `wait [--timeout S] [--all]` | the next queued stop or log; at once when stopped |
| `events` | every queued event |
| `stack [--task ID]`, `locals [--frame N] [--depth N]`, `print PATH... [--frame N] [--depth N] [--items N]` | |
| `tasks [--max N]` | the main VM's tasks with their top location (MSCI script and code index) |
| `continue`, `step [over|into|out|msci]`, `pause`, `detach`, `clear [ID | --all]`, `list`, `config --stop-timeout S` | |
| `run FILE` | the commands of FILE, one per line (`#` comments); stops at the first error |
| `eval EXPR [--block] [--file F] [--frame N] [--on ID] [--write] [--budget N] [--ms N] [-v]` | a KC expression / block (`return X;`) in the stopped frame, on an object, or in the globals' context; prints its value |
| `watch this.FIELD \| #ID.FIELD \| class.CLASS.FIELD \| global.NAME [--change] [--if ...] [--log ...] [--once] [--count N]` | a watchpoint: stop / log at the write (the writer's location, old and new value) |
| `snapshot -o FILE.json [--objects CLASS,...] [--chars N]` | the script world with the sidecar's names (objects, globals, statics, arrays, tables, tasks) |
| `diff A.json B.json [--max N]` | what changed between two snapshots (no game needed) |
| `reload [File.kc \| Class::Method] --src PROJECT` | Hot reload (above) |

`--if` takes one KC expression (compiled against the sidecar in the hit frame; all `--if` of a breakpoint hold), or the
old form `PATH OP VALUE` (a path compare); `--log` takes paths or KC expressions (comma-separated at the top level:
when one is not a path they all run as one compiled array).

Locations print as `File.kc:line Class::Method`. PATH: a base - `NAME` (a live variable of the frame, else a field of
`this`, else a global), `local.NAME` / `param.NAME`, `this`, `global.NAME`, `#ID`, `class.CLASS`, `@CELL` (a raw cell of
the frame) - then `.FIELD`, `[N]`, `["KEY"]` and a cast `:CLASS` (whose fields the next `.FIELD` names). `print` reads
an unknown object's class from the game; `--if` / `--log` paths are compiled at `break`, so a field of an object whose
class is not known then needs the cast (`local.target:Ship.hull`).

Steps (client side, from the sidecar): `over` - every statement start of the method outside the current line (depth
`le`) and the caller's return pc (`lt`); `into` - the same with `into`; `out` - the caller's return pc (`lt`); `msci` -
the MSCI line loop's head (`le`): the next MSCI line the task runs in that `__runScript` frame (a script it calls with
`call script` runs in the same frame: msci steps into it).

MSCI breakpoints are KC breakpoints at the line loop head of `702::__runScript` (hello's `msciHead`, checked to be a
statement of the method in the sidecar) with two conditions from the sidecar's `__runScript` variables by name:
`script[0] == SCRIPT` and `pc == the code index`. They unbind the MSCI native case (every script then runs as bytecode
while one exists).

## Module API (`x3kc/kcdbg.py`)

- `DebugInfo.load(path)`: `name`, `build`, `size`, `crc32`, `sha256`, `files`, `globals`, `classes`, `constants`,
  `methods` (sorted by entry); `matches(size=, crc32=, data=)`; `method_at(pc)`; `location(pc, resume=True)` ->
  `Location(file, line, col, method)`; `frame_vars(method, pc, resume=True)` -> `[(name, kind, cell)]`;
  `class_name(cid)`, `field_name(cid, slot)`, `global_name(slot)`.
- `Symbolizer(dirs, warn=, raw=)`: `line(text)` rewrites one log line (and notes load lines); `info(name)`,
  `location(name, pc)`.
- `find(name, dirs, size=, crc32=)` (the sidecar of that identity), `find_all(name, dirs)`, `head(path)` (the
  identity from the first line), `sidecar_path(obj)`, `identity(bytes)`; `collect` / `write` (the compiler's side).
  `DebugInfo.load` reads plain and gzipped files. A method's `origin` is its loaded entry (a reload sidecar's tail
  methods: `entry` is the placement), `reloaded` whether the game runs it reloaded.
- `x3kc/kcsnip.py`: `symbols(info)` (the compiler's symbol table from a sidecar), `compile_snippet(info, text, block=,
  cls=, outer={name: cell})` -> the compiled block plus `cells`, `lines`, `natives`; `relocations(code, pool)`.
- `x3kc/kcreload.py`: `plan(info, obj, src, what, objpath)` (ReloadError: why not), `write_sidecar(path, plan,
  placed, obj)`.
- `x3kc/dbg.py`: `snapshot_diff(a, b, limit)`, `named_snapshot(info, reply)`.
