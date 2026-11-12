"""x3kc command line: `python -m x3kc <command> ...` (or `x3kc <command>` when installed)."""
import sys

from . import __version__

USAGE = """x3kc %s - X3: Terran Conflict / Albion Prelude KC script tools

KC objects (l/*.obj: x3story, x3galedit, x3intro, x3config)
  extract <game dir> <out dir> [--addon addon2]
                                        copy the objects the game loads out of its catalogues
  decompile <obj> <out dir> [--names auto|FILE] [--pcs] [--asm-all] [--no-offsets] [--names-from PREV.obj]
            [--split]                   object -> source project (.kc + .pool + .ptch; --split: a split
                                        project, see split); --no-offsets: no
                                        code offset after `function` lines; --names-from: the previous
                                        build's identifiers (v12, m5, g7 too) stay where the code still
                                        matches (both: small diffs between builds)
  compile <src> -o <out.obj> [--check ORIG] [--base OBJ [--build N] [--force]]
                                        source project -> object; --base keeps old savegames loading.
                                        <src>: a .kc file or a split project (its directory or .kcs index)
  split <file.kc> <dir>                 single-file project -> split project: <name>.kcs index (the order),
                                        @header.kc (directives, globals), one <Class>.kc per class, one
                                        @<Function>.kc per global function; .pool/.ptch copied unchanged
  join <dir | index.kcs> <file.kc>      split project -> single file; join(split(x)) == x byte for byte
  selftest <obj>... [--split]           decompile and recompile, compare byte for byte (--split: through a
                                        split project)
  test <obj> [-v N]                     per-method decompile/regenerate check
  dis <obj> [method | 0xOFFSET] [-n N]  plain disassembly
  names pack <obj> <names.json> <out>   offset-keyed annotations made on <obj> -> portable form
  names unpack <obj> <names.json> <out> portable annotations -> offset-keyed form for <obj>

Checking a build without the game
  verify <obj> [-v]                     static bytecode verifier (compile runs it on every build)
  savecheck <src> <base.obj>            which savegame return sites (calls a task can wait in) the new source
                                        breaks, and why
  methoddiff <old.obj> <new.obj>        methods whose code differs between two builds
  smoke <old.obj> <new.obj> [--budget N] [-v]
                                        run every method of both builds on the emulator, compare outcomes
  bugscan <obj> [--names auto|FILE] -o <out.jsonl>
                                        static scan for typical bug patterns (candidates to review by hand)

MSCI scripts (addon/scripts/*.pck|xml)
  msci-setup <game dir> <work dir> [--addon addon2] [--lang 044] [--xscript x3fl.xml]
                                        texts, ware names, command database, unpacked scripts;
                                        --addon addon2: Farnham's Legacy (X3FL.exe)
  msci learn <db.json> <dir> [-o out.json] [--xscript x3fl.xml] [--no-synthetic]
                                        editor texts the game files lack, from the scripts' saved text; the
                                        rest get synthetic texts from the signature (msci-setup runs it)
  msci ref <db.json> <out.md> [--game NAME] [--map map.json] [--scripts DIR] [--other db.json --other-name N]
           [--json out.json]            command reference by the editor's menu: syntax, parameter types, flags,
                                        KC handler, use in the shipped scripts, engine bugs
  msci dis <db.json> <script.xml>...    script as the game's editor shows it
  msci asm <db.json> <script.xml> <edited.txt> > new.xml
                                        edited text -> script XML (header/arguments from script.xml)
  msci new <db.json> <script.txt> [-o name.xml] [--scripts DIR] [--engine N]
                                        a new script XML from text alone: the `//` header (as `dis` prints it)
                                        gives name, description and arguments
  msci check <db.json> <dir>            decoder vs the <sourceplaintext> of every script
  msci verify <db.json> <dir>           assembler vs the <codearray> of every script
  msci map <x3story.kc|dir> <texts.tsv> <out.md> [--json F]
                                        which KC methods/natives every MSCI command calls
  msci cost <x3story.obj> <db.json> [line ...]
                                        cost of MSCI lines in KC instructions (emulator, game interpreter)
  msci usage <db.json> <dir>            which commands the scripts use, how often inside loops

Game files
  cat <file.cat> [pattern] [-o dir] [--pack PATH=FILE...]   list, extract or write catalogues
  unpck <src dir> <dst dir> [ext]       .pck -> plain files
  texts <game dir> <lang 044> <out.tsv> all texts of a language as the game sees them
  wares <game dir> <texts.tsv> <out.json>   ware names
""" % __version__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    if argv[0] in ('-V', '--version'):
        print(__version__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'msci':
        if not rest:
            print(USAGE)
            return 2
        sub, rest = rest[0], rest[1:]
        table = {'dis': ('msci', 'dis'), 'check': ('msci', 'check'), 'asm': ('msci_asm', 'asm'),
                 'new': ('msci_asm', 'new'),
                 'verify': ('msci_asm', 'check'), 'map': ('msci_map', None),
                 'cost': ('msci_cost', None), 'usage': ('msci_usage', None), 'learn': ('msci_learn', None),
                 'ref': ('msci_ref', None)}
        if sub not in table:
            print('unknown msci command %r' % sub)
            return 2
        mod, word = table[sub]
        return call(mod, 'main', ([word] if word else []) + rest, 'x3kc msci ' + sub)
    if cmd == 'names':
        return call('annotations', 'main', rest, 'x3kc names')
    table = {'extract': ('gameio', 'main_extract'), 'msci-setup': ('gameio', 'main_msci_setup'),
             'decompile': ('kcd', 'main'), 'compile': ('kcc', 'main'), 'selftest': ('kcselftest', 'main'),
             'test': ('kctest', 'main'), 'dis': ('kcdis', 'main'), 'cat': ('catx', 'main'),
             'unpck': ('unpck', 'main'), 'texts': ('texts_tsv', 'main'), 'wares': ('wares', 'main'),
             'verify': ('kcverify', 'main'), 'savecheck': ('savecheck', 'main'), 'methoddiff': ('methoddiff', 'main'),
             'smoke': ('smoke', 'main'), 'bugscan': ('bugscan', 'main'),
             'split': ('kcsplit', 'main_split'), 'join': ('kcsplit', 'main_join')}
    if cmd not in table:
        print('unknown command %r (x3kc help)' % cmd)
        return 2
    mod, fn = table[cmd]
    return call(mod, fn, rest, 'x3kc ' + cmd)


def call(mod, fn, args, prog):
    import importlib
    m = importlib.import_module('x3kc.' + mod)
    old = sys.argv
    sys.argv = [prog] + args
    try:
        r = getattr(m, fn)()
    finally:
        sys.argv = old
    return r or 0


if __name__ == '__main__':
    sys.exit(main())
