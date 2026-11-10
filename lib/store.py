"""Paths, config.toml (hand-edited, read-only here except appends), state.json, the env file and the log."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import time
import tomllib

from layout import LayoutError, from_dict, to_toml


def _dir(var, fallback):
    return Path(os.environ.get(var) or Path.home() / fallback)


TMP = '/tmp'   # parent of the runtime directory when XDG_RUNTIME_DIR is unset


def private_dir(path):
    """PATH as a directory only this user can use, created 0700 if missing.

    Shells eval the env file written below it, so a directory another user created first (in a world-writable
    parent like /tmp), a symlink, or one others can write to is refused rather than used or repaired.
    """
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(f'{path} is not a private directory of this user (owner uid {st.st_uid}, mode '
                           f'{stat.S_IMODE(st.st_mode):o}{", symlink" if stat.S_ISLNK(st.st_mode) else ""}); '
                           'remove it or set XDG_RUNTIME_DIR')
    return path


@dataclass
class Paths:
    config: Path = None
    state: Path = None
    runtime: Path = None

    def __post_init__(self):
        root = os.environ.get('DISPLAYD_CONFIG_DIR')
        self.config = self.config or (Path(root) if root else _dir('XDG_CONFIG_HOME', '.config') / 'displayd')
        root = os.environ.get('DISPLAYD_STATE_DIR')
        self.state = self.state or (Path(root) if root else _dir('XDG_STATE_HOME', '.local/state') / 'displayd')
        if self.runtime is None:
            root = os.environ.get('DISPLAYD_RUNTIME_DIR')
            xdg = os.environ.get('XDG_RUNTIME_DIR')
            if root:
                self.runtime = Path(root)
            elif xdg:
                self.runtime = Path(xdg) / 'displayd'
            else:
                self.runtime = private_dir(Path(TMP) / f'displayd-{os.getuid()}') / 'displayd'

    @property
    def config_file(self):
        return self.config / 'config.toml'

    @property
    def state_file(self):
        return self.state / 'state.json'

    @property
    def env_file(self):
        return self.runtime / 'env.sh'

    @property
    def log_file(self):
        return self.state / 'displayd.log'

    def hook_dir(self, kind):
        return self.config / f'{kind}.d'


@dataclass
class Config:
    internal: list = field(default_factory=list)
    primary: str = 'external'
    fallback: str = 'right'
    scales: list = field(default_factory=lambda: [1.0, 0.9, 0.8, 0.75, 1.25, 1.5])
    exec: list = field(default_factory=list)
    notify: bool = True
    debounce: float = 1.0
    lid_poll: float = 2.0
    confirm: float = 0.0                           # seconds to keep a layout that turns the open laptop off; 0 = never ask
    lid_closed_wait: float = 30.0                  # closed lid, last external gone: seconds before the panel; 0 = at once
    monitors: dict = field(default_factory=dict)   # fingerprint -> label
    profiles: dict = field(default_factory=dict)   # name -> Layout, file order
    errors: list = field(default_factory=list)


def load_config(paths):
    cfg = Config()
    try:
        with open(paths.config_file, 'rb') as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return cfg
    except (OSError, tomllib.TOMLDecodeError) as exc:
        cfg.errors.append(f'{paths.config_file}: {exc}')
        return cfg
    general = data.get('general') or {}
    for key in ('internal', 'primary', 'fallback', 'scales', 'exec', 'notify', 'debounce', 'lid_poll',
                'confirm', 'lid_closed_wait'):
        if key in general:
            setattr(cfg, key, general[key])
    if isinstance(cfg.exec, str):
        cfg.exec = [cfg.exec]
    if cfg.primary not in ('external', 'internal'):
        cfg.errors.append(f'general.primary must be external or internal, not {cfg.primary!r}')
        cfg.primary = 'external'
    cfg.monitors = {str(k): str(v) for k, v in (data.get('monitors') or {}).items()}
    for name, raw in (data.get('profiles') or {}).items():
        try:
            cfg.profiles[name] = from_dict(name, raw, 'config')
        except (LayoutError, TypeError, ValueError) as exc:
            cfg.errors.append(str(exc))
    return cfg


def append_profile(paths, layout):
    """Append LAYOUT as a new [profiles."NAME"] section; refuse names already in the file."""
    cfg = load_config(paths)
    if layout.name in cfg.profiles:
        raise LayoutError(f'profile {layout.name!r} already exists in {paths.config_file}')
    paths.config.mkdir(parents=True, exist_ok=True)
    existing = paths.config_file.read_text() if paths.config_file.exists() else ''
    sep = '' if not existing or existing.endswith('\n\n') else ('\n' if existing.endswith('\n') else '\n\n')
    with open(paths.config_file, 'a') as fh:
        fh.write(sep + to_toml(layout))
    if layout.name not in load_config(paths).profiles:
        raise LayoutError(f'appended profile {layout.name!r} did not load back; check {paths.config_file}')


class State:
    """defaults: setup key -> profile name; layouts: menu-made layouts; last: what was applied;
    confirmed: 'setup|layout' pairs kept when asked; hold: setup key kept on the laptop panel by a rescue."""

    def __init__(self, paths):
        self.paths = paths
        self.data = {'defaults': {}, 'layouts': {}, 'env_keys': [], 'last': {}, 'confirmed': [], 'hold': ''}
        try:
            loaded = json.loads(paths.state_file.read_text())
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + '\n')
        tmp.replace(self.paths.state_file)


def write_env(paths, profile, env, previous_keys, show=''):
    """Write the env file shells source: exports for ENV, unsets for keys earlier profiles set.

    Its comment lines name the profile and, with SHOW, a short description of
    the screens for the shell's startup info line.
    """
    paths.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [f'# displayd: profile {profile}']
    if show:
        lines.append('# displayd: show ' + ' '.join(show.split()))
    lines += [f'export {k}={shlex.quote(v)}' for k, v in sorted(env.items())]
    lines += [f'unset {k}' for k in sorted(set(previous_keys) - set(env))]
    tmp = paths.env_file.with_suffix('.tmp')
    tmp.write_text('\n'.join(lines) + '\n')
    tmp.replace(paths.env_file)
    return sorted(set(previous_keys) | set(env))


@contextmanager
def locked(path, blocking=True):
    """Hold an exclusive flock on PATH; yields False when non-blocking and it is taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Log:
    MAX = 1 << 20

    def __init__(self, paths, echo=False):
        self.path, self.echo = paths.log_file, echo

    def __call__(self, msg):
        line = f'{time.strftime("%F %T")} [{os.getpid()}] {msg}'
        if self.echo:
            print(line, flush=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.MAX:
                self.path.replace(self.path.with_suffix('.log.1'))
            with open(self.path, 'a') as fh:
                fh.write(line + '\n')
        except OSError:
            pass
