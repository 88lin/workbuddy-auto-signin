# workbuddy-checkin

> 一个自包含的小脚本，每天自动帮你领取 **WorkBuddy** 的每日签到积分。零依赖、零内置密钥——只要在本机登录过 WorkBuddy 桌面端就能跑。

WorkBuddy（腾讯的 AI 编程助手）每天可以通过签到领取免费积分。本脚本逆向出桌面端实际使用的签到接口，读取**你自己机器上的登录态**，帮你自动领积分——幂等、每天可跑。

## 特性
- **纯 Python 标准库**——不用 `pip install`，任意 Python 3 即可。
- **单文件**，约 290 行，完全自包含。
- **幂等安全**——先查状态，未签才领；重复运行不会多领。
- **一行智能汇报**——如 `成功领取 100 积分（连续 7 天，累计 700 积分）` / `今日已签过（...）` / `签到活动未开启` / `登录态已失效`。
- **健壮**——兼容"今日已签"的两种返回形态（`null` 与 `400 + code 10001`），识别 401/403 登录态过期，识别非签到季（`active=false`）。
- **跨平台**——自动探测 Windows / macOS / Linux 下的 WorkBuddy 凭据文件。
- **仓库不含任何密钥**——只读取运行者本机的登录凭据，可安全 fork 和分享。

## 工作原理
登录后，WorkBuddy 桌面端会写出一个明文 JSON 会话文件 `workbuddy-desktop.info`，里面含 `accessToken`。脚本流程：
1. 定位该文件（按平台自动探测，或用环境变量 `WORKBUDDY_AUTH_FILE` 覆盖）。
2. `POST /v2/billing/meter/checkin-activity-status`——今天是否已领？
3. 若未领，`POST /v2/billing/meter/daily-checkin`——领取。
4. 输出一行 JSON，`report` 字段是人话汇报。

所有请求都打到官方客户端用的同一个 endpoint（`https://copilot.tencent.com`）。

## 前置条件
1. 已安装并**登录过 WorkBuddy 桌面端**（登录后会写出凭据文件）。
2. 本机有 **Python 3**（任意版本，无需任何第三方包）。

## 快速开始
```bash
git clone https://github.com/88lin/workbuddy-auto-signin.git
cd workbuddy-auto-signin
python checkin.py auto
```
看到 `今日已签过` 或 `成功领取 N 积分` 就说明通了。

## 用法
```
python checkin.py auto     # 每日自动化：查状态→未签才领→输出一行汇报
python checkin.py status   # 仅查状态（调试）
python checkin.py claim    # 仅领取（调试，幂等安全）
python checkin.py all      # 查状态 + 领取（调试）
```
输出始终是一行 JSON，`report` 字段是可读的中文汇报。

## 设置每天自动领（00:05）
推荐用 **WorkBuddy 的定时自动化**。本脚本依赖你**本机**的桌面端登录态，所以云端 CI（如 GitHub Actions）跑不了。

1. 把 `checkin.py` 放到固定位置，例如 `<工作区>/.workbuddy/automations/daily-checkin/checkin.py`。
2. 在 WorkBuddy 里新建自动化：
   - **名称**：每日自动领 WorkBuddy 积分
   - **计划**：每天 00:05
   - **提示词**：
     ```
     运行 python "<checkin.py 绝对路径>" auto，
     把命令输出的 JSON 里 report 字段的内容，直接一句话汇报给我。
     若 report 含"领取失败"或"登录态已失效"，提醒我重新登录 WorkBuddy 桌面端。
     ```

## 配置
| 环境变量 | 作用 |
|---|---|
| `WORKBUDDY_AUTH_FILE` | 自动探测失败时，手动指定凭据文件路径。 |

## 排错
| 现象 | 处理 |
|---|---|
| `NO_AUTH / 未找到登录凭据` | 先登录一次 WorkBuddy 桌面端；或设置 `WORKBUDDY_AUTH_FILE`。 |
| `NO_SESSION / HTTP 401\|403` | 登录态过期——重新登录桌面端，自动化会自动恢复。 |
| `INACTIVE / 签到活动未开启` | 当前不在签到活动期内，属正常，无需处理。 |
| 调试原始接口返回 | `python checkin.py status` 或 `python checkin.py all`。 |

## 安全与隐私
- 脚本只读取**你自己本机**的 WorkBuddy 会话文件，不含、不内嵌、不传输任何第三方密钥。
- 永远不会打印 `accessToken`，`Authorization` 头不会出现在日志里。
- 可安全 fork、分享、在自己机器上运行——它只作用于**你自己的**登录态。

## 免责声明
本项目为**非官方**工具，与腾讯或 WorkBuddy 无任何隶属关系。签到接口系从桌面端 `app.asar` 逆向得到，仅供个人自动化使用。使用风险自负；接口可能随时变动且不另行通知。请遵守相关服务条款。

## 协议
[MIT](LICENSE) © 2026 88lin
