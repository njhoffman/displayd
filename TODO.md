# displayd task list

Open work for displayd. Every item carries a priority, `(p1)` high, `(p2)`
normal or `(p3)` low, and a category: `[bug]`, `[feature]`, `[refactor]`,
`[docs]`, `[test]`, `[chore]` or `[perf]`.

Reviewed: 2026-11-01

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

## 2. Packaging and docs

- [ ] **Add a LICENSE.** (p2) [chore] The repo is meant to be public; pick a
  license before publishing.
- [ ] **Screenshots of the menus in the README.** (p3) [docs] The rofi
  placement and adjust menus.
- [ ] **CI.** (p3) [test] Run `just test` and `just lint` on push (GitHub
  Actions: bats, shellcheck, pyflakes; the fake X needs nothing else).
