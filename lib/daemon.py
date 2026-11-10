"""The session daemon: hotplug (udev), lid (acpid, else polling /proc), resume (clock jump) -> evaluate."""
import glob
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import threading
import time

import core
import store

ACPID_SOCKETS = ('/run/acpid.socket', '/var/run/acpid.socket')
DRM_STATUS = '/sys/class/drm/card*-*/status'


class Daemon:
    def __init__(self, system):
        self.system = system
        self.log = system.log
        self.events = queue.Queue()
        self.menu = None
        self.menu_setup = None
        self.udev = None

    # -- event sources --------------------------------------------------
    def watch_udev(self):
        """drm change events; polls connector status files when udevadm is unusable."""
        while True:
            try:
                proc = self.udev = subprocess.Popen(['udevadm', 'monitor', '--udev', '--subsystem-match=drm'],
                                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as exc:
                self.log(f'udevadm unavailable ({exc}); polling {DRM_STATUS}')
                self.poll_drm()
                return
            self.log('hotplug: udevadm monitor')
            for line in proc.stdout:
                if line.startswith('UDEV') and ' change ' in f' {line} ':
                    self.events.put('hotplug')
            proc.wait()
            self.log(f'udevadm monitor exited ({proc.returncode}); restarting in 5s')
            time.sleep(5)

    def poll_drm(self):
        def snapshot():
            out = {}
            for path in glob.glob(DRM_STATUS):
                try:
                    out[path] = Path(path).read_text().strip()
                except OSError:
                    pass
            return out
        last = snapshot()
        while True:
            time.sleep(2)
            now = snapshot()
            if now != last:
                last = now
                self.events.put('hotplug')

    def watch_lid(self):
        """acpid's event socket; polls the lid state file meanwhile when acpid is missing or drops."""
        while True:
            sock = self.acpid()
            if sock:
                self.log('lid: acpid socket')
                with sock, sock.makefile('r', errors='replace') as stream:
                    for line in stream:
                        if line.startswith('button/lid'):
                            self.events.put('lid')
                self.log('acpid socket closed; polling the lid state')
            else:
                self.log(f'lid: acpid not available; polling every {self.system.config.lid_poll:g}s')
            self.poll_lid(60)

    def acpid(self):
        for path in ACPID_SOCKETS:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(path)
                return sock
            except OSError:
                sock.close()
        return None

    def poll_lid(self, duration):
        last, end = core.read_lid(), time.monotonic() + duration
        while time.monotonic() < end:
            time.sleep(max(0.5, float(self.system.config.lid_poll)))
            now = core.read_lid()
            if now != last:
                last = now
                self.events.put('lid')

    def watch_resume(self):
        """Suspend shows as BOOTTIME pulling ahead of MONOTONIC."""
        def gap():
            return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        last = gap()
        while True:
            time.sleep(5)
            now = gap()
            if now - last > 3:
                self.events.put('resume')
            last = now

    def exit(self):
        """SIGTERM: stop udevadm too (it would linger until the next drm event), then exit."""
        self.log('stopped (SIGTERM)')
        if self.udev and self.udev.poll() is None:
            self.udev.terminate()
        os._exit(0)

    # -- loop -----------------------------------------------------------
    def open_menu(self, setup):
        if self.menu and self.menu.poll() is None:
            self.log('menu already open')
            return
        self.menu_setup = setup.key
        self.menu = self.system.spawn([str(core.DISPLAYD), 'menu', '--new'])

    def later(self, seconds):
        """Evaluate again in SECONDS (a closed lid waiting for an external to come back)."""
        timer = threading.Timer(seconds + 0.1, self.events.put, ('lid-wait',))
        timer.daemon = True
        timer.start()

    def run(self):
        signal.signal(signal.SIGUSR1, lambda *_: self.events.put('force'))
        signal.signal(signal.SIGTERM, lambda *_: self.exit())
        for target in (self.watch_udev, self.watch_lid, self.watch_resume):
            threading.Thread(target=target, daemon=True).start()
        self.events.put('start')
        debounce = max(0.2, float(self.system.config.debounce))
        while True:
            reasons = {self.events.get()}
            while True:
                try:
                    reasons.add(self.events.get(timeout=debounce))
                except queue.Empty:
                    break
            if self.menu and self.menu.poll() is None and 'force' not in reasons:
                # the open menu applies (or falls back) itself; check again once it is gone
                if reasons != {'recheck'}:
                    self.log(f'menu open; deferring {sorted(reasons)}')
                threading.Timer(2, self.events.put, ('recheck',)).start()
                continue
            try:
                result = core.evaluate(self.system, sorted(reasons), True, self.open_menu, self.later)
                self.log(f'{sorted(reasons)} -> {result}')
            except Exception as exc:  # keep the daemon alive through xrandr hiccups
                self.log(f'evaluate failed: {exc!r}')


def running(paths):
    """The daemon's pid while it runs (0 when its pid file is unreadable), else None."""
    with store.locked(paths.runtime / 'daemon.lock', blocking=False) as got:
        if got:
            return None
    try:
        return int((paths.runtime / 'daemon.pid').read_text())
    except (OSError, ValueError):
        return 0


def _wait(paths, up, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if (running(paths) is not None) == up:
            return True
        time.sleep(0.1)
    return False


def start(system):
    """Start the daemon in its own session. False when it was already running or did not come up."""
    if running(system.paths) is not None:
        return False
    system.spawn([str(core.DISPLAYD), 'daemon'])
    return _wait(system.paths, True)


def stop(system):
    """SIGTERM the daemon. False when it was not running or did not stop."""
    pid = running(system.paths)
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        system.log(f'stop: {exc}')
        return False
    return _wait(system.paths, False)


def reload(system):
    """SIGUSR1 the running daemon (re-apply now). False when it is not running: the pid file outlives the
    daemon, and SIGUSR1 kills a process that does not handle it, so only a pid behind the held lock is signalled."""
    pid = running(system.paths)
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as exc:
        system.log(f'reload: {exc}')
        return False
    return True


def main(system):
    with store.locked(system.paths.runtime / 'daemon.lock', blocking=False) as got:
        if not got:
            print('displayd: daemon already running')
            return 0
        (system.paths.runtime / 'daemon.pid').write_text(f'{os.getpid()}\n')
        Daemon(system).run()
    return 0
