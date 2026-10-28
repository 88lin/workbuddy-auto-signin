#!/bin/bash
# Qoder CN 每日 100 Credits · macOS launchd 安装脚本
#
# 用法：bash install-qoder-macos.sh
# 卸载：bash uninstall-qoder-macos.sh
#
# 只创建一个任务，不碰 WorkBuddy 的两个任务：
#   每天 10:05 主领取（Credits 按 UTC+8 每天 10:00 刷新，没有补领）
#   11:05 / 15:05 / 19:05 / 23:05 四次兜底（睡眠/关机错过 10:05 时补上）
#   登录时立即跑一次
#
# 与 Windows 版 install-qoder-windows.ps1 的排程一致；领取接口幂等，
# 已领过只会查询，不会重复领。
#
# 失败处理原则（评审意见 #24）：
#   · plist 用 plistlib 生成，路径里的 & < > 不会破坏 XML
#   · 先生成并校验临时 plist，全部通过后才替换配置、注销旧任务
#   · 备份失败立即退出，保留原任务；bootstrap 失败自动回滚
#   · 自检与定时任务使用同一套环境变量（QODER_EXE / QODER_AUTH_DIR）
set -u

LABEL="qoder-daily-credits"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BACKUP="$PLIST.bak"
LOGDIR="$HOME/Library/Logs/QoderDailyCredits"
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$DIR/qoder_signin.py"

TMPPLIST=""
ERRTMP=""
cleanup() {
  [ -n "$TMPPLIST" ] && rm -f "$TMPPLIST"
  [ -n "$ERRTMP" ] && rm -f "$ERRTMP"
}
trap cleanup EXIT

# launchctl print 才是权威查询；`launchctl list | grep` 在部分环境会漏判。
is_loaded() {
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
}
die() { echo "✗ $*" >&2; exit 1; }

# ── Python 解释器 ────────────────────────────────────────────
# 别用 /usr/bin/python3：没装 Xcode Command Line Tools 时它只是占位壳，
# 命令行里跑会弹安装框，launchd 里跑则直接失败且错误只进 .err。
# 优先 Homebrew 的固定路径（比 PATH 里偶尔出现的临时解释器稳定），
# 再由 PATH 兜底；QODER_PYTHON 可强制指定。
PYTHON="${QODER_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
              "$(command -v python3 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ] && [ "$cand" != "/usr/bin/python3" ]; then
      PYTHON="$cand"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  if [ -x /usr/bin/python3 ]; then
    PYTHON=/usr/bin/python3
    echo "⚠ 只找到 /usr/bin/python3（可能是 Xcode 占位壳）。"
    echo "  若任务失败，请装 Homebrew Python 后重跑，或用 QODER_PYTHON 指定真实路径。"
  else
    echo "✗ 找不到 Python 3，请先安装（brew install python）。"
    exit 1
  fi
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
  echo "✗ $PYTHON 不是 Python 3.8+，请换一个解释器（或用 QODER_PYTHON 指定）。"
  exit 1
fi
if [ ! -f "$SCRIPT" ]; then
  echo "✗ 找不到 $SCRIPT（请在仓库目录里运行本脚本）"
  exit 1
fi

# 自检沿用调用者的工作目录，launchd 则在仓库目录运行。
# 提前展开 ~ 与相对路径，让两边使用完全相同的绝对路径。
absolute_path() {
  "$PYTHON" -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}
if [ -n "${QODER_EXE:-}" ]; then
  QODER_EXE="$(absolute_path "$QODER_EXE")" || die "无法解析 QODER_EXE"
  export QODER_EXE
fi
if [ -n "${QODER_AUTH_DIR:-}" ]; then
  QODER_AUTH_DIR="$(absolute_path "$QODER_AUTH_DIR")" || die "无法解析 QODER_AUTH_DIR"
  export QODER_AUTH_DIR
fi

# ── 登录态自检：不联网、不领取 ────────────────────────────────
# 注意：这里用到的 QODER_EXE / QODER_AUTH_DIR 必须一并写进 plist，
# 否则会出现「自检通过、安装成功，launchd 里却报 NO_AUTH」——
# launchd 不继承安装时终端的环境变量。
# QODER_TOKEN 不会保存到 plist，自检必须排除它，实际验证桌面登录态。
echo "[0/3] 登录态自检（不联网、不领取）"
if DOCTOR="$(QODER_TOKEN= "$PYTHON" "$SCRIPT" doctor 2>&1)"; then
  echo "  ✓ $DOCTOR"
else
  echo "  ✗ 自检失败：$DOCTOR"
  echo "    请先打开 Qoder CN 桌面端确认已登录，然后重试。"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR" || die "无法创建 $HOME/Library/LaunchAgents 或 $LOGDIR"
TMPPLIST="$(mktemp "${TMPDIR:-/tmp}/$LABEL.plist.XXXXXX")" || die "无法创建临时文件"
ERRTMP="$(mktemp "${TMPDIR:-/tmp}/$LABEL.boot.XXXXXX")" || true

# ── 生成 plist（plistlib，自动转义 XML）──────────────────────
# 直接拼 XML 时，路径里出现 & < > 会让 plist 无法解析；
# 用标准库生成可彻底避免这类问题。
echo "[1/3] 生成 launchd 配置"
if ! PL_LABEL="$LABEL" PL_PYTHON="$PYTHON" PL_SCRIPT="$SCRIPT" PL_DIR="$DIR" \
     PL_LOGDIR="$LOGDIR" PL_EXE="${QODER_EXE:-}" PL_AUTHDIR="${QODER_AUTH_DIR:-}" \
     "$PYTHON" - "$TMPPLIST" <<'PYEOF'
import os
import plistlib
import sys

schedule = (10, 11, 15, 19, 23)

# 只透传「非敏感」的配置；访问令牌一律不写入 plist。
env = {"PYTHONIOENCODING": "utf-8"}
if os.environ.get("PL_EXE"):
    env["QODER_EXE"] = os.environ["PL_EXE"]
if os.environ.get("PL_AUTHDIR"):
    env["QODER_AUTH_DIR"] = os.environ["PL_AUTHDIR"]

plist = {
    "Label": os.environ["PL_LABEL"],
    "ProgramArguments": [os.environ["PL_PYTHON"], os.environ["PL_SCRIPT"], "auto"],
    "WorkingDirectory": os.environ["PL_DIR"],
    "StartCalendarInterval": [{"Hour": hour, "Minute": 5} for hour in schedule],
    "RunAtLoad": True,
    "ProcessType": "Background",
    "EnvironmentVariables": env,
    "StandardOutPath": os.path.join(os.environ["PL_LOGDIR"], "out.log"),
    "StandardErrorPath": os.path.join(os.environ["PL_LOGDIR"], "err.log"),
}

with open(sys.argv[1], "wb") as handle:
    plistlib.dump(plist, handle)
PYEOF
then
  die "生成 plist 失败"
fi

# 用真实解析器校验，避免写出「语法看起来对但 launchd 读不了」的文件
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TMPPLIST" >/dev/null 2>&1 || die "生成的 plist 无法解析，已中止（未改动现有任务）"
fi
echo "  ✓ 配置已生成并校验"

# ── 备份 ─────────────────────────────────────────────────────
# 顺序必须是「先备份，再替换/注销」。备份失败说明后续没有任何退路，
# 此时必须直接退出并保留原任务，绝不能继续覆盖配置。
if [ -f "$PLIST" ]; then
  if ! cp "$PLIST" "$BACKUP"; then
    die "备份现有配置失败（$BACKUP）。已中止，原任务未被改动"
  fi
  echo "  ✓ 已备份现有配置 → $BACKUP"
fi

# 校验通过、备份就绪，才真正替换配置
if ! mv -f "$TMPPLIST" "$PLIST"; then
  die "写入 $PLIST 失败"
fi
TMPPLIST=""

# ── 注册 ─────────────────────────────────────────────────────
echo "[2/3] 注册服务"
if is_loaded; then
  if ! launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then
    # 旧任务没注销掉，新配置也还没生效：恢复备份，保持原状
    if [ -f "$BACKUP" ]; then
      cp "$BACKUP" "$PLIST" && echo "  ↺ 已恢复原配置"
    fi
    die "注销旧任务失败，已中止（原任务状态未变）"
  fi
fi

if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>"$ERRTMP"; then
  echo "  ✗ bootstrap 失败：$(head -1 "$ERRTMP" 2>/dev/null)"
  if [ -f "$BACKUP" ]; then
    echo "  ↺ 正在用备份恢复原配置…"
    cp "$BACKUP" "$PLIST" || die "恢复备份失败，请手动执行：launchctl bootstrap gui/\$(id -u) \"$PLIST\""
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    sleep 2
    if is_loaded; then
      die "新配置注册失败，已回滚到原配置（定时任务仍在运行）"
    fi
    die "回滚也失败，请手动执行：launchctl bootstrap gui/\$(id -u) \"$PLIST\""
  fi
  die "注册失败且没有可用备份"
fi
sleep 2

# ── 验证 ─────────────────────────────────────────────────────
echo "[3/3] 验证"
echo
if is_loaded; then
  echo "✓ 已注册，排程："
  plutil -extract StartCalendarInterval xml1 -o - "$PLIST" 2>/dev/null \
    | grep integer | sed 's/[^0-9]//g' | paste - - \
    | awk '{printf "    %02d:%02d\n", $1, $2}'
  echo "    + 每次登录/开机时补跑一次"
else
  echo "✗ 注册后仍未找到服务，请重跑本脚本"
  exit 1
fi

echo
echo "────────────────────────────────────────"
echo "Python：  $PYTHON"
echo "日志目录：$LOGDIR"
echo "  out.log  每次运行的汇报（一行 JSON）"
echo "  err.log  报错（解密失败、Python 异常、权限问题）"
if [ -n "${QODER_EXE:-}" ] || [ -n "${QODER_AUTH_DIR:-}" ]; then
  echo "已写入 plist 的自定义配置："
  [ -n "${QODER_EXE:-}" ] && echo "  QODER_EXE=$QODER_EXE"
  [ -n "${QODER_AUTH_DIR:-}" ] && echo "  QODER_AUTH_DIR=$QODER_AUTH_DIR"
fi
echo
echo "最近一次运行输出："
sleep 1
tail -n 2 "$LOGDIR/out.log" 2>/dev/null || echo "（暂无，等第一次触发）"
echo
echo "查看全部：tail -f $LOGDIR/out.log"
echo "卸载：    bash $DIR/uninstall-qoder-macos.sh"
echo "────────────────────────────────────────"
echo
echo "说明："
echo "  · Credits 按 UTC+8 每天 10:00 刷新，窗口 10:00 ~ 次日 09:59；"
echo "    窗口内任意一次触发领取都有效，过期不补领。"
echo "  · 国际版活动实例的下发可能比 10:00 晚几分钟到几十分钟，"
echo "    11:05 起的兜底触发会接住。"
echo "  · 令牌由桌面端负责刷新；长期不打开 Qoder CN 导致过期时，"
echo "    err.log 会出现 AUTH_REJECTED / 解密失败，打开一次桌面端即可恢复。"
echo "  · 首次运行可能弹出钥匙串授权框（读取 Qoder CN Safe Storage），"
echo "    选「始终允许」后后台任务才不会卡住。"
