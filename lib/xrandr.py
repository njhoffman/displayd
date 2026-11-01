"""Read outputs from `xrandr --query --prop` and build the xrandr commands that apply a layout."""
from dataclasses import dataclass, field
import hashlib
import re
import subprocess

INTERNAL_PREFIXES = ('eDP', 'LVDS', 'DSI')
ROTATIONS = ('normal', 'left', 'right', 'inverted')

_HEADER = re.compile(
    r'^(?P<name>\S+) (?P<state>connected|disconnected)(?P<primary> primary)?'
    r'(?: (?P<w>\d+)x(?P<h>\d+)\+(?P<x>\d+)\+(?P<y>\d+))?'
    r'(?: (?P<rot>left|right|inverted))?')
_MODE = re.compile(r'^\s{2,}(?P<w>\d+)x(?P<h>\d+)i?\s+(?P<rates>.*)$')
_RATE = re.compile(r'(\d+(?:\.\d+)?)([ *+]*)')


@dataclass
class Mode:
    width: int
    height: int
    rates: list = field(default_factory=list)
    current_rate: float = None
    preferred: bool = False

    @property
    def size(self):
        return f'{self.width}x{self.height}'


@dataclass
class Output:
    name: str
    connected: bool
    primary: bool = False
    geometry: tuple = None  # (w, h, x, y) when the output is enabled
    rotate: str = 'normal'
    edid: str = ''
    modes: list = field(default_factory=list)

    @property
    def active(self):
        return self.geometry is not None

    @property
    def fingerprint(self):
        """Stable id of the monitor: a hash of its EDID, else the port name."""
        if not self.edid:
            return f'port:{self.name}'
        return hashlib.sha256(bytes.fromhex(self.edid)).hexdigest()[:12]

    @property
    def label(self):
        return monitor_name(self.edid) or self.name

    def internal(self, names=()):
        return self.name in names if names else self.name.startswith(INTERNAL_PREFIXES)

    def mode(self, size=None):
        """The mode named SIZE ('WxH'), else the preferred one, else the first."""
        if size:
            return next((m for m in self.modes if m.size == size), None)
        return next((m for m in self.modes if m.preferred), self.modes[0] if self.modes else None)

    @property
    def current_mode(self):
        return next((m for m in self.modes if m.current_rate is not None), None)


def parse(text):
    """Outputs in xrandr order from `xrandr --query --prop` output."""
    outputs, current, in_edid = [], None, False
    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            current = Output(header['name'], header['state'] == 'connected', bool(header['primary']))
            if header['w']:
                current.geometry = tuple(int(header[k]) for k in ('w', 'h', 'x', 'y'))
                current.rotate = header['rot'] or 'normal'
            outputs.append(current)
            in_edid = False
            continue
        if current is None:
            continue
        if line.startswith('\tEDID:'):
            in_edid = True
            continue
        if in_edid and line.startswith('\t\t') and re.fullmatch(r'[0-9a-fA-F]+', line.strip()):
            current.edid += line.strip().lower()
            continue
        in_edid = False
        mode = _MODE.match(line)
        if mode and not line.startswith('\t'):
            entry = Mode(int(mode['w']), int(mode['h']))
            for rate, marks in _RATE.findall(mode['rates']):
                entry.rates.append(float(rate))
                if '*' in marks:
                    entry.current_rate = float(rate)
                if '+' in marks:
                    entry.preferred = True
            current.modes.append(entry)
    return outputs


def query(run=subprocess.run):
    """Probe the outputs (this re-reads connector state, as on hotplug)."""
    proc = run(['xrandr', '--query', '--prop'], capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f'xrandr --query failed: {proc.stderr.strip()}')
    return parse(proc.stdout)


def monitor_name(edid_hex):
    """'Dell S3221QS'-style name from the EDID's monitor-name descriptor (or vendor + product)."""
    if len(edid_hex) < 256:
        return ''
    data = bytes.fromhex(edid_hex[:256])
    name = ''
    for start in (54, 72, 90, 108):
        block = data[start:start + 18]
        if block[:3] == b'\0\0\0' and block[3] == 0xFC:
            name = block[5:].split(b'\n')[0].decode('ascii', 'replace').strip()
    vendor_bits = (data[8] << 8) | data[9]
    vendor = ''.join(chr(((vendor_bits >> s) & 0x1F) + 64) for s in (10, 5, 0))
    if not vendor.isalpha():
        vendor = ''
    if name:
        return name if not vendor or name.upper().startswith(vendor) else f'{vendor} {name}'
    product = data[10] | (data[11] << 8)
    return f'{vendor} {product:04x}' if vendor else ''


def effective_size(width, height, rotate='normal', scale=1.0):
    """Screen area an output covers: its mode, scaled, swapped when rotated a quarter turn."""
    w, h = round(width * scale), round(height * scale)
    return (h, w) if rotate in ('left', 'right') else (w, h)


def commands(targets, outputs):
    """xrandr argument lists that switch OUTPUTS to TARGETS ({port: resolved OutputCfg}).

    Outputs that are active (or connected) and not targeted are turned off. When an
    output that stays on is already active they are turned off in a first command,
    which frees CRTCs; otherwise everything goes in one command
    so the screen never has zero outputs.
    """
    enabled = {p: c for p, c in targets.items() if not c.off}
    off = [o.name for o in outputs if o.name not in enabled and (o.active or o.connected)]
    still_on = any(o.active and o.name in enabled for o in outputs)
    cmds = []
    if off and still_on:
        cmds.append(['xrandr'] + [a for p in off for a in ('--output', p, '--off')])
        off = []
    main = ['xrandr']
    for port, cfg in enabled.items():
        main += ['--output', port, '--mode', cfg.mode]
        if cfg.rate:
            main += ['--rate', f'{cfg.rate:.2f}']
        main += ['--pos', cfg.pos, '--rotate', cfg.rotate]
        if cfg.scale_from:
            main += ['--scale-from', cfg.scale_from]
        else:
            main += ['--scale', f'{cfg.scale:g}x{cfg.scale:g}']
        if cfg.primary:
            main.append('--primary')
    for port in off:
        main += ['--output', port, '--off']
    cmds.append(main)
    return cmds
