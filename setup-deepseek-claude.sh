#!/usr/bin/env bash
set -euo pipefail

printf '========================================\n'
printf '  话本RP — macOS 环境配置\n'
printf '========================================\n\n'

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '[错误] 此脚本仅支持 macOS。\n' >&2
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  printf '[错误] 未检测到 Homebrew。请先安装 Homebrew 后重试：\n'
  printf '  https://brew.sh/\n'
  exit 1
fi

ensure_brew_package() {
  local package="$1"
  local command_name="$2"
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '[OK] %s 已安装：%s\n' "$package" "$(command -v "$command_name")"
    return
  fi

  printf '[安装] 正在安装 %s...\n' "$package"
  brew install "$package"
}

ensure_brew_package node node
ensure_brew_package git git

if command -v claude >/dev/null 2>&1; then
  printf '[OK] Claude Code 已安装：%s\n' "$(command -v claude)"
else
  printf '[安装] 正在安装 Claude Code...\n'
  npm install -g @anthropic-ai/claude-code
fi

printf '\n请输入 DeepSeek API Key: '
read -r deepseek_key
if [[ -z "$deepseek_key" ]]; then
  printf '[错误] API Key 不能为空。\n' >&2
  exit 1
fi

shell_name="$(basename "${SHELL:-zsh}")"
case "$shell_name" in
  zsh) profile="$HOME/.zshrc" ;;
  bash) profile="$HOME/.bash_profile" ;;
  *) profile="$HOME/.zshrc" ;;
esac

touch "$profile"

start_marker="# >>> AIRP_ClaudeCode DeepSeek config >>>"
end_marker="# <<< AIRP_ClaudeCode DeepSeek config <<<"
tmp_file="$(mktemp)"

python3 - "$profile" "$tmp_file" "$start_marker" "$end_marker" <<'PY'
import sys
from pathlib import Path

profile = Path(sys.argv[1])
tmp = Path(sys.argv[2])
start = sys.argv[3]
end = sys.argv[4]
text = profile.read_text(encoding="utf-8") if profile.exists() else ""
while start in text and end in text:
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    text = before.rstrip() + "\n" + after.lstrip()
tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
PY

cat "$tmp_file" > "$profile"
rm -f "$tmp_file"

cat >> "$profile" <<EOF
$start_marker
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_AUTH_TOKEN="$deepseek_key"
export ANTHROPIC_MODEL="deepseek-v4-pro[1m]"
export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro[1m]"
export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro[1m]"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"
export CLAUDE_CODE_EFFORT_LEVEL="max"
$end_marker
EOF

printf '\n========================================\n'
printf '  配置完成\n'
printf '========================================\n'
printf '已写入：%s\n' "$profile"
printf '请执行：source "%s"\n' "$profile"
printf '然后在卡片文件夹中运行 claude，再输入 /rp。\n'
