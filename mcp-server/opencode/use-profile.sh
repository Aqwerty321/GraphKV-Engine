#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OPENCODE_DIR="${OPENCODE_DIR:-$HOME/.config/opencode}"
PROFILE_ROOT="$SCRIPT_DIR/profiles"

usage() {
  cat <<'USAGE'
Usage:
  use-profile.sh binary-star
  use-profile.sh classic
  use-profile.sh restore /path/to/backup
  use-profile.sh status
USAGE
}

backup_current() {
  local backup_dir="$OPENCODE_DIR/backups/graphkv-profile-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup_dir"
  [[ -f "$OPENCODE_DIR/opencode.json" ]] && cp -a "$OPENCODE_DIR/opencode.json" "$backup_dir/opencode.json"
  [[ -f "$OPENCODE_DIR/AGENTS.md" ]] && cp -a "$OPENCODE_DIR/AGENTS.md" "$backup_dir/AGENTS.md"
  [[ -d "$OPENCODE_DIR/commands" ]] && cp -a "$OPENCODE_DIR/commands" "$backup_dir/commands"
  printf '%s\n' "$backup_dir"
}

install_profile() {
  local profile="$1"
  local source_dir="$PROFILE_ROOT/$profile"
  if [[ ! -d "$source_dir" ]]; then
    printf 'unknown profile: %s\n' "$profile" >&2
    exit 2
  fi
  local backup_dir
  backup_dir="$(backup_current)"
  mkdir -p "$OPENCODE_DIR"
  cp -a "$source_dir/opencode.json" "$OPENCODE_DIR/opencode.json"
  cp -a "$source_dir/AGENTS.md" "$OPENCODE_DIR/AGENTS.md"
  rm -rf "$OPENCODE_DIR/commands"
  cp -a "$source_dir/commands" "$OPENCODE_DIR/commands"
  printf 'installed profile: %s\nbackup: %s\nrepo: %s\n' "$profile" "$backup_dir" "$REPO_ROOT"
}

restore_backup() {
  local backup_dir="$1"
  if [[ ! -d "$backup_dir" ]]; then
    printf 'backup not found: %s\n' "$backup_dir" >&2
    exit 2
  fi
  local new_backup
  new_backup="$(backup_current)"
  [[ -f "$backup_dir/opencode.json" ]] && cp -a "$backup_dir/opencode.json" "$OPENCODE_DIR/opencode.json"
  [[ -f "$backup_dir/AGENTS.md" ]] && cp -a "$backup_dir/AGENTS.md" "$OPENCODE_DIR/AGENTS.md"
  if [[ -d "$backup_dir/commands" ]]; then
    rm -rf "$OPENCODE_DIR/commands"
    cp -a "$backup_dir/commands" "$OPENCODE_DIR/commands"
  fi
  printf 'restored backup: %s\npre-restore backup: %s\n' "$backup_dir" "$new_backup"
}

show_status() {
  printf 'opencode dir: %s\n' "$OPENCODE_DIR"
  if grep -q 'graphkv-binary-star' "$OPENCODE_DIR/opencode.json" 2>/dev/null; then
    printf 'active profile: binary-star\n'
  else
    printf 'active profile: classic-or-custom\n'
  fi
  find "$OPENCODE_DIR/backups" -maxdepth 1 -type d -name 'graphkv-*' 2>/dev/null | sort | tail -5
}

case "${1:-}" in
  binary-star|classic)
    install_profile "$1"
    ;;
  restore)
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    restore_backup "$2"
    ;;
  status)
    show_status
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac