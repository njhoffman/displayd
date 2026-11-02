# displayd

Display layouts for X without running xrandr by hand. `displayd daemon`
(started by the window manager) watches monitor hotplug (udev), the laptop lid
(acpid; polling `/proc/acpi/button/lid` when acpid is missing) and resume from
suspend:

- A set of monitors seen before gets its default layout, else the first
  fitting profile in `~/.config/displayd/config.toml`. Monitors are matched by
  EDID, so a layout follows a monitor to any port.
- A new set opens a rofi menu: a saved profile, or a placement (right of /
  left of / above the laptop, external only, laptop only, mirror, portrait),
  then resolution and scale, then **Make default**, **Use once** or **Save to
  config.toml as...**. Dismissing it applies `general.fallback` without
  remembering it.
- Closing the lid with a monitor connected turns the laptop panel off;
  opening it, or unplugging the monitor, turns it back on.
- Every switch shows a notification with a **Change layout** button.
- A layout that turns the laptop panel off while the lid is open asks
  **Keep this display layout?** the first time it is used for a set of
  monitors. With no answer within `general.confirm` seconds (15; 0 = never
  ask) the external screen is presumably dark, and displayd goes back to the
  laptop panel.
- `displayd rescue` (bind it to a key, it works blind) turns the laptop panel
  on alone and holds it there: hotplug events from a flaky monitor or dock do
  not switch away until the set of monitors changes or `displayd apply` runs.
- The main menu also stops the daemon (**Disable displayd**) while it runs, or
  starts it (**Enable displayd**) while it does not; stopped, nothing follows
  hotplug or the lid until it is enabled again or the next login.
- `displayd adjust` (also a main menu entry, **Adjust a monitor**)
  changes the resolution and scale of one monitor in the layout on screen, once or as the default; the other monitors move so the
  screens still touch and keep their alignment (centred, start or end).

```sh
displayd status                  # connected setup, applied, default, fitting profiles
displayd monitors                # connected monitors with fingerprints and names
displayd list                    # profiles (* default, + fits what is connected)
displayd default NAME            # make NAME the default for what is connected
displayd apply [NAME]            # apply NAME, or what the daemon would choose
displayd save NAME --default     # keep the live layout (e.g. tuned in arandr)
displayd menu | adjust           # the rofi menus
displayd rescue                  # laptop panel only, held until the monitors change
displayd stop | start            # stop the daemon (until start or the next login) / start it again
```

Requires Python 3.11+ (standard library only), xrandr, udevadm and rofi;
acpid for lid events; `notify-send` (or `notify-send.py`) for notifications.

## Install

```sh
just install           # links bin/displayd into ~/.local/bin (or $XDG_BIN_HOME)
cp share/config.example.toml ~/.config/displayd/config.toml   # annotated template
```

i3:

```
exec --no-startup-id ~/.local/bin/displayd daemon
bindsym $mod+Escape --release exec --no-startup-id ~/.local/bin/displayd menu
bindsym $mod+Shift+Escape --release exec --no-startup-id ~/.local/bin/displayd rescue
```

Config `~/.config/displayd/config.toml`, state and log in
`~/.local/state/displayd/` (defaults, menu-made layouts, `displayd.log`).
Executables in `~/.config/displayd/preswitch.d/` and `postswitch.d/` run on
every switch with `DISPLAYD_PROFILE`, `DISPLAYD_OUTPUTS`, `DISPLAYD_PRIMARY`,
`DISPLAYD_LID` and friends; a profile's `exec` commands run after it.

## Shell integration

A profile's `env` table, and a summary of the screens, are written to
`$XDG_RUNTIME_DIR/displayd/env.sh` on every switch:

```sh
# displayd: profile desk
# displayd: show desk          # a config profile's name, else e.g. "laptop 1920x1080 + DEL U2719D 2560x1440"
export GDK_SCALE=1
unset QT_SCALE_FACTOR           # set by an earlier profile, not by this one
```

Source it before each prompt to pick up switches in open shells, e.g. zsh:

```zsh
_displayd_env() {
  local f=${XDG_RUNTIME_DIR:-/tmp/displayd-$UID}/displayd/env.sh c=
  [[ -r $f ]] && c=$(<$f)
  [[ $c == "$_displayd_env_last" ]] && return
  typeset -g _displayd_env_last=$c
  eval "$c"
}
autoload -Uz add-zsh-hook && add-zsh-hook precmd _displayd_env
```

## Login screen (SDDM)

`sddm/displayd-greeter` is a DisplayCommand for SDDM: it runs the stock
Xsetup, then, with the lid closed and a monitor connected, turns the panel off
so the login form is on the monitor. It always exits 0 and turns the panel back
on unless the monitor is confirmed active, so a failure leaves the default
layout instead of blocking login. `just install-greeter` installs it as
`/usr/local/libexec/displayd-greeter`; point SDDM at it with a drop-in:

```ini
# /etc/sddm.conf.d/displayd.conf
[X11]
DisplayCommand=/usr/local/libexec/displayd-greeter
```

Its log is `/var/log/displayd-greeter.log`.

## Tests

`just test`: the Python cases (`tests/displayd_cases.py`) against a fake X
and stub rofi, and the greeter against a stub xrandr (`tests/displayd.bats`).
