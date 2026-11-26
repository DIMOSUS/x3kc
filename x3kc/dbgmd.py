"""x3kc dbg md: Mission Director debugging on top of the KC debugger (no MD hook in the game).

MD runs in KC: every cue is a Cue object (x3story class 308; the director, class 307 CueDirector, is its root), its
condition and action elements are the tables the director built from the XML (x3kc/mdmap.py: the cue map). So

    md break FILE.xmd:LINE | CUE   a KC breakpoint on the Cue method that handles the element (Activate, Complete,
                                   Cancel, Condition / CheckCondition, Action / DoAction / DoAll) whose compiled
                                   condition is `cueName == "CUE" && <its table> == <the element's path>`
    md tree / cue / why            the live cue objects (a snapshot of the Cue classes) and read-only evals on them
    md watch CUE --state           tracepoints on the state changes (Activate, Complete, Cancel, InitData's reset)
    md where / md wait             the MD context of a stop: the cue, its parents, the element being run (found by
                                   the identity of the frame's table among the cue's element tables), its variables
"""
import argparse
import json
import os
import re
import sys
import time

from . import md
from . import mdmap

STATES = {0: 'waiting', 1: 'active', 2: 'complete', 3: 'cancelled'}
CUE_CLASS = 308
DIRECTOR_CLASS = 307
# the Cue methods whose first parameter is an element's table, and that parameter
ELEM_METHODS = {'CheckCondition': 'condition', 'Condition': 'condition', 'DoAction': 'action', 'DoAll': 'action',
                'Action': 'action', 'DoIf': 'action', 'DoAny': 'action', 'CheckValue': 'condition'}
# check_value and its kin: CheckValue(condition, fixed value, unit kind) (Cue::CheckCondition)
VALUE_CODES = {'check_value': 0, 'check_age': 4, 'check_time': 3, 'check_distance': 1}
CUE_EVENTS = ('cue_activated', 'cue_cancelled', 'cue_completed', 'cue_destroyed')
CUE_TESTS = ('cue_exists', 'cue_is_active', 'cue_is_cancelled', 'cue_is_complete', 'cue_timer')
# CueDirector::IsEvent of AP: only a fallback - md why asks the game's own IsEvent (FL has more events)
AP_EVENTS = set('''all_objects_destroyed any_cluster_decoupled any_object_attacked any_object_attacked_by_player
any_object_captured any_object_changed_sector any_object_claimed any_object_collected any_object_destroyed
any_object_destroyed_by_player any_object_docked any_object_ejected any_object_finished any_object_repaired
any_object_repaired_by_player any_object_scanned any_object_targeted any_object_undocked briefing_closed
briefing_viewed cluster_decoupled conversation_completed cue_activated cue_cancelled cue_completed cue_destroyed
cutscene_started cutscene_stopped object_attacked object_attacked_by_player object_captured object_changed_sector
object_claimed object_collected object_destroyed object_destroyed_by_player object_docked object_ejected
object_finished object_repaired object_repaired_by_player object_scanned object_targeted object_undocked
objective_cancelled player_built_station player_changed_ship player_killed_object player_owned_killed_object
player_scanned_object player_target_lost player_target_set question_answered'''.split())


def kc_str(s):
    return json.dumps(s, ensure_ascii=False)


def state_name(v):
    return STATES.get(v, 'state %s' % v)


def sval(v):
    """a snapshot value -> python: {"s": x} -> x, {"o": id} -> ('o', id), containers ('a'|'m', id)"""
    if isinstance(v, dict):
        k, x = next(iter(v.items()))
        if k == 's':
            return x
        if k in ('d', 'l'):
            return x
        return (k, x)
    return v


class LiveCue:
    __slots__ = ('id', 'cls', 'name', 'state', 'parent', 'static', 'kind', 'activated', 'actions', 'instances',
                 'handle', 'period', 'next_check', 'fail', 'fields', 'children')

    def where(self):
        return '#%d' % self.id


class MdDbg:
    def __init__(self, cli, a):
        self.cli, self.a = cli, a
        self.sess = cli.session()
        self.info = self.sess.main_info()
        self._map = None
        self._live = None
        self.now = None

    # ---- the map ----------------------------------------------------------------------------------------------
    def map_sources(self):
        srcs = list(getattr(self.a, 'md', None) or [])
        if not srcs and os.environ.get('X3KC_MD'):
            srcs = [p for p in os.environ['X3KC_MD'].split(os.pathsep) if p]
        if srcs:
            return srcs
        # x3_src: the build's -kc object is <checkout>/build/.../kc/<ed>/x3story.obj; the director sources are the
        # checkout's game/ap/addon/director (FL game/fl/addon2/director) .xmd, the overlay <build>/game the XML
        ed = self.sess.hello.get('edition', 'AP').lower()
        sub = ('fl', 'addon2') if ed == 'fl' else ('ap', 'addon')
        obj = self.sess.main_object().get('file') or ''
        d = os.path.dirname(os.path.abspath(obj)) if obj else ''
        while d and os.path.dirname(d) != d:
            cand = os.path.join(d, 'game', sub[0], sub[1], 'director')
            if os.path.isdir(cand) and any(n.endswith(md.EXT) for n in os.listdir(cand)):
                return [cand]
            d = os.path.dirname(d)
        raise DbgError('md: where are the director files? --md DIR (.xmd or .xml; repeatable) or X3KC_MD')

    def map(self):
        if self._map is None:
            try:
                self._map = mdmap.MdMap.load(self.map_sources())
            except md.MdError as x:
                raise DbgError(str(x))
        return self._map

    # ---- the live cues ----------------------------------------------------------------------------------------
    def cue_classes(self):
        from .dbg import class_chain
        return sorted(c for c in self.info.classes if CUE_CLASS in class_chain(self.info, c))

    def field_slot(self, name, cid=CUE_CLASS):
        from .dbg import class_fields
        names = class_fields(self.info, cid)
        if name not in names:
            raise DbgError('the sidecar has no field %s in class %d' % (name, cid))
        return names.index(name)

    def live(self):
        """every Cue object (a snapshot of the Cue classes only: no containers)"""
        if self._live is not None:
            return self._live
        from .dbg import class_fields
        r = self.sess.conn.call('snapshot', classes=self.cue_classes(), chars=200, deadline=120)
        out = {}
        for o in r.get('objects', []):
            names = class_fields(self.info, o['c'])
            f = {(names[i] if i < len(names) else 'f%d' % i): sval(v) for i, v in enumerate(o['f'])}
            c = LiveCue()
            c.id, c.cls, c.fields = o['id'], o['c'], f
            c.name = f.get('cueName') if isinstance(f.get('cueName'), str) else str(f.get('cueName'))
            c.state = f.get('state')
            # (object references are object ids: plain numbers in a cell)
            p = f.get('parentCue')
            c.parent = (p[1] if isinstance(p, tuple) else p) or None
            s = f.get('staticCue')
            c.static = (s[1] if isinstance(s, tuple) else s) or None
            c.kind = f.get('cueKind')
            c.activated, c.actions = f.get('activatedTime'), f.get('actionCount')
            c.instances, c.handle = f.get('instanceCount'), f.get('conditionHandle')
            c.period, c.next_check, c.fail = f.get('checkPeriod'), f.get('nextCheckTime'), f.get('failMode')
            c.children = []
            out[c.id] = c
        for c in out.values():
            if c.parent in out:
                out[c.parent].children.append(c)
        self._live = out
        return out

    def game_now(self):
        if self.now is None:
            v, err = self.ro_eval(None, 'TI_GetAbsTime()')
            self.now = v.get('v') if v and v.get('t') == 'int' else None
        return self.now

    def named(self, text):
        """NAME | #ID -> [LiveCue] (a name: the static cue and its instances)"""
        live = self.live()
        m = re.match(r'^#?(-?\d+)$', text)
        if m and int(m.group(1)) in live:
            return [live[int(m.group(1))]]
        return sorted((c for c in live.values() if c.name == text), key=lambda c: (c.static is not None, c.id))

    def def_of(self, c):
        """the map's definition of a live cue (its name; several: the one whose parent names match)"""
        cands = self.map().runnable(c.name)
        if len(cands) <= 1:
            return cands[0] if cands else None
        live = self.live()
        chain = []
        p = c.parent
        while p in live:
            chain.append(live[p].name)
            p = live[p].parent
        best, score = cands[0], -1
        for d in cands:
            s, q = 0, d.parent
            for n in chain:
                if q is None or q.name != n:
                    break
                s, q = s + 1, q.parent
            if s > score:
                best, score = d, s
        return best

    def chain_of(self, c):
        live, out, p = self.live(), [], c.parent
        while p in live:
            out.append(live[p])
            p = live[p].parent
        return out

    # ---- evals ------------------------------------------------------------------------------------------------
    def ro_eval(self, on, text, block=False, frame=None):
        """a read-only eval on object `on` (None: the globals; frame: the stop's frame N) -> (value, error)"""
        from .dbg import DbgError as E, kcsnip_line
        req = {'depth': 1, 'items': 50, 'readOnly': True}
        cls = 0
        fr = None
        if frame is not None:
            _, fr = self.cli.frame_of(None, frame)
            req['frame'] = frame
        elif on is not None:
            cls = self.live()[on].cls if on in self.live() else self.cli.object_class(on)
            req['on'] = on
        try:
            snip = self.cli.snippet(self.info, text, fr, cls if fr is None else None, block, read_only=True)
        except E as x:
            return None, str(x)
        req.update({k: snip[k] for k in ('code', 'jumps', 'strings', 'low', 'high')})
        r = self.sess.conn.call('eval', **req)
        if 'failed' in r:
            return None, r['failed']
        return r.get('value'), None

    def fmt(self, v):
        from .dbg import format_value
        return format_value(self.info, v).replace('\n', ' ')

    def read(self, paths, depth=1, items=50):
        return self.sess.conn.call('read', paths=paths, depth=depth, items=items, chars=300)['values']

    def steps_of(self, oid, elem):
        st = [['object', oid], ['field', self.field_slot(mdmap.ROOT_FIELD[elem.kind])]]
        for k, v in elem.steps:
            st.append([k, v])
        return st

    # ---- text -------------------------------------------------------------------------------------------------
    def cue_line(self, c, d=None):
        d = d if d is not None else self.def_of(c)
        # (cueKind: the instantiate attribute, "off" or "static"; an instance's "instance")
        kind = 'instance' if c.static else {'off': 'cue', 'static': 'instantiating'}.get(c.kind, c.kind or 'cue')
        extra = []
        if c.state == 1 or (c.state in (2, 3) and c.activated):
            extra.append('activated t=%s' % self.secs(c.activated))
        if c.static is None and c.kind == 'instance' or c.instances:
            pass
        n_inst = len([x for x in self.live().values() if x.static == c.id])
        if n_inst:
            extra.append('%d instance%s' % (n_inst, 's' if n_inst != 1 else ''))
        return '%s #%d %s (%s)%s%s' % (c.name, c.id, state_name(c.state), kind,
                                       ' ' + ', '.join(extra) if extra else '',
                                       '  %s' % d.where() if d else '  (not in the map)')

    def parents_text(self, chain, limit=5):
        """the parent chain, nearest first (the top-level cue last); a long one cut in the middle"""
        part = chain if len(chain) <= limit + 1 else chain[:limit - 1] + [None] + chain[-1:]
        return '  parents: ' + ' <- '.join(('... %d more' % (len(chain) - limit)) if p is None else
                                           '%s #%d %s' % (p.name, p.id, state_name(p.state)) for p in part)

    def secs(self, ms):
        if not isinstance(ms, int):
            return '?'
        return '%.1fs' % (ms / 1000.0)

    # ---- commands ---------------------------------------------------------------------------------------------
    def c_tree(self):
        a = self.a
        live = self.live()
        roots = [c for c in live.values() if c.parent not in live]
        want_file = self.map().file_named(a.file) if a.file else None
        if a.file and want_file is None:
            raise DbgError('%s: no such director file in the map' % a.file)
        lines = []
        counts = {}
        for c in live.values():
            counts[c.state] = counts.get(c.state, 0) + 1
        now = self.game_now()
        lines.append('%d cues: %s; game time %s' % (len(live), ', '.join('%d %s' % (n, state_name(s)) for s, n in
                                                                       sorted(counts.items())), self.secs(now)))

        def keep(c):
            if want_file:
                d = self.def_of(c)
                if d is None or d.file != want_file:
                    return any(keep(k) for k in c.children)
            if a.active and c.state != 1:
                return any(keep(k) for k in c.children)
            return True

        def show(c, depth, under_active):
            if a.depth is not None and depth > a.depth:
                return
            statics = [k for k in c.children if k.static is None]
            insts = {}
            for k in c.children:
                if k.static is not None:
                    insts.setdefault(k.static, []).append(k)
            if not keep(c) and not (a.active and under_active):
                return
            lines.append('  ' * depth + self.cue_line(c))
            for k in sorted(statics, key=lambda x: (x.name or '', x.id)):
                show(k, depth + 1, c.state == 1)
                for i in sorted(insts.get(k.id, []), key=lambda x: x.id):
                    show(i, depth + 2, c.state == 1)
            for sid, ins in insts.items():
                if not any(s.id == sid for s in statics):
                    for i in ins:
                        show(i, depth + 1, c.state == 1)
        for r in sorted(roots, key=lambda x: (x.name or '', x.id)):
            if r.static is None or r.static not in live:
                show(r, 0, False)
                for i in sorted((x for x in live.values() if x.static == r.id and x.parent not in live),
                                key=lambda x: x.id):
                    show(i, 1, False)
        if a.active:
            lines.append('(--active: active cues and the cues directly under them)')
        lines.append('times: activation (the cue keeps no completion / cancel time: md watch NAME --state records '
                     'them)')
        self.cli.emit({'cues': len(live)}, '\n'.join(lines))
        return 0

    def vars_of(self, oid):
        names = ['values', 'counters', 'context', 'objectRegistry']
        paths = [[['object', oid], ['field', self.field_slot(n)]] for n in names]
        vals = self.read(paths, depth=2, items=40)
        return list(zip(names, vals))

    def c_cue(self):
        cues = self.live_of(self.a.cue)
        if not cues:
            return self.missing(self.a.cue)
        out = []
        for c in cues[:self.a.max]:
            d = self.def_of(c)
            out.append(self.cue_line(c, d))
            if d:
                out.append('  source: %s (XML %s:%d)' % (d.where(), d.xml_file, d.xml_line))
            chain = self.chain_of(c)
            if chain:
                out.append(self.parents_text(chain))
            f = c.fields
            out.append('  check: %s, fail mode %s, next check t=%s, event-driven %s, actions run %s, instances live %s'
                       % ('every pass' if not c.period else 'every %s' % self.secs(c.period), f.get('failMode'),
                          self.secs(c.next_check), 'yes' if c.handle else 'no', c.actions, c.instances))
            for n, v in self.vars_of(c.id):
                if v.get('t') in ('table', 'array') and v.get('size', 0) == 0:
                    continue
                out.append('  %s: %s' % (n, self.fmt(v)))
        if len(cues) > self.a.max:
            out.append('... %d more (--max)' % (len(cues) - self.a.max))
        self.cli.emit({'cues': [c.id for c in cues]}, '\n'.join(out))
        return 0

    def missing(self, name, lines=None, depth=0, d=None):
        """a cue with no live object: why (its parent's state, up to the first live ancestor)"""
        lines = [] if lines is None else lines
        if re.match(r'^#-?\d+$', name):
            raise DbgError('%s is not a live cue object (deleted - a finished instance is cleaned up -, or not a cue)'
                           % name)
        d = d or self.pick_def(name)
        lines.append('%s%s (%s): no live cue object' % ('  ' * depth, name, d.where()))
        if d.parent is None:
            lines.append('%s  a top-level cue: the director creates it at load when its game / map filters hold '
                         '(game: %s, map: %s) and its file loads' % ('  ' * depth, dict(d.attrs).get('game'),
                                                                     dict(d.attrs).get('map')))
        else:
            pt = d.parent.target or d.parent
            plive = [x for x in self.named(d.parent.ref or d.parent.name)
                     if self.def_of(x) is not None and (self.def_of(x).target or self.def_of(x)) is pt]
            if not plive:
                lines.append('%s  sub-cues are created when their parent activates; the parent %s has no live object '
                             'either' % ('  ' * depth, d.parent.name))
                if depth < 8:
                    self.missing(d.parent.ref or d.parent.name, lines, depth + 1, d.parent)
            else:
                states = ', '.join('#%d %s' % (x.id, state_name(x.state)) for x in plive[:6])
                lines.append('%s  sub-cues are created when their parent activates; parent %s: %s'
                             % ('  ' * depth, d.parent.name, states))
                if any(x.state == 0 for x in plive):
                    lines.append('%s  -> md why %s' % ('  ' * depth, kc_str(d.parent.name) if ' ' in d.parent.name
                                                       else d.parent.name))
                elif any(x.state in (1, 2) for x in plive):
                    lines.append('%s  the parent ran: this cue is not created when its game / map filters do not '
                                 'hold, or it was added to the file after the parent activated (a save of an older '
                                 'file: a reload refreshes only)' % ('  ' * depth))
        if depth == 0:
            self.cli.emit({'missing': name}, '\n'.join(lines))
        return 0

    # -- md why --
    def c_why(self):
        cues = self.live_of(self.a.cue)
        if not cues:
            return self.missing(self.a.cue)
        c = cues[0]
        if self.a.cue.startswith('#') is False and len(cues) > 1:
            pass
        d = self.def_of(c)
        out = [self.cue_line(c, d)]
        chain = self.chain_of(c)
        if chain:
            out.append(self.parents_text(chain))
        if c.state != 0:
            what = {1: 'it is active: its condition is no longer checked (the actions run at their trigger times; '
                       'the cue completes when none is left)',
                    2: 'it is complete: its condition is no longer checked',
                    3: 'it was cancelled: its condition is no longer checked'}.get(c.state, '')
            out.append('  ' + what)
            inst = [x for x in self.live().values() if x.static == c.id]
            if c.state == 0 or inst:
                out.append('  instances: ' + ', '.join('#%d %s' % (x.id, state_name(x.state)) for x in inst[:10]))
            self.cli.emit({'cue': c.id, 'state': c.state}, '\n'.join(out))
            return 0
        # who checks it: the parent's walk (the director: always; a cue: only while active or complete)
        blocked = [p for p in chain if p.state in (0, 3)]
        if chain and chain[0].state in (0, 3):
            out.append('  NOT CHECKED: its parent %s is %s (a cue\'s sub-cues are checked while it is active or '
                       'complete)' % (chain[0].name, state_name(chain[0].state)))
        elif blocked:
            out.append('  NOT CHECKED: the ancestor %s is %s' % (blocked[0].name, state_name(blocked[0].state)))
        now = self.game_now()
        if c.period:
            out.append('  checked every %s; next check t=%s (now %s)' % (self.secs(c.period), self.secs(c.next_check),
                                                                         self.secs(now)))
        if c.handle:
            out.append('  event-driven: its condition holds an event, so it is checked only when one of its events '
                       'comes (CueDirector.eventCueSets), never by the per-frame walk')
        if c.fail not in (None, 'none'):
            out.append('  fail mode %s: a false check %s the cue' % (c.fail, 'cancels' if c.fail == 'cancel' else
                                                                      'completes'))
        if d is None or 'condition' not in d.parts:
            out.append('  no condition: it activates at the first check' if d is not None else
                       '  (not in the map: the condition cannot be shown)')
            self.cli.emit({'cue': c.id}, '\n'.join(out))
            return 0
        root = d.parts['condition']
        self.ask_events(c.id, [e.tag for e in root.walk() if mdmap.has_code(e.tag)])
        results = {}
        self.why_elem(c, root, results)
        out.append('  condition (%s, evaluated now, read-only):' % root.where())
        self.why_lines(root, results, out, 2)
        r = results.get(id(root))
        verdict = r[0] if r else None
        if verdict is True:
            out.append('  -> the condition holds now: the cue activates at its next check' +
                       (' (event-driven: at its next event)' if c.handle else ''))
        elif verdict is False:
            leaves = self.false_leaves(root, results)
            events = [e for e in leaves if self.is_event(e.tag)]
            others = [e for e in leaves if not self.is_event(e.tag)]
            for e in others:
                out.append('  -> false now: %s %s' % (e.where(), e.text))
                if results[id(e)][1]:
                    out.append('     ' + results[id(e)][1])
            for e in events:
                out.append('  -> waits for the event: %s %s' % (e.where(), e.text))
            if events and others:
                out.append('     (when the event comes, the parts false now must hold at that moment)')
        else:
            out.append('  -> not known: a part could not be evaluated read-only (above)')
        self.cli.emit({'cue': c.id, 'result': verdict}, '\n'.join(out))
        return 0

    def ask_events(self, oid, tags):
        """CueDirector::IsEvent of the loaded edition for these tags (one read-only eval; cached)"""
        if not hasattr(self, '_events'):
            self._events = {}
        tags = sorted(set(t for t in tags if t not in self._events))
        if not tags:
            return
        v, err = self.ro_eval(oid, '{%s}' % ', '.join('CueDirector.IsEvent(%s)' % kc_str(t) for t in tags))
        if err or not v or v.get('t') != 'array':
            print('note: CueDirector.IsEvent could not be asked (%s): the AP list is used' % err, file=sys.stderr)
            for t in tags:
                self._events[t] = t in AP_EVENTS
            return
        for t, x in zip(tags, v.get('items', [])):
            self._events[t] = bool(x.get('v')) if x.get('t') == 'int' else False

    def is_event(self, tag):
        ev = getattr(self, '_events', {})
        return ev[tag] if tag in ev else tag in AP_EVENTS

    def false_leaves(self, e, results):
        """the leaf elements that make e false: check_all - the false children's, check_any - every child's (all are
        false); a negated group: the group itself"""
        r = results.get(id(e))
        if r is None or r[0] is not False:
            return []
        if e.tag in ('check_all', 'check_any') and e.attr('negate') in (None, '', '0'):
            out = []
            for k in e.children:
                out += self.false_leaves(k, results)
            return out or [e]
        return [e]

    def why_elem(self, c, e, results):
        """evaluate e on the live cue c (read-only); results[id(e)] = (value: True/False/None, detail text)"""
        expr = e.kc_expr()
        neg = e.attr('negate') not in (None, '', '0')
        chance = e.attr('chance')
        notes = []
        if chance is not None:
            notes.append('chance %s%%: rolled at every check (not here)' % chance)
        code = e.tag
        val = None
        if code in ('check_all', 'check_any'):
            kids = []
            for k in e.children:
                self.why_elem(c, k, results)
                kids.append(results[id(k)][0])
            known = [x for x in kids if x is not None]
            if code == 'check_all':
                val = False if any(x is False for x in kids) else (True if len(known) == len(kids) else None)
            else:
                val = True if any(x is True for x in kids) else (False if len(known) == len(kids) else None)
            if e.attr('min') or e.attr('max') or e.attr('exact') or e.attr('list'):
                notes.append('repeated (min/max/exact/list): the list is checked several times')
        elif self.is_event(code):
            val = False
            notes.append('event: true only when the event %s comes (a check without it is false)' % code)
            if code in CUE_EVENTS and e.attr('cue') is not None:
                v, err = self.ro_eval(c.id, 'EvalInstance(ReadText(%s, "cue"))' % expr)
                notes.append(self.ref_cue_text(e.attr('cue'), v, err, event=code))
        else:
            # (on a shallow clone of the element: CheckCountObjects / CheckCountSectors write their count into the
            # element's own table first - refused in a read-only run; the clone is the run's own table)
            v, err = self.ro_eval(c.id, 'CheckCondition(SE_TableClone(%s), 0, "", {})' % expr)
            if err:
                notes.append('cannot evaluate read-only: %s' % err)
            else:
                val = bool(v.get('v')) if v.get('t') == 'int' else (False if v.get('t') == 'empty' else None)
                if val is None:
                    notes.append('value %s' % self.fmt(v))
            if code in VALUE_CODES:
                notes.append(self.value_detail(c, e, expr, VALUE_CODES[code]))
            elif code in CUE_TESTS and e.attr('cue') is not None:
                v, err = self.ro_eval(c.id, 'FindInstance(ReadText(%s, "cue"))' % expr)
                notes.append(self.ref_cue_text(e.attr('cue'), v, err))
        if neg and val is not None:
            val = not val
            notes.append('negate: the result inverted')
        results[id(e)] = (val, '; '.join(n for n in notes if n))

    def ref_cue_text(self, name, v, err, event=None):
        if err:
            return 'cue %s: cannot evaluate read-only: %s' % (name, err)
        if not v or v.get('t') != 'obj' or v.get('null'):
            return 'cue %s: no such live cue now%s' % (name, ' (the event cannot come from it)' if event else '')
        live = self.live()
        x = live.get(v['id'])
        st = state_name(x.state) if x else '?'
        txt = 'cue %s = #%d %s' % (name, v['id'], st)
        if event == 'cue_completed' and x and x.state == 2 or event == 'cue_activated' and x and x.state in (1, 2) \
                or event == 'cue_cancelled' and x and x.state == 3:
            txt += ' - ALREADY happened: an event that has passed does not come again (this cue may never fire)'
        elif event and x:
            txt += ' (the event comes when it changes)'
        return txt

    def value_detail(self, c, e, expr, unit):
        # Cue::CheckValue: a value that is an expression is compared as a number (CheckRange: exact, list, min /
        # max read as numbers), any other text as a string (CheckString: exact or list as text)
        parts = []
        txt = e.attr('value')
        is_expr = True
        if txt is not None:
            v, err = self.ro_eval(c.id, '{CueDirector.IsExpression(ReadText(%s, "value")), ReadText(%s, "value")}'
                                  % (expr, expr))
            if err:
                return 'value %s: cannot evaluate read-only: %s' % (md.sugar(txt), err)
            is_expr = bool(v.get('t') == 'array' and v['items'][0].get('v'))
            if is_expr:
                v2, err = self.ro_eval(c.id, 'ReadNumber(%s, "value", %d)' % (expr, unit))
                parts.append('value %s = %s' % (md.sugar(txt), err and ('<%s>' % err) or self.fmt(v2)))
            else:
                parts.append('value %s = %s (a text: compared as a string)' % (md.sugar(txt), self.fmt(v['items'][1])))
        elif e.tag != 'check_value':
            what = {4: 'the game age', 3: 'the time', 1: 'the distance'}.get(unit, '')
            parts.append('compares %s' % what)
        for k in ('exact', 'min', 'max', 'list'):
            if e.attr(k) is None:
                continue
            raw = md.sugar(e.attr(k))
            if k == 'list' or not is_expr:
                v, err = self.ro_eval(c.id, 'ReadText(%s, %s)' % (expr, kc_str(k)))
            else:
                v, err = self.ro_eval(c.id, 'ReadNumber(%s, %s, %d)' % (expr, kc_str(k), unit))
            shown = '<%s>' % err if err else self.fmt(v)
            parts.append('%s %s%s' % (k, raw, '' if shown in (raw, kc_str(raw)) else ' = %s' % shown))
        return ', '.join(parts)

    def why_lines(self, e, results, out, depth):
        r = results.get(id(e), (None, ''))
        mark = {True: 'true ', False: 'FALSE', None: '  ?  '}[r[0] if r[0] in (True, False, None) else bool(r[0])]
        out.append('%s[%s] %s:%d  %s' % ('  ' * depth, mark, e.cue.file if e.cue else '?', e.line, e.text))
        if r[1]:
            out.append('%s        %s' % ('  ' * depth, r[1]))
        if e.tag in ('check_all', 'check_any'):
            for k in e.children:
                self.why_lines(k, results, out, depth + 1)

    # ---- breakpoints ------------------------------------------------------------------------------------------
    def resolve_target(self, text):
        """FILE.xmd:LINE | FILE.xml:LINE | CUE -> (cue def, elem or None)"""
        m = re.match(r'^(.+\.(xmd|xml)):(\d+)$', text, re.I)
        if m:
            try:
                return self.map().at(m.group(1), int(m.group(3)), xml=m.group(2).lower() == 'xml')
            except md.MdError as x:
                raise DbgError(str(x))
        return self.pick_def(text), None

    def pick_def(self, name):
        """the map's definition of the cue NAME; several (a name used twice, in other parents): --in FILE or
        PARENT picks one, else the candidates are listed"""
        defs = self.map().runnable(name)
        if not defs:
            raise DbgError('%s: no cue of that name in the map (FILE.xmd:LINE for a line)' % name)
        within = getattr(self.a, 'within', None)
        if within:
            f = self.map().file_named(within)
            defs = [d for d in defs if d.file == f or (d.parent is not None and d.parent.name == within)]
            if not defs:
                raise DbgError('%s: no cue of that name in %s' % (name, within))
        if len({id(d.target or d) for d in defs}) > 1:
            raise DbgError('%s names %d cues: --in FILE or --in PARENT picks one:\n%s' % (
                name, len(defs), '\n'.join('  %s (parent %s)' % (d.where(), d.parent.name if d.parent else
                                                                  'the director') for d in defs)))
        return defs[0]

    def live_of(self, name):
        """the live cues of NAME | #ID; with several definitions of the name only those of the one picked"""
        cues = self.named(name)
        if re.match(r'^#?-?\d+$', name) and cues:
            return cues
        def target(c):
            d = self.def_of(c)
            return id(d.target or d) if d is not None else None
        if self.map().ambiguous(name):
            if not getattr(self.a, 'within', None) and len({target(c) for c in cues}) == 1:
                return cues      # (only one of the definitions has live cues)
            d = self.pick_def(name)
            cues = [c for c in cues if target(c) == id(d.target or d)]
        return cues

    def filter_of(self, cue, kind=None):
        """(KC condition, names): the live cues that run cue's tables (part kind) - its name, the references of it
        (Cue::GetLibrary: a <cue ref> runs under the referenced name, a part ref under the referencing cue's), and
        the parent's name for a name the map has twice"""
        runners = self.map().runners(cue, kind)
        if not runners:
            raise DbgError('%s: no cue runs it (a library cue that nothing references)' % cue.where())
        terms, names = [], []
        for r in runners:
            name = r.ref or r.name
            t = 'cueName == %s' % kc_str(name)
            if self.map().ambiguous(name) and r.parent is not None:
                t += ' && parentCue.GetName() == %s' % kc_str(r.parent.ref or r.parent.name)
            if t not in terms:
                terms.append(t)
                names.append(name)
        return (terms[0] if len(terms) == 1 else '(%s)' % ' || '.join('(%s)' % t for t in terms)), names

    def c_break(self):
        a = self.a
        cue, elem = self.resolve_target(a.target)
        on = a.on
        name = cue.ref or cue.name
        if elem is not None and elem.cue is not cue:
            cue = elem.cue
        if on is None:
            if elem is None:
                on = 'activate'
            elif elem.kind == 'condition':
                on = 'condition'
            elif elem.kind in ('action', 'force'):
                on = 'action'
            else:
                raise DbgError('%s:%d is in <%s>: breakpoints take a condition or action element, or the cue (--on '
                               'activate|complete|cancel|condition|action)' % (cue.file, elem.line, elem.kind))
        base, names = self.filter_of(cue, None if on in ('activate', 'complete', 'cancel') else
                                     ('condition' if on == 'condition' else (elem.kind if elem is not None and
                                                                             elem.kind == 'force' else 'action')))
        if len(names) > 1 or names[0] != name:
            name = ' / '.join(names)
        if on in ('activate', 'complete', 'cancel'):
            method = {'activate': 'Cue::Activate', 'complete': 'Cue::Complete', 'cancel': 'Cue::Cancel'}[on]
            cond, what = base, '%s %s' % (name, on)
        elif on == 'condition':
            if elem is None or elem.kind != 'condition' or elem is cue.parts.get('condition'):
                if 'condition' not in cue.parts:
                    raise DbgError('%s has no condition' % name)
                method, cond = 'Cue::Condition', base + ' && condition == conditionDef'
                elem = cue.parts['condition']
                what = '%s condition check' % name
            else:
                method = 'Cue::CheckCondition'
                cond = base + ' && condition == ' + elem.kc_expr()
                what = '%s condition %s:%d %s' % (name, elem.cue.file, elem.line, elem.tag)
        elif on == 'action':
            if elem is None or elem.kind not in ('action', 'force'):
                if 'action' not in cue.parts:
                    raise DbgError('%s has no action' % name)
                elem = cue.parts['action']
                method, cond = 'Cue::Action', base + ' && action == actionDef'
                what = '%s actions' % name
            else:
                if not elem.exec:
                    raise DbgError('%s:%d <%s> is an argument of <%s>, not run by itself: break on that element'
                                   % (cue.file, elem.line, elem.tag, elem.parent.tag if elem.parent else '?'))
                method = 'Cue::' + elem.exec
                cond = base + ' && action == ' + elem.kc_expr()
                what = '%s action %s:%d %s' % (name, elem.cue.file, elem.line, elem.tag)
        else:
            raise DbgError('--on activate|complete|cancel|condition|action')
        if a.cond:
            cond = '(%s) && (%s)' % (cond, ' '.join(' '.join(w) for w in a.cond))
        ns = argparse.Namespace(cond=[[cond]], log=a.log, depth=a.depth, count=a.count, once=a.once, this=0,
                                writable=a.writable)
        s = self.sess
        out = []
        for pc, meth in self.cli.resolve_location(self.info, method):
            req = self.cli.bp_request(ns, self.info, pc, meth, 'md ' + what)
            r = s.conn.call('break', **req)
            extra = ' (unbound: %s)' % ', '.join(r['unbound']) if r.get('unbound') else ''
            out.append('md breakpoint %d: %s%s (%s)%s' % (r['bp'], what, ' (log)' if r.get('log') else '',
                                                         method, extra))
            for n in names:
                self.check_paths(n, elem)
            self.cli.emit(dict(r, md=what), out[-1])
        return 0

    def check_paths(self, name, elem):
        """the map against the game: the element's table at its path in a live cue of that name has its code"""
        if elem is None:
            return
        cues = self.named(name)
        if not cues:
            print('note: no live cue %s yet: the element path is the map\'s (not checked against the game)' % name,
                  file=sys.stderr)
            return
        v = self.read([self.steps_of(cues[0].id, elem) + [['key', 'code']]], depth=0)[0]
        want = elem.tag if mdmap.has_code(elem.tag) else None
        got = v.get('v') if v.get('t') == 'str' else None
        if want and got != want:
            print('warning: in #%d the path %s holds %s, the map says <%s>: the map does not match the loaded file'
                  % (cues[0].id, elem.kc_expr(), self.fmt(v), elem.tag), file=sys.stderr)

    # ---- the stop's MD context --------------------------------------------------------------------------------
    def c_where(self):
        stop = self.cli.stop_state()
        if stop is None:
            raise DbgError('the game is not stopped')
        self.cli.emit({'stop': stop}, self.context_text(stop))
        return 0

    def context_text(self, stop):
        info, frames = self.cli.frames()
        out = []
        cue_frame = None
        for f in frames:
            if f.method is not None and f.method.cls in self.cue_classes() and f.raw.get('this') is not None:
                cue_frame = f
                break
        if cue_frame is None:
            return 'no Cue method on the stack: not in the Mission Director'
        oid = cue_frame.raw['this']
        self._live = None
        live = self.live()
        c = live.get(oid)
        if c is None:
            return 'the receiver #%d is not a live cue' % oid
        d = self.def_of(c)
        if (stop.get('tag') or '').startswith('md '):
            out.append('md breakpoint %s: %s' % (stop.get('bp'), stop['tag'][3:]))
        out.append('MD: ' + self.cue_line(c, d))
        doing = {'Activate': 'activating (its condition held; state set next)', 'Complete': 'completing',
                 'Cancel': 'being cancelled', 'Condition': 'checking its condition',
                 'CheckCondition': 'checking a condition element', 'Action': 'running its actions',
                 'DoAction': 'running an action element', 'DoAll': 'running a do_when / do_otherwise branch',
                 'InitData': 'being created or reset'}.get(frames[0].method.name if frames[0].method else '')
        if doing and frames[0].raw.get('this') == oid:
            out.append('  now: ' + doing)
        chain = self.chain_of(c)
        if chain:
            out.append(self.parents_text(chain))
        # the elements on the stack: each Cue frame's element parameter, by its table's identity
        if d is not None:
            ids = {}
            elems = list(d.elements())
            if elems:
                vals = self.read([self.steps_of(oid, e) for e in elems], depth=0)
                for e, v in zip(elems, vals):
                    if v.get('t') == 'table' and e.exec:
                        ids.setdefault(v['id'], e)
                    elif v.get('t') == 'table' and v['id'] not in ids:
                        ids[v['id']] = e
            shown = set()
            for i, f in enumerate(frames):
                if f.method is None or f.method.cls not in self.cue_classes() or f.raw.get('this') != oid:
                    continue
                pname = ELEM_METHODS.get(f.method.name)
                if not pname:
                    continue
                cell = next((cl for n, k, cl in f.vars() if n == pname and k == 'param'), None)
                if cell is None:
                    continue
                v = self.read([[['frame', i, cell]]], depth=0)[0]
                e = ids.get(v.get('id')) if v.get('t') == 'table' else None
                if e is None or id(e) in shown:
                    continue
                shown.add(id(e))
                out.append('  #%d %s: %s:%d (XML %s:%d)  %s' % (i, f.method.name, e.cue.file, e.line, e.cue.xml_file,
                                                                e.xml_line, e.text))
        for n, v in self.vars_of(oid):
            if v.get('t') in ('table', 'array') and v.get('size', 0) == 0:
                continue
            out.append('  %s: %s' % (n, self.fmt(v)))
        return '\n'.join(out)

    def c_wait(self):
        rc = self.cli.c_wait(argparse.Namespace(timeout=self.a.timeout, all=False))
        # (wait polls on connections of its own and closes the session's: a new one for the context)
        self.cli.close()
        self.sess, self._live = self.cli.session(), None
        stop = self.cli.stop_state() if rc == 0 else None
        if stop is not None:
            print(self.context_text(stop))
        return rc

    # ---- watch ------------------------------------------------------------------------------------------------
    def c_watch(self):
        a = self.a
        cue, _ = self.resolve_target(a.cue)
        base, names = self.filter_of(cue)
        name = ' / '.join(names)
        s = self.sess
        out = []
        # the state's writers: Activate (1), Complete (2), Cancel (3), Refresh (3 under a cancelled parent),
        # InitData (0: a reset); a tracepoint at each one's first statement, logging the cue and its old state
        for meth, to in (('Cue::Activate', 'active'), ('Cue::Complete', 'complete'), ('Cue::Cancel', 'cancelled'),
                         ('Cue::InitData', 'waiting')):
            ns = argparse.Namespace(cond=[[base]], log=['this, state, TI_GetAbsTime(), activatedTime'], depth=0,
                                    count=0, once=False, this=0, writable=False)
            for pc, m in self.cli.resolve_location(self.info, meth):
                req = self.cli.bp_request(ns, self.info, pc, m, 'md state %s -> %s' % (name, to))
                req['labels'] = ['cue', 'old state', 'time', 'activated']
                r = s.conn.call('break', **req)
                out.append('md watch %d: %s -> %s (%s)' % (r['bp'], name, to, meth))
        self.cli.emit({'watch': name}, '\n'.join(out) + '\n(the logs come with x3kc dbg wait / events: md events '
                                                         'prints them as state changes)')
        return 0

    def c_events(self):
        r = self.sess.conn.call('events', max=1000)
        out = []
        for e in r.get('events', []):
            tag = e.get('tag') or ''
            if e.get('event') == 'log' and tag.startswith('md state '):
                v = e.get('value') or {}
                items = v.get('items', []) if v.get('t') == 'array' else []
                # (this: an object id, logged as a number)
                cue = (items[0].get('id', items[0].get('v')) if items else None) or '?'
                old = items[1].get('v') if len(items) > 1 else None
                t = items[2].get('v') if len(items) > 2 else None
                act = items[3].get('v') if len(items) > 3 else None
                to = tag.split(' -> ')[1]
                if to == 'waiting':
                    # InitData: a new cue (never activated) or a reset of one
                    to, old_text = 'waiting', ('new' if old == 0 and not act else '%s, reset' % state_name(old))
                else:
                    old_text = state_name(old)
                out.append('t=%s %s #%s: %s -> %s  (task %s)' % (self.secs(t), tag[9:].split(' -> ')[0], cue, old_text,
                                                                to, e.get('task')))
            else:
                _, text = self.cli.describe_event(self.info, e)
                out.append(text)
        self.cli.emit({'events': r.get('events', [])}, '\n'.join(out) if out else 'no events')
        return 0

    def c_map(self):
        m = self.map()
        if self.a.target:
            cue, elem = self.resolve_target(self.a.target)
            cues = [cue]
        else:
            cues = m.cues
        out = ['%d files, %d cues' % (len(m.files), len(m.cues))] if not self.a.target else []
        for c in cues[:self.a.max]:
            out.append('%s %s (XML %s:%d)%s%s' % (c.name, c.where(), c.xml_file, c.xml_line,
                                                  ' ref %s' % c.ref if c.ref else '', ' library' if c.library else ''))
            for e in c.elements():
                out.append('  %-9s %4d xml %4d  %-14s %-36s %s' % (e.kind, e.line, e.xml_line, e.exec or '-',
                                                                    e.kc_expr(), e.text[:70]))
            out += ['  note: ' + n for n in c.notes]
        self.cli.emit({'cues': len(cues)}, '\n'.join(out))
        return 0

    def c_verify(self):
        """the map against the live game: every element of every live static cue has the map's tag at its path"""
        live = self.live()
        bad = checked = 0
        out = []
        for c in live.values():
            if c.static is not None:
                continue
            d = self.def_of(c)
            if d is None:
                continue
            elems = [e for e in d.elements() if mdmap.has_code(e.tag) and e.kind in ('condition', 'action')]
            if not elems:
                continue
            vals = self.read([self.steps_of(c.id, e) + [['key', 'code']] for e in elems], depth=0)
            for e, v in zip(elems, vals):
                checked += 1
                if v.get('t') != 'str' or v.get('v') != e.tag:
                    bad += 1
                    if bad <= 20:
                        out.append('%s #%d %s:%d <%s>: the game has %s at %s' % (c.name, c.id, e.cue.file, e.line,
                                                                               e.tag, self.fmt(v), e.kc_expr()))
        out.append('%d elements of %d live cues checked, %d differ' % (checked, len(live), bad))
        self.cli.emit({'checked': checked, 'differ': bad}, '\n'.join(out))
        return 0 if not bad else 1


class DbgError(Exception):
    pass


def add_parser(sub):
    p = sub.add_parser('md', help='Mission Director: break on cues and elements, the live cue tree, why a cue does '
                                  'not fire, state changes')
    p.add_argument('--md', action='append', metavar='DIR', help='the director files (.xmd or .xml, folders or '
                   'files; repeatable; default X3KC_MD, else the x3_src checkout of the -kc object: '
                   'game/ap/addon/director, FL game/fl/addon2/director)')
    s = p.add_subparsers(dest='mdcmd', required=True)
    q = s.add_parser('break', help='FILE.xmd:LINE | FILE.xml:LINE | CUE: stop at a cue\'s activation, completion, '
                                   'cancel, its condition check or one condition / action element')
    q.add_argument('target')
    q.add_argument('--on', choices=['activate', 'complete', 'cancel', 'condition', 'action'])
    q.add_argument('--in', dest='within', metavar='FILE|PARENT', help='a cue name used twice: the one in this file '
                   'or under this parent')
    from .dbg import add_bp_options
    add_bp_options(q)
    q = s.add_parser('tree', help='the live cue tree: states, instances, activation times')
    q.add_argument('file', nargs='?', help='only the cues of this director file (and their parents)')
    q.add_argument('--active', action='store_true', help='only the active cues and the cues directly under them')
    q.add_argument('--depth', type=int)
    q = s.add_parser('cue', help='one cue (NAME: the static cue and its instances; #ID one object) and its variables')
    q.add_argument('cue')
    q.add_argument('--in', dest='within', metavar='FILE|PARENT', help='a cue name used twice: the one in this file '
                   'or under this parent')
    q.add_argument('--max', type=int, default=5)
    q = s.add_parser('why', help='why a waiting cue does not activate: each condition element evaluated read-only')
    q.add_argument('cue')
    q.add_argument('--in', dest='within', metavar='FILE|PARENT', help='a cue name used twice: the one in this file '
                   'or under this parent')
    q = s.add_parser('watch', help='log the state changes of a cue (all its instances): tracepoints')
    q.add_argument('cue')
    q.add_argument('--in', dest='within', metavar='FILE|PARENT', help='a cue name used twice: the one in this file '
                   'or under this parent')
    q.add_argument('--state', action='store_true', help='(the default and only kind)')
    s.add_parser('events', help='the queued events, md state logs as state changes')
    s.add_parser('where', help='the MD context of the stop: the cue, its parents, the element being run')
    q = s.add_parser('wait', help='wait for a stop (as dbg wait), then print its MD context')
    q.add_argument('--timeout', type=float, default=30)
    q = s.add_parser('map', help='the cue map (no game needed with --md): cues, elements, their KC paths')
    q.add_argument('target', nargs='?', help='a cue or FILE.xmd:LINE')
    q.add_argument('--max', type=int, default=100000)
    s.add_parser('verify', help='the map against the game: the code of every element of every live cue')
    return p


def run(cli, a):
    from .dbg import DbgError as E
    try:
        dm = MdDbg(cli, a) if a.mdcmd != 'map' or not a.md else MdOffline(cli, a)
        return getattr(dm, 'c_' + a.mdcmd)()
    except DbgError as x:
        raise E(str(x))


class MdOffline(MdDbg):
    """md map with --md: no game"""
    def __init__(self, cli, a):
        self.cli, self.a = cli, a
        self._map = None
