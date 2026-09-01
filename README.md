<div align="center">

# 🤖 workbuddy-auto-signin

**自动领取 WorkBuddy 每日签到积分的小脚本**

零依赖 · 纯标准库 · 跨平台 · 幂等安全

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org/)
[![No Dependencies](https://img.shields.io/badge/dependencies-0-green.svg)]()
[![Platform](https://img.shields.io/badge/platform-Win%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Stars](https://img.shields.io/github/stars/88lin/workbuddy-auto-signin.svg)](https://github.com/88lin/workbuddy-auto-signin)

</div>

> 一个自包含的 Python 脚本，每天自动帮你领取 **WorkBuddy**（腾讯 AI 编程助手）的每日签到积分。只读取你自己机器上的登录态，零内置密钥，可安全分享。

## 💖 赞助商

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://api.justwoker.icu/register?aff=wpiO"><img src="https://cdn.jsdmirror.com/gh/88lin/picx-images-hosting@master/ScreenShot_2026-09-01_130420_632.webp" alt="JustDoWork" width="150"></a>
</td>
<td valign="middle"><b><a href="https://api.justwoker.icu/register?aff=wpiO">JustDoWork</a></b>&nbsp;是免费公益大模型API平台，可用Claude Opus 5 模型，目前稳定可用。注册送＄100（每日签到得＄30左右），支持GitHub登录。</td>
</tr>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://agentrouter.org/register?aff=ugVO"><img src="https://cdn.jsdmirror.com/gh/88lin/picx-images-hosting@master/90C5FAD072EA247822CB88BB32512A41.webp" alt="Agent Router" width="150"></a>
</td>
<td valign="middle"><b><a href="https://agentrouter.org/register?aff=ugVO">Agent Router</a></b>&nbsp;是免费公益大模型API平台，支持GPT-5.6、Claude Opus 5、glm-5.3、deepseek-v4-flash 等主流模型，国内直连。注册送＄175（每日签到得＄25，被邀得＄50），支持GitHub/LinuxDo登录。</td>
</tr>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://www.sheapi.top/sign-up?aff=MvcR"><img src="https://cdn.jsdmirror.com/gh/88lin/picx-images-hosting@master/ScreenShot_2026-08-06_174058_726.webp" alt="SheApi" width="150"></a>
</td>
<td valign="middle"><b><a href="https://www.sheapi.top/sign-up?aff=MvcR">SheApi</a></b>&nbsp;是一家可靠高效的 API 中转服务提供商，主要提供 Claude Code、Codex 等主流模型的高稳定中转能力，Codex 倍率补贴低至 0.06，GPT-Image-2生图每张0.04。受邀注册送$1 体验金，每日签到还可领取专属免费额度。</td>
</tr>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://www.workbuddy.cn/events/invite?inviteCode=w0x2ic45z"><img src="https://download.codebuddy.cn/web/workbuddy/0bebf86e38e7d71ff0c313d661e7753ff996c54e/assets/workbuddy-logo-WhgOvEF7.png" alt="WorkBuddy" width="150"></a>
</td>
<td valign="middle"><b><a href="https://www.workbuddy.cn/events/invite?inviteCode=w0x2ic45z">WorkBuddy</a></b>&nbsp;是腾讯出品的全能 AI 工作台，是中国最受欢迎的效率 AI 智能体服务，说出要求、开始执行任务、交付完整成果。其中Hy3模型限时免费使用，注册即可获取2000积分，每月再赠送500积分，可用Kimi-K3、GLM-5.2等模型。</td>
</tr>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://gorouter.app/sign-up?aff=KPQj"><img src="https://cdn.jsdmirror.com/gh/88lin/picx-images-hosting@master/ScreenShot_2026-07-31_200922_732.webp" alt="GoRouter" width="150"></a>
</td>
<td valign="middle"><b><a href="https://gorouter.app/sign-up?aff=KPQj">GoRouter</a></b>&nbsp;是免费公益大模型API平台，可用Claude Opus 5 模型。注册送＄70，每日签到得＄10左右，支持GitHub登录。</td>
</tr>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://seekai.cc/sign-up?aff=Plh5"><img src="https://cdn.jsdmirror.com/gh/88lin/picx-images-hosting@master/ScreenShot_2026-07-31_213220_778.webp" alt="SeekAi" width="150"></a>
</td>
<td valign="middle"><b><a href="https://seekai.cc/sign-up?aff=Plh5">SeekAi</a></b>&nbsp;是免费公益大模型API平台，可用claude-fable-5、Claude-Opus-5、kimi-k3、gpt-5.6-sol、grok-4.6、glm-5.3-flash、DeepSeek-V4-Flash-0731等主流模型。注册送＄200，每日签到得＄20，支持GitHub和TG登录。</td>
</tr>
</table>

---

## ✨ 特性

| | 特性 |
|:---:|---|
| 🧩 | **零依赖** — 纯 Python 标准库，不用 `pip install`，任意 Python 3 即可 |
| 📦 | **单文件** — 完全自包含 |
| 🔁 | **幂等安全** — 先查状态，未签才领；重复运行不会多领 |
| 🐱 | **成长中心** — 自动领 Buddy 旅行礼物、派 Buddy 出发、开盲盒、领任务奖励 |
| ⏰ | **双定时模式** — AI 自动化（跨平台）或系统级静默（Win，零 token） |
| 🧠 | **智能汇报** — 一行 JSON，如 `成功领取 100 积分（连续 7 天，累计 700 积分）` |
| 🛡️ | **健壮** — 兼容"已签"两种返回形态、识别 401/403 登录态过期、识别非签到季 |
| 🌐 | **跨平台** — 自动探测 Windows / macOS / Linux 凭据文件 |
| 🔒 | **无密钥** — 仓库不含任何密钥，只读取运行者本机登录凭据 |

---

## 📋 前置条件

- ✅ 已安装并**登录过 WorkBuddy 桌面端**（登录后自动写出凭据文件）
- ✅ 本机有 **Python 3**（任意版本，无需任何第三方包）

---

## ⏰ 每日定时自动化

本脚本依赖本机桌面端登录态，云端 CI（如 GitHub Actions）跑不了。提供**两种定时模式**，按需选择：

### 模式对比

| 对比项 | 模式 A：AI 自动化 | 模式 B：系统级静默 ⭐ |
|:---:|---|---|
| **平台** | 🌐 Win / macOS / Linux | 🪟 仅 Windows |
| **原理** | WorkBuddy 自动化触发 → AI 模型跑脚本 → 模型汇报 | Windows 任务计划程序 → `pythonw.exe` 直接跑脚本 → 写日志文件 |
| **Token 消耗** | 每次一次模型调用 | **零** |
| **聊天记录** | 每次一条 | **零** |
| **弹窗** | 无 | 无 |
| **可靠性** | 依赖模型可用性 | 纯系统级，更可靠 |
| **日志** | 在聊天记录里 | 独立日志文件 `signin.log` |
| **关机错过** | 错过就错过 | 可设"错过后下次启动时补跑" |
| **设置难度** | 低（在 WorkBuddy 里建自动化） | 中（一条命令） |

---

### 模式 A：AI 自动化（跨平台）

在 WorkBuddy 里新建自动化：

1. 把 `signin.py` 放到固定位置，例如 `<工作区>/.workbuddy/automations/daily-signin/signin.py`
2. 新建 WorkBuddy 自动化：
   - **名称**：每日自动领 WorkBuddy 积分
   - **计划**：每天 00:05
   - **提示词**：
     ```text
     运行 python "<signin.py 绝对路径>" auto，
     把命令输出的 JSON 里 report 字段的内容，直接一句话汇报给我。
     若 report 含"领取失败"或"登录态已失效"，提醒我重新登录 WorkBuddy 桌面端。
     ```

> [!TIP]
> **懒人一键**：直接把仓库链接发给 WorkBuddy，让它帮你跑起来并设置定时——
> `帮我把这个仓库跑起来并设置每天 00:05 自动签到：https://github.com/88lin/workbuddy-auto-signin`

> [!NOTE]
> 模式 A 每次运行会消耗一次 AI 模型调用并产生一条聊天记录。签到逻辑本身是确定性代码，模型仅负责"跑命令 + 汇报"。

---

### 模式 B：系统级静默（Windows，推荐）

用 Windows 自带的任务计划程序 + `pythonw.exe`（无窗口 Python）直接运行脚本，**完全不经过 AI 模型**。

**一键设置**（在终端中运行）：

```powershell
# 替换为你的 pythonw.exe 和 signin.py 实际路径
# pythonw.exe 可用任意 Python 3 自带的，不限于 WorkBuddy 托管版本
$pythonw = "pythonw.exe"   # 或完整路径，如 C:\Python313\pythonw.exe
$signin  = (Resolve-Path "signin.py").Path  # 或写绝对路径

# 创建定时任务：每天 00:05 静默运行，错过后下次启动时补跑
$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent"
$trigger  = New-ScheduledTaskTrigger -Daily -At "00:05"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName "WorkBuddyAutoSignin" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "WorkBuddy daily auto signin (silent, zero token)"
```

**查看日志**：

```bash
tail -5 signin.log   # 或用记事本打开
```

日志格式（每行一条 JSON）：

```
[2026-08-30 10:34:04] {"result": "ALREADY", "report": "今日已签过（今日 +100，连续 16 天，累计 1600 积分）", ...}
```

**卸载定时任务**：

```powershell
Unregister-ScheduledTask -TaskName "WorkBuddyAutoSignin" -Confirm:$false
```

> [!TIP]
> **懒人一键**：直接把仓库链接发给 WorkBuddy，让它帮你创建 Windows 定时任务——
> `帮我在 Windows 上用任务计划程序设置这个仓库每天 00:05 静默自动签到（pythonw.exe + silent 模式，错过后补跑）：https://github.com/88lin/workbuddy-auto-signin`

> [!NOTE]
> 模式 B 的 `silent` 参数让脚本把结果写入 `signin.log` 而非 stdout，配合 `pythonw.exe`（无控制台窗口）实现完全静默。日志文件路径可用环境变量 `WORKBUDDY_SIGNIN_LOG` 覆盖。

---

## 🛠️ 手动运行（调试用）

如需手动跑一次确认脚本可用：

```bash
git clone https://github.com/88lin/workbuddy-auto-signin.git
cd workbuddy-auto-signin
python signin.py auto
```

看到 `今日已签过` 或 `成功领取 N 积分` 就说明通了。

<details>
<summary>📖 全部命令</summary>

```
python signin.py auto     # 签到 + 成长中心（领旅行礼物/派Buddy/开盲盒/领任务奖）
python signin.py silent   # 同 auto，但输出写入日志文件而非 stdout（配合定时任务静默运行）
python signin.py growth   # 仅成长中心（不签到）
python signin.py status   # 仅查签到状态（调试）
python signin.py claim    # 仅领取签到（调试，幂等）
python signin.py all      # 查签到状态 + 领取（调试）
```

</details>

---

## ⚙️ 工作原理

登录后，WorkBuddy 桌面端写出明文 JSON 会话文件 `workbuddy-desktop.info`（含 `accessToken`）。脚本流程：

1. 📂 **定位**凭据文件（自动探测，或用 `WORKBUDDY_AUTH_FILE` 覆盖）
2. 🔍 **查询** `POST /v2/billing/meter/checkin-activity-status` — 今天是否已领？
3. 🎁 **领取** 若未领，`POST /v2/billing/meter/daily-checkin`
4. 🐱 **成长中心** 领 Buddy 旅行礼物 → 派 Buddy 出发 → 开盲盒 → 领任务奖励
5. 📤 **输出** 一行 JSON，`report` 字段是人话汇报

> [!NOTE]
> 所有请求都打到官方客户端用的同一个 endpoint（`https://copilot.tencent.com`）。签到接口系从桌面端 `app.asar` 逆向得到，仅供个人自动化使用。

---

## 🔧 配置

| 环境变量 | 作用 |
|---|---|
| `WORKBUDDY_AUTH_FILE` | 自动探测失败时，手动指定凭据文件路径 |
| `WORKBUDDY_SIGNIN_LOG` | `silent` 模式下日志文件路径（默认 `signin.log`） |

---

## 🧪 排错

| 现象 | 处理 |
|---|---|
| `NO_AUTH / 未找到登录凭据` | 先登录一次 WorkBuddy 桌面端；或设置 `WORKBUDDY_AUTH_FILE` |
| `NO_SESSION / HTTP 401\|403` | 登录态过期——重新登录桌面端，自动化自动恢复 |
| `INACTIVE / 签到活动未开启` | 非签到季，属正常，无需处理 |
| 调试原始返回 | `python signin.py status` 或 `python signin.py all` |

> [!IMPORTANT]
> 登录态失效时脚本会明确返回 `NO_SESSION` 并提醒重新登录桌面端；重新登录后自动化无需任何改动即自动恢复。

---

## 🔐 安全与隐私

- 脚本只读取**你自己本机**的 WorkBuddy 会话文件，不含、不内嵌、不传输任何第三方密钥
- 永远不会打印 `accessToken`，`Authorization` 头不会出现在日志里
- 可安全 fork、分享、在自己机器上运行——它只作用于**你自己的**登录态

---

## ⚠️ 免责声明

> [!WARNING]
> 本项目为**非官方**工具，与腾讯或 WorkBuddy 无任何隶属关系。签到接口系从桌面端 `app.asar` 逆向得到。使用风险自负；接口可能随时变动且不另行通知。请遵守相关服务条款。

---

## 📊 Star History

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/88lin/workbuddy-auto-signin/star-history/assets/my-star-history/star-history-dark.svg">
  <img alt="Star History" src="https://raw.githubusercontent.com/88lin/workbuddy-auto-signin/star-history/assets/my-star-history/star-history-light.svg">
</picture>

---

## 📄 协议

[MIT](LICENSE) © 2026 88lin
