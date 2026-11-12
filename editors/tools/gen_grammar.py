"""Generate the KC TextMate grammar (editors/kc-textmate/syntaxes/kc.tmLanguage.json).

The token set follows x3kc/kcparse.py (lexer TOKEN, the keywords and contextual words the parser tests with at() /
accept(), the builtins of BodyParser.builtin and name_value). The native names come from x3kc/natives.txt, the asm
mnemonics from x3kc/kcisa.py (MN). Run it again when either list changes:

    python -X utf8 -B editors/tools/gen_grammar.py

Speed rules (the grammar must stay fast on an 8 MB file):
- every pattern starts with a literal, a word boundary or a fixed character class; no pattern has a nested or
  unbounded lookahead; lookbehinds are one fixed character;
- every rule is line-local except the block comment, the function parameter list and the asm body;
- the 987 native names form one alternation grouped by prefix (B3D_(?:...)|SA_(?:...)...), so a non-native word
  fails on its first letters.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, 'editors', 'kc-textmate', 'syntaxes', 'kc.tmLanguage.json')

ID = r'[A-Za-z_][A-Za-z0-9_]*'
NUM = r'(?:0[xX][0-9a-fA-F]+|[0-9]+)'


def natives():
    names = []
    with open(os.path.join(ROOT, 'x3kc', 'natives.txt'), encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                names.append(line)
    groups = {}
    for n in names:
        pre, rest = n.split('_', 1)
        groups.setdefault(pre, []).append(rest)
    alts = []
    for pre in sorted(groups):
        rests = sorted(set(groups[pre]), key=lambda s: (-len(s), s))    # longest first: no prefix shadowing
        alts.append('%s_(?:%s)' % (pre, '|'.join(re.escape(r) for r in rests)))
    return names, sorted(groups), r'\b(?:%s)\b' % '|'.join(alts)


def mnemonics():
    sys.path.insert(0, ROOT)
    from x3kc import kcisa
    return sorted(kcisa.MN.values(), key=lambda s: (-len(s), s))


def grammar():
    native_list, prefixes, native_re = natives()
    mn = mnemonics()
    repo = {}
    repo['comments'] = {'patterns': [
        {'name': 'comment.block.kc', 'begin': r'/\*', 'end': r'\*/',
         'beginCaptures': {'0': {'name': 'punctuation.definition.comment.begin.kc'}},
         'endCaptures': {'0': {'name': 'punctuation.definition.comment.end.kc'}}},
        {'name': 'comment.line.double-slash.kc', 'match': r'(//).*$',
         'captures': {'1': {'name': 'punctuation.definition.comment.kc'}}},
    ]}
    # '#' starts a directive anywhere (lexer: \#[^\n]*); the parser drops what follows '//'
    repo['directive'] = {
        'name': 'meta.preprocessor.directive.kc',
        'begin': r'(#)\s*(object|sections|pool|patch|stamp|' + ID + r')?',
        'beginCaptures': {'1': {'name': 'punctuation.definition.directive.kc'},
                          '2': {'name': 'keyword.control.directive.kc'}},
        'end': r'(?=//)|$',
        'patterns': [
            {'include': '#numbers'},
            {'name': 'support.constant.section.kc', 'match': r'\b(?:CODE|SYMB|VARS|CLAS|STRG|STRU|PTCH)\b'},
            {'name': 'string.unquoted.path.kc', 'match': r'[^\s/]+'},
        ]}
    repo['strings'] = {
        'name': 'string.quoted.double.kc',
        'begin': '"', 'end': r'"|$',
        'beginCaptures': {'0': {'name': 'punctuation.definition.string.begin.kc'}},
        'endCaptures': {'0': {'name': 'punctuation.definition.string.end.kc'}},
        'patterns': [
            {'name': 'constant.character.escape.kc', 'match': r'\\(?:[nrt\\"]|x[0-9a-fA-F]{2})'},
            {'name': 'invalid.illegal.unknown-escape.kc', 'match': r'\\.?'},
            # printf conversions of SE_SPrintf & co. (kcnatives.SPEC); %% included
            {'name': 'constant.other.placeholder.kc',
             'match': r'%(?:![0-9])?[-+ #0]*(?:\*|[0-9]+)?(?:\.(?:\*|[0-9]*))?[hlL]?[diouxXcsfeEgGpaA%]'},
        ]}
    repo['numbers'] = {'patterns': [
        {'name': 'constant.numeric.hex.kc', 'match': r'\b0[xX][0-9a-fA-F]+\b'},
        {'name': 'constant.numeric.float.kc', 'match': r'\b[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?\b'},
        {'name': 'constant.numeric.float.kc', 'match': r'\b[0-9]+[eE][+-]?[0-9]+\b'},
        {'name': 'constant.numeric.decimal.kc', 'match': r'\b[0-9]+\b'},
    ]}
    repo['stamp'] = {
        'match': r'(@)\s*(-?' + NUM + r')',
        'captures': {'1': {'name': 'storage.modifier.stamp.kc'}, '2': {'name': 'constant.numeric.stamp.kc'}}}
    # class Name [= id] [: Parent] [@stamp] {
    repo['class-decl'] = {
        'match': r'\b(class)\s+(' + ID + r')(?:\s*(=)\s*(-?' + NUM + r'))?(?:\s*(:)\s*(' + ID + r'))?',
        'captures': {'1': {'name': 'storage.type.class.kc'},
                     '2': {'name': 'entity.name.type.class.kc'},
                     '3': {'name': 'keyword.operator.assignment.kc'},
                     '4': {'name': 'constant.numeric.class-id.kc'},
                     '5': {'name': 'punctuation.separator.inheritance.kc'},
                     '6': {'name': 'entity.other.inherited-class.kc'}}}
    # function Name(params) [frame N] [asm] {
    repo['function-decl'] = {
        'name': 'meta.function.kc',
        'begin': r'\b(function)\s+(' + ID + r')\s*(\()',
        'beginCaptures': {'1': {'name': 'storage.type.function.kc'},
                          '2': {'name': 'entity.name.function.kc'},
                          '3': {'name': 'punctuation.definition.parameters.begin.kc'}},
        'end': r'\)',
        'endCaptures': {'0': {'name': 'punctuation.definition.parameters.end.kc'}},
        'patterns': [
            {'include': '#comments'},
            {'name': 'variable.parameter.kc', 'match': ID},
            {'name': 'punctuation.separator.parameter.kc', 'match': ','},
        ]}
    repo['frame'] = {
        'match': r'(?<=\))\s*(frame)\s+(-?' + NUM + r')',
        'captures': {'1': {'name': 'storage.modifier.frame.kc'}, '2': {'name': 'constant.numeric.frame.kc'}}}
    # var name [= init] ;   (field, global or local)
    repo['var-decl'] = {
        'match': r'\b(var)\s+(' + ID + r')',
        'captures': {'1': {'name': 'storage.type.var.kc'}, '2': {'name': 'variable.other.declaration.kc'}}}
    # switch (e) : dense(lo, hi) | : sparse
    repo['switch-mode'] = {
        'match': r'(?<=\))\s*(:)\s*(dense|sparse)\b',
        'captures': {'1': {'name': 'punctuation.separator.kc'}, '2': {'name': 'storage.modifier.switch-table.kc'}}}
    repo['asm-body'] = {
        'name': 'meta.asm.kc',
        'begin': r'\b(asm)\s*(\{)',
        'beginCaptures': {'1': {'name': 'storage.modifier.asm.kc'}, '2': {'name': 'punctuation.section.block.begin.kc'}},
        'end': r'\}',
        'endCaptures': {'0': {'name': 'punctuation.section.block.end.kc'}},
        'patterns': [
            {'include': '#comments'},
            {'include': '#strings'},
            {'include': '#numbers'},
            {'match': r'(' + ID + r')\s*(:)(?!:)',
             'captures': {'1': {'name': 'entity.name.label.kc'}, '2': {'name': 'punctuation.separator.label.kc'}}},
            {'begin': r'\{', 'end': r'\}', 'patterns': [            # sswitch tables {k: L, ...}{"s": L, ...}
                {'include': '#strings'}, {'include': '#numbers'},
                {'name': 'entity.name.label.kc', 'match': ID}, {'name': 'punctuation.separator.kc', 'match': r'[:,]'}]},
            {'name': 'keyword.other.mnemonic.kc', 'match': r'\b(?:%s)\b' % '|'.join(mn)},
            {'include': '#native-names'},
            {'include': '#qualified'},                              # callc C150::F, callg ::F
            {'name': 'punctuation.separator.kc', 'match': r'::|[\[\],-]'},
            {'name': 'entity.name.label.kc', 'match': ID},
        ]}
    repo['native-names'] = {'patterns': [
        {'name': 'support.function.native.kc', 'match': native_re},
        # a group prefix with a name x3kc does not know: kcc warns about such calls
        {'name': 'support.function.native.unlisted.kc', 'match': r'\b(?:%s)_[A-Za-z0-9_]+\b' % '|'.join(prefixes)},
    ]}
    repo['keywords'] = {'patterns': [
        {'name': 'keyword.control.kc',
         'match': r'\b(?:if|else|while|for|do|switch|case|default|return|break|continue|fork)\b'},
        {'name': 'keyword.operator.new.kc', 'match': r'\b(?:new|delete)\b'},
        {'name': 'variable.language.this.kc', 'match': r'\bthis\b'},
        {'name': 'storage.type.kc', 'match': r'\b(?:var|function|class)\b'},
        # BodyParser.builtin / Parser.field: raw member, global, double, single-operand logic, non-short-circuit
        {'name': 'support.function.intrinsic.kc', 'match': r'\b(?:__m|__g|__f64|__and|__or|__opland|__oplor|__init)\b'},
        # name_value: frame cells
        {'name': 'variable.language.frame.kc', 'match': r'\b(?:__argc|__retpc|__ctx)\b'},
    ]}
    repo['qualified'] = {'patterns': [
        # native::SE_X(   __global::F(
        {'match': r'\b(native)\s*(::)\s*(' + ID + r')',
         'captures': {'1': {'name': 'keyword.other.qualifier.kc'}, '2': {'name': 'punctuation.separator.scope.kc'},
                      '3': {'name': 'support.function.native.kc'}}},
        {'match': r'\b(__global)\s*(::)\s*(' + ID + r')',
         'captures': {'1': {'name': 'keyword.other.qualifier.kc'}, '2': {'name': 'punctuation.separator.scope.kc'},
                      '3': {'name': 'entity.name.function.call.kc'}}},
        # C1000::F(   x.C150::F(
        {'match': r'\b(' + ID + r')\s*(::)\s*(' + ID + r')',
         'captures': {'1': {'name': 'entity.name.type.class.kc'}, '2': {'name': 'punctuation.separator.scope.kc'},
                      '3': {'name': 'entity.name.function.call.kc'}}},
        # ::F(   global function explicitly
        {'match': r'(::)\s*(' + ID + r')',
         'captures': {'1': {'name': 'punctuation.separator.scope.kc'}, '2': {'name': 'entity.name.function.call.kc'}}},
        # this->F(
        {'match': r'\b(this)\s*(->)\s*(' + ID + r')',
         'captures': {'1': {'name': 'variable.language.this.kc'}, '2': {'name': 'punctuation.accessor.arrow.kc'},
                      '3': {'name': 'entity.name.function.member.kc'}}},
        # new Name(
        {'match': r'\b(new)\s+(' + ID + r')(?=\s*\()',
         'captures': {'1': {'name': 'keyword.operator.new.kc'}, '2': {'name': 'entity.name.type.class.kc'}}},
        # x.F(   C150.F(
        {'match': r'(\.)\s*(' + ID + r')\b(?!\s*::)',       # x.C150::F goes to the qualified rule above
         'captures': {'1': {'name': 'punctuation.accessor.dot.kc'}, '2': {'name': 'entity.name.function.member.kc'}}},
    ]}
    repo['names'] = {'patterns': [
        {'include': '#native-names'},
        {'name': 'entity.name.function.call.kc', 'match': ID + r'(?=\s*\()'},
        {'name': 'entity.name.type.class.kc', 'match': r'\bC[0-9]+\b'},             # default class names
        {'name': 'variable.parameter.kc', 'match': r'\barg[0-9]+\b'},
        {'name': 'variable.other.local.kc', 'match': r'\bv[0-9]+\b'},
        {'name': 'variable.other.member.kc', 'match': r'\bm[0-9]+\b'},
        {'name': 'variable.other.global.kc', 'match': r'\bg[0-9]+\b'},
        {'name': 'variable.other.kc', 'match': ID},
    ]}
    repo['operators'] = {'patterns': [
        {'name': 'keyword.operator.assignment.compound.kc', 'match': r'<<=|>>=|[-+*/%&|^]='},
        {'name': 'keyword.operator.comparison.kc', 'match': r'==|!=|<=|>='},
        {'name': 'keyword.operator.increment.kc', 'match': r'\+\+|--'},
        {'name': 'keyword.operator.logical.kc', 'match': r'&&|\|\||!'},
        {'name': 'keyword.operator.bitwise.shift.kc', 'match': r'<<|>>'},
        {'name': 'keyword.operator.comparison.kc', 'match': r'[<>]'},
        {'name': 'keyword.operator.assignment.kc', 'match': '='},
        {'name': 'keyword.operator.bitwise.kc', 'match': r'[&|^~]'},
        {'name': 'keyword.operator.arithmetic.kc', 'match': r'[-+*/%]'},
        {'name': 'keyword.operator.ternary.kc', 'match': r'[?:]'},
        {'name': 'punctuation.terminator.statement.kc', 'match': ';'},
        {'name': 'punctuation.separator.comma.kc', 'match': ','},
        {'name': 'punctuation.section.braces.kc', 'match': r'[{}]'},
        {'name': 'punctuation.section.parens.kc', 'match': r'[()]'},
        {'name': 'punctuation.section.brackets.kc', 'match': r'[\[\]]'},
        {'name': 'punctuation.accessor.dot.kc', 'match': r'\.'},
        # anything the lexer refuses ("bad character")
        {'name': 'invalid.illegal.character.kc', 'match': r'[^\sA-Za-z0-9_"#/*+\-%&|^!~<>=?:;,.(){}\[\]@]'},
    ]}
    order = ['comments', 'directive', 'strings', 'class-decl', 'function-decl', 'asm-body', 'frame', 'switch-mode',
             'var-decl', 'stamp', 'numbers', 'qualified', 'keywords', 'names', 'operators']
    g = {
        '$schema': 'https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json',
        'name': 'KC',
        'scopeName': 'source.kc',
        'fileTypes': ['kc'],
        'comment': 'KC script language of X3 (x3kc). Generated by editors/tools/gen_grammar.py from x3kc/kcparse.py, '
                   'x3kc/natives.txt (%d natives) and x3kc/kcisa.py - do not edit by hand.' % len(native_list),
        'patterns': [{'include': '#' + k} for k in order],
        'repository': repo,
    }
    return g


def main():
    g = grammar()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(g, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print('wrote', OUT)


if __name__ == '__main__':
    main()
