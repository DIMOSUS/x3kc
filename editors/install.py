"""Install or remove the KC grammar for Visual Studio (TextMate) or VS Code. Only when you run it.

    python editors/install.py vs                 copy to %USERPROFILE%/.vs/Extensions/x3kc-kc/Syntaxes/
    python editors/install.py vs --uninstall     remove that folder
    python editors/install.py vscode             copy to %USERPROFILE%/.vscode/extensions/x3kc.x3kc-kc-<version>/
    python editors/install.py vscode --uninstall
    add --dry-run to print what would be done

Visual Studio reads user grammars at start-up: restart it after installing. A marker file x3kc-installed.txt is
written into the target, and --uninstall refuses to delete a folder that does not have it.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, 'kc-textmate')
MARKER = 'x3kc-installed.txt'


def targets():
    home = os.path.expanduser('~')
    with open(os.path.join(PKG, 'package.json'), encoding='utf-8') as f:
        p = json.load(f)
    return {
        # Visual Studio: <profile>/.vs/Extensions/<name>/Syntaxes/*.tmLanguage(.json)
        'vs': (os.path.join(home, '.vs', 'Extensions', 'x3kc-kc'),
               [('syntaxes/kc.tmLanguage.json', 'Syntaxes/kc.tmLanguage.json'),
                ('language-configuration.json', 'Syntaxes/language-configuration.json')]),
        # VS Code: an unpacked extension folder <publisher>.<name>-<version>
        'vscode': (os.path.join(home, '.vscode', 'extensions', '%s.%s-%s' % (p['publisher'], p['name'], p['version'])),
                   [('package.json', 'package.json'),
                    ('language-configuration.json', 'language-configuration.json'),
                    ('syntaxes/kc.tmLanguage.json', 'syntaxes/kc.tmLanguage.json')]),
    }


def install(dest, files, dry):
    if os.path.isdir(dest) and not os.path.isfile(os.path.join(dest, MARKER)):
        sys.exit('%s exists and was not made by this script; remove it yourself' % dest)
    for src, rel in files:
        d = os.path.join(dest, rel)
        print('copy %s -> %s' % (os.path.join(PKG, src), d))
        if not dry:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(os.path.join(PKG, src), d)
    if not dry:
        with open(os.path.join(dest, MARKER), 'w', encoding='utf-8') as f:
            f.write('installed by x3kc editors/install.py; remove with --uninstall\n')
    print('done; restart the editor' if not dry else '(dry run)')


def uninstall(dest, dry):
    if not os.path.isdir(dest):
        print('not installed:', dest)
        return
    if not os.path.isfile(os.path.join(dest, MARKER)):
        sys.exit('%s was not made by this script (no %s); not touching it' % (dest, MARKER))
    print('remove', dest)
    if not dry:
        shutil.rmtree(dest)
    print('done' if not dry else '(dry run)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('editor', choices=('vs', 'vscode'))
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    dest, files = targets()[a.editor]
    if a.uninstall:
        uninstall(dest, a.dry_run)
    else:
        install(dest, files, a.dry_run)


if __name__ == '__main__':
    main()
