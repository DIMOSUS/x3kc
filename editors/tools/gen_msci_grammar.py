"""Generate the TextMate grammar of the MSCI script text form (editors/kc-textmate/syntaxes/msci.tmLanguage.json).

The text form is what `x3kc msci dis` prints and `x3kc msci new` / `asm` read: one command per line, blocks by
indentation, `@` = interruptible, `*` lines = comments / disabled commands, `//` header lines (name, version,
engine, command, arguments). Command words come from the command database (docs/msci_commands_ap.json and
_fl.json, the `text` templates; `%N` is a parameter): every literal fragment of a template with at least three
letters becomes one alternative of `keyword.other.command`, longest first.

    python -X utf8 -B editors/tools/gen_msci_grammar.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, 'editors', 'kc-textmate', 'syntaxes', 'msci.tmLanguage.json')

FLOW = ['else if not', 'else if', 'skip if not', 'skip if', 'while not', 'do if', 'if not', 'if', 'else', 'end',
        'while', 'break', 'continue', 'return', 'endsub', 'gosub', 'goto label', 'START']


def fragments():
    out = set()
    for ed in ('ap', 'fl'):
        p = os.path.join(ROOT, 'docs', 'msci_commands_%s.json' % ed)
        if not os.path.isfile(p):
            continue
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        for c in d['commands']:
            # pageid= / textid= stay out of the command words: the text-reference rule takes them with their numbers
            for part in re.split(r'%\d|(?<![\w])(?:pageid|textid|page|id)=', c.get('text') or ''):
                s = ' '.join(part.split())
                if sum(ch.isalpha() for ch in s) >= 3 and not s.startswith('<'):
                    out.add(s)
    return sorted(out, key=lambda s: (-len(s), s))


def frag_rx(s):
    words = s.split(' ')
    rx = r'\s+'.join(re.escape(w) for w in words)
    if s[0].isalnum():
        rx = r'(?<![\w$.])' + rx
    if s[-1].isalnum():
        rx += r'(?![\w])'
    return rx


def trie_rx(frs):
    """The fragments as one word trie: get(?:\\s+(?:player(?:\\s+(?:name|money)...)...)), so a line is tried
    against a few first words instead of ~2900 alternatives."""
    root = {}
    for s in frs:
        node = root
        for w in s.split(' '):
            node = node.setdefault(w, {})
        node[''] = {}

    def emit(node):
        alts = []
        for w in sorted((k for k in node if k), key=lambda k: (-len(k), k)):
            child = node[w]
            tail = r'(?![\w])' if w[-1].isalnum() else ''
            cont = emit(child) if any(k for k in child) else None
            ew = re.escape(w)
            if cont and '' in child:
                alts.append('%s(?:\\s+%s|%s)' % (ew, cont, tail))
            elif cont:
                alts.append('%s\\s+%s' % (ew, cont))
            else:
                alts.append(ew + tail)
        return '(?:%s)' % '|'.join(alts)

    first_alnum = [w for w in root if w and w[0].isalnum()]
    other = [w for w in root if w and not w[0].isalnum()]
    parts = []
    if first_alnum:
        parts.append(r'(?<![\w$.])' + emit({w: root[w] for w in first_alnum}))
    if other:
        parts.append(emit({w: root[w] for w in other}))
    return '|'.join(parts)


def grammar():
    frs = fragments()
    repo = {
        'header': {'patterns': [
            {'match': r'^(//)\s*(version|engine|command)(:)\s*(-?\d+)',
             'captures': {'1': {'name': 'punctuation.definition.comment.msci'},
                          '2': {'name': 'keyword.other.header.msci'},
                          '3': {'name': 'punctuation.separator.msci'},
                          '4': {'name': 'constant.numeric.msci'}},
             'name': 'meta.header.msci'},
            {'match': r'^(//)\s+(\$[\w.]+)(:)\s*([\w/]+)\s*("[^"]*")?',
             'captures': {'1': {'name': 'punctuation.definition.comment.msci'},
                          '2': {'name': 'variable.parameter.msci'},
                          '3': {'name': 'punctuation.separator.msci'},
                          '4': {'name': 'support.type.msci'},
                          '5': {'name': 'string.quoted.double.msci'}},
             'name': 'meta.header.argument.msci'},
            {'match': r'^(//)\s*([\w.!\-]+)\s*(\(.*\))?\s*$',
             'captures': {'1': {'name': 'punctuation.definition.comment.msci'},
                          '2': {'name': 'entity.name.function.script.msci'},
                          '3': {'name': 'comment.line.description.msci'}},
             'name': 'meta.header.name.msci'},
            {'match': r'^\s*//.*$', 'name': 'comment.line.double-slash.msci'},
        ]},
        'comment': {'match': r'^\s*(@\s*)?\*.*$', 'name': 'comment.line.asterisk.msci'},
        'label': {'match': r'^\s*([\w.]+)(:)\s*$',
                  'captures': {'1': {'name': 'entity.name.label.msci'}, '2': {'name': 'punctuation.separator.msci'}}},
        'interruptible': {'match': r'^\s*(@)', 'captures': {'1': {'name': 'storage.modifier.interruptible.msci'}}},
        'flow': {'patterns': [
            {'match': r'\b(gosub|goto label)\s+([\w.]+)',
             'captures': {'1': {'name': 'keyword.control.msci'}, '2': {'name': 'entity.name.label.msci'}}},
            {'match': r'(?<![\w$.])(%s)(?![\w])' % '|'.join(r'\s+'.join(w.split()) for w in FLOW),
             'name': 'keyword.control.msci'},
        ]},
        'script-call': {'match': r'(?<![\w$.])((?:call|interrupt with|start|run|unregister) script|with script|'
                                 r'to call script|named script|(?:event |callback |check)?script=|scriptname=)'
                                 r'\s*([A-Za-z!_][\w.!\-]*)',
                        'captures': {'1': {'name': 'keyword.other.command.msci'},
                                     '2': {'name': 'entity.name.function.script.msci'}}},
        'text-ref': {'patterns': [
            {'match': r'\{\s*\d+\s*,\s*\d+\s*\}', 'name': 'constant.other.text-reference.msci'},
            {'match': r'(?<![\w$.])(pageid=|page=)\s*(-?\d+)(,?\s*)(textid=|id=)\s*(-?\d+)',
             'captures': {'1': {'name': 'keyword.other.command.msci'}, '2': {'name': 'constant.other.text-reference.msci'},
                          '4': {'name': 'keyword.other.command.msci'}, '5': {'name': 'constant.other.text-reference.msci'}}},
        ]},
        'strings': {'name': 'string.quoted.single.msci', 'begin': "'", 'end': "'|$",
                    'patterns': [{'include': '#text-ref'}]},
        'variables': {'match': r'\$[\w.]+', 'name': 'variable.other.msci'},
        'constants': {'patterns': [
            {'match': r'\[[^\[\]\n]{1,60}\]', 'name': 'constant.language.msci'},
            {'match': r'(?<![\w$.])null(?![\w])', 'name': 'constant.language.null.msci'},
            {'match': r'(?<![\w$.])-?\d+(?![\w])', 'name': 'constant.numeric.msci'},
        ]},
        'commands': {'match': trie_rx(frs), 'name': 'keyword.other.command.msci'},
        'operators': {'patterns': [
            {'match': r'->', 'name': 'keyword.operator.call.msci'},
            {'match': r'==|!=|<=|>=|<|>', 'name': 'keyword.operator.comparison.msci'},
            {'match': r'(?<![\w$.])(?:AND|OR|mod)(?![\w])|[&|^~!]', 'name': 'keyword.operator.logical.msci'},
            {'match': r'[-+*/]', 'name': 'keyword.operator.arithmetic.msci'},
            {'match': '=', 'name': 'keyword.operator.assignment.msci'},
            {'match': r'[,:()]', 'name': 'punctuation.separator.msci'},
        ]},
        'words': {'match': r'[A-Za-z_][\w.]*', 'name': 'meta.value.msci'},
    }
    order = ['header', 'comment', 'label', 'interruptible', 'strings', 'text-ref', 'script-call', 'flow', 'variables',
             'constants', 'commands', 'operators', 'words']
    return {
        '$schema': 'https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json',
        'name': 'MSCI',
        'scopeName': 'source.msci',
        'fileTypes': ['msci'],
        'comment': 'X3 MSCI script text form (x3kc msci dis / new). Generated by editors/tools/gen_msci_grammar.py '
                   'from docs/msci_commands_ap.json and _fl.json (%d command fragments) - do not edit by hand.'
                   % len(frs),
        'patterns': [{'include': '#' + k} for k in order],
        'repository': repo,
    }


def main():
    g = grammar()
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(g, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print('wrote', OUT)


if __name__ == '__main__':
    main()
