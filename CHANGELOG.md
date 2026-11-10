# Changelog

All notable changes to displayd. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/).

## [Unreleased]

## [0.7.0] - 2026-11-10

### Changed

- `displayd adjust` keeps alignment along a chain of screens: a screen beyond a neighbour that was re-aligned
  (e.g. the lower Zenbook Duo panel under the upper one, under a monitor) now follows that neighbour instead of
  staying where it was. A screen touching two others lines up with the one it is aligned to (start, centre or
  end) rather than whichever was placed first.

## [0.6.5] - 2026-11-10

### Fixed

- Without `XDG_RUNTIME_DIR`, the runtime directory (`/tmp/displayd-$UID`, which holds the `env.sh` shells source)
  was used even when another user had created it first, or it was a symlink or open to others. It is now created
  0700, and displayd refuses to run with an error naming the directory when an existing one is not private to
  you. Sessions started by logind always set `XDG_RUNTIME_DIR` and are not affected.

## [0.6.4] - 2026-11-10

### Fixed

- A `udevadm monitor` that starts but dies at once (no netlink access, a broken install) was restarted every 5s
  forever, and no hotplug was ever seen. After three exits within 5s of starting in a row, the daemon polls
  `/sys/class/drm/*/status` instead, as it already did when `udevadm` is missing.

## [0.6.3] - 2026-11-10

### Fixed

- `displayd apply NAME`, `displayd default NAME` and `displayd save NAME --default` could undo what the daemon
  had just saved (a kept layout, a rescue hold, a default): they wrote back the state they read when they
  started. They now re-read it under the apply lock before writing.

## [0.6.2] - 2026-11-10

### Added

- CI: GitHub Actions runs `just lint` and `just test` on every push and pull request, on Python 3.11 (the
  minimum) and 3.13.

## [0.6.1] - 2026-11-10

### Fixed

- `displayd reload` sent SIGUSR1 to whatever pid the daemon's pid file named, even after the daemon had stopped;
  a process that reused that pid was killed. It now signals only a running daemon (the one holding its lock).

## [0.6.0] - 2026-11-10

### Changed

- With the lid closed, losing the last external monitor no longer turns the panel on at once: displayd waits
  `general.lid_closed_wait` seconds (default 30) for a monitor to come back, so a hub or dock that drops its
  link for a moment leaves the layout (and the windows on it) alone. Opening the lid or `displayd apply` does not
  wait; `lid_closed_wait = 0` restores the old behavior.

## [0.5.1] - 2026-11-02

### Added

- MIT license (`LICENSE`).

## [0.5.0] - 2026-11-02

### Changed

- The keep-or-revert question is off by default (`general.confirm = 0`): answering it takes a mouse click, and
  a missed one reverted a working monitor. Set `confirm = 15` to turn it on; `displayd rescue` on a key covers
  a dark screen without it.

## [0.4.0] - 2026-11-02

### Added

- `displayd stop` / `displayd start`, and the main menu's last entry: **Disable displayd** while the daemon
  runs, **Enable displayd** while it does not. Stopping lasts until it is started again or the next login (the
  window manager starts the daemon). `displayd status` shows whether the daemon runs.

### Fixed

- A stopped daemon (SIGTERM) left its `udevadm monitor` running until the next drm event; it is stopped too now.

## [0.3.1] - 2026-11-02

### Fixed

- With `notify-send.py`, the keep-or-revert question never waited: `--action` takes any number of values, so it
  swallowed the summary and body, the command failed at once, and the failure counted as **Keep** (and was
  remembered). The action now goes after the text. A notifier that fails now keeps the layout without
  remembering it as kept; only the Keep button or closing the notification by hand counts as an answer.
- The **Change layout** button on switch notifications never appeared with `notify-send.py`, for the same reason.

## [0.3.0] - 2026-11-02

### Added

- The main menu's last entry, **Adjust a monitor: resolution and scale**, opens `displayd adjust`, so one key
  reaches both. Left out of the menu the daemon opens for a new set of monitors (nothing to adjust yet).

### Changed

- README: the i3 example binds the menu to `$mod+Escape` and drops the separate adjust key.

## [0.2.0] - 2026-11-02

### Added

- `displayd rescue`: the laptop panel alone, held there until the set of monitors changes or `displayd apply`
  runs, so a monitor or dock that keeps re-sending hotplug events cannot switch back to a dark screen. Meant for a
  key binding (the README binds it to `$mod+Shift+Escape`, next to `adjust` on `$mod+Escape`). `displayd status` shows the hold.
- Confirm-or-revert: when the daemon turns the laptop panel off with the lid open, a **Keep this display
  layout?** notification asks once per set of monitors and layout; with no answer in `general.confirm` seconds
  (default 15, 0 turns it off) it goes back to the laptop panel. Closing the notification counts as keeping it.

## [0.1.0] - 2026-11-01

First release on its own, extracted from njhoffman/.shell (`scripts/tools/displayd/`), where it
shipped in 0.33.1 through 0.39.0.

### Added

- `displayd daemon`: watches monitor hotplug (udev), the lid (acpid, else polling) and resume; applies the
  default for a known set of monitors (matched by EDID), else the first fitting profile, else a lone display,
  else opens the rofi menu. A closed lid with an external monitor turns the laptop panel off; opening it or
  unplugging the monitor turns it back on. Every switch shows a notification with a Change layout button.
- `displayd menu`: placements (right of / left of / above the laptop, external only, laptop only, mirror,
  portrait), resolution and scale; use once, make default, or save to config.toml. Nerd Font icons.
- `displayd adjust`: resolution and scale of one monitor in the layout on screen, once or as the default; the
  others move so the screens still touch and keep their alignment along the shared edge (centred, start, end).
- `status`, `monitors`, `list`, `apply`, `default`, `save`, `reload`.
- Profiles with `env` (written to `$XDG_RUNTIME_DIR/displayd/env.sh`, with `# displayd: profile` and
  `# displayd: show` comment lines for shells) and `exec`; `preswitch.d` / `postswitch.d` hooks.
- `sddm/displayd-greeter`: SDDM DisplayCommand that puts the login form on the external monitor when the lid
  is closed; always exits 0.
- `just test`, `just lint`, `just install`, `just install-greeter`.

### Changed

- Notifications use `notify-send.py` from PATH when it is there, else `notify-send`.
