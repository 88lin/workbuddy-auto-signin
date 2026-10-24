#!/bin/bash
# Qoder CN 每日 100 Credits · macOS launchd 卸载脚本
# 用法：bash uninstall-qoder-macos.sh
set -u

LABEL="qoder-daily-credits"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/QoderDailyCredits"

echo "=== 卸载 $LABEL ==="
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" && echo "  ✓ 已从 launchd 注销"
else
  echo "  （任务未在运行，跳过注销）"
fi

if [ -f "$PLIST" ]; then
  rm -f "$PLIST" && echo "  ✓ 已删除 $PLIST"
else
  echo "  （plist 不存在，跳过）"
fi

[ -f "$PLIST.bak" ] && rm -f "$PLIST.bak" && echo "  ✓ 已删除备份"

echo
echo "日志保留在 ${LOGDIR}（如需一并删除：rm -rf \"$LOGDIR\"）"
echo "仓库文件未动，以后想恢复：bash install-qoder-macos.sh"
