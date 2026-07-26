#!/usr/bin/env bash
set -euo pipefail

printf '========================================\n'
printf '  话本RP — 项目更新\n'
printf '========================================\n\n'

cd "$(dirname "$0")"

if [[ ! -d .git ]]; then
  printf '[错误] 当前目录不是 git 仓库，无法更新。\n' >&2
  exit 1
fi

printf '[配置] 启用自动暂存 (autostash)...\n'
git config pull.rebase true
git config rebase.autoStash true

printf '[检查] 当前分支: '
git rev-parse --abbrev-ref HEAD
printf '\n'

if [[ -n "$(git status --porcelain)" ]]; then
  printf '[信息] 检测到本地修改:\n'
  git status --porcelain
else
  printf '[信息] 工作区干净，无本地修改。\n'
fi

printf '\n[更新] 正在拉取最新代码...\n'
if git pull --rebase --autostash; then
  printf '\n========================================\n'
  printf '  更新完成！\n'
  printf '========================================\n'
else
  printf '\n[错误] 拉取失败，请按以下步骤检查并恢复：\n' >&2
  printf '  git status\n' >&2
  printf '  git stash list\n' >&2
  printf '  git rebase --continue   # 解决冲突后继续\n' >&2
  printf '  git rebase --abort      # 放弃本次 rebase\n' >&2
  exit 1
fi
