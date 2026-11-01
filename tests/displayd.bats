#!/usr/bin/env bats
# displayd: Python cases against a fake X, and the SDDM greeter script against
# a stub xrandr.

GREETER="$BATS_TEST_DIRNAME/../sddm/displayd-greeter"

setup() {
  T=$(mktemp -d)
  mkdir -p "$T/bin"
  # stub xrandr: --query prints $T/query, --listactivemonitors $T/active, else logs args
  cat >"$T/bin/xrandr" <<EOF
#!/bin/bash
case \$1 in
  --query) cat "$T/query" ;;
  --listactivemonitors) cat "$T/active" 2>/dev/null ;;
  *) echo "\$*" >>"$T/calls" ;;
esac
EOF
  chmod +x "$T/bin/xrandr"
  printf 'state:      closed\n' >"$T/lid"
  printf 'Screen 0: minimum 8 x 8\neDP-1 connected primary 1920x1080+0+0 (normal)\nDP-3 connected 2560x1440+1920+0 (normal)\nDP-1 disconnected (normal)\n' >"$T/query"
  printf 'Monitors: 1\n 0: +*DP-3 2560/698x1440/393+0+0  DP-3\n' >"$T/active"
  printf '#!/bin/sh\necho stock >>"%s/calls"\n' "$T" >"$T/stock"
  chmod +x "$T/stock"
  export DISPLAYD_LID_STATE="$T/lid" DISPLAYD_GREETER_STOCK="$T/stock" DISPLAYD_GREETER_LOG="$T/log"
}

teardown() { rm -rf "$T"; }

greeter() { PATH="$T/bin:$PATH" bash "$GREETER"; }

@test "displayd: python cases" {
  run python3 "$BATS_TEST_DIRNAME/displayd_cases.py"
  [ "$status" -eq 0 ] || { printf '%s\n' "$output"; false; }
}

@test "displayd: CLI help" {
  run "$BATS_TEST_DIRNAME/../bin/displayd" --help
  [ "$status" -eq 0 ]
  [[ $output == *"apply"* && $output == *"menu"* ]]
}

@test "greeter: lid closed + external -> panel off, external primary, stock Xsetup first" {
  run greeter
  [ "$status" -eq 0 ]
  [ "$(sed -n 1p "$T/calls")" = stock ]
  [ "$(sed -n 2p "$T/calls")" = "--output DP-3 --auto --pos 0x0 --primary --output eDP-1 --off" ]
  [ "$(wc -l <"$T/calls")" -eq 2 ]
}

@test "greeter: panel comes back when no external output ends up active" {
  printf 'Monitors: 0\n' >"$T/active"
  run greeter
  [ "$status" -eq 0 ]
  [ "$(tail -1 "$T/calls")" = "--output eDP-1 --auto --primary" ]
}

@test "greeter: lid open or no external leaves the layout alone" {
  printf 'state:      open\n' >"$T/lid"
  run greeter
  [ "$(cat "$T/calls")" = stock ]
  printf 'state:      closed\n' >"$T/lid"
  printf 'eDP-1 connected primary 1920x1080+0+0 (normal)\nDP-3 disconnected (normal)\n' >"$T/query"
  run greeter
  [ "$(cat "$T/calls")" = $'stock\nstock' ]
}

@test "greeter: always exits 0, even when xrandr and Xsetup fail" {
  printf '#!/bin/sh\nexit 3\n' >"$T/bin/xrandr"
  printf '#!/bin/sh\nexit 4\n' >"$T/stock"
  run greeter
  [ "$status" -eq 0 ]
  grep -q 'exited 4' "$T/log"
}
