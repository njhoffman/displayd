"""rofi menus. run: pick a saved profile or build one from a placement, mode and scale, or go to adjust;
adjust: change the resolution and scale of one output in the layout on screen. Both end in use once or make
default."""
import html
import subprocess

import daemon
import layout as lay
import store

# Nerd Font glyphs from the Font Awesome range, which Nerd Fonts v2 and v3 both carry
# (v3 moved the Material Design icons); rofi's Pango falls back to the Symbols font.
ICONS = {
    'laptop': '\uf109', 'monitor': '\uf108', 'right': '\uf061', 'left': '\uf060', 'up': '\uf062',
    'mirror': '\uf24d', 'portrait': '\uf10b', 'columns': '\uf0db', 'off': '\uf011',
    'default': '\uf005', 'saved': '\uf0c7', 'config': '\uf1de', 'mode': '\uf065',
    'larger': '\uf00e', 'smaller': '\uf010', 'native': '\uf00c', 'once': '\uf00c', 'save': '\uf0c7',
    'start': '\uf04b',
}
PRESET_ICONS = {
    'right': 'right', 'left': 'left', 'above': 'up', 'external': 'monitor', 'laptop': 'laptop',
    'mirror': 'mirror', 'left-portrait': 'portrait', 'right-portrait': 'portrait', 'single': 'monitor',
    'row': 'columns', 'row-reverse': 'columns',
}


ADJUST_ENTRY = 'Adjust a monitor: resolution and scale'
DISABLE_ENTRY = 'Disable displayd (stop the daemon until enabled or the next login)'
ENABLE_ENTRY = 'Enable displayd (start the daemon)'


class Rofi:
    def __init__(self, run=subprocess.run):
        self.run = run

    def pick(self, prompt, entries, mesg='', selected=0, icons=None):
        """Index of the chosen entry, or None when dismissed. ICONS: a key of ICONS per entry."""
        cmd = ['rofi', '-dmenu', '-i', '-no-custom', '-format', 'i', '-p', prompt,
               '-selected-row', str(selected)]
        if mesg:
            cmd += ['-mesg', html.escape(mesg)]
        if icons:
            entries = [f'{ICONS.get(i, " ")}  {e}' for i, e in zip(icons, entries)]
        proc = self.run(cmd, input='\n'.join(entries), capture_output=True, text=True)
        out = proc.stdout.strip()
        return int(out) if proc.returncode == 0 and out.isdigit() and int(out) < len(entries) else None

    def ask(self, prompt, mesg=''):
        """Free text, or None when dismissed or empty."""
        cmd = ['rofi', '-dmenu', '-p', prompt, '-theme-str', 'listview { lines: 0; }']
        if mesg:
            cmd += ['-mesg', html.escape(mesg)]
        proc = self.run(cmd, input='', capture_output=True, text=True)
        out = proc.stdout.strip()
        return out if proc.returncode == 0 and out else None


def _mode_entries(out):
    entries = []
    for mode in out.modes:
        tags = [t for t, on in (('preferred', mode.preferred), ('current', mode.current_rate is not None)) if on]
        rates = ', '.join(f'{r:.2f}' for r in dict.fromkeys(mode.rates))
        entries.append(f'{mode.size:<11} {rates}' + (f'  ({", ".join(tags)})' if tags else ''))
    return entries


def _mode_icons(out):
    return ['default' if m.preferred else 'mode' for m in out.modes]


def _scale_entries(scales, current=None):
    labels = [f'{s:g}' + ('  (native)' if s == 1 else '  (larger UI)' if s < 1 else '  (smaller UI)')
              + ('  (current)' if current is not None and abs(s - current) < 0.001 else '') for s in scales]
    icons = ['native' if s == 1 else 'larger' if s < 1 else 'smaller' for s in scales]
    return labels, icons


def _pick_action(rofi, title, extra=()):
    """0 make default, 1 use once, 2.. EXTRA; None when dismissed."""
    actions = ['Make default', 'Use once', *extra]
    icons = ['default', 'once', *('save' for _ in extra)]
    return rofi.pick('apply', actions, title, 0, icons)


def run(system, rofi=None, new=False):
    """The interactive flow. NEW: dismissing it applies the fallback layout (a setup nobody chose for)."""
    rofi = rofi or Rofi()
    system.reload()
    setup = system.detect()

    def dismissed():
        if not new:
            return 'dismissed'
        with store.locked(system.paths.runtime / 'apply.lock'):
            current = system.detect()
            layout, targets = system.fallback(current)
            ok = system.apply(layout, targets, current, 'fallback')
        if ok:
            system.notify('Display layout', f'Layout: {layout.name} (fallback, not remembered)', change=True)
        return 'fallback'

    if not setup.available:
        return 'none'
    mesg = system.describe(setup)
    default = system.state['defaults'].get(setup.key)
    matches = system.matching(setup)
    presets = system.presets(setup)
    entries = [f'{lay_.name}' + ('  (default)' if lay_.name == default else '') +
               ('' if lay_.source == 'config' else '  [saved]') for lay_, _ in matches]
    entries += [f'New: {label}' for _, label, _ in presets]
    icons = ['default' if lay_.name == default else 'config' if lay_.source == 'config' else 'saved'
             for lay_, _ in matches]
    icons += [PRESET_ICONS.get(key, 'monitor') for key, _, _ in presets]
    # adjust works on the layout on screen; a setup nobody chose a layout for yet has none worth adjusting
    can_adjust = not new and bool(setup.active)
    if can_adjust:
        entries.append(ADJUST_ENTRY)
        icons.append('mode')
    # the daemon's own --new menu leaves it alone
    is_running = None if new else daemon.running(system.paths) is not None
    if is_running is not None:
        entries.append(DISABLE_ENTRY if is_running else ENABLE_ENTRY)
        icons.append('off' if is_running else 'start')
    choice = rofi.pick('display', entries, mesg, 0, icons)
    if choice is None:
        return dismissed()
    if entries[choice] == ADJUST_ENTRY:
        return adjust(system, rofi)
    if entries[choice] in (DISABLE_ENTRY, ENABLE_ENTRY):
        return toggle_daemon(system, is_running)

    if choice < len(matches):
        layout, _ = matches[choice]
        made = False
    else:
        key, label, placement = presets[choice - len(matches)]
        modes = {}
        enabled = [p for p in setup.available if p not in placement.get('off', [])]
        for port in enabled:
            out = setup.available[port]
            if port == setup.internal or len(out.modes) < 2:
                continue
            entries = _mode_entries(out)
            preferred = next((i for i, m in enumerate(out.modes) if m.preferred), 0)
            pick = rofi.pick(f'mode {port}', entries, f'{system.label(out)}: resolution', preferred,
                             _mode_icons(out))
            if pick is None:
                return dismissed()
            modes[port] = out.modes[pick].size
        scale = 1.0
        if any(p != setup.internal for p in enabled) and system.config.scales:
            scales = [float(s) for s in system.config.scales]
            labels, icons = _scale_entries(scales)
            pick = rofi.pick('scale', labels, 'Scale of the external display(s)',
                             scales.index(1.0) if 1.0 in scales else 0, icons)
            if pick is None:
                return dismissed()
            scale = scales[pick]
        names = ', '.join(system.label(setup.available[p]) for p in enabled if p != setup.internal)
        extra = ''.join(f' {modes[p]}' for p in modes) + (f' @{scale:g}' if scale != 1 else '')
        name = f'{label}: {names}{extra}' if names else label
        layout, _ = system.preset_layout(setup, key, modes, scale, name)
        made = True

    action = _pick_action(rofi, layout.name, ['Save to config.toml as...'] if made else [])
    if action is None:
        return dismissed()
    if action == 2:
        name = rofi.ask('profile name', f'Saved to {system.paths.config_file}; add env/exec there')
        if name is None:
            return dismissed()
        layout.name = name
        try:
            store.append_profile(system.paths, layout)
        except lay.LayoutError as exc:
            system.notify('displayd', str(exc), urgency='critical')
            return 'error'

    with store.locked(system.paths.runtime / 'apply.lock'):
        system.reload()
        current = system.detect()
        targets = lay.resolve(layout, current.available)
        if targets is None or current.key != setup.key:
            system.notify('displayd', 'Displays changed while the menu was open; nothing applied',
                          urgency='critical')
            return 'stale'
        if action in (0, 2):
            if layout.source in ('preset', 'auto') and action == 0:
                layout.source = 'saved'
                system.state['layouts'][layout.name] = lay.to_dict(layout)
            system.state['defaults'][current.key] = layout.name
        why = 'once' if action == 1 else 'new default'
        ok = system.apply(layout, targets, current, why)
    if ok:
        system.notify('Display layout', f'Layout: {layout.name} ({why})')
    return why if ok else 'failed'


def toggle_daemon(system, is_running):
    """Stop the running daemon, or start a stopped one; a notification says which."""
    if is_running:
        ok = daemon.stop(system)
        system.notify('displayd disabled' if ok else 'displayd did not stop',
                      'Monitors and the lid are not watched until enabled again (menu or `displayd start`) '
                      'or the next login' if ok else 'See `displayd status`', urgency='normal' if ok else 'critical')
        return 'disabled' if ok else 'failed'
    ok = daemon.start(system)
    system.notify('displayd enabled' if ok else 'displayd did not start',
                  'Watching monitors and the lid' if ok else f'See {system.paths.log_file}',
                  urgency='normal' if ok else 'critical')
    return 'enabled' if ok else 'failed'


def _adjusted_name(system, base, setup, layout):
    """'<placement or profile>: <monitor> <mode> @<scale>, ...' for the outputs that are not plain laptop."""
    prefix = base if base in system.config.profiles else (base or 'Custom').split(': ', 1)[0]
    parts = []
    for port, cfg in layout.outputs.items():
        out = setup.available[port]
        if cfg.off or cfg.scale_from:
            continue
        if port == setup.internal and cfg.mode == out.mode().size and cfg.scale == 1:
            continue
        parts.append(f'{system.label(out)} {cfg.mode}' + (f' @{cfg.scale:g}' if cfg.scale != 1 else ''))
    name = f'{prefix}: {", ".join(parts)}' if parts else prefix
    return f'{name} (menu)' if name in system.config.profiles and name != base else name


def adjust(system, rofi=None):
    """Change one output's resolution and scale in the layout on screen; then use once or make default.

    Make default stores a menu layout and points this setup at it; when the layout on
    screen came from a menu-saved layout, that one is replaced (config profiles are
    never rewritten). Use once applies without remembering anything.
    """
    rofi = rofi or Rofi()
    system.reload()
    setup = system.detect()
    last = system.state['last']
    base = last.get('profile') if last.get('setup') == setup.key else None
    try:
        current = system.live_layout(setup, base or 'Custom', 'live')
    except lay.LayoutError:
        return 'none'
    known = system.profiles().get(base) if base else None
    if known:
        current.env, current.exec = dict(known.env), list(known.exec)
    mesg = system.describe(setup) + (f'\nLayout: {base}' if base else '')

    enabled = current.enabled
    port = enabled[0]
    if len(enabled) > 1:
        entries = [f'{system.label(setup.available[p])} ({p})  {current.outputs[p].mode} '
                   f'@{current.outputs[p].scale:g}' for p in enabled]
        icons = ['laptop' if p == setup.internal else 'monitor' for p in enabled]
        pick = rofi.pick('output', entries, mesg, 0, icons)
        if pick is None:
            return 'dismissed'
        port = enabled[pick]
    out, cfg = setup.available[port], current.outputs[port]
    label = system.label(out)

    selected = next((i for i, m in enumerate(out.modes) if m.size == cfg.mode), 0)
    pick = rofi.pick(f'mode {port}', _mode_entries(out), f'{label}: resolution', selected, _mode_icons(out))
    if pick is None:
        return 'dismissed'
    mode = out.modes[pick].size

    scales = [float(s) for s in system.config.scales] or [1.0]
    if not any(abs(s - cfg.scale) < 0.001 for s in scales):
        scales.append(cfg.scale)
    labels, icons = _scale_entries(scales, cfg.scale)
    selected = next(i for i, s in enumerate(scales) if abs(s - cfg.scale) < 0.001)
    pick = rofi.pick('scale', labels, f'{label}: scale', selected, icons)
    if pick is None:
        return 'dismissed'

    try:
        layout = lay.resize(current, port, setup.available, mode, scales[pick])
    except lay.LayoutError as exc:
        system.notify('displayd', str(exc), urgency='critical')
        return 'error'
    layout.name = _adjusted_name(system, base, setup, layout)
    action = _pick_action(rofi, layout.name)
    if action is None:
        return 'dismissed'

    with store.locked(system.paths.runtime / 'apply.lock'):
        system.reload()
        now = system.detect()
        targets = lay.resolve(layout, now.available)
        if targets is None or now.key != setup.key:
            system.notify('displayd', 'Displays changed while the menu was open; nothing applied',
                          urgency='critical')
            return 'stale'
        if action == 0:
            layouts, defaults = system.state['layouts'], system.state['defaults']
            if base in layouts and base not in system.config.profiles:
                del layouts[base]
                for key, name in list(defaults.items()):
                    if name == base:
                        defaults[key] = layout.name
            layout.source = 'saved'
            layouts[layout.name] = lay.to_dict(layout)
            defaults[now.key] = layout.name
        why = 'new default' if action == 0 else 'once'
        ok = system.apply(layout, targets, now, why)
    if ok:
        system.notify('Display layout', f'Layout: {layout.name} ({why})')
    return why if ok else 'failed'
