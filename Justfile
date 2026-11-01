default:
    @just --list

# run the test suite (bats; the Python cases run inside it)
test *ARGS:
    bats {{ ARGS }} tests

# lint the greeter script and the Python modules
lint:
    shellcheck sddm/displayd-greeter
    pyflakes bin/displayd lib/*.py tests/*.py

# symlink bin/displayd into ~/.local/bin (or $XDG_BIN_HOME); refuses to replace anything that is not a symlink
install:
    #!/usr/bin/env bash
    set -euo pipefail
    src="$(pwd -P)/bin/displayd"
    dest="${XDG_BIN_HOME:-$HOME/.local/bin}/displayd"
    mkdir -p "${dest%/*}"
    if [[ -e "$dest" && ! -L "$dest" ]]; then
      echo "install: $dest exists and is not a symlink; remove it first" >&2
      exit 1
    fi
    ln -sfn "$src" "$dest"
    echo "$dest -> $src"

# install sddm/displayd-greeter as /usr/local/libexec/displayd-greeter (sudo)
install-greeter:
    sudo install -D -m 0755 -o root -g root sddm/displayd-greeter /usr/local/libexec/displayd-greeter
