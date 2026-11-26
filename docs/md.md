# Mission Director text form (.xmd)

Mission Director files (`addon/director/*.xml`, `addon2/director/*.xml` for Farnham's Legacy) are XML: deep,
wordy and costly to read for people and agents. `x3kc md` turns a director file into a C-like text and back:

```
x3kc md dis <file.xml | dir>... [-o out]     XML -> text (.xmd); checks that the text rebuilds the file byte for byte
x3kc md asm <file.xmd | dir>... [-o out]     text -> XML; unknown names, braces and cue references checked
x3kc md check <file.xml | dir>... [-v]       dis + asm in memory, compare, print N/N identical (writes nothing)
x3kc md schema <out.json> <director dir>...  rebuild x3kc/data/md_schema.json (see «Schema»)
```
A directory takes every director `.xml` (`dis`, `check`) or every `.xmd` (`asm`) in it; with several inputs `-o` is a
directory (default: next to each input). `dis --no-verify` skips the rebuild check, `asm --no-warn` the warnings.

**The round trip is lossless**: `asm(dis(x)) == x` byte for byte for every stock file — AP 88/88, FL 85/85 — including
the prolog, encoding and BOM, line ends, indentation, blank lines, comments, attribute order, quoting and spacing,
and the `<signature>`. Whatever the structure does not predict is carried by a small mark in the text (below);
the stock set needs few of them.

## Example

XML (FL `2.183 Dual Convoy.xml`):
```xml
        <cue name="L2M183 Offer">
          <condition>
            <cue_completed cue="L2M183"/>
          </condition>
          <action>
            <do_choose>
              <do_when value="{object.exists@L2M183.StartStation1}*{object.exists@L2M183.EndStation2}" exact="0">
                <set_group_invincible group="L2M183.StationGroup" invincible="0"/>
                <cancel_cue cue="L2M183"/>
              </do_when>
              <do_otherwise>
                <set_value name="this.SectorChanges" exact="0"/>
                <set_value name="L2M183 Offer.PageID" exact="3383"/>
                <create_actor name="L2M183 Offer.Guide" race="{value@L2M183.OfferRace}" voiceflags="32" object="L2M183.OfferStation" location="crew"/>
                <create_offer actor="L2M183 Offer.Guide" conversation="L2OfferNormal" discipline="XFXX " cue="L2M183 Offer"/>
              </do_otherwise>
            </do_choose>
          </action>
          <cues>
            <cue name="L2M183 Briefing">
```
Text:
```c
  cue "L2M183 Offer" {
    condition {
      cue_completed(L2M183);
    }
    action {
      if ({object.exists@L2M183.StartStation1}*{object.exists@L2M183.EndStation2} == 0) {
        set_group_invincible(L2M183.StationGroup, invincible: 0);
        cancel_cue(L2M183);
      } else {
        this.SectorChanges = 0;
        "L2M183 Offer.PageID" = 3383;
        create_actor("L2M183 Offer.Guide", race: $L2M183.OfferRace, voiceflags: 32, object: L2M183.OfferStation, location: crew);
        create_offer("L2M183 Offer.Guide", conversation: L2OfferNormal, discipline: "XFXX ", cue: "L2M183 Offer");
      }
    }
    cue "L2M183 Briefing" {
```
(The `<do_when>` value is shortened here; the real lines are longer in both forms.)

**Size over the stock sets** (all files, measured with `dis`):

| | XML chars | text chars | | tokens, approx. | | lines |
|---|---|---|---|---|---|---|
| AP, 88 files | 5 287 417 | 3 269 825 | **1.62×** | 1 118 095 → 778 663 | **1.44×** | 70 970 → 62 307 |
| FL, 85 files | 3 219 913 | 2 178 627 | **1.48×** | 848 313 → 598 031 | **1.42×** | 51 591 → 45 858 |

Tokens are approximated as runs of letters, runs of digits and single punctuation characters (no tokenizer was
available); whitespace is not counted. The floor is the content itself: in FL the attribute values, comments and
the signatures alone are about 1.07 M characters, a third of the XML, and the text keeps every one of them. The
structure that remains (element names, attribute names that are not positional) is what a reader needs.
Indentation is about a fifth of the text's characters but costs little in tokens.

## File layout

```
#eol crlf                    the file's line ends are CR LF (default LF)
#bom                         the file starts with a UTF-8 BOM
#encoding utf-8              the encoding (default ISO-8859-1, spelled as in the XML declaration)
#prolog "<?xml ...?>\n"      everything before <director>, when it is not the usual two lines
#indent "\t"                 the XML indentation step (default two spaces; dis picks the best fit)
#end ""                      the text after </director> (default one line end)
#close "..."                 the whitespace before </director>, when unusual
#director test               <director name="test" xmlns:xsi=... xsi:noNamespaceSchemaLocation="director.xsd">
#root director(name: x)      any other root tag, its attributes as arguments
```
Directives come first, one per line; dis writes only the ones that differ from the default. Then the statements of
`<director>`: `documentation { ... }`, the cues, `signature "...";`. The usual prolog is
`<?xml version="1.0" encoding="ENC" ?>` and `<?xml-stylesheet href="director.xsl" type="text/xsl" ?>`.

The text is UTF-8; `asm` encodes the XML in the file's encoding. Text indentation is free (dis uses two spaces);
only braces count. Blank lines are kept: a blank line in the text is a blank line in the XML.

## Statements

**Generic element** — every element of the schema works this way:
```c
name;                                   <name/>
name(args);                             <name a="..." b="..."/>
name(args) { statements }               <name ...> children </name>
name(args) {}                           <name ...></name>
name(args) "text";                      <name ...>text</name>   (only signature "...";)
```
Arguments keep the XML attribute order. `key: value` names an attribute; the element's *positional* attribute may
come first without its name when it is first in the XML (`cancel_cue(L2M183)` is `cue="L2M183"`,
`check_value({player.age} >= 10s)` is `value=...`). The positional attribute of each element is the one that comes
first in at least 80 % of the stock uses (exact/min/max/list/comment/chance never): `name` for `cue`, `set_value`,
`find_*`, `create_actor`, `param`; `value` for `check_value`, `do_if`, `do_when`; `cue` for `cancel_cue`,
`reset_cue`, `cue_completed`; `object`, `group`, `actor`, `sector`, `race` for the commands that act on them; and so
on — `x3kc/data/md_schema.json`, field `pos`.

**Comparisons** after a positional `value`:

| text | attributes |
|---|---|
| `X == v` | `value="X" exact="v"` |
| `X != v` | `value="X" exact="v" negate="1"` |
| `X >= a` / `X <= b` | `min="a"` / `max="b"` |
| `X in [a, b]` | `min="a" max="b"` |

They chain in attribute order (`X >= a, list: ...`) and can be mixed with named arguments. After any other
positional attribute a comparison is an error (`set_value(X == 3)` would otherwise set `X`).

**Sugar**

| text | XML |
|---|---|
| `cue NAME(args) { ... }` | `<cue name="NAME" ...>` (name first; otherwise `cue(args)`) |
| cues directly in a cue body (or at the top level) | the `<cues>` wrapper; the cues must stand together, and a body with an explicit `cues { }` block takes its cues there |
| `action { a; b; }` (two or more elements; comments do not count) | `<action><do_all> a b </do_all></action>` |
| `action { a; }`, `action { //c a; }` | `<action> a </action>` |
| `@action { a; b; }` | `<action> a b </action>` (no implicit `<do_all>`) |
| `if (args) { ... }` / `if (args);` | `<do_if args>` |
| `if (...) {} else if (...) {} else {}` | `<do_choose><do_when>...<do_when>...<do_otherwise>` |
| `N = V;` | `<set_value name="N" exact="V"/>` |
| `N += V;` `-=` `*=` `/=` `:=` | `<set_value name="N" operation="add|subtract|multiply|divide|set" exact="V"/>` (spaces optional: `N+=1;`) |
| `N += V ~swap;` | the same with `exact` before `operation` in the XML |
| `N++;` / `N--;` | `<set_value name="N" operation="add|subtract"/>` |
| `N = random(a, b);` | `<set_value name="N" min="a" max="b"/>` |
| `N = V, chance: 50;` | further attributes after the assignment |
| `N = V;` inside `params { }` | `<param name="N" value="V"/>` |
| `stmt; /// text` (or `{ /// text`) | a last attribute `comment="text"` |
| `$X` inside a value | `{value@X}` |
| `@X` inside a value (not after a letter, digit, `_` or `}`) | `{param@X}` |

A `do_choose` with a single `do_when` and nothing else stays `do_choose { do_when(...) {...} }` (it would read as
`do_if`). `N` may be a string: `"L2M183 Offer.PageID" = 3383;`.

**Values.** A value without spaces and punctuation stays bare: letters, digits and `_ . { } @ | + - * / % ? $`
(`{player.sector}`, `this.X`, `{player.age}+600`, `default|terran`). Anything else is a string: `"..."` with C
escapes `\" \\ \n \r \t \xHH`. The general `{...}` expression syntax is kept unchanged; only `$X` and `@X` are
shortened, and inside strings too. `r"..."` is a raw string: no `$`/`@` expansion (dis uses it when a value
contains a literal `$` or `@name` that is not a parameter). XML entities are decoded in the text and written back
as `&amp; &lt; &quot;`; a character the file's encoding lacks (an em dash in an ISO-8859-1 file) is written as a
character reference `&#x2014;`.

**Element text** (only `<signature>` has any) is a string too: `"..."` is the decoded text, written back with
`&amp; &lt;`; `r"..."` is the XML text verbatim. A string statement anywhere else is an error.

**Comments.** `//text` is `<!--text-->` (the text exactly, no space added); `/*text*/` a comment over several
lines; `//~"..."` a comment as an escaped string (when its line ends differ from the file's). A `//` comment on the
line of the previous statement is a comment on the line of the previous tag in the XML. `///` is the `comment`
attribute (above). XML forbids `--` inside a comment and `-` at its end, and a comment cannot hold a character the
file's encoding lacks: asm reports both as errors.

## Layout marks

Rare, written by `dis` only where the structure does not predict the bytes. The indentation of a node is predicted
as its previous sibling's, the first child's as the parent's plus one step (a tab when the parent is indented with
a tab).

| mark | meaning |
|---|---|
| `~"\t\t\t" stmt` | the node's indentation differs from the prediction (the following siblings follow it) |
| `~~"..." stmt` | the exact text before the node (trailing spaces, no line break) |
| `~"..." }` / `~~"..." }` | the same for the end tag |
| `~>" " }` | spaces inside the end tag: `</cue >` |
| `name(a: 1, ~"  " b: 2)` | the spaces before an attribute (two spaces, a line break...) |
| `name(a: 1 ~" ")` | the spaces before `>` or `/>` |
| `` name(`  a = 'x'`) `` | an attribute written verbatim (single quotes, spaces around `=`) |
| `N += V ~swap` | `exact` comes before `operation` |

The marks (and `#indent`, `#end`, `#close`) take whitespace only; anything else is an error.

Over the stock sets the marks are mostly in the files edited by hand with tabs (FL: about 1 100 `~"..."` and 50
`~~"..."` in 85 files).

## Checks in asm

Errors (`file:line: error: ...`, nothing is written for that file):
- `unknown element X`, `X has no attribute Y`, `attribute Y given twice` — against the element table;
- `missing } for the block opened at line N`, `unbalanced '}'`, `expected ';' or '{' after X` (at the line of
  the statement that lacks it),
  `expected ',' or ')' in the arguments of X`;
- `X: positional argument not allowed here (use name: value)` — a second unnamed argument or one after a named one;
- `'else' without 'if'`, `expected '{' after else`;
- `the sub-cues of a cue must stand together: X at line N is between them`, `cue has two <cues>: ...` (cues both
  in a `cues { }` block and directly in the body);
- `a comparison follows only a positional value (here: name)`, `a /// comment and comment: on one element`,
  `the target X+ ends with '+': write the operator with =`;
- `an XML comment cannot contain '--' or end with '-'`, `'—' (U+2014) cannot be written in ISO-8859-1 in a
  comment`, `text is allowed only in signature`, `the ~ mark takes whitespace only`;
- `unknown escape`, `unterminated string`, `unknown directive #X`, `missing #director line`.

Warnings (`file:line: warning: ...`, the XML is written):
- `X refers to cue 'Y', which this file does not define` — a `cue="..."` that names no cue of the file (values with
  `{...}`, `this...`, `parent...` are skipped). Cues of other files trigger it too: the stock sets give 41 (AP) and
  25 (FL); 8 and 7 of them name a cue that no stock file of the edition defines (`L0M81 Cleanup Actor`,
  `L0M82 Cleanup Actor`, `L2M145 Mission Reward`, `Destruction And Removal`, `L0M15`, AP also
  `L2M023.A5 Armistice Goner Cease Fire Offer`) — not checked further, candidates for review.

## Schema

`x3kc/data/md_schema.json` lists every element with its attributes and positional attribute. It is built by
`x3kc md schema` from `director.xsd` + `dirobjdb.xsd` of AP and FL (attributes, attribute groups, type extensions
resolved) united with what the stock files use: 446 elements. Children are not checked against the schema.

## The cue map

`x3kc/mdmap.py` maps the source to what the running game has (used by `x3kc dbg md`, `docs/debugging.md`). Each file
is parsed in both forms (the `.xmd` text and its XML, or the XML and its `dis` text) and the two trees are paired element
by element, so every element has its `.xmd` line and its XML line. The director (x3story `CueDirector.LoadArray` /
`LoadTable`) makes each `<cue>` a Cue object whose fields hold tables of the XML nodes:

- `conditionDef` = the first element inside `<condition>`, `actionDef` the first inside `<action>`, `forceDef`
  `<force>`'s, `timingDef` `<timing>` itself; later elements of a `<condition>` / `<action>` are ignored by the loader
  (the map notes them; none in the stock files);
- a table is the node's attributes plus `code` (the tag, when it has a `_` after its first letter); a group node
  (check_all, check_any, do_*, data) keeps its element children in `data` (`["data"][i]`, document order, comments
  and text left out); any other node keeps them by tag (`["sector"]`, a repeated tag `["ware"][k]`);
- so an element's identity at run time is the table at its path, e.g. `actionDef["data"][1]["data"][0]` (an
  instance shares its static cue's tables); `Cue::CheckCondition` evaluates a condition element, `Cue::DoAction` runs
  an action element, `Cue::DoAll` a do_when / do_otherwise branch (the children of a do_choose); a node under a
  non-group element is an argument, never run itself;
- the text's implicit `<do_all>` (`action { a; b; }`) has the `action {` line; `if (...) {` is the do_choose and its
  first do_when (a breakpoint there: the do_choose), `} else {` a do_when or do_otherwise;
- a `<cue ref="L">` runs the record of the cue L under the name L (Cue::GetLibrary: any entry of that name that is
  not itself a reference, a library or a normal cue): its parts and sub-cues are L's; a part reference
  (`<action ref="L"/>`, also condition, timing, force, cues, patch) puts L's part - the same table - into the
  referencing cue, which runs it under its own name (`Cue.part_refs`, resolved in `resolve_refs`; `MdMap.runners(cue,
  part)` lists every cue that runs a cue's tables). The map takes a cue L in the same file first (a library before a
  normal cue), else the first in the map's files; the game searches the parent's sub-cues, then up the parents;
- `MdMap.ambiguous(name)`: the name is used by cues that run different tables (references of one cue count as one).

Checked against a running AP game (`x3kc dbg md verify`: the `code` at each path of every live cue): 4345 elements of
392 cues, all as the map says; with the part references resolved, 3655 elements of 214 cues on another session, all as
the map says. Building the map: AP 88 files, 2516 cues in about 1 s (cached after).

    x3kc dbg md --md DIR map ["CUE" | FILE.xmd:LINE]    # no game needed

## Signature

`<signature>` is kept verbatim (`signature "...";`). It cannot be recomputed: the director loader (KC
`CueDirector.LoadFile`, in x3story) checks it with `VerifyData(signature, <cues> inner text + file name)` — an
RSA-2048 PKCS#1 v1.5 signature over MD5 (Windows CryptoAPI), checked against a public key built into the game
(`GetPubKey`); the private key is Egosoft's. So the signature stays valid only while the bytes inside `<cues>` and the
file name are unchanged, which `md dis`/`md asm` without edits guarantees. When an edited file fails the check:
the game already "modified" loads it anyway; an unmodified game loads a required file (listed in `mdfiles`) and
marks itself modified, and skips an extra file.
