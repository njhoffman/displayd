# CLAUDE.md

Guidance for Claude Code in this repository.

## Commands

- `just test [bats options]` — the whole suite: `tests/displayd.bats` runs
  `tests/displayd_cases.py` (unittest, fake X + stub rofi) and the greeter
  tests (stub xrandr). One Python case: `python3 tests/displayd_cases.py Adjust`.
- `just lint` — shellcheck on the greeter, pyflakes on the Python.
- `just install` — link `bin/displayd` into `~/.local/bin`.

## Layout

- `bin/displayd` — CLI entry (argparse); puts `lib/` on `sys.path`.
- `lib/xrandr.py` — `xrandr --query --verbose` parsing (outputs, modes, EDID
  fingerprints), `effective_size`, and the xrandr argument lists for a switch.
- `lib/layout.py` — `Layout` / `OutputCfg`, TOML/JSON loading, matching a
  layout onto connected outputs (`resolve`), generated placements (`build`),
  `resize` for adjust.
- `lib/core.py` — `System`: detect, choose (default > first fitting profile >
  lone display > menu), apply (hooks, xrandr, env file, state), notifications,
  `evaluate`.
- `lib/daemon.py` — the event loop: `udevadm monitor`, acpid socket or lid
  polling, BOOTTIME jumps for resume, debounce, single-instance lock.
- `lib/menu.py` — rofi menus (`run`, `adjust`); `lib/store.py` — paths,
  config, state, env file, log.
- `sddm/displayd-greeter` — bash, runs as root under SDDM; must always exit 0.

## Invariants

- Standard library only (Python 3.11+, `tomllib`); no installs needed.
- A failed xrandr turns one display back on (`System.rescue`); the screen is
  never left dark. xrandr cannot see a monitor that takes the mode and shows
  nothing, so a layout that turns the open laptop panel off is confirmed
  (`System.confirm`, a thread; `settle` under the apply lock) and reverts to
  `System.laptop_only` without an answer. `state.hold` keeps a rescued setup
  on the panel until its key changes or a `force` evaluate.
- `config.toml` is only appended to (menu "Save to config.toml"); config
  profiles are never rewritten. Menu-made layouts live in the state file.
- The env file is rewritten atomically on every switch; its comment lines
  (`# displayd: profile`, `# displayd: show`) are read by shells, keep them.
- Paths come from `store.Paths` and honor `DISPLAYD_CONFIG_DIR`,
  `DISPLAYD_STATE_DIR`, `DISPLAYD_RUNTIME_DIR`, `DISPLAYD_LID_STATE` (tests
  use these; never touch the real ones in tests).

## Versioning

Semantic versioning in `VERSION`, an annotated tag `vX.Y.Z` per release,
Keep a Changelog entries in `CHANGELOG.md`, open work in `TODO.md`.
