#!/bin/bash
# Copyright (c) 2026 bachbnt. All rights reserved.
#
# Run alert_daemon.py in the background on macOS with launchd, and control it.
#
#   scripts/daemon.sh start      install (first time) and start; starts again at every login
#   scripts/daemon.sh stop       stop now AND keep it off at login until you run `start`
#   scripts/daemon.sh restart    restart the running daemon
#   scripts/daemon.sh status     is it running, last exit code, recent log lines
#   scripts/daemon.sh logs [-f]  show (or follow) the log
#   scripts/daemon.sh uninstall  stop it and remove the launchd agent
#   scripts/daemon.sh plist      print the launchd plist that `start` would install (changes nothing)
#
# It is a per-user LaunchAgent: no sudo, nothing outside your home folder, and you can stop it at any
# time. The agent restarts the daemon if it crashes (KeepAlive), which is why `stop` unloads it
# with `launchctl bootout`: a plain `kill` would just make launchd start it again.
#
# Secrets (TELEGRAM_BOT_TOKEN) stay in the project's .env, which alert_daemon.py reads itself;
# they are never written into the plist.
#
# IMPORTANT (macOS privacy protection): a launchd job is not allowed to read ~/Desktop, ~/Documents,
# ~/Downloads, iCloud Drive or external volumes. If the project (code + .venv) lives in one of them, Python
# hangs silently at startup. `start` therefore refuses to install from such a folder. Move the project
# elsewhere (for example ~/Developer/finhub-mcp) or use Docker instead; see the README.
#
# Optional overrides (mostly for testing): FINHUB_LAUNCHD_LABEL, FINHUB_PYTHON, FINHUB_ROOT,
# FINHUB_LAUNCH_AGENTS_DIR, FINHUB_LOG_DIR, FINHUB_ALLOW_PROTECTED=1 (skip the folder check, only if you
# granted Python Full Disk Access). FINHUB_CONFIG and FINHUB_ALERT_STORE, if set when you run `start`,
# are copied into the agent.

set -u

LABEL="${FINHUB_LAUNCHD_LABEL:-com.finhub.alertdaemon}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FINHUB_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PYTHON="${FINHUB_PYTHON:-$ROOT/.venv/bin/python}"
AGENT_DIR="${FINHUB_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${FINHUB_LOG_DIR:-$HOME/Library/Logs/finhub-mcp}"
PLIST="$AGENT_DIR/$LABEL.plist"
OUT_LOG="$LOG_DIR/daemon.out.log"
ERR_LOG="$LOG_DIR/daemon.err.log"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
esc() { printf '%s' "$1" | xml_escape; }

render_plist() {
  local extra=""
  if [ -n "${FINHUB_CONFIG:-}" ]; then
    extra="$extra
    <key>FINHUB_CONFIG</key><string>$(esc "$FINHUB_CONFIG")</string>"
  fi
  if [ -n "${FINHUB_ALERT_STORE:-}" ]; then
    extra="$extra
    <key>FINHUB_ALERT_STORE</key><string>$(esc "$FINHUB_ALERT_STORE")</string>"
  fi
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$(esc "$LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(esc "$PYTHON")</string>
    <string>$(esc "$ROOT/alert_daemon.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(esc "$ROOT")</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(esc "$OUT_LOG")</string>
  <key>StandardErrorPath</key><string>$(esc "$ERR_LOG")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>$extra
  </dict>
</dict>
</plist>
EOF
}

require_macos() {
  [ "$(uname -s)" = "Darwin" ] || die "launchd only exists on macOS. On Linux use systemd or Docker (see README)."
  command -v launchctl >/dev/null 2>&1 || die "launchctl not found"
}

is_loaded() { launchctl print "$TARGET" >/dev/null 2>&1; }

wait_unloaded() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    is_loaded || return 0
    sleep 0.5
  done
  return 1
}

unload() {
  if is_loaded; then
    launchctl bootout "$TARGET" >/dev/null 2>&1 || true
    wait_unloaded || die "launchd did not unload $LABEL in time; try again"
  fi
}

# macOS TCC-protected locations that background (launchd) jobs cannot read.
is_protected_path() {
  case "$1" in
    "$HOME/Desktop"|"$HOME/Desktop/"*) return 0 ;;
    "$HOME/Documents"|"$HOME/Documents/"*) return 0 ;;
    "$HOME/Downloads"|"$HOME/Downloads/"*) return 0 ;;
    "$HOME/Library/Mobile Documents/"*|"$HOME/Library/CloudStorage/"*) return 0 ;;
    /Volumes/*) return 0 ;;
  esac
  return 1
}

check_location() {
  [ "${FINHUB_ALLOW_PROTECTED:-}" = "1" ] && return 0
  local where="" venv_dir real_python
  venv_dir="$(dirname "$(dirname "$PYTHON")")"
  if is_protected_path "$ROOT"; then where="$ROOT"; elif is_protected_path "$venv_dir"; then where="$venv_dir"; fi
  [ -z "$where" ] && return 0
  real_python="$("$PYTHON" -c 'import os,sys;print(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))' 2>/dev/null || echo "$PYTHON")"
  {
    echo "error: the project is in a macOS-protected folder:"
    echo "         $where"
    echo "       launchd background jobs are not allowed to read Desktop, Documents, Downloads, iCloud Drive or"
    echo "       external volumes. The daemon would start but hang silently while Python opens its venv (this was"
    echo "       verified), so nothing would ever be monitored. Pick one:"
    echo
    echo "         1. Move the project somewhere unprotected, then run 'start' from there. For example:"
    echo "              mv \"$ROOT\" ~/Developer/finhub-mcp"
    echo "            and update the path in your Claude MCP config (the .venv keeps working after the move)."
    echo "         2. Run everything in Docker instead (README, \"Docker\"): no folder permissions involved."
    echo "         3. Grant Full Disk Access to the real Python binary in System Settings > Privacy & Security:"
    echo "              $real_python"
    echo "            Not recommended: every script run by that Python can then read all your files."
    echo "            After doing so, re-run with FINHUB_ALLOW_PROTECTED=1."
  } >&2
  exit 1
}

preflight() {
  [ "$(id -u)" -ne 0 ] || die "do not run this with sudo: it installs a per-user agent"
  check_location
  [ -x "$PYTHON" ] || die "Python not found at $PYTHON. Create the venv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  [ -f "$ROOT/alert_daemon.py" ] || die "alert_daemon.py not found in $ROOT"
  (cd "$ROOT" && "$PYTHON" -c "import finhub_mcp.alerts, dotenv, requests" 2>/dev/null) \
    || die "the project's dependencies are missing in $PYTHON. Run: $PYTHON -m pip install -r $ROOT/requirements.txt"
  if [ -f "$ROOT/.env" ]; then
    local mode
    mode="$(stat -f '%Lp' "$ROOT/.env" 2>/dev/null || echo 600)"
    case "$mode" in
      *00) ;;
      *) say "note: $ROOT/.env is readable by other users (mode $mode). It holds your Telegram token; consider: chmod 600 .env" ;;
    esac
  fi
}

START_WAIT=12

# The daemon prints a line as soon as it starts; no output means it is hung or crashing silently.
wait_for_output() {
  local i
  for i in $(seq 1 $((START_WAIT * 2))); do
    if [ -s "$OUT_LOG" ] || [ -s "$ERR_LOG" ]; then return 0; fi
    sleep 0.5
  done
  return 1
}

cmd_start() {
  require_macos
  preflight
  mkdir -p "$AGENT_DIR" "$LOG_DIR"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/finhub-plist.XXXXXX")" || die "cannot create a temporary file"
  render_plist > "$tmp"
  plutil -lint "$tmp" >/dev/null || { rm -f "$tmp"; die "generated plist is invalid"; }

  launchctl enable "$TARGET" >/dev/null 2>&1 || true
  if is_loaded && [ -f "$PLIST" ] && cmp -s "$tmp" "$PLIST"; then
    rm -f "$tmp"
    say "Already running. Use 'restart' to restart it, 'status' for details."
    return 0
  fi
  unload
  mv "$tmp" "$PLIST"
  chmod 644 "$PLIST"
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1 \
    || die "launchctl bootstrap failed. It needs a logged-in macOS session (not plain SSH). Plist: $PLIST"
  if wait_for_output; then
    say "Started. The alert daemon is running in the background and will start at every login."
  else
    say "Started, but the daemon has printed nothing after ${START_WAIT}s, so it may be stuck."
    say "  Check:  scripts/daemon.sh status   and   $ERR_LOG"
    say "  Common causes: a macOS-protected folder (Desktop, Documents, Downloads, iCloud) or a missing dependency."
  fi
  say "  status:  scripts/daemon.sh status"
  say "  stop:    scripts/daemon.sh stop"
  say "  logs:    $OUT_LOG"
}

cmd_stop() {
  require_macos
  if [ ! -f "$PLIST" ] && ! is_loaded; then
    say "Not installed, nothing to stop."
    return 0
  fi
  unload
  launchctl disable "$TARGET" >/dev/null 2>&1 || true
  say "Stopped. It stays off, including after a restart or login, until you run: scripts/daemon.sh start"
}

cmd_restart() {
  require_macos
  is_loaded || die "not running. Use: scripts/daemon.sh start"
  launchctl kickstart -k "$TARGET" >/dev/null 2>&1 || die "restart failed"
  say "Restarted."
}

field() { launchctl print "$TARGET" 2>/dev/null | sed -n "s/^[[:space:]]*$1 = //p" | head -1; }

cmd_status() {
  require_macos
  if [ ! -f "$PLIST" ]; then
    say "Not installed. Run: scripts/daemon.sh start"
    return 3
  fi
  local disabled=no
  launchctl print-disabled "$DOMAIN" 2>/dev/null | grep -Eq "\"$LABEL\" => (disabled|true)" && disabled=yes
  local code=3
  if is_loaded; then
    local state pid last runs
    state="$(field state)"; pid="$(field pid)"; last="$(field 'last exit code')"; runs="$(field runs)"
    say "Installed: yes ($PLIST)"
    say "State:     ${state:-unknown}${pid:+ (pid $pid)}"
    say "Runs:      ${runs:-?}   last exit code: ${last:-?}"
    [ "${state:-}" = "running" ] && code=0
  else
    say "Installed: yes ($PLIST)"
    say "State:     stopped$([ "$disabled" = yes ] && echo ' (disabled: stays off at login)')"
  fi
  say "Logs:      $LOG_DIR"
  if [ -s "$OUT_LOG" ]; then say; say "--- last output ---"; tail -n 8 "$OUT_LOG"; fi
  if [ -s "$ERR_LOG" ]; then say; say "--- last errors ---"; tail -n 5 "$ERR_LOG"; fi
  return $code
}

cmd_logs() {
  mkdir -p "$LOG_DIR"
  touch "$OUT_LOG" "$ERR_LOG"
  if [ "${1:-}" = "-f" ]; then
    exec tail -n 20 -f "$OUT_LOG" "$ERR_LOG"
  fi
  say "--- $OUT_LOG ---"; tail -n 40 "$OUT_LOG"
  say; say "--- $ERR_LOG ---"; tail -n 20 "$ERR_LOG"
}

cmd_uninstall() {
  require_macos
  unload
  launchctl enable "$TARGET" >/dev/null 2>&1 || true   # clear the 'disabled' override left by `stop`
  if [ -f "$PLIST" ]; then rm -f "$PLIST"; say "Removed $PLIST"; else say "No agent file to remove."; fi
  say "Uninstalled. Logs were kept in $LOG_DIR (delete the folder if you do not need them)."
}

usage() { sed -n '4,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  start|install) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "${1:-}" ;;
  uninstall) cmd_uninstall ;;
  plist) render_plist ;;
  -h|--help|help|"") usage ;;
  *) usage; exit 2 ;;
esac
