#!/bin/bash
# Qoder CN 每日 100 Credits · macOS launchd 卸载脚本
# 用法：bash uninstall-qoder-macos.sh
#
# 评审意见 #24：注销失败时绝不能删除配置并以成功退出——删掉磁盘上的 plist
# 并不会注销已加载的任务，那会留下一个「看不见但还在跑」的定时器。
# 因此：注销失败 → 保留配置并返回非零；删除后二次确认服务确实不在。
set -u

LABEL="qoder-daily-credits"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/QoderDailyCredits"

is_loaded() {
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
}

echo "=== 卸载 $LABEL ==="

if is_loaded; then
  if ! launchctl bootout "gui/$(id -u)/$LABEL"; then
    echo "  ✗ 注销失败，任务仍在运行"
    echo "    已保留配置：$PLIST"
    echo "    请手动执行：launchctl bootout gui/\$(id -u)/$LABEL"
    exit 1
  fi
  if is_loaded; then
    echo "  ✗ 注销后任务仍在加载，已保留配置：$PLIST"
    exit 1
  fi
  echo "  ✓ 已从 launchd 注销"
else
  echo "  （任务未在运行，跳过注销）"
fi

if [ -f "$PLIST" ]; then
  if ! rm -f "$PLIST"; then
    echo "  ✗ 删除 $PLIST 失败"
    exit 1
  fi
  echo "  ✓ 已删除 $PLIST"
else
  echo "  （plist 不存在，跳过）"
fi

if [ -f "$PLIST.bak" ]; then
  rm -f "$PLIST.bak" && echo "  ✓ 已删除备份"
fi

echo
echo "日志保留在 ${LOGDIR}（如需一并删除：rm -rf \"$LOGDIR\"）"
echo "仓库文件未动，以后想恢复：bash install-qoder-macos.sh"
