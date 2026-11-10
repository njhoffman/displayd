"""Decide and apply: the connected setup, which layout it gets, the switch itself, hooks and notifications."""
from dataclasses import dataclass
import glob
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import layout as lay
import store
import xrandr

ROOT = Path(__file__).resolve().parent.parent
DISPLAYD = ROOT / 'bin' / 'displayd'
LID_GLOB = '/proc/acpi/button/lid/*/state'


def read_lid():
    """'open' or 'closed'; 'open' on machines without a lid."""
    for path in sorted(glob.glob(os.environ.get('DISPLAYD_LID_STATE') or LID_GLOB)):
        try:
            return 'closed' if 'closed' in Path(path).read_text() else 'open'
        except OSError:
            continue
    return 'open'


@dataclass
class Setup:
    outputs: list        # every output xrandr reports
    lid: str
    available: dict      # port -> Output that should be used: connected, minus lid-closed panels
    internal: str        # first internal port in `available`, else None
    externals: list      # other ports in `available`
    connected: dict      # port -> fingerprint of every connected output

    @property
    def key(self):
        return '+'.join(sorted(o.fingerprint for o in self.available.values()))

    @property
    def active(self):
        return sorted(o.name for o in self.outputs if o.active)


def detect(outputs, lid, internal_names=()):
    connected = {o.name: o for o in outputs if o.connected}
    internal = [n for n, o in connected.items() if o.internal(internal_names)]
    externals = [n for n in connected if n not in internal]
    available = dict(connected)
    if lid == 'closed' and externals:
        for name in internal:
            del available[name]
    avail_internal = [n for n in internal if n in available]
    return Setup(outputs, lid, available, avail_internal[0] if avail_internal else None,
                 [n for n in available if n not in avail_internal[:1]],
                 {n: o.fingerprint for n, o in connected.items()})


class System:
    """Everything with side effects, so tests can replace run/spawn/notify."""

    def __init__(self, paths=None, run=subprocess.run, spawn=None, log=None, echo=False):
        self.paths = paths or store.Paths()
        self.run = run
        self.spawn = spawn or self._spawn
        self.log = log or store.Log(self.paths, echo)
        self._pending = None   # the 'setup|layout' pair a confirm notification is waiting on
        self._lid_wait = None  # (setup key, monotonic deadline) while a closed lid waits for an external
        self.clock = time.monotonic
        self.reload()

    def reload(self):
        self.config = store.load_config(self.paths)
        self.state = store.State(self.paths)
        for err in self.config.errors:
            self.log(f'config: {err}')

    # -- probing --------------------------------------------------------
    def detect(self):
        return detect(xrandr.query(self.run), read_lid(), self.config.internal)

    def label(self, out):
        return self.config.monitors.get(out.fingerprint) or out.label

    def describe(self, setup):
        parts = []
        for port, out in setup.available.items():
            kind = 'laptop' if out.internal(self.config.internal) else self.label(out)
            parts.append(f'{kind} ({port})')
        if setup.lid == 'closed':
            parts.append('lid closed')
        return ', '.join(parts)

    def summary(self, layout, targets, setup):
        """One line for the shell: a config profile's name, else each screen on with its mode ('laptop 1920x1080')."""
        if layout.source == 'config':
            return layout.name
        parts = []
        for port, cfg in targets.items():
            out = setup.available.get(port)
            if cfg.off or out is None:
                continue
            kind = 'laptop' if out.internal(self.config.internal) else self.label(out)
            mode = cfg.mode or (out.mode().size if out.mode() else '')
            parts.append(f'{kind} {mode}'.strip())
        return ' + '.join(parts) or layout.name

    def live_layout(self, setup, name, source='config'):
        """The layout on screen now (also after arandr): live modes, positions, rotation, scale, primary."""
        outputs = {}
        for port, out in setup.available.items():
            mode = out.current_mode
            edid = out.fingerprint if out.edid else ''
            if not out.active or mode is None:
                outputs[port] = {'edid': edid, 'off': True}
                continue
            w, h, x, y = out.geometry
            mw = mode.height if out.rotate in ('left', 'right') else mode.width
            outputs[port] = {'edid': edid, 'mode': mode.size, 'rate': mode.current_rate or 0.0,
                             'pos': f'{x}x{y}', 'rotate': out.rotate, 'scale': round(w / mw, 3),
                             'primary': out.primary}
        return lay.from_dict(name, {'outputs': outputs}, source)

    def changes(self, setup):
        """Human lines for what changed since the last switch."""
        last = self.state['last']
        before, now = last.get('connected', {}), setup.connected
        by_name = {o.name: o for o in setup.outputs}
        lines = [f'Connected: {self.label(by_name[p])} ({p})' for p in now if before.get(p) != now[p]]
        labels = self.state.data.get('labels', {})
        lines += [f'Disconnected: {labels.get(fp, p)} ({p})' for p, fp in before.items() if p not in now]
        if last.get('lid') and last['lid'] != setup.lid:
            lines.append(f'Lid {"closed" if setup.lid == "closed" else "opened"}')
        return lines

    def lid_wait(self, setup, reasons=()):
        """Seconds left before a closed panel is turned on because the last external went away, else 0.

        Waits only while the lid is closed, no external is connected and the panel is off in X now: a hub
        or dock that drops its monitor for a moment then gets it back without the session moving to a
        panel nobody can see. 'force' (displayd apply) does not wait.
        """
        try:
            grace = float(self.config.lid_closed_wait)
        except (TypeError, ValueError):
            grace = 0.0
        if (grace <= 0 or 'force' in reasons or setup.lid != 'closed' or setup.externals
                or not setup.internal or setup.internal in setup.active):
            self._lid_wait = None
            return 0.0
        now = self.clock()
        if not self._lid_wait or self._lid_wait[0] != setup.key:
            self._lid_wait = (setup.key, now + grace)
        left = self._lid_wait[1] - now
        if left <= 0:
            self._lid_wait = None
            return 0.0
        return left

    # -- choosing -------------------------------------------------------
    def profiles(self):
        """Config profiles, then menu-saved layouts (config wins on a name clash)."""
        found = dict(self.config.profiles)
        for name, raw in self.state['layouts'].items():
            if name not in found:
                try:
                    found[name] = lay.from_dict(name, raw, 'saved')
                except (lay.LayoutError, TypeError, ValueError) as exc:
                    self.log(f'state: {exc}')
        return found

    def matching(self, setup):
        """[(layout, targets)] of saved profiles that fit SETUP, default first."""
        default = self.state['defaults'].get(setup.key)
        out = []
        for name, layout in self.profiles().items():
            targets = lay.resolve(layout, setup.available)
            if targets:
                out.append((layout, targets))
        out.sort(key=lambda item: item[0].name != default)
        return out

    def presets(self, setup):
        return lay.placements(setup.internal, setup.externals)

    def preset_layout(self, setup, key, modes=None, scale=1.0, name=None):
        for pkey, label, placement in self.presets(setup):
            if pkey == key:
                layout = lay.build(name or label, placement, setup.available, modes, scale,
                                   setup.internal, self.config.primary)
                return layout, lay.resolve(layout, setup.available)
        return None, None

    def choose(self, setup):
        """(layout, targets, why) for SETUP without asking, or (None, None, None) for a new setup."""
        default = self.state['defaults'].get(setup.key)
        matches = self.matching(setup)
        if matches:
            return (*matches[0], 'default' if matches[0][0].name == default else 'profile')
        if len(setup.available) == 1:
            port = next(iter(setup.available))
            layout, targets = self.preset_layout(setup, 'single', name=f'{self.label(setup.available[port])} only')
            return layout, targets, 'only display'
        return None, None, None

    def fallback(self, setup):
        layout, targets = self.preset_layout(setup, self.config.fallback)
        if not targets:
            first = self.presets(setup)[0][0]
            layout, targets = self.preset_layout(setup, first)
        return layout, targets

    # -- applying -------------------------------------------------------
    def hook_env(self, layout, targets, setup):
        enabled = [p for p, c in targets.items() if not c.off]
        env = dict(os.environ)
        env.update(layout.env)
        env.update({
            'DISPLAYD_PROFILE': layout.name,
            'DISPLAYD_PREV': self.state['last'].get('profile', ''),
            'DISPLAYD_SETUP': setup.key,
            'DISPLAYD_OUTPUTS': ' '.join(enabled),
            'DISPLAYD_PRIMARY': next((p for p, c in targets.items() if c.primary), ''),
            'DISPLAYD_LID': setup.lid,
        })
        return env

    def hooks(self, kind):
        d = self.paths.hook_dir(kind)
        if not d.is_dir():
            return []
        return [str(p) for p in sorted(d.iterdir()) if p.is_file() and os.access(p, os.X_OK)]

    def apply(self, layout, targets, setup, why=''):
        """Switch to LAYOUT. False (after restoring a usable screen) when xrandr fails."""
        env = self.hook_env(layout, targets, setup)
        for hook in self.hooks('preswitch'):
            try:
                self.run([hook], env=env, timeout=10, capture_output=True)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(f'preswitch {hook}: {exc}')
        for cmd in xrandr.commands(targets, setup.outputs):
            self.log(f'run: {" ".join(cmd)}')
            proc = self.run(cmd, capture_output=True, text=True, timeout=20)
            if proc.returncode != 0:
                self.log(f'xrandr failed ({proc.returncode}): {(proc.stderr or "").strip()}')
                self.rescue(setup)
                self.notify('Display switch failed', f'{layout.name}: {(proc.stderr or "").strip()[:200]}',
                            urgency='critical')
                return False
        self.state['env_keys'] = store.write_env(self.paths, layout.name, layout.env, self.state['env_keys'],
                                                 self.summary(layout, targets, setup))
        labels = dict(self.state.data.get('labels', {}))
        labels.update({o.fingerprint: self.label(o) for o in setup.outputs if o.connected})
        self.state['labels'] = labels
        self.state['last'] = {
            'profile': layout.name, 'setup': setup.key, 'lid': setup.lid, 'why': why,
            'connected': setup.connected, 'enabled': sorted(p for p, c in targets.items() if not c.off),
        }
        self.state.save()
        self.log(f'applied {layout.name!r} ({why or layout.source}) for {setup.key}')
        for command in layout.exec + list(self.config.exec):
            self.spawn(['sh', '-c', command], env)
        for hook in self.hooks('postswitch'):
            self.spawn([hook], env)
        return True

    def rescue(self, setup):
        """Turn on one display (the laptop panel when connected) so the screen is never dark."""
        port = next((n for n, o in setup.available.items() if o.internal(self.config.internal)), None)
        port = port or next(iter(setup.available), None)
        if port:
            self.log(f'rescue: enabling {port}')
            self.run(['xrandr', '--output', port, '--auto', '--primary'], capture_output=True, timeout=20)

    # -- rescue and confirm ---------------------------------------------
    def laptop_only(self, setup, why='rescue'):
        """Laptop panel alone, held for SETUP: hotplug noise from the same monitors does not undo it."""
        if not setup.internal:
            return False
        layout, targets = self.preset_layout(setup, 'laptop' if setup.externals else 'single',
                                             name='Laptop only (rescue)')
        ok = self.apply(layout, targets, setup, why)
        if ok:
            self.state['hold'] = setup.key
            self.state.save()
            self.log(f'hold: {setup.key} stays on the laptop panel until the monitors change or apply')
        return ok

    def needs_confirm(self, layout, targets, setup):
        """True when LAYOUT turns the open laptop panel off and was never kept for this setup."""
        try:
            seconds = float(self.config.confirm)
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0 or setup.lid != 'open' or not setup.internal:
            return False
        cfg = targets.get(setup.internal)
        if cfg is not None and not cfg.off:
            return False
        return f'{setup.key}|{layout.name}' not in self.state['confirmed']

    def confirm(self, layout, setup, lines):
        """Ask in the background whether to keep LAYOUT; no answer in time goes back to the laptop panel."""
        pair = f'{setup.key}|{layout.name}'
        if self._pending == pair:
            return
        self._pending = pair
        threading.Thread(target=self._confirm, args=(pair, setup.key, layout.name, lines), daemon=True).start()

    def _confirm(self, pair, key, name, lines):
        try:
            seconds = float(self.config.confirm)
            body = '\n'.join(lines + [f'Layout: {name}', f'Back to the laptop panel in {seconds:g}s unless kept'])
            answer = self.ask_keep('Keep this display layout?', body, seconds)
            if answer is None:
                self.log(f'confirm: could not ask; keeping {name!r} without remembering it')
                return
            self.settle(pair, key, name, answer)
        except Exception as exc:  # a thread must not die silently
            self.log(f'confirm failed: {exc!r}')
        finally:
            self._pending = None

    def ask_keep(self, summary, body, seconds):
        """True when kept (the Keep button, or the notification closed by hand: someone saw it); False when
        nobody answered within SECONDS (the screen showing it is probably dark); None when it could not ask."""
        if not self.config.notify:
            return None
        cmd = self.notify_cmd(summary, body, 'critical', int((seconds + 5) * 1000), ('keep', 'Keep'), False)
        if not cmd:
            return None
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        except OSError as exc:
            self.log(f'confirm: {exc}')
            return None
        if proc.returncode != 0:
            self.log(f'confirm: {cmd[0]} exited {proc.returncode}: {(proc.stderr or "").strip()[:200]}')
            return None
        return True

    def settle(self, pair, key, name, kept):
        """Remember a kept layout, or go back to the laptop panel if NAME is still on screen for KEY."""
        with store.locked(self.paths.runtime / 'apply.lock'):
            self.reload()
            if kept:
                if pair not in self.state['confirmed']:
                    self.state['confirmed'] = self.state['confirmed'] + [pair]
                    self.state.save()
                self.log(f'confirm: kept {name!r} for {key}')
                return 'kept'
            setup = self.detect()
            last = self.state['last']
            if setup.key != key or last.get('setup') != key or last.get('profile') != name:
                self.log(f'confirm: no answer, but {name!r} is no longer on screen; leaving it')
                return 'stale'
            self.log(f'confirm: no answer for {name!r}; back to the laptop panel')
            ok = self.laptop_only(setup)
            self.notify('Display layout reverted',
                        f'{name} was not kept.\nRun `displayd apply` to try it again.', urgency='critical')
            return 'reverted' if ok else 'failed'

    def _spawn(self, cmd, env=None):
        try:
            with open(self.paths.log_file, 'a') as log:
                return subprocess.Popen(cmd, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                        start_new_session=True)
        except OSError as exc:
            self.log(f'spawn {cmd}: {exc}')
            return None

    # -- notifications --------------------------------------------------
    def notify_cmd(self, summary, body, urgency='normal', timeout_ms=8000, action=None, replace=True):
        """The notify-send(.py) command, or None without one. ACTION: (key, label) for a button.

        notify-send.py's --action takes any number of values, so it goes after SUMMARY and BODY; before them it
        swallows both and the command fails at once.
        """
        helper = shutil.which('notify-send.py')
        if helper:
            cmd = [helper, '-a', 'displayd', '-u', urgency, '-t', str(timeout_ms), '-i', 'video-display']
            if replace:
                cmd += ['--replaces-process', 'displayd']
            cmd += [summary, body]
            if action:
                cmd += ['--action', f'{action[0]}:{action[1]}']
            return cmd
        if shutil.which('notify-send'):
            cmd = ['notify-send', '-a', 'displayd', '-u', urgency, '-t', str(timeout_ms), '-i', 'video-display']
            if action:
                cmd += ['-A', f'{action[0]}={action[1]}']
            return cmd + [summary, body]
        return None

    def notify(self, summary, body='', urgency='normal', change=False):
        """Desktop notification; with CHANGE a 'Change layout' button opens the menu."""
        if not self.config.notify:
            return
        cmd = self.notify_cmd(summary, body, urgency, action=('change', 'Change layout') if change else None)
        if not cmd:
            return
        if not change:
            self.spawn(cmd)
            return
        threading.Thread(target=self._await_action, args=(cmd,), daemon=True).start()

    def _await_action(self, cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            return
        if proc.stdout.strip().splitlines()[-1:] == ['change']:
            self.spawn([str(DISPLAYD), 'menu'])


def evaluate(system, reasons=(), interactive=True, open_menu=None, schedule=None):
    """Bring the screen in line with what is connected. Returns what it did, for logs and tests.

    SCHEDULE(seconds) asks the caller to evaluate again later (a closed lid waiting for an external).
    """
    with store.locked(system.paths.runtime / 'apply.lock'):
        system.reload()
        setup = system.detect()
        if not setup.available:
            system.log('no connected outputs; leaving the screen alone')
            return 'none'
        wait = system.lid_wait(setup, reasons)   # every evaluate, so an external that came back resets it
        hold = system.state['hold']
        if hold and ('force' in reasons or hold != setup.key):
            system.state['hold'] = ''
            system.state.save()
            system.log(f'hold released: {"forced" if "force" in reasons else "monitors changed"}')
        elif hold:
            system.log(f'held on the laptop panel ({", ".join(reasons) or "check"}): {setup.key}')
            return 'held'
        last = system.state['last']
        if ('force' not in reasons and last.get('setup') == setup.key and last.get('lid') == setup.lid
                and last.get('enabled') == setup.active):
            system.log(f'unchanged ({", ".join(reasons) or "check"}): {setup.key}')
            return 'unchanged'
        if wait > 0:
            system.log(f'lid closed, no external: waiting {wait:.0f}s for one before turning on {setup.internal}')
            if schedule:
                schedule(wait)
            return 'waiting'
        changes = system.changes(setup)
        layout, targets, why = system.choose(setup)
        if layout is None and interactive and open_menu:
            system.notify('New display setup', '\n'.join(changes + ['Choose a layout']))
            open_menu(setup)
            return 'menu'
        if layout is None:
            layout, targets = system.fallback(setup)
            why = 'fallback'
        ok = system.apply(layout, targets, setup, why)
        if ok and system.needs_confirm(layout, targets, setup):
            system.confirm(layout, setup, changes)
        elif ok:
            body = '\n'.join(changes + [f'Layout: {layout.name} ({why})'])
            system.notify('Display changed' if changes else 'Display layout', body, change=True)
        return why if ok else 'failed'
