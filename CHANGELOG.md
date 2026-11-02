# Changelog

All notable changes to displayd. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/).

## [Unreleased]

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
