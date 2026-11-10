# displayd task list

Open work for displayd. Every item carries a priority, `(p1)` high, `(p2)`
normal or `(p3)` low, and a category: `[bug]`, `[feature]`, `[refactor]`,
`[docs]`, `[test]`, `[chore]` or `[perf]`.

Reviewed: 2026-11-10

## 1. Features

- [ ] **Keyboard-docked input for the Zenbook Duo.** (p2) [feature] Turn the
  lower screen (eDP-2) off when the detachable keyboard sits on it: watch udev
  usb add/remove of the keyboard and drop eDP-2 the way a closed lid drops the
  panel. Today placements treat only the first internal panel as "the laptop";
  `general.internal` should name both panels, and profiles cover laptop only,
  both screens, and both plus an external monitor.
- [ ] **Re-centre outputs past the resized one.** (p3) [feature]
  `layout.resize` keeps a neighbour's alignment only for outputs touching the
  resized one; a third monitor beyond a re-aligned neighbour does not follow
  it. Rare with two screens; matters for the Duo plus an external.

- [ ] **Confirm layouts chosen in the menu too.** (p3) [feature] The
  keep-or-revert question covers only the daemon's automatic switches;
  `menu` and `adjust` apply what was picked without asking. A mode the
  monitor (or a dock's DP-to-HDMI converter) accepts but cannot show leaves
  the screen dark the same way. Route their apply through
  `System.needs_confirm` / `confirm`, minus the once-per-setup memory.
- [ ] **Already-kept layouts are never re-asked.** (p3) [feature] A monitor
  that worked before and then fails (a bad cable, a dock that lost its link)
  goes dark without the question; only `displayd rescue` helps. Idea: ask
  again when the kernel logs a link-training failure or the output flaps
  (several hotplugs for the same setup within a minute).
- [x] **A closed lid gets the panel when the last external goes.** (p2) [bug]
  `detect()` (`lib/core.py`) drops lid-closed panels only while an external is
  connected, so losing the only external lights the closed panel. On
  2026-11-10 a USB-C hub dropped DP-3 at 14:44:34 with the lid shut: displayd
  applied `charon` (eDP-1 only) and the session moved to a screen nobody could
  see until another monitor was plugged in a minute later. Keeping X with no
  output is not an option, but with the lid closed displayd could leave the
  layout alone for a grace period (say 30s) waiting for an external to return,
  and only then turn the panel on; a `lid_closed_fallback` setting
  (`panel` | `wait`) would keep today's behavior available. Done in 0.6.0 as
  `general.lid_closed_wait` (seconds, 0 = old behavior).
- [x] **`displayd reload` can signal a stranger.** (p2) [bug] `cmd_reload`
  (`bin/displayd`) reads `daemon.pid` and sends SIGUSR1 without checking the
  daemon runs; the pid file is never removed on exit, so after `displayd stop`
  or a crash a reused pid gets SIGUSR1, whose default action kills it. Use
  `daemon.running(paths)` (the lock check) like `start`/`stop` do, and test it.
- [ ] **`default`, `save` and `apply NAME` write stale state.** (p3) [bug]
  `cmd_default` / `cmd_save --default` save `state.json` without
  `apply.lock`; a daemon `evaluate` that reloaded state just before and saves
  just after drops the new default. `cmd_apply NAME` takes the lock but never
  `reload()`s, so it saves the state read when the command started, losing
  anything the daemon wrote meanwhile (`confirmed`, `hold`, labels). Take the
  lock and `reload()` first in all three, as `cmd_rescue` does.
- [ ] **A udevadm that keeps failing means no hotplug at all.** (p3) [bug]
  `Daemon.watch_udev` falls back to polling `/sys/class/drm` only when
  `udevadm` cannot be started; one that starts and exits at once (no netlink
  access, a broken install) is restarted every 5s forever and no hotplug is
  ever seen. Fall back to `poll_drm` after a few quick exits in a row.
- [ ] **The /tmp runtime fallback trusts whoever made the directory.** (p3)
  [bug] Without `XDG_RUNTIME_DIR`, `store.Paths` uses
  `/tmp/displayd-$UID/displayd`; `mkdir(exist_ok=True, mode=0o700)` neither
  sets the mode nor checks the owner of an existing directory, and shells
  `eval` the `env.sh` written there. Another local user who creates it first
  gets code run in your shells. Check owner and mode (or refuse) before
  writing; logind sessions always set `XDG_RUNTIME_DIR`, so this is rare.

## 2. Packaging and docs

- [x] **Add a LICENSE.** (p2) [chore] MIT, 2026-11-02.
- [ ] **Screenshots of the menus in the README.** (p3) [docs] The rofi
  placement and adjust menus.
- [x] **CI.** (p3) [test] Run `just test` and `just lint` on push (GitHub
  Actions: bats, shellcheck, pyflakes; the fake X needs nothing else).
- [ ] **CLAUDE.md names the wrong xrandr query.** (p3) [docs] It says
  `lib/xrandr.py` parses `xrandr --query --verbose`; the code runs
  `xrandr --query --prop`.
