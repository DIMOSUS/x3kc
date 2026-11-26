"""The script profiler's client side (`x3kc dbg profile ...`; the protocol: docs/debugging.md "Script profiler").

The game collects (x3_src ScriptDebug build): sampled stacks of the game thread's script runs (nodes: a KC frame by
its pc, a native, an MSCI script line), and the exact times of every native call (by call site), native body and
native case (by method), top-level script run (by its task's root) and frame (the slowest kept in detail). This module
turns that raw dump into reports, folded stacks and a speedscope file, symbolized by the object's .kcdbg sidecar.

Sampled times are estimates: a method's share of the samples times the measured script time (a sample weighs the
sampler ticks it holds: ticks counted while one was pending are not lost). Timed ones are exact: the time stamp
counter (rdtsc) read around every call, its frequency measured against QueryPerformanceCounter - it assumes an
invariant TSC synchronized across the cores, as every x64 CPU the game supports has. The debugger's own work (serving
the pipe, a code run) and its stops are left out of every time."""
import json
import os
import tempfile

from . import kcdbg

KC, NATIVE, MSCI, TRUNCATED = 1, 2, 3, 4
MARKS = {0: '', 1: ' [body]', 2: ' [case]'}


def saved_dir():
    return os.path.join(tempfile.gettempdir(), 'x3kc_dbg')


def saved_path(pid):
    return os.path.join(saved_dir(), 'profile-%d.json' % pid)


def newest_saved():
    d = saved_dir()
    try:
        files = [os.path.join(d, n) for n in os.listdir(d) if n.startswith('profile-') and n.endswith('.json')]
    except OSError:
        return None
    return max(files, key=os.path.getmtime) if files else None


class Profile:
    """a dump (the game's `profile dump` reply, or a saved file) with its sidecar; line_of(script, index) gives an
    MSCI code index's editor line and command id (None when not known)"""

    def __init__(self, dump, info=None, msci_line=None):
        self.d = dump
        self.info = info
        self.msci_line = msci_line or (lambda script, index: None)
        self.freq = float(dump.get('frequency') or 1)
        self.nodes = dump.get('nodes', [])
        self.stacks = dump.get('stacks', [])
        # (the weighted samples; a status reply has no stacks: its count)
        self.samples = sum(s[0] for s in self.stacks) if self.stacks else dump.get('samples', 0)
        self.script_ms = self.ms(dump.get('scriptTicks', 0))
        self._names = {}

    @classmethod
    def load(cls, path, dirs=(), msci_line=None):
        with open(path, encoding='utf-8') as h:
            dump = json.load(h)
        return cls(dump, find_info(dump, dirs), msci_line)

    def ms(self, ticks):
        return ticks * 1000.0 / self.freq

    def sample_ms(self, n):
        """n samples as milliseconds of script time (their share of the measured script time)"""
        return self.script_ms * n / self.samples if self.samples else 0.0

    # -- names --
    def method(self, pc):
        return self.info.method_at(pc) if self.info is not None else None

    def method_name(self, pc):
        m = self.method(pc)
        return m.qualname if m is not None else '0x%x' % pc

    def where(self, pc, resume=True):
        loc = self.info.location(pc, resume) if self.info is not None else None
        return '%s:%d' % (loc.file, loc.line) if loc is not None else '0x%x' % pc

    def method_key(self, pc):
        m = self.method(pc)
        return m.entry if m is not None else pc

    def node_name(self, i, lines=False):
        """a node's name in a stack: Class::Method [body|case], native NAME, msci SCRIPT:LINE"""
        key = (i, lines)
        if key in self._names:
            return self._names[key]
        n = self.nodes[i]
        if n[0] == KC:
            text = (self.where(n[1]) + ' ' if lines else '') + self.method_name(n[1]) + MARKS.get(n[2], '')
        elif n[0] == NATIVE:
            text = 'native ' + n[3]
        elif n[0] == MSCI:
            line = self.msci_line(n[1], n[2])
            text = 'msci %s:%s' % (n[1], line[0] if line else 'i%d' % n[2])
        else:
            text = '[deeper frames]'
        self._names[key] = text
        return text

    # -- the reports --
    def header(self):
        d = self.d
        el = self.ms(d.get('elapsedTicks', 0) - d.get('heldTicks', 0)) / 1000.0  # (stops, the debugger's work out)
        frames = d.get('frames', 0)
        fms = self.ms(d.get('frameTicks', 0)) / max(1, frames - d.get('stoppedFrames', 0))
        req = d.get('requests', 0)
        ticks = req + d.get('coalesced', 0) + d.get('idle', 0)  # (the sampler's ticks outside holds)
        lat = self.ms(d.get('latencySumTicks', 0)) * 1000.0 / max(1, d.get('taken', self.samples))
        over = self.ms(d.get('overheadTicks', 0))
        text = ('profile of %s (%s, %s): %.1f s, %d frames (%.2f ms a frame, the slowest %.1f ms), scripts %.1f ms '
                '(%.1f%% of the time, %d runs), natives %.1f ms; %d samples at %d Hz (%.0f a second reached) in %d walks (%d asked, %d ticks '
                'coalesced into a pending one, %d while no script ran, %d lost; latency avg %.1f us max %.2f ms), the '
                'samples cost %.1f ms (%.2f%%)'
                % (d.get('object', {}).get('name', '?'), 'timed' if d.get('timed', True) else 'untimed',
                   d.get('state', '?'), el, frames, fms, self.ms(d.get('maxFrameTicks', 0)), self.script_ms,
                   100.0 * self.script_ms / max(1e-9, el * 1000.0), d.get('runs', 0),
                   self.ms(d.get('nativeTicks', 0)), self.samples, d.get('hz', 0), ticks / max(1e-9, el),
                   d.get('taken', self.samples), req,
                   d.get('coalesced', 0), d.get('idle', 0), d.get('lost', 0), lat,
                   self.ms(d.get('latencyMaxTicks', 0)), over, 100.0 * over / max(1e-9, el * 1000.0)))
        if d.get('dropped') or d.get('sitesDropped'):
            text += '; over the tables\' limits: %d samples, %d sites' % (d.get('dropped', 0), d.get('sitesDropped', 0))
        if d.get('ended'):
            text += '; collecting ended: %s' % d['ended']
        return text

    def by_method(self):
        """[{method, where, excl, natives, incl, (ms)}]: excl = the samples whose innermost node is the method's frame
        (its bytecode, or its body / case C++ - marked), natives = the samples in a native it calls itself, incl = the
        samples with the method anywhere on the stack"""
        rows = {}

        def row(pc, mark):
            k = (self.method_key(pc), mark)
            r = rows.get(k)
            if r is None:
                r = rows[k] = {'method': self.method_name(pc) + MARKS.get(mark, ''), 'where': self.where(k[0], False),
                               'entry': k[0], 'mark': mark, 'excl': 0, 'natives': 0, 'incl': 0}
            return r

        for s in self.stacks:
            count, ids = s[0], s[1:]
            seen = set()
            innermost_kc = None
            leaf_native = bool(ids) and self.nodes[ids[-1]][0] == NATIVE
            for i in ids:
                n = self.nodes[i]
                if n[0] != KC:
                    continue
                r = row(n[1], n[2])
                innermost_kc = r
                if id(r) not in seen:
                    seen.add(id(r))
                    r['incl'] += count
            if innermost_kc is not None:
                # (the innermost KC frame: its own time, or the native it calls; an MSCI node between is its line)
                last_kc = max(j for j, i in enumerate(ids) if self.nodes[i][0] == KC)
                after = [self.nodes[i][0] for i in ids[last_kc + 1:]]
                if leaf_native and NATIVE in after and after.index(NATIVE) == len(after) - 1:
                    innermost_kc['natives'] += count
                else:
                    innermost_kc['excl'] += count
        out = sorted(rows.values(), key=lambda r: (-r['excl'], -r['incl']))
        for r in out:
            r['exclMs'], r['nativesMs'], r['inclMs'] = (self.sample_ms(r['excl']), self.sample_ms(r['natives']),
                                                        self.sample_ms(r['incl']))
        return out

    def by_native(self):
        """the natives timed (every call): by name with their calling methods (the call site's method)"""
        names = {}
        for pc, group, command, name, calls, ticks, nested, mx, samples in self.d.get('natives', []):
            r = names.setdefault(name, {'native': name, 'calls': 0, 'ms': 0.0, 'nestedMs': 0.0, 'maxMs': 0.0,
                                        'samples': 0, 'callers': {}})
            r['calls'] += calls
            r['ms'] += self.ms(ticks)
            r['nestedMs'] += self.ms(nested)
            r['maxMs'] = max(r['maxMs'], self.ms(mx))
            r['samples'] += samples
            c = r['callers'].setdefault(self.method_name(pc), {'calls': 0, 'ms': 0.0, 'sites': []})
            c['calls'] += calls
            c['ms'] += self.ms(ticks)
            c['sites'].append(self.where(pc, False))
        out = sorted(names.values(), key=lambda r: -r['ms'])
        for r in out:
            r['callers'] = sorted(({'method': k, 'calls': v['calls'], 'ms': v['ms'], 'sites': sorted(set(v['sites']))}
                                   for k, v in r['callers'].items()), key=lambda c: -c['ms'])
        return out

    def by_cxx(self):
        """the native bodies and cases (C++ in place of bytecode, timed: every call)"""
        out = []
        for kind, key in (('body', 'bodies'), ('case', 'cases')):
            for entry, calls, ticks, mx, samples in self.d.get(key, []):
                out.append({'method': self.method_name(entry), 'kind': kind, 'where': self.where(entry, False),
                            'calls': calls, 'ms': self.ms(ticks), 'maxMs': self.ms(mx), 'samples': samples})
        return sorted(out, key=lambda r: -r['ms'])

    def by_msci(self):
        """MSCI: per script (the samples with it on the stack), per line and per command (the innermost MSCI node)"""
        scripts, lines, commands = {}, {}, {}
        for s in self.stacks:
            count, ids = s[0], s[1:]
            inner = None
            seen = set()
            for i in ids:
                n = self.nodes[i]
                if n[0] == MSCI:
                    inner = n
                    if n[1] not in seen:
                        seen.add(n[1])
                        scripts[n[1]] = scripts.get(n[1], 0) + count
            if inner is not None:
                ln = self.msci_line(inner[1], inner[2])
                k = (inner[1], ln[0] if ln else 'i%d' % inner[2])
                lines[k] = lines.get(k, 0) + count
                cmd = ln[1] if ln else None
                commands[cmd] = commands.get(cmd, 0) + count
        return {
            'scripts': sorted(({'script': k, 'samples': v, 'ms': self.sample_ms(v)} for k, v in scripts.items()),
                              key=lambda r: -r['samples']),
            'lines': sorted(({'script': k[0], 'line': k[1], 'samples': v, 'ms': self.sample_ms(v)}
                             for k, v in lines.items()), key=lambda r: -r['samples']),
            'commands': sorted(({'command': k, 'samples': v, 'ms': self.sample_ms(v)} for k, v in commands.items()),
                               key=lambda r: -r['samples'])}

    def by_task(self):
        """the top-level script runs (timed) by their task's root method (and the MSCI script of a __runScript task)"""
        out = []
        for entry, script, runs, ticks, mx in self.d.get('roots', []):
            out.append({'root': (self.method_name(entry) if entry else '?') + (' ' + script if script else ''),
                        'runs': runs, 'ms': self.ms(ticks), 'maxMs': self.ms(mx)})
        return sorted(out, key=lambda r: -r['ms'])

    def frames(self, top=10):
        out = []
        for f in self.d.get('slowest', [])[:top]:
            out.append({
                'frame': f['index'], 'ms': self.ms(f['ticks']), 'scriptMs': self.ms(f['scriptTicks']),
                'nativeMs': self.ms(f['nativeTicks']), 'runs': f['runs'], 'samples': f['samples'],
                'topRuns': [{'root': (self.method_name(e) if e else '?') + (' ' + s if s else ''),
                             'ms': self.ms(t), 'runs': c} for e, s, t, c in sorted(f.get('topRuns', []), key=lambda x: -x[2])],
                'topNatives': [{'native': self.site_native(pc), 'at': self.where(pc, False) + ' ' + self.method_name(pc),
                                'ms': self.ms(t), 'calls': c} for pc, t, c in sorted(f.get('topNatives', []), key=lambda x: -x[1])],
                'topLeaves': [{'node': self.node_name(i, True), 'samples': c} for i, c in f.get('topLeaves', [])]})
        return out

    def site_native(self, pc):
        if not hasattr(self, '_sites'):
            self._sites = {r[0]: r[3] for r in self.d.get('natives', [])}
        return self._sites.get(pc, '?')

    # -- flame graphs --
    def folded(self):
        """Brendan Gregg's folded stacks: root;...;leaf count (flamegraph.pl, speedscope, inferno)"""
        agg = {}
        for s in self.stacks:
            names = [self.node_name(i).replace(';', ',') for i in s[1:]]
            k = ';'.join(names) or '[empty]'
            agg[k] = agg.get(k, 0) + s[0]
        return ['%s %d' % (k, v) for k, v in sorted(agg.items())]

    def speedscope(self):
        """a speedscope file (https://www.speedscope.app/file-format-schema.json): one sampled profile, weights in
        milliseconds of script time"""
        frames, index = [], {}
        samples, weights = [], []
        per = self.script_ms / self.samples if self.samples else 0.0
        for s in self.stacks:
            seq = []
            for i in s[1:]:
                if i not in index:
                    n = self.nodes[i]
                    fr = {'name': self.node_name(i)}
                    if n[0] == KC:
                        m = self.method(n[1])
                        if m is not None:
                            fr['file'], fr['line'] = m.file, m.line
                    index[i] = len(frames)
                    frames.append(fr)
                seq.append(index[i])
            samples.append(seq)
            weights.append(s[0] * per)
        total = sum(weights)
        return {'$schema': 'https://www.speedscope.app/file-format-schema.json',
                'shared': {'frames': frames},
                'profiles': [{'type': 'sampled', 'name': 'scripts of %s' % self.d.get('object', {}).get('name', '?'),
                              'unit': 'milliseconds', 'startValue': 0, 'endValue': total,
                              'samples': samples, 'weights': weights}],
                'name': 'x3 script profile', 'exporter': 'x3kc dbg profile', 'activeProfileIndex': 0}


def check_speedscope(doc):
    """the speedscope file format's rules (its schema's required members, types and index ranges); [] when valid"""
    errs = []
    if doc.get('$schema') != 'https://www.speedscope.app/file-format-schema.json':
        errs.append('$schema')
    frames = doc.get('shared', {}).get('frames')
    if not isinstance(frames, list) or any(not isinstance(f, dict) or not isinstance(f.get('name'), str)
                                           for f in frames):
        errs.append('shared.frames: objects with a string name')
        frames = []
    for f in frames:
        if 'line' in f and not isinstance(f['line'], int) or 'file' in f and not isinstance(f['file'], str):
            errs.append('a frame\'s file / line')
            break
    profs = doc.get('profiles')
    if not isinstance(profs, list) or not profs:
        errs.append('profiles')
        return errs
    for p in profs:
        if p.get('type') != 'sampled':
            errs.append('profile type')
        if p.get('unit') not in ('none', 'nanoseconds', 'microseconds', 'milliseconds', 'seconds', 'bytes'):
            errs.append('unit')
        for k in ('startValue', 'endValue'):
            if not isinstance(p.get(k), (int, float)):
                errs.append(k)
        s, w = p.get('samples'), p.get('weights')
        if not isinstance(s, list) or not isinstance(w, list) or len(s) != len(w):
            errs.append('samples / weights')
            continue
        for seq in s:
            if not isinstance(seq, list) or any(not isinstance(i, int) or not 0 <= i < len(frames) for i in seq):
                errs.append('a sample\'s frame index')
                break
        if any(not isinstance(x, (int, float)) or x < 0 for x in w):
            errs.append('weights')
    ai = doc.get('activeProfileIndex')
    if ai is not None and not (isinstance(ai, int) and 0 <= ai < len(profs)):
        errs.append('activeProfileIndex')
    return errs


def find_info(dump, dirs=()):
    """the sidecar of the dump's object: the path the client saved with it, else by name / size / crc32 in dirs"""
    o = dump.get('object', {})
    for p in [dump.get('sidecar')]:
        if p and os.path.isfile(p):
            info = kcdbg.DebugInfo.load(p)
            if info.matches(o.get('size'), o.get('crc32')):
                return info
    found = kcdbg.find(o.get('name', ''), list(dirs), o.get('size'), o.get('crc32')) if o.get('name') else None
    return kcdbg.DebugInfo.load(found) if found else None


def text_report(p, by, top):
    """the report as text lines"""
    out = [p.header()]
    if by == 'body':
        out.append('native bodies and cases (C++ in place of bytecode; timed, every call)')
        for r in p.by_cxx()[:top]:
            out.append('  %8.2f ms %9d calls  max %7.1f us  %-44s %s' % (r['ms'], r['calls'], r['maxMs'] * 1000,
                                                                      r['method'] + ' [%s]' % r['kind'], r['where']))
    elif by == 'method':
        out.append('by method (excl: its own bytecode or its body / case C++, natives: in natives it calls itself, '
                   'incl: on the stack; ms estimated from the samples\' share of the script time)')
        out.append('   excl%%  excl ms  +nat ms   incl%%  incl ms  %-44s %s' % ('method', 'where'))
        for r in p.by_method()[:top]:
            out.append('  %6.2f %8.1f %8.1f  %6.2f %8.1f  %-44s %s' % (
                100.0 * r['excl'] / max(1, p.samples), r['exclMs'], r['nativesMs'], 100.0 * r['incl'] / max(1, p.samples),
                r['inclMs'], r['method'], r['where']))
        cxx = p.by_cxx()
        if cxx:
            out.append('native bodies and cases (C++ in place of bytecode; timed, every call)')
            for r in cxx[:top]:
                out.append('  %8.2f ms %9d calls  max %7.1f us  %-44s %s' % (r['ms'], r['calls'], r['maxMs'] * 1000,
                                                                          r['method'] + ' [%s]' % r['kind'], r['where']))
    elif by == 'native':
        out.append('by native (timed, every call; exclusive of the script runs a native makes, which are their own)')
        for r in p.by_native()[:top]:
            out.append('  %8.2f ms %9d calls  avg %6.2f us  max %7.1f us%s  %s' % (
                r['ms'], r['calls'], 1000.0 * r['ms'] / max(1, r['calls']), r['maxMs'] * 1000,
                '  (+%.1f ms in scripts it ran)' % r['nestedMs'] if r['nestedMs'] >= 0.05 else '', r['native']))
            for c in r['callers'][:3]:
                out.append('        %8.2f ms %9d calls  from %s (%s)' % (c['ms'], c['calls'], c['method'],
                                                                         ', '.join(c['sites'][:3])))
    elif by == 'msci':
        m = p.by_msci()
        out.append('by MSCI script (the samples with the script on the stack; ms estimated)')
        for r in m['scripts'][:top]:
            out.append('  %8.1f ms %7d samples  %s' % (r['ms'], r['samples'], r['script']))
        out.append('by MSCI line (the innermost script line: what it runs and the KC / natives below it)')
        for r in m['lines'][:top]:
            out.append('  %8.1f ms %7d samples  %s:%s' % (r['ms'], r['samples'], r['script'], r['line']))
        out.append('by MSCI command (the innermost line\'s command id)')
        for r in m['commands'][:top]:
            out.append('  %8.1f ms %7d samples  %s' % (r['ms'], r['samples'],
                                                       'command %s' % r['command'] if r['command'] is not None
                                                       else '(line not known)'))
    elif by == 'task':
        out.append('by task root (the top-level script runs, timed: the task\'s bottom method; an MSCI task\'s script)')
        for r in p.by_task()[:top]:
            out.append('  %8.2f ms %8d runs  max %7.2f ms  %s' % (r['ms'], r['runs'], r['maxMs'], r['root']))
    return out


def text_frames(p, top):
    out = [p.header(), 'the slowest frames (a frame with a debugger stop is left out)']
    for f in p.frames(top):
        out.append('  frame %d: %.2f ms, scripts %.2f ms (%d runs), natives %.2f ms, %d samples' % (
            f['frame'], f['ms'], f['scriptMs'], f['runs'], f['nativeMs'], f['samples']))
        for r in f['topRuns']:
            out.append('      run    %7.2f ms x%-5d %s' % (r['ms'], r['runs'], r['root']))
        for r in f['topNatives']:
            out.append('      native %7.2f ms x%-5d %s at %s' % (r['ms'], r['calls'], r['native'], r['at']))
        for r in f['topLeaves']:
            out.append('      sample %4d  %s' % (r['samples'], r['node']))
    return out
