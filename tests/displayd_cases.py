"""displayd: xrandr parsing, layout matching and placement, choosing, applying, menu, against a fake X."""
from dataclasses import replace
import argparse
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import contextlib  # noqa: E402
import core  # noqa: E402
import daemon  # noqa: E402
import layout as lay  # noqa: E402
import menu  # noqa: E402
import store  # noqa: E402
import xrandr  # noqa: E402

_loader = importlib.machinery.SourceFileLoader('displayd_cli', str(Path(__file__).resolve().parent.parent / 'bin' / 'displayd'))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(cli)


def edid(vendor='DEL', name='U2719D', product=0x4123, serial=1):
    data = bytearray(128)
    data[0:8] = bytes.fromhex('00ffffffffffff00')
    bits = sum((ord(c) - 64) << s for c, s in zip(vendor, (10, 5, 0)))
    data[8], data[9] = bits >> 8, bits & 0xFF
    data[10], data[11] = product & 0xFF, product >> 8
    data[12] = serial
    if name:
        data[54:59] = b'\0\0\0\xfc\0'
        data[59:72] = (name.encode() + b'\n').ljust(13, b' ')
    return data.hex()


LAPTOP = edid('LGD', '', 0x061f)
DELL = edid('DEL', 'U2719D')
LG = edid('GSM', 'LG HDR 4K')
LAPTOP_MODES = [('1920x1080', [60.02, 60.01], True), ('1280x720', [60.0], False)]
DELL_MODES = [('2560x1440', [59.95, 144.0], True), ('3840x2160', [60.0], False), ('1920x1080', [60.0], False)]


class FakeX:
    """Renders `xrandr --query --prop` from a model and applies xrandr commands to it."""

    def __init__(self):
        self.outputs = {}
        self.commands = []
        self.fail = False

    def plug(self, name, edid_hex, modes, geometry=None, primary=False):
        self.outputs[name] = {'connected': True, 'edid': edid_hex, 'modes': modes, 'geometry': geometry,
                              'primary': primary, 'rotate': 'normal'}

    def unplug(self, name):
        self.outputs[name]['connected'] = False

    def text(self):
        lines = ['Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384']
        for name, o in self.outputs.items():
            head = f'{name} {"connected" if o["connected"] else "disconnected"}'
            if o['primary']:
                head += ' primary'
            if o['geometry']:
                w, h, x, y = o['geometry']
                head += f' {w}x{h}+{x}+{y}' + ('' if o['rotate'] == 'normal' else f' {o["rotate"]}')
            lines.append(head + ' (normal left inverted right x axis y axis)')
            if not o['connected']:
                continue
            lines.append('\tEDID: ')
            lines += [f'\t\t{o["edid"][i:i + 32]}' for i in range(0, len(o['edid']), 32)]
            lines.append('\tnon-desktop: 0 ')
            lines.append('\t\trange: (0, 1)')
            current = o['geometry'] and o.get('mode')
            for size, rates, preferred in o['modes']:
                marks = []
                for i, r in enumerate(rates):
                    m = '*' if current == size and i == 0 else ' '
                    m += '+' if preferred and i == 0 else ' '
                    marks.append(f'{r:.2f}{m}')
                lines.append(f'   {size:<12} ' + '  '.join(marks))
        return '\n'.join(lines) + '\n'

    def __call__(self, cmd, **kw):
        if cmd[:2] == ['xrandr', '--query']:
            return subprocess.CompletedProcess(cmd, 0, self.text(), '')
        if cmd[0] == 'xrandr':
            self.commands.append(cmd)
            if self.fail and '--auto' not in cmd:
                return subprocess.CompletedProcess(cmd, 1, '', 'X Error: BadMatch')
            args, i = cmd[1:], 0
            if '--primary' in args:
                for o in self.outputs.values():
                    o['primary'] = False
            while i < len(args):
                if args[i] == '--output':
                    port, i = args[i + 1], i + 2
                    o = self.outputs[port]
                    opts = {}
                    while i < len(args) and args[i] != '--output':
                        if args[i] in ('--off', '--auto', '--primary'):
                            opts[args[i]] = True
                            i += 1
                        else:
                            opts[args[i]] = args[i + 1]
                            i += 2
                    if '--off' in opts:
                        o['geometry'] = None
                    else:
                        size = opts.get('--mode') or next(m for m, _, p in o['modes'] if p)
                        w, h = map(int, size.split('x'))
                        x, y = map(int, opts.get('--pos', '0x0').split('x'))
                        rot = opts.get('--rotate', 'normal')
                        if '--scale' in opts:
                            sx, sy = map(float, opts['--scale'].split('x'))
                            w, h = round(w * sx), round(h * sy)
                        if rot in ('left', 'right'):
                            w, h = h, w
                        o.update(geometry=(w, h, x, y), mode=size, rotate=rot)
                        if '--primary' in opts:
                            o['primary'] = True
                else:
                    i += 1
            return subprocess.CompletedProcess(cmd, 0, '', '')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    def active(self):
        return {n: o['geometry'] for n, o in self.outputs.items() if o['geometry']}


class TestSystem(core.System):
    def __init__(self, *a, **kw):
        self.notes, self.spawned = [], []
        self.pending, self.answer = [], True   # confirm requests; what ask_keep answers
        super().__init__(*a, spawn=lambda cmd, env=None: self.spawned.append((cmd, env)), **kw)

    def notify(self, summary, body='', urgency='normal', change=False):
        self.notes.append((summary, body, urgency, change))

    def confirm(self, layout, setup, lines):
        # the real one asks in a thread; tests run _confirm themselves once evaluate is done
        self.pending.append((f'{setup.key}|{layout.name}', setup.key, layout.name, lines))

    def ask_keep(self, summary, body, seconds):
        self.notes.append((summary, body, 'critical', False))
        return self.answer


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = store.Paths(root / 'config', root / 'state', root / 'run')
        self.lid = root / 'lid'
        self.lid.write_text('state:      open\n')
        os.environ['DISPLAYD_LID_STATE'] = str(self.lid)
        self.x = FakeX()
        self.x.plug('eDP-1', LAPTOP, LAPTOP_MODES, (1920, 1080, 0, 0), primary=True)
        self.x.outputs['eDP-1']['mode'] = '1920x1080'
        self.x.outputs['DP-1'] = {'connected': False, 'edid': '', 'modes': [], 'geometry': None,
                                  'primary': False, 'rotate': 'normal'}

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('DISPLAYD_LID_STATE', None)

    def system(self, config=''):
        self.paths.config.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(config)
        return TestSystem(self.paths, run=self.x)

    def close_lid(self):
        self.lid.write_text('state:      closed\n')


class Parse(Case):
    def test_outputs_modes_edid(self):
        self.x.plug('DP-3', DELL, DELL_MODES, (1440, 2560, 1920, 0))
        self.x.outputs['DP-3'].update(mode='2560x1440', rotate='left')
        outs = {o.name: o for o in xrandr.parse(self.x.text())}
        self.assertEqual(list(outs), ['eDP-1', 'DP-1', 'DP-3'])
        self.assertTrue(outs['eDP-1'].primary and outs['eDP-1'].connected)
        self.assertFalse(outs['DP-1'].connected)
        dp = outs['DP-3']
        self.assertEqual(dp.geometry, (1440, 2560, 1920, 0))
        self.assertEqual(dp.rotate, 'left')
        self.assertEqual(dp.edid, DELL)
        self.assertEqual(dp.mode().size, '2560x1440')
        self.assertEqual(dp.current_mode.size, '2560x1440')
        self.assertEqual(dp.mode('3840x2160').rates, [60.0])
        self.assertIsNone(dp.mode('800x600'))
        self.assertEqual(len(dp.fingerprint), 12)
        self.assertNotEqual(dp.fingerprint, outs['eDP-1'].fingerprint)
        self.assertEqual(outs['DP-1'].fingerprint, 'port:DP-1')

    def test_monitor_names(self):
        self.assertEqual(xrandr.monitor_name(DELL), 'DEL U2719D')
        self.assertEqual(xrandr.monitor_name(LG), 'GSM LG HDR 4K')
        self.assertEqual(xrandr.monitor_name(LAPTOP), 'LGD 061f')
        self.assertEqual(xrandr.monitor_name(''), '')

    def test_internal_detection(self):
        o = xrandr.Output('eDP-2', True)
        self.assertTrue(o.internal())
        self.assertFalse(o.internal(['eDP-1']))
        self.assertFalse(xrandr.Output('HDMI-1', True).internal())


class Detect(Case):
    def test_lid_closed_drops_panel_only_with_external(self):
        self.close_lid()
        setup = core.detect(xrandr.parse(self.x.text()), core.read_lid())
        self.assertEqual(list(setup.available), ['eDP-1'])
        self.x.plug('DP-3', DELL, DELL_MODES)
        setup = core.detect(xrandr.parse(self.x.text()), core.read_lid())
        self.assertEqual(list(setup.available), ['DP-3'])
        self.assertIsNone(setup.internal)
        self.assertEqual(setup.lid, 'closed')

    def test_no_lid_file_means_open(self):
        os.environ['DISPLAYD_LID_STATE'] = str(Path(self.tmp.name) / 'missing*')
        self.assertEqual(core.read_lid(), 'open')



class RuntimeDir(Case):
    """Without XDG_RUNTIME_DIR the runtime directory goes in /tmp, and must be this user's alone."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.tmp.name) / f'displayd-{os.getuid()}'

    def fallback(self):
        saved = {k: os.environ.pop(k, None) for k in ('XDG_RUNTIME_DIR', 'DISPLAYD_RUNTIME_DIR')}
        tmp, store.TMP = store.TMP, self.tmp.name
        try:
            return store.Paths(self.paths.config, self.paths.state)
        finally:
            store.TMP = tmp
            os.environ.update({k: v for k, v in saved.items() if v is not None})

    def test_created_private(self):
        paths = self.fallback()
        self.assertEqual(paths.runtime, self.dir / 'displayd')
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.fallback().runtime, paths.runtime)   # an existing private one is fine

    def test_open_or_symlinked_directory_is_refused(self):
        self.dir.mkdir(mode=0o700)
        self.dir.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, 'not a private directory.*mode 777'):
            self.fallback()
        self.dir.rmdir()
        target = Path(self.tmp.name) / 'elsewhere'
        target.mkdir(mode=0o700)
        self.dir.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.fallback()

class Layouts(Case):
    def available(self):
        self.x.plug('DP-3', DELL, DELL_MODES)
        return core.detect(xrandr.parse(self.x.text()), 'open').available

    def test_edid_follows_monitor_to_another_port(self):
        avail = self.available()
        fp = avail['DP-3'].fingerprint
        layout = lay.from_dict('t', {'outputs': {'HDMI-9': {'edid': fp, 'primary': True},
                                                 'eDP-1': {'pos': '2560x0'}}})
        targets = lay.resolve(layout, avail)
        self.assertEqual(set(targets), {'DP-3', 'eDP-1'})
        self.assertEqual(targets['DP-3'].mode, '2560x1440')

    def test_must_cover_exactly(self):
        avail = self.available()
        only = lay.from_dict('t', {'outputs': {'eDP-1': {}}})
        self.assertIsNone(lay.resolve(only, avail))
        with_off = lay.from_dict('t', {'outputs': {'eDP-1': {}, 'DP-3': {'off': True}}})
        self.assertIsNotNone(lay.resolve(with_off, avail))
        bad_mode = lay.from_dict('t', {'outputs': {'eDP-1': {}, 'DP-3': {'mode': '640x480'}}})
        self.assertIsNone(lay.resolve(bad_mode, avail))

    def test_rate_kept_only_when_offered(self):
        avail = self.available()
        layout = lay.from_dict('t', {'outputs': {'eDP-1': {'rate': 60.02}, 'DP-3': {'rate': 75}}})
        targets = lay.resolve(layout, avail)
        self.assertEqual(targets['eDP-1'].rate, 60.02)
        self.assertEqual(targets['DP-3'].rate, 0.0)

    def test_validation(self):
        for raw in ({'outputs': {}}, {'outputs': {'a': {'rotate': 'sideways'}}},
                    {'outputs': {'a': {'bogus': 1}}}, {'outputs': {'a': {}}, 'env': {'1BAD': 'x'}},
                    {'outputs': {'a': {'off': True}}}):
            with self.assertRaises(lay.LayoutError):
                lay.from_dict('t', raw)

    def test_placements(self):
        avail = self.available()
        place = {k: p for k, _, p in lay.placements('eDP-1', ['DP-3'])}
        right = lay.build('r', place['right'], avail, internal='eDP-1')
        self.assertEqual((right.outputs['eDP-1'].pos, right.outputs['DP-3'].pos), ('0x0', '1920x0'))
        self.assertTrue(right.outputs['DP-3'].primary)
        left = lay.build('l', place['left'], avail, scale=0.9, internal='eDP-1')
        self.assertEqual(left.outputs['eDP-1'].pos, '2304x0')
        self.assertEqual(left.outputs['DP-3'].scale, 0.9)
        self.assertEqual(left.outputs['eDP-1'].scale, 1.0)
        above = lay.build('a', place['above'], avail, internal='eDP-1')
        self.assertEqual((above.outputs['DP-3'].pos, above.outputs['eDP-1'].pos), ('0x0', '320x1440'))
        portrait = lay.build('p', place['left-portrait'], avail, internal='eDP-1')
        self.assertEqual(portrait.outputs['DP-3'].rotate, 'right')
        self.assertEqual(portrait.outputs['eDP-1'].pos, '1440x0')
        mirror = lay.build('m', place['mirror'], avail, internal='eDP-1')
        self.assertEqual(mirror.outputs['eDP-1'].scale_from, '2560x1440')
        ext = lay.build('e', place['external'], avail, internal='eDP-1')
        self.assertTrue(ext.outputs['eDP-1'].off)
        internal_primary = lay.build('i', place['right'], avail, internal='eDP-1', primary='internal')
        self.assertTrue(internal_primary.outputs['eDP-1'].primary)
        self.assertEqual([k for k, _, _ in lay.placements(None, ['DP-3'])], ['single'])
        self.assertEqual([k for k, _, _ in lay.placements(None, ['DP-3', 'DP-4'])], ['row', 'row-reverse'])

    def test_toml_round_trip(self):
        avail = self.available()
        place = {k: p for k, _, p in lay.placements('eDP-1', ['DP-3'])}
        layout = lay.build('Right of "laptop"', place['right'], avail, scale=0.8, internal='eDP-1')
        layout.env, layout.exec = {'GDK_SCALE': '2'}, ['echo hi']
        store.append_profile(self.paths, layout)
        loaded = store.load_config(self.paths).profiles['Right of "laptop"']
        self.assertEqual(loaded.outputs, layout.outputs)
        self.assertEqual((loaded.env, loaded.exec), (layout.env, layout.exec))
        with self.assertRaises(lay.LayoutError):
            store.append_profile(self.paths, layout)


class Commands(Case):
    def test_off_first_when_an_output_stays_on(self):
        self.x.plug('DP-3', DELL, DELL_MODES)
        outs = xrandr.parse(self.x.text())
        targets = {'DP-3': lay.OutputCfg(mode='2560x1440', primary=True)}
        cmds = xrandr.commands(targets, outs)
        self.assertEqual(len(cmds), 1)  # nothing that stays on is active: one command
        self.assertIn('--off', cmds[0])
        targets = {'eDP-1': lay.OutputCfg(mode='1920x1080')}
        cmds = xrandr.commands(targets, outs)
        self.assertEqual(cmds[0], ['xrandr', '--output', 'DP-3', '--off'])
        self.assertEqual(cmds[1][:5], ['xrandr', '--output', 'eDP-1', '--mode', '1920x1080'])
        self.assertIn('1x1', cmds[1])

    def test_mirror_uses_scale_from(self):
        outs = xrandr.parse(self.x.text())
        cmds = xrandr.commands({'eDP-1': lay.OutputCfg(mode='1920x1080', scale_from='2560x1440')}, outs)
        self.assertIn('--scale-from', cmds[-1])
        self.assertNotIn('--scale', cmds[-1])


CONFIG = '''
[general]
exec = ["echo general"]

[profiles.laptop.outputs.eDP-1]
mode = "1920x1080"
primary = true

[profiles.desk]
exec = ["echo desk"]
[profiles.desk.env]
GDK_SCALE = "1"
[profiles.desk.outputs.DP-3]
edid = "{fp}"
primary = true

[profiles.desk-big]
[profiles.desk-big.env]
QT_SCALE_FACTOR = "1.5"
[profiles.desk-big.outputs.DP-3]
edid = "{fp}"
scale = 0.8
'''


class Evaluate(Case):
    def config(self):
        fp = xrandr.Output('x', True, edid=DELL).fingerprint
        return CONFIG.replace('{fp}', fp)

    def test_new_setup_opens_menu_or_falls_back(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        opened = []
        self.assertEqual(core.evaluate(sysm, ['hotplug'], True, opened.append), 'menu')
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.x.commands, [])
        self.assertEqual(sysm.notes[0][0], 'New display setup')
        self.assertEqual(core.evaluate(sysm, ['hotplug'], False), 'fallback')
        self.assertEqual(self.x.active()['DP-3'][2:], (1920, 0))  # fallback: right of laptop
        self.assertNotIn(sysm.state['last']['setup'], sysm.state['defaults'])

    def test_lid_close_with_external_turns_panel_off_and_back(self):
        sysm = self.system(self.config())
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()
        self.assertEqual(core.evaluate(sysm, ['lid']), 'profile')
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.assertTrue(self.x.outputs['DP-3']['primary'])
        self.assertEqual(sysm.state['last']['profile'], 'desk')
        self.assertTrue(sysm.notes[-1][3])  # notification has the Change button
        self.assertEqual(core.evaluate(sysm, ['lid']), 'unchanged')
        # unplug while the lid is closed: the panel comes back after the wait for an external
        self.x.unplug('DP-3')
        now = [100.0]
        sysm.clock = lambda: now[0]
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'waiting')
        now[0] += 30
        self.assertEqual(core.evaluate(sysm, ['lid-wait']), 'profile')
        self.assertEqual(set(self.x.active()), {'eDP-1'})
        self.assertIn('Disconnected: DEL U2719D (DP-3)', sysm.notes[-1][1])

    def test_single_unknown_display_is_used_without_asking(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()
        self.assertEqual(core.evaluate(sysm, ['lid'], True, lambda s: self.fail('menu')), 'only display')
        self.assertEqual(set(self.x.active()), {'DP-3'})
        # not a config profile: the startup line names the screen and its mode
        self.assertIn('# displayd: show DEL U2719D 2560x1440\n', self.paths.env_file.read_text())

    def test_default_beats_file_order_and_env_is_written(self):
        sysm = self.system(self.config())
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()
        core.evaluate(sysm, ['lid'])
        env = self.paths.env_file.read_text()
        self.assertIn("export GDK_SCALE=1", env)
        self.assertIn('# displayd: show desk\n', env)
        sysm.state['defaults'][sysm.state['last']['setup']] = 'desk-big'
        sysm.state.save()
        self.assertEqual(core.evaluate(sysm, ['force']), 'default')
        env = self.paths.env_file.read_text()
        self.assertIn("export QT_SCALE_FACTOR=1.5", env)
        self.assertIn('unset GDK_SCALE', env)
        self.assertIn('2560x1440', ' '.join(self.x.commands[-1]))
        self.assertIn('0.8x0.8', self.x.commands[-1])

    def test_exec_and_hooks_get_profile_env(self):
        hooks = self.paths.config / 'postswitch.d'
        hooks.mkdir(parents=True)
        (hooks / '10-x').write_text('#!/bin/sh\n')
        (hooks / '10-x').chmod(0o755)
        (hooks / 'README').write_text('not executable')
        sysm = self.system(self.config())
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()
        core.evaluate(sysm, ['lid'])
        cmds = [c for c, _ in sysm.spawned]
        self.assertEqual(cmds, [['sh', '-c', 'echo desk'], ['sh', '-c', 'echo general'], [str(hooks / '10-x')]])
        env = sysm.spawned[0][1]
        self.assertEqual((env['DISPLAYD_PROFILE'], env['DISPLAYD_OUTPUTS'], env['DISPLAYD_LID'], env['GDK_SCALE']),
                         ('desk', 'DP-3', 'closed', '1'))

    def test_xrandr_failure_rescues_a_screen(self):
        sysm = self.system(self.config())
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()
        self.x.fail = True
        self.assertEqual(core.evaluate(sysm, ['lid']), 'failed')
        self.assertEqual(self.x.commands[-1], ['xrandr', '--output', 'DP-3', '--auto', '--primary'])
        self.assertEqual(sysm.notes[-1][2], 'critical')
        self.assertEqual(sysm.state['last'], {})

    def test_bad_config_is_reported_not_fatal(self):
        sysm = self.system('[profiles.x.outputs.eDP-1]\nrotate = "up"\n')
        self.assertTrue(sysm.config.errors)
        sysm = self.system('not = [toml')
        self.assertTrue(sysm.config.errors)
        self.assertEqual(core.evaluate(sysm, ['start']), 'only display')



class LidWait(Case):
    """Lid closed and the last external gone: wait general.lid_closed_wait for one before lighting the panel."""

    def docked(self, wait=None):
        fp = xrandr.Output('x', True, edid=DELL).fingerprint
        config = CONFIG.replace('{fp}', fp)
        if wait is not None:
            config = f'[general]\nlid_closed_wait = {wait}\n' + config.replace('[general]\n', '', 1)
        sysm = self.system(config)
        self.now = [1000.0]
        sysm.clock = lambda: self.now[0]
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()
        self.assertEqual(core.evaluate(sysm, ['lid']), 'profile')
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.applied = len(self.x.commands)
        return sysm

    def test_waits_then_turns_the_panel_on(self):
        sysm = self.docked()
        self.x.unplug('DP-3')
        later = []
        self.assertEqual(core.evaluate(sysm, ['hotplug'], True, None, later.append), 'waiting')
        self.assertEqual(later, [30.0])
        self.assertEqual(len(self.x.commands), self.applied)       # nothing touched X
        self.now[0] += 12
        self.assertEqual(core.evaluate(sysm, ['hotplug'], True, None, later.append), 'waiting')
        self.assertEqual(later[-1], 18.0)                         # the deadline does not restart
        self.now[0] += 18
        self.assertEqual(core.evaluate(sysm, ['lid-wait']), 'profile')
        self.assertEqual(set(self.x.active()), {'eDP-1'})

    def test_external_back_in_time_keeps_the_panel_off(self):
        sysm = self.docked()
        self.x.unplug('DP-3')
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'waiting')
        self.now[0] += 10
        self.x.outputs['DP-3']['connected'] = True
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'unchanged')
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.assertEqual(len(self.x.commands), self.applied)
        # a later drop gets a fresh wait
        self.now[0] += 100
        self.x.unplug('DP-3')
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'waiting')

    def test_opening_the_lid_force_or_zero_does_not_wait(self):
        sysm = self.docked()
        self.x.unplug('DP-3')
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'waiting')
        self.lid.write_text('state:      open\n')
        self.assertEqual(core.evaluate(sysm, ['lid']), 'profile')
        self.assertEqual(set(self.x.active()), {'eDP-1'})

        self.x.outputs['DP-3']['connected'] = True
        self.close_lid()
        core.evaluate(sysm, ['lid'])
        self.x.unplug('DP-3')
        self.assertEqual(core.evaluate(sysm, ['force']), 'profile')
        self.assertEqual(set(self.x.active()), {'eDP-1'})

        sysm = self.docked(wait=0)
        self.x.unplug('DP-3')
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'profile')

    def test_panel_already_on_does_not_wait(self):
        sysm = self.system()
        sysm.clock = lambda: 0.0
        self.close_lid()
        self.assertEqual(core.evaluate(sysm, ['start']), 'only display')



class Cli(Case):
    """CLI commands that write state reload it under the apply lock, so the daemon's writes survive."""

    def setup_desk(self):
        fp = xrandr.Output('x', True, edid=DELL).fingerprint
        sysm = self.system(CONFIG.replace('{fp}', fp))
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.close_lid()   # DP-3 alone: 'desk' fits
        return sysm

    def quiet(self, fn, *a):
        with open(os.devnull, 'w') as null, contextlib.redirect_stdout(null):
            return fn(*a)

    def daemon_writes(self):
        other = TestSystem(self.paths, run=self.x)
        other.state['confirmed'] = ['k|written-by-daemon']
        other.state.save()

    def kept(self):
        return 'k|written-by-daemon' in store.State(self.paths)['confirmed']

    def test_apply_name_keeps_what_the_daemon_wrote(self):
        sysm = self.setup_desk()
        self.daemon_writes()
        self.assertEqual(cli.cmd_apply(sysm, argparse.Namespace(name='desk', default=True)), 0)
        self.assertTrue(self.kept())
        self.assertEqual(store.State(self.paths)['last']['profile'], 'desk')

    def test_default_keeps_what_the_daemon_wrote(self):
        sysm = self.setup_desk()
        self.daemon_writes()
        self.assertEqual(self.quiet(cli.cmd_default, sysm, argparse.Namespace(name='desk')), 0)
        state = store.State(self.paths)
        self.assertTrue(self.kept())
        self.assertIn('desk', state['defaults'].values())

    def test_save_default_keeps_what_the_daemon_wrote(self):
        sysm = self.setup_desk()
        core.evaluate(sysm, ['lid'])   # something live to save
        self.daemon_writes()
        self.assertEqual(self.quiet(cli.cmd_save, sysm, argparse.Namespace(name='live', default=True)), 0)
        state = store.State(self.paths)
        self.assertTrue(self.kept())
        self.assertIn('live', state['defaults'].values())


DOCKED = '''
[general]
confirm = {confirm}

[profiles.laptop.outputs.eDP-1]
mode = "1920x1080"
primary = true

[profiles.docked.outputs.eDP-1]
off = true
[profiles.docked.outputs.DP-3]
edid = "{fp}"
primary = true
'''


class Rescue(Case):
    """A layout that turns the open laptop panel off is confirmed; no answer goes back to the panel."""

    def docked(self, confirm=15):
        fp = xrandr.Output('x', True, edid=DELL).fingerprint
        sysm = self.system(DOCKED.replace('{fp}', fp).replace('{confirm}', str(confirm)))
        self.x.plug('DP-3', DELL, DELL_MODES)
        return sysm

    def test_no_answer_goes_back_to_the_laptop_and_holds(self):
        sysm = self.docked()
        sysm.answer = False
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'profile')
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.assertEqual(len(sysm.pending), 1)
        self.assertNotIn('Display changed', [n[0] for n in sysm.notes])  # the question replaces it
        key = sysm.state['last']['setup']
        sysm._confirm(*sysm.pending[0])
        self.assertEqual(set(self.x.active()), {'eDP-1'})
        self.assertEqual(sysm.state['hold'], key)
        self.assertEqual(sysm.notes[-1][:3], ('Display layout reverted',
                                              'docked was not kept.\nRun `displayd apply` to try it again.',
                                              'critical'))
        # a flaky monitor re-sending hotplug does not switch back
        before = len(self.x.commands)
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'held')
        self.assertEqual(len(self.x.commands), before)
        # unplugging releases the hold; plugging in again asks again (never kept)
        self.x.unplug('DP-3')
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'profile')
        self.assertEqual(sysm.state['hold'], '')
        self.x.plug('DP-3', DELL, DELL_MODES)
        core.evaluate(sysm, ['hotplug'])
        self.assertEqual(len(sysm.pending), 2)

    def test_kept_layout_is_not_asked_again(self):
        sysm = self.docked()
        core.evaluate(sysm, ['hotplug'])
        sysm._confirm(*sysm.pending[0])
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.assertEqual(sysm.state['confirmed'], [sysm.pending[0][0]])
        self.x.unplug('DP-3')
        core.evaluate(sysm, ['hotplug'])
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.assertEqual(core.evaluate(sysm, ['hotplug']), 'profile')
        self.assertEqual(len(sysm.pending), 1)
        self.assertEqual(sysm.notes[-1][0], 'Display changed')

    def test_late_answer_for_a_layout_no_longer_on_screen_does_nothing(self):
        sysm = self.docked()
        core.evaluate(sysm, ['hotplug'])
        pair, key, name, _ = sysm.pending[0]
        self.x.unplug('DP-3')
        core.evaluate(sysm, ['hotplug'])
        before = len(self.x.commands)
        self.assertEqual(sysm.settle(pair, key, name, False), 'stale')
        self.assertEqual(len(self.x.commands), before)
        self.assertEqual(sysm.state['hold'], '')

    def test_not_asked_with_the_lid_closed_or_confirm_off(self):
        self.assertEqual(store.Config().confirm, 0)   # off unless the config asks for it
        sysm = self.docked(confirm=0)
        core.evaluate(sysm, ['hotplug'])
        self.assertEqual(sysm.pending, [])
        self.x.unplug('DP-3')
        sysm = self.docked()
        self.close_lid()
        # closed lid: the panel is not available, so the layout leaves nothing visible off
        core.evaluate(sysm, ['lid'])
        self.assertEqual(sysm.pending, [])

    def test_failed_notifier_keeps_without_remembering(self):
        sysm = self.docked()
        sysm.answer = None
        core.evaluate(sysm, ['hotplug'])
        sysm._confirm(*sysm.pending[0])
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.assertEqual(sysm.state['confirmed'], [])
        self.assertEqual(sysm.state['hold'], '')

    def test_real_ask_keep_and_notify_command(self):
        """The command notify-send.py gets, and how its outcome is read (the tests above stub ask_keep)."""
        sysm = self.docked()
        which, run = core.shutil.which, core.subprocess.run
        calls, outcome = [], {}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if outcome.get('timeout'):
                raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
            return subprocess.CompletedProcess(cmd, outcome.get('rc', 0), '', 'usage: ...')
        core.shutil.which = lambda name: '/bin/notify-send.py' if name == 'notify-send.py' else None
        core.subprocess.run = fake_run
        try:
            self.assertTrue(core.System.ask_keep(sysm, 'Keep?', 'body', 15))
            # --action takes any number of values: it must come after the summary and body
            self.assertEqual(calls[-1][-4:], ['Keep?', 'body', '--action', 'keep:Keep'])
            self.assertNotIn('--replaces-process', calls[-1])
            outcome['timeout'] = True
            self.assertFalse(core.System.ask_keep(sysm, 'Keep?', 'body', 15))
            outcome.update(timeout=False, rc=2)
            self.assertIsNone(core.System.ask_keep(sysm, 'Keep?', 'body', 15))
            cmd = sysm.notify_cmd('Display changed', 'b', action=('change', 'Change layout'))
            self.assertEqual(cmd[-4:], ['Display changed', 'b', '--action', 'change:Change layout'])
        finally:
            core.shutil.which, core.subprocess.run = which, run

    def test_rescue_and_force(self):
        sysm = self.docked()
        sysm.state['confirmed'] = []
        core.evaluate(sysm, ['hotplug'])
        setup = sysm.detect()
        self.assertTrue(sysm.laptop_only(setup))
        self.assertEqual(set(self.x.active()), {'eDP-1'})
        self.assertEqual(core.evaluate(sysm, ['lid']), 'held')
        # `displayd apply` (force) releases the hold and applies what the daemon would
        self.assertEqual(core.evaluate(sysm, ['force']), 'profile')
        self.assertEqual(set(self.x.active()), {'DP-3'})
        self.assertEqual(sysm.state['hold'], '')
        # nothing to go back to with the lid closed
        self.close_lid()
        self.assertFalse(sysm.laptop_only(sysm.detect()))


class FakeRofi:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts, self.selected, self.icons = [], [], []

    def pick(self, prompt, entries, mesg='', selected=0, icons=None):
        self.prompts.append((prompt, entries))
        self.selected.append(selected)
        self.icons.append(icons)
        answer = self.answers.pop(0)
        return entries.index(next(e for e in entries if e.startswith(answer))) if isinstance(answer, str) else answer

    def ask(self, prompt, mesg=''):
        return self.answers.pop(0)


class Menu(Case):
    def test_build_and_make_default(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        rofi = FakeRofi('New: Left of laptop', '3840x2160', '0.9', 'Make default')
        self.assertEqual(menu.run(sysm, rofi), 'new default')
        self.assertEqual([p for p, _ in rofi.prompts], ['display', 'mode DP-3', 'scale', 'apply'])
        name = sysm.state['last']['profile']
        self.assertEqual(name, 'Left of laptop: DEL U2719D 3840x2160 @0.9')
        self.assertEqual(sysm.state['defaults'][sysm.state['last']['setup']], name)
        self.assertIn(name, sysm.state['layouts'])
        self.assertEqual(self.x.active()['eDP-1'][2:], (3456, 0))
        # next time the setup is known: no menu, the default is applied
        self.x.outputs['DP-3']['geometry'] = None
        self.assertEqual(core.evaluate(sysm, ['hotplug'], True, lambda s: self.fail('menu')), 'default')
        rofi = FakeRofi(None)
        self.assertEqual(menu.run(sysm, rofi), 'dismissed')
        self.assertTrue(rofi.prompts[0][1][0].endswith('(default)  [saved]'))

    def test_use_once_does_not_remember(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.assertEqual(menu.run(sysm, FakeRofi('New: Mirror', '2560x1440', '1  ', 'Use once')), 'once')
        self.assertEqual(sysm.state['defaults'], {})

    def test_save_to_config(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        rofi = FakeRofi('New: Right of laptop', '2560x1440', '1  ', 'Save to config', 'office')
        self.assertEqual(menu.run(sysm, rofi), 'new default')
        self.assertIn('office', store.load_config(self.paths).profiles)
        self.assertEqual(sysm.state['layouts'], {})

    def test_dismissed_new_setup_falls_back(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.assertEqual(menu.run(sysm, FakeRofi(None), new=True), 'fallback')
        self.assertIn('DP-3', self.x.active())
        self.assertEqual(sysm.state['defaults'], {})

    def test_stale_menu_applies_nothing(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)

        class Unplugging(FakeRofi):
            def pick(inner, prompt, entries, mesg='', selected=0, icons=None):
                if prompt == 'apply':
                    self.x.unplug('DP-3')
                return FakeRofi.pick(inner, prompt, entries, mesg, selected, icons)
        result = menu.run(sysm, Unplugging('New: Right of laptop', '2560x1440', '1  ', 'Use once'))
        self.assertEqual(result, 'stale')
        self.assertEqual(self.x.commands, [])


    def test_adjust_from_the_main_menu(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        menu.run(sysm, FakeRofi('New: Right of laptop', '2560x1440', '1  ', 'Use once'))
        rofi = FakeRofi(menu.ADJUST_ENTRY, 1, '1920x1080', '1  ', 'Use once')   # output 1: the Dell
        self.assertEqual(menu.run(sysm, rofi), 'once')
        self.assertEqual([p for p, _ in rofi.prompts], ['display', 'output', 'mode DP-3', 'scale', 'apply'])
        self.assertEqual(rofi.prompts[0][1][-2], menu.ADJUST_ENTRY)   # then enable/disable displayd
        self.assertEqual(self.x.active()['DP-3'], (1920, 1080, 1920, 0))
        # not offered for a setup that has no layout chosen yet (the daemon's --new menu)
        rofi = FakeRofi(None)
        menu.run(sysm, rofi, new=True)
        self.assertNotIn(menu.ADJUST_ENTRY, rofi.prompts[0][1])

    def test_daemon_running_start_stop(self):
        sysm = self.system()
        self.assertIsNone(daemon.running(self.paths))
        self.assertFalse(daemon.stop(sysm))
        held = contextlib.ExitStack()
        # start: the "daemon" comes up by taking its lock
        sysm.spawn = lambda cmd, env=None: (sysm.spawned.append((cmd, env)),
                                            held.enter_context(store.locked(self.paths.runtime / 'daemon.lock')))
        self.assertTrue(daemon.start(sysm))
        self.assertEqual(sysm.spawned[-1][0], [str(core.DISPLAYD), 'daemon'])
        (self.paths.runtime / 'daemon.pid').write_text('4242\n')
        self.assertEqual(daemon.running(self.paths), 4242)
        self.assertFalse(daemon.start(sysm))                       # already running: nothing spawned
        self.assertEqual(len(sysm.spawned), 1)
        killed = []
        kill = daemon.os.kill
        daemon.os.kill = lambda pid, sig: (killed.append((pid, sig)), held.close())
        try:
            self.assertTrue(daemon.stop(sysm))
        finally:
            daemon.os.kill = kill
        self.assertEqual(killed, [(4242, daemon.signal.SIGTERM)])
        self.assertIsNone(daemon.running(self.paths))

    def test_udevadm_that_keeps_dying_falls_back_to_polling(self):
        sysm = self.system()
        d = daemon.Daemon(sysm)
        started, polled, clock = [], [], [0.0]

        class Dying:
            def __init__(self, cmd, **kw):
                started.append(cmd)
                self.stdout, self.returncode = iter(['UDEV  [1] change   /devices/drm/card1 (drm)\n']), None

            def wait(self):
                clock[0] += 1 if len(started) != 2 else 60   # the second one ran a minute
                self.returncode = 1

        saved = daemon.subprocess.Popen, daemon.time.sleep, daemon.time.monotonic
        daemon.subprocess.Popen, daemon.time.sleep = Dying, lambda s: None
        daemon.time.monotonic = lambda: clock[0]
        d.poll_drm = lambda: polled.append(True)
        try:
            d.watch_udev()
        finally:
            daemon.subprocess.Popen, daemon.time.sleep, daemon.time.monotonic = saved
        # quick, long (resets the count), then three quick in a row
        self.assertEqual((len(started), polled), (5, [True]))
        self.assertEqual(d.events.qsize(), 5)

    def test_reload_signals_only_a_running_daemon(self):
        sysm = self.system()
        killed = []
        kill = daemon.os.kill
        daemon.os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            # a pid file left behind by a daemon that is gone: its pid may belong to anything now
            self.paths.runtime.mkdir(parents=True, exist_ok=True)
            (self.paths.runtime / 'daemon.pid').write_text('4242\n')
            self.assertFalse(daemon.reload(sysm))
            self.assertEqual(killed, [])
            with store.locked(self.paths.runtime / 'daemon.lock'):
                self.assertTrue(daemon.reload(sysm))
                (self.paths.runtime / 'daemon.pid').write_text('garbage\n')
                self.assertFalse(daemon.reload(sysm))
        finally:
            daemon.os.kill = kill
        self.assertEqual(killed, [(4242, daemon.signal.SIGUSR1)])

    def test_menu_enables_and_disables_the_daemon(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        saved = daemon.running, daemon.start, daemon.stop
        try:
            daemon.running, daemon.stop = (lambda paths: 7), (lambda system: True)
            rofi = FakeRofi(menu.DISABLE_ENTRY)
            self.assertEqual(menu.run(sysm, rofi), 'disabled')
            self.assertNotIn(menu.ENABLE_ENTRY, rofi.prompts[0][1])
            self.assertEqual(sysm.notes[-1][0], 'displayd disabled')
            daemon.running, daemon.start = (lambda paths: None), (lambda system: False)
            rofi = FakeRofi(menu.ENABLE_ENTRY)
            self.assertEqual(menu.run(sysm, rofi), 'failed')
            self.assertEqual(sysm.notes[-1][:3][0::2], ('displayd did not start', 'critical'))
            self.assertEqual(self.x.commands, [])
            # the daemon's own menu for a new setup offers neither
            rofi = FakeRofi(None)
            menu.run(sysm, rofi, new=True)
            self.assertFalse({menu.ENABLE_ENTRY, menu.DISABLE_ENTRY} & set(rofi.prompts[0][1]))
        finally:
            daemon.running, daemon.start, daemon.stop = saved

    def test_entries_carry_icons(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        rofi = FakeRofi('New: Left of laptop', '2560x1440', '1  ', 'Use once')
        menu.run(sysm, rofi)
        for (prompt, entries), icons in zip(rofi.prompts, rofi.icons):
            self.assertEqual(len(icons), len(entries), prompt)
            self.assertTrue(all(i in menu.ICONS for i in icons), prompt)
        self.assertIn('left', rofi.icons[0])
        self.assertEqual(rofi.icons[1][0], 'default')          # preferred mode
        self.assertEqual(rofi.icons[-1], ['default', 'once', 'save'])

    def test_rofi_renders_glyphs(self):
        calls = []

        def run(cmd, input='', **kw):
            calls.append(input)
            return subprocess.CompletedProcess(cmd, 0, '1\n', '')
        self.assertEqual(menu.Rofi(run).pick('p', ['a', 'b'], icons=['laptop', 'nope']), 1)
        self.assertEqual(calls[0], f'{menu.ICONS["laptop"]}  a\n   b')
        menu.Rofi(run).pick('p', ['a', 'b'])
        self.assertEqual(calls[1], 'a\nb')


class Adjust(Case):
    def desk(self, sysm, how='Make default'):
        """Right of laptop with the Dell at 2560x1440, through the main menu."""
        self.x.plug('DP-3', DELL, DELL_MODES)
        menu.run(sysm, FakeRofi('New: Right of laptop', '2560x1440', '1  ', how))
        return sysm.state['last']['profile']

    def test_resize_moves_the_neighbour_and_makes_default(self):
        sysm = self.system()
        old = self.desk(sysm)
        rofi = FakeRofi(0, '1280x720', '1  ', 'Make default')       # output 0: the laptop
        self.assertEqual(menu.adjust(sysm, rofi), 'new default')
        self.assertEqual([p for p, _ in rofi.prompts], ['output', 'mode eDP-1', 'scale', 'apply'])
        self.assertEqual(rofi.icons[0], ['laptop', 'monitor'])
        self.assertEqual(rofi.selected[1], 0)                          # current mode selected
        self.assertEqual(self.x.active(), {'eDP-1': (1280, 720, 0, 0), 'DP-3': (2560, 1440, 1280, 0)})
        name = sysm.state['last']['profile']
        self.assertEqual(name, 'Right of laptop: LGD 061f 1280x720, DEL U2719D 2560x1440')
        # the menu-saved layout it came from is replaced, its default follows
        self.assertNotIn(old, sysm.state['layouts'])
        self.assertIn(name, sysm.state['layouts'])
        self.assertEqual(list(sysm.state['defaults'].values()), [name])
        self.x.outputs['DP-3']['geometry'] = None
        self.assertEqual(core.evaluate(sysm, ['hotplug'], True, lambda s: self.fail('menu')), 'default')
        self.assertEqual(self.x.active()['DP-3'], (2560, 1440, 1280, 0))

    def test_scale_shrinks_the_screen_and_use_once_remembers_nothing(self):
        sysm = self.system()
        old = self.desk(sysm)
        layouts = dict(sysm.state['layouts'])
        rofi = FakeRofi(1, '3840x2160', '0.75', 'Use once')
        self.assertEqual(menu.adjust(sysm, rofi), 'once')
        self.assertEqual(rofi.prompts[2][1][0], '1  (native)  (current)')
        self.assertIn('0.75x0.75', ' '.join(self.x.commands[-1]))
        self.assertEqual(self.x.active()['DP-3'], (2880, 1620, 1920, 0))
        self.assertEqual(sysm.state['layouts'], layouts)
        self.assertEqual(list(sysm.state['defaults'].values()), [old])
        self.assertEqual(sysm.state['last']['profile'], 'Right of laptop: DEL U2719D 3840x2160 @0.75')
        # adjusting again starts from what is on screen, scale included
        rofi = FakeRofi(1, '3840x2160', '0.75', None)
        self.assertEqual(menu.adjust(sysm, rofi), 'dismissed')
        self.assertEqual(rofi.selected[1:3], [1, 3])

    def test_config_profile_is_never_rewritten(self):
        cfg = ('[profiles.desk.outputs.DP-3]\nedid = "%s"\nmode = "2560x1440"\nprimary = true\n'
               '[profiles.desk.outputs.eDP-1]\noff = true\n' % xrandr.Output('x', True, LAPTOP).fingerprint)
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        dell = next(o for o in sysm.detect().outputs if o.name == 'DP-3').fingerprint
        (self.paths.config_file).write_text(cfg.replace(xrandr.Output('x', True, LAPTOP).fingerprint, dell))
        sysm.reload()
        self.assertEqual(core.evaluate(sysm, ['hotplug'], True, lambda s: self.fail('menu')), 'profile')
        rofi = FakeRofi('1920x1080', '1  ', 'Make default')               # one output on: no output step
        self.assertEqual(menu.adjust(sysm, rofi), 'new default')
        self.assertEqual(rofi.prompts[0][0], 'mode DP-3')
        self.assertIn('desk', store.load_config(self.paths).profiles)
        self.assertEqual(list(sysm.state['layouts']), ['desk: DEL U2719D 1920x1080'])
        self.assertEqual(self.x.active(), {'DP-3': (1920, 1080, 0, 0)})

    def test_stale_adjust_applies_nothing(self):
        sysm = self.system()
        self.desk(sysm)
        count = len(self.x.commands)

        class Unplugging(FakeRofi):
            def pick(inner, prompt, entries, mesg='', selected=0, icons=None):
                if prompt == 'apply':
                    self.x.unplug('DP-3')
                return FakeRofi.pick(inner, prompt, entries, mesg, selected, icons)
        self.assertEqual(menu.adjust(sysm, Unplugging(1, '1920x1080', '1  ', 'Use once')), 'stale')
        self.assertEqual(len(self.x.commands), count)

    def test_resize_carries_alignment_down_a_chain(self):
        """A screen beyond a re-aligned neighbour follows that neighbour, not just the resized one."""
        sysm = self.system()
        fhd = [('1920x1080', [60.0], True)]
        self.x.plug('DP-3', DELL, DELL_MODES)
        self.x.plug('eDP-2', edid('LGD', '', 0x0620), LAPTOP_MODES)
        avail = sysm.detect().available
        # Zenbook Duo under a monitor: DP-3 on top, eDP-1 centred below it, eDP-2 centred below eDP-1
        duo = lay.from_dict('duo', {'outputs': {
            'DP-3': {'mode': '2560x1440', 'pos': '0x0', 'primary': True},
            'eDP-1': {'mode': '1920x1080', 'pos': '320x1440'},
            'eDP-2': {'mode': '1920x1080', 'pos': '320x2520'}}})
        small = lay.resize(duo, 'DP-3', avail, '1920x1080', 1.0)
        self.assertEqual({p: c.pos for p, c in small.outputs.items()},
                         {'DP-3': '0x0', 'eDP-1': '0x1080', 'eDP-2': '0x2160'})
        back = lay.resize(small, 'DP-3', avail, '2560x1440', 1.0)
        self.assertEqual({p: c.pos for p, c in back.outputs.items()},
                         {'DP-3': '0x0', 'eDP-1': '320x1440', 'eDP-2': '320x2520'})

        # right, then down: HDMI-1 centred right of DP-3, HDMI-2 below HDMI-1 and flush with its left edge
        self.x.unplug('eDP-2')
        self.x.plug('HDMI-1', edid('GSM', 'LG A', serial=2), fhd)
        self.x.plug('HDMI-2', edid('GSM', 'LG B', serial=3), fhd)
        self.close_lid()
        avail = sysm.detect().available
        row = lay.from_dict('row', {'outputs': {
            'DP-3': {'mode': '2560x1440', 'pos': '0x0', 'primary': True},
            'HDMI-1': {'mode': '1920x1080', 'pos': '2560x180'},
            'HDMI-2': {'mode': '1920x1080', 'pos': '2560x1260'}}})
        small = lay.resize(row, 'DP-3', avail, '1920x1080', 1.0)
        self.assertEqual({p: c.pos for p, c in small.outputs.items()},
                         {'DP-3': '0x0', 'HDMI-1': '1920x0', 'HDMI-2': '1920x1080'})

    def test_resize_layouts(self):
        sysm = self.system()
        self.x.plug('DP-3', DELL, DELL_MODES)
        avail = sysm.detect().available
        place = {k: p for k, _, p in lay.placements('eDP-1', ['DP-3'])}
        above = lay.build('a', place['above'], avail, internal='eDP-1')
        self.assertEqual(above.outputs['eDP-1'].pos, '320x1440')
        smaller = lay.resize(above, 'DP-3', avail, '1920x1080', 1.0)
        # shifted past the bottom edge and re-centred under the smaller screen
        self.assertEqual((smaller.outputs['DP-3'].pos, smaller.outputs['eDP-1'].pos), ('0x0', '0x1080'))
        bigger = lay.resize(smaller, 'DP-3', avail, '2560x1440', 1.0)
        self.assertEqual((bigger.outputs['DP-3'].pos, bigger.outputs['eDP-1'].pos), ('0x0', '320x1440'))
        # resizing the laptop keeps the external centred over it
        laptop = lay.resize(above, 'eDP-1', avail, None, 2.0)
        self.assertEqual((laptop.outputs['DP-3'].pos, laptop.outputs['eDP-1'].pos), ('640x0', '0x1440'))

        def placed(pos):
            out = lay.Layout('p', {p: replace(c) for p, c in above.outputs.items()})
            out.outputs['eDP-1'].pos = pos
            return out
        # right-aligned stays right-aligned; any other offset stays where it was
        right = lay.resize(placed('640x1440'), 'DP-3', avail, '1920x1080', 1.0)
        self.assertEqual(right.outputs['eDP-1'].pos, '0x1080')
        odd = lay.resize(placed('100x1440'), 'DP-3', avail, '1920x1080', 1.0)
        self.assertEqual(odd.outputs['eDP-1'].pos, '100x1080')
        mirror = lay.build('m', place['mirror'], avail, internal='eDP-1')
        big = lay.resize(mirror, 'DP-3', avail, '3840x2160', 0.5)
        self.assertEqual(big.outputs['eDP-1'].scale_from, '1920x1080')
        left = lay.build('l', place['left'], avail, internal='eDP-1')
        moved = lay.resize(left, 'DP-3', avail, None, 0.5)
        self.assertEqual((moved.outputs['DP-3'].pos, moved.outputs['eDP-1'].pos), ('0x0', '1280x0'))
        with self.assertRaises(lay.LayoutError):
            lay.resize(lay.build('o', place['external'], avail, internal='eDP-1'), 'eDP-1', avail)


if __name__ == '__main__':
    unittest.main(verbosity=1)
