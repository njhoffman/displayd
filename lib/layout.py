"""Layouts (profiles): what each output shows, matching against connected monitors, generated presets."""
from dataclasses import dataclass, field, replace
import json
import re

from xrandr import ROTATIONS, effective_size

_POS = re.compile(r'^\d+x\d+$')
_ENV_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass
class OutputCfg:
    edid: str = ''          # monitor fingerprint; pins the entry to that monitor on any port
    mode: str = ''          # 'WxH'; empty = the output's preferred mode
    rate: float = 0.0       # 0 = xrandr's choice
    pos: str = '0x0'
    rotate: str = 'normal'
    scale: float = 1.0
    scale_from: str = ''    # mirror: scale this output to cover WxH
    primary: bool = False
    off: bool = False       # listed only to say the monitor stays off


@dataclass
class Layout:
    name: str
    outputs: dict = field(default_factory=dict)   # port name -> OutputCfg
    env: dict = field(default_factory=dict)
    exec: list = field(default_factory=list)
    source: str = 'config'  # config | saved | preset | auto

    @property
    def enabled(self):
        return [p for p, c in self.outputs.items() if not c.off]


class LayoutError(ValueError):
    pass


def from_dict(name, data, source='config'):
    """Layout from a TOML/JSON table: {outputs = {PORT = {...}}, env = {...}, exec = [...]}."""
    if not isinstance(data, dict):
        raise LayoutError(f'profile {name!r}: not a table')
    outputs = {}
    for port, raw in (data.get('outputs') or {}).items():
        if not isinstance(raw, dict):
            raise LayoutError(f'profile {name!r}: output {port!r} is not a table')
        unknown = set(raw) - set(OutputCfg.__dataclass_fields__)
        if unknown:
            raise LayoutError(f'profile {name!r}: output {port!r}: unknown keys {sorted(unknown)}')
        cfg = OutputCfg(**raw)
        if cfg.rotate not in ROTATIONS:
            raise LayoutError(f'profile {name!r}: output {port!r}: rotate must be one of {ROTATIONS}')
        if not _POS.match(cfg.pos) or (cfg.mode and not _POS.match(cfg.mode)):
            raise LayoutError(f'profile {name!r}: output {port!r}: pos and mode are WxH')
        if not 0.1 <= float(cfg.scale) <= 4:
            raise LayoutError(f'profile {name!r}: output {port!r}: scale out of range')
        cfg.scale, cfg.rate = float(cfg.scale), float(cfg.rate)
        outputs[port] = cfg
    if not any(not c.off for c in outputs.values()):
        raise LayoutError(f'profile {name!r}: no enabled outputs')
    env = data.get('env') or {}
    bad = [k for k in env if not _ENV_KEY.match(k)]
    if bad:
        raise LayoutError(f'profile {name!r}: invalid env names {bad}')
    commands = data.get('exec') or []
    if isinstance(commands, str):
        commands = [commands]
    return Layout(name, outputs, {k: str(v) for k, v in env.items()}, list(commands), source)


def to_dict(layout):
    outputs = {}
    for port, cfg in layout.outputs.items():
        defaults = OutputCfg()
        outputs[port] = {k: v for k, v in vars(cfg).items() if v != getattr(defaults, k) or k in ('pos',)}
    data = {'outputs': outputs}
    if layout.env:
        data['env'] = dict(layout.env)
    if layout.exec:
        data['exec'] = list(layout.exec)
    return data


def to_toml(layout):
    """A [profiles."NAME"] section for appending to config.toml (no TOML writer in the stdlib)."""
    def value(v):
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, (int, float)):
            return f'{v:g}'
        if isinstance(v, list):
            return '[' + ', '.join(value(x) for x in v) + ']'
        return json.dumps(str(v))
    data = to_dict(layout)
    key = json.dumps(layout.name)
    lines = [f'[profiles.{key}]']
    if layout.exec:
        lines.append(f'exec = {value(layout.exec)}')
    if layout.env:
        lines.append(f'[profiles.{key}.env]')
        lines += [f'{k} = {value(v)}' for k, v in layout.env.items()]
    for port, cfg in data['outputs'].items():
        lines.append(f'[profiles.{key}.outputs.{json.dumps(port)}]')
        lines += [f'{k} = {value(v)}' for k, v in cfg.items()]
    return '\n'.join(lines) + '\n'


def resolve(layout, available):
    """Map LAYOUT onto AVAILABLE ({port: Output}); None when it does not fit this setup.

    An entry with an edid follows that monitor to whatever port it is on; one
    without uses its port. The layout fits when it covers exactly the available
    outputs and every mode exists. Modes left empty become the preferred mode.
    """
    by_fp = {}
    for port, out in available.items():
        by_fp.setdefault(out.fingerprint, []).append(port)
    targets, used = {}, set()
    for port, cfg in layout.outputs.items():
        if cfg.edid:
            port = next((p for p in by_fp.get(cfg.edid, []) if p not in used), None)
        if port is None or port not in available or port in used:
            return None
        used.add(port)
        out = available[port]
        if cfg.off:
            targets[port] = cfg
            continue
        mode = out.mode(cfg.mode or None)
        if mode is None:
            return None
        rate = cfg.rate if cfg.rate and any(abs(r - cfg.rate) < 0.02 for r in mode.rates) else 0.0
        targets[port] = replace(cfg, mode=mode.size, rate=rate)
    if used != set(available):
        return None
    enabled = [c for c in targets.values() if not c.off]
    if not enabled:
        return None
    if not any(c.primary for c in enabled):
        enabled[0].primary = True
    return targets


# Placements offered by the menu. Each is (key, label, builder); builders get the
# internal port (or None) and external ports and return a placement dict:
#   row: ports left to right; above: ports in a row above `row`;
#   rotate: {port: rotation}; mirror: (source, copy); off: ports turned off.
def placements(internal, externals):
    out = []
    ext = list(externals)
    if internal and ext:
        out += [
            ('right', 'Right of laptop', {'row': [internal] + ext}),
            ('left', 'Left of laptop', {'row': ext + [internal]}),
            ('above', 'Above laptop', {'row': [internal], 'above': ext}),
            ('external', 'External only (laptop off)', {'row': ext, 'off': [internal]}),
            ('laptop', 'Laptop only', {'row': [internal], 'off': ext}),
        ]
        if len(ext) == 1:
            out += [
                ('mirror', 'Mirror', {'mirror': (ext[0], internal)}),
                ('left-portrait', 'Left of laptop, portrait',
                 {'row': ext + [internal], 'rotate': {ext[0]: 'right'}}),
                ('right-portrait', 'Right of laptop, portrait',
                 {'row': [internal] + ext, 'rotate': {ext[0]: 'left'}}),
            ]
    else:
        ports = ([internal] if internal else []) + ext
        if len(ports) == 1:
            out.append(('single', 'Single display', {'row': ports}))
        else:
            out.append(('row', 'Side by side', {'row': ports}))
            out.append(('row-reverse', 'Side by side, reversed', {'row': ports[::-1]}))
    return out


def build(name, placement, available, modes=None, scale=1.0, internal=None, primary='external',
          edid=True):
    """Layout for PLACEMENT. MODES maps port -> 'WxH' (default preferred); SCALE applies to externals."""
    modes = modes or {}
    rotate = placement.get('rotate', {})
    cfgs = {}

    def cfg(port):
        out = available[port]
        mode = out.mode(modes.get(port))
        s = 1.0 if port == internal else scale
        return OutputCfg(edid=out.fingerprint if edid and out.edid else '', mode=mode.size,
                         rotate=rotate.get(port, 'normal'), scale=s), mode

    def size(port):
        c, mode = cfg(port)
        return effective_size(mode.width, mode.height, c.rotate, c.scale)

    if 'mirror' in placement:
        src, copy = placement['mirror']
        c, _ = cfg(src)
        w, h = size(src)
        cfgs[src] = c
        mc, _ = cfg(copy)
        mc.scale_from = f'{w}x{h}'
        mc.scale = 1.0
        cfgs[copy] = mc
    else:
        row, above = placement.get('row', []), placement.get('above', [])
        row_w = sum(size(p)[0] for p in row)
        above_w = sum(size(p)[0] for p in above)
        above_h = max((size(p)[1] for p in above), default=0)
        x = max(0, (row_w - above_w) // 2)
        for p in above:
            c, _ = cfg(p)
            c.pos = f'{x}x0'
            cfgs[p] = c
            x += size(p)[0]
        x = max(0, (above_w - row_w) // 2)
        for p in row:
            c, _ = cfg(p)
            c.pos = f'{x}x{above_h}'
            cfgs[p] = c
            x += size(p)[0]
    for p in placement.get('off', []):
        cfgs[p] = OutputCfg(edid=available[p].fingerprint if edid and available[p].edid else '', off=True)
    if p := choose_primary(cfgs, internal, primary):
        cfgs[p].primary = True
    return Layout(name, cfgs, source='preset')


def choose_primary(cfgs, internal, primary='external'):
    enabled = [p for p, c in cfgs.items() if not c.off]
    if not enabled:
        return None
    externals = [p for p in enabled if p != internal]
    if primary == 'external' and externals:
        return externals[0]
    return internal if internal in enabled else enabled[0]


def _rect(cfg, mode):
    """(x, y, w, h) an enabled output covers; a mirror copy covers its scale_from area."""
    if cfg.scale_from:
        w, h = (int(v) for v in cfg.scale_from.split('x'))
    else:
        w, h = effective_size(mode.width, mode.height, cfg.rotate, cfg.scale)
    x, y = (int(v) for v in cfg.pos.split('x'))
    return x, y, w, h


def _realign(start, size, ref, ref_size, new_ref_size, fallback):
    """Where a neighbour spanning START..START+SIZE goes along an edge of REF..REF+REF_SIZE when that edge becomes NEW_REF_SIZE long.

    A neighbour aligned to the edge's centre (within a pixel), start or end
    keeps that alignment, centre first (one as long as the edge is all three);
    any other keeps FALLBACK.
    """
    if abs(2 * start + size - 2 * ref - ref_size) <= 2:
        return ref + (new_ref_size - size) // 2
    if start == ref:
        return ref
    if start + size == ref + ref_size:
        return ref + new_ref_size - size
    return fallback


def _follow(rect, ref, new_ref):
    """((x, y), aligned) for RECT touching REF's edge (spans overlapping) when REF moves and resizes to NEW_REF;
    None when it does not touch. Beyond the right (bottom) edge it stays flush with the new edge, before the left
    (top) one it moves with REF; along the shared edge it keeps its alignment (_realign: ALIGNED) or else moves
    with REF."""
    cx, cy, cw, ch = rect
    rx, ry, rw, rh = ref
    nrx, nry, nrw, nrh = new_ref
    dx, dy = nrx - rx, nry - ry
    beside = cy < ry + rh and cy + ch > ry
    stacked = cx < rx + rw and cx + cw > rx
    if stacked and (cy == ry + rh or cy + ch == ry):
        ny = nry + nrh if cy == ry + rh else cy + dy
        nx = _realign(cx, cw, rx, rw, nrw, None)
        return ((cx if nx is None else nx) + dx, ny), nx is not None
    if beside and (cx == rx + rw or cx + cw == rx):
        nx = nrx + nrw if cx == rx + rw else cx + dx
        ny = _realign(cy, ch, ry, rh, nrh, None)
        return (nx, (cy if ny is None else ny) + dy), ny is not None
    return None


def resize(layout, port, available, mode=None, scale=None, name=None):
    """Copy of LAYOUT (resolved onto AVAILABLE, so keyed by port) with PORT at MODE ('WxH') and SCALE, the others moved so they stay adjacent.

    Placement spreads outward from PORT: a screen touching one already placed
    (PORT first, then each screen as it is placed) stays flush with that
    screen's new edge and keeps how it lined up with it along the edge: start,
    centre or end. So a screen beyond a re-centred neighbour follows the
    neighbour. Screens touching none shift by the change in PORT's width
    (height) when they start at or beyond its right (bottom) edge. An output
    covering exactly PORT's area is its mirror and follows it through
    scale_from. Positions are then moved so the screen starts at 0x0. A changed
    mode drops the pinned rate.
    """
    targets = resolve(layout, available)
    if targets is None or port not in targets or targets[port].off:
        raise LayoutError(f'{port} is not on in {layout.name!r}')
    out = available[port]
    cfgs = {p: replace(c) for p, c in targets.items()}
    old = targets[port]
    new_mode = out.mode(mode or old.mode)
    if new_mode is None:
        raise LayoutError(f'{port} has no mode {mode}')
    x, y, w, h = _rect(old, out.mode(old.mode))
    rects = {p: _rect(c, available[p].mode(c.mode)) for p, c in targets.items() if not c.off}
    twins = [p for p, r in rects.items() if p != port and r == (x, y, w, h)]
    changed = cfgs[port]
    if new_mode.size != old.mode:
        changed.mode, changed.rate = new_mode.size, 0.0
    if scale is not None:
        changed.scale, changed.scale_from = float(scale), ''
    nw, nh = effective_size(new_mode.width, new_mode.height, changed.rotate, changed.scale)
    dw, dh = nw - w, nh - h
    for p in twins:
        cfgs[p].pos, cfgs[p].scale, cfgs[p].scale_from = changed.pos, 1.0, f'{nw}x{nh}'
    # a screen that keeps an alignment with a placed neighbour is placed by it; one that only moves along with a
    # neighbour waits until nothing aligned is left, so a neighbour it lines up with wins
    placed = {port: (x, y, nw, nh)}
    queue, loose = [port], {}
    while queue:
        ref = queue.pop(0)
        for p, rect in rects.items():
            if p in placed or p in twins:
                continue
            follow = _follow(rect, rects[ref], placed[ref])
            if follow is None:
                continue
            (fx, fy), aligned = follow
            if aligned:
                placed[p] = (fx, fy, rect[2], rect[3])
                loose.pop(p, None)
                queue.append(p)
            else:
                loose.setdefault(p, (fx, fy, rect[2], rect[3]))
        if not queue and loose:
            placed.update(loose)
            queue, loose = list(loose), {}
    for p, (cx, cy, cw, ch) in rects.items():
        if p == port or p in twins:
            continue
        if p in placed:
            nx, ny = placed[p][:2]
        else:
            nx = cx + dw if cx >= x + w else cx
            ny = cy + dh if cy >= y + h else cy
        cfgs[p].pos = f'{nx}x{ny}'
    on = [c for c in cfgs.values() if not c.off]
    mx = min(int(c.pos.split('x')[0]) for c in on)
    my = min(int(c.pos.split('x')[1]) for c in on)
    for c in on:
        cx, cy = (int(v) for v in c.pos.split('x'))
        c.pos = f'{cx - mx}x{cy - my}'
    return Layout(name or layout.name, cfgs, dict(layout.env), list(layout.exec), layout.source)
