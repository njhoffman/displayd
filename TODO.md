# displayd task list

Open work for displayd. Every item carries a priority, `(p1)` high, `(p2)`
normal or `(p3)` low, and a category: `[bug]`, `[feature]`, `[refactor]`,
`[docs]`, `[test]`, `[chore]` or `[perf]`.

Reviewed: 2026-11-02

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

## 2. Packaging and docs

- [ ] **Add a LICENSE.** (p2) [chore] The repo is meant to be public; pick a
  license before publishing.
- [ ] **Screenshots of the menus in the README.** (p3) [docs] The rofi
  placement and adjust menus.
- [ ] **CI.** (p3) [test] Run `just test` and `just lint` on push (GitHub
  Actions: bats, shellcheck, pyflakes; the fake X needs nothing else).
