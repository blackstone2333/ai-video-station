#!/usr/bin/env bash

set -euo pipefail

package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$package_dir/.." && pwd)"
fnpack_bin="${FNPACK_BIN:-fnpack}"
output_dir="${1:-$repo_dir/dist/fnos}"

command -v "$fnpack_bin" >/dev/null 2>&1 || {
  printf '找不到 fnpack。请先从 https://developer.fnnas.com/docs/cli/fnpack/ 安装官方工具。\n' >&2
  exit 1
}

mkdir -p "$output_dir"
work_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT

(
  cd "$work_dir"
  "$fnpack_bin" build --directory "$package_dir"
)

install -m 0644 \
  "$work_dir/ai-video-station.fpk" \
  "$output_dir/ai-video-station-2.2.0.fpk"
printf '已生成 %s\n' "$output_dir/ai-video-station-2.2.0.fpk"
