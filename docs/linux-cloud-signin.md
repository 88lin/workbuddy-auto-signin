# 🐧 云端沙箱 / 手机 App 配置指南

给**没有 WorkBuddy 桌面端**的环境看的一篇：Linux 云沙箱、WorkBuddy 手机 App / 小程序
背后的服务器。

脚本本身是纯 Python 标准库，在 Linux 上直接能跑。**唯一的卡点是登录凭据**——
这篇讲的就是怎么把它拿到手。

## 🔍 先判断环境

```bash
python3 --version                                             # 3.x 即可，无第三方依赖
curl -s -o /dev/null -w '%{http_code}' https://copilot.tencent.com   # 200 说明 API 通的
which crontab; systemctl status                                # 大概率都不存在
python3 /workspace/workbuddy-auto-signin/signin.py doctor       # 报 NO_AUTH 就往下走
```

| 能力 | 结论 |
|:---:|---|
| 🐍 脚本在 Linux 运行 | ✅ 支持，纯标准库 |
| 🔑 凭据探测 | ✅ 读 `~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info` |
| ⏰ 系统级定时（cron / systemd） | ❌ 不支持，容器里没有，本仓库也没提供 |
| 🤖 定时方式 | ✅ 只能用**模式 A：WorkBuddy 自动化** |

## 🧩 为什么会 `NO_AUTH`

云端沙箱的登录态是平台通过 `ACC_PRODUCT_CONFIG_V2/V3` 两个环境变量，以
`type=custom-token` 的方式**注入**的。走注入模式时 CLI 不会把凭据写成文件，
脚本自然找不到。

解决办法：把注入变量摘掉，让 CLI 退回 `product.json` 里配置的
`cli-external-link` 流程——生成登录链接 → 人在手机/浏览器上授权 → 凭据落地。

CLI 等授权用的是**服务端轮询**，不是本地端口回调，所以沙箱不需要开放入站端口。

## 🚀 操作步骤

### 1. 取脚本

```bash
git clone https://gh-proxy.com/https://github.com/88lin/workbuddy-auto-signin.git /workspace/workbuddy-auto-signin
```

> [!TIP]
> `gh-proxy.com` 是国内镜像，GitHub 慢的时候用它。能直连就把前缀去掉。

### 2. 拿登录凭据

```bash
python3 scripts/cli_login.py --wait 1500
```

脚本打印一行：

```
LOGIN_URL=https://copilot.tencent.com/login?platform=CLI&state=...
```

**把这行链接发到微信聊天框，再从微信里点开登录**（见下方坑位 6）。
进程在后台等回调，授权成功后打印 `CREDENTIAL_READY=<路径>` 并退出。

> [!NOTE]
> 授权必须人来点，AI 代劳不了。拿到链接后停下来等，不要空转重试。

### 3. 验证

```bash
python3 /workspace/workbuddy-auto-signin/signin.py doctor
# 期望：{"result": "AUTH_READY", "credential_format": "plaintext", ...}

python3 /workspace/workbuddy-auto-signin/signin.py auto
# 实跑一次：已签到返回 ALREADY，未签到返回领取的积分
```

`credential_format` 必须是 `plaintext`。若是 `sym-v1`，说明拿到的是桌面端加密凭据，
解密要调 WorkBuddy 客户端的 Electron 运行时，Linux 上没有，只能换一份明文凭据。

### 4. 建定时任务（建两条）

只能用模式 A（WorkBuddy 自动化），不能用 cron/systemd。Cron 是 6 位
（`秒 分 时 日 月 周`）：

**任务一 · 每日签到（主力）**

| 参数 | 值 |
|---|---|
| 名称 | 每日自动领 WorkBuddy 积分 |
| 频率 | 每天执行（daily） |
| Cron | `0 5 0 * * *`（每天 00:05） |
| 时区 | Asia/Shanghai |
| 超时 | 300 秒 |

**任务二 · 每 4 小时轮询（兜底）**

| 参数 | 值 |
|---|---|
| 名称 | WorkBuddy 签到轮询兜底 |
| 频率 | 按间隔执行（interval） |
| Cron | `0 0 */4 * * *`（每 4 小时） |
| 时区 | Asia/Shanghai |

两条的执行指令都用：

```text
运行 python3 /workspace/workbuddy-auto-signin/signin.py auto，
把命令输出的 JSON 里 report 字段的内容，直接一句话汇报给我。
若 JSON 的 needs_attention 为 true 或命令退出码非 0，额外提醒我处理。
```

**轮询也用 `auto`，不要用 `silent-poll`**：`auto` 会先查签到状态、未签才补，
所以能兜住 00:05 那次没跑成的情况；已签过时只是一次查询，不会重复领取。

> [!IMPORTANT]
> 建完任务后，在 App / 小程序的任务列表里点进这两条**编辑 → 选模型 hy3（免费）**。
> 创建任务的接口没有模型参数，模型只能在界面上选。

## ⚠️ 六个坑（都是实测踩出来的）

1. **`-p` 模式下 `/login` 不生效**。`codebuddy -p "/login"` 只回一句
   "Authentication required. Please use /login"，必须在 TUI 里敲斜杠命令，
   所以要用伪终端（`pty`）驱动。
2. **按键要用 `\r` 不是 `\n`**。TUI 认 `\r` 为回车，喂 `\n` 完全没反应。
3. **先过「是否信任此目录」提示**。TUI 启动后会卡在信任确认，不先回车就到不了登录界面。
   时间线：约 8 秒 `\r`（信任）→ 14 秒 `/login\r` → 22 秒 `\r`（选「中国站」）。
4. **终端窗口要够宽**。默认 80 列会把登录 URL 折行截断，拿到的是半截链接。
   设 250 列（`TIOCSWINSZ`）。
5. **必须摘掉注入变量**。`ACC_PRODUCT_CONFIG_V2`、`ACC_PRODUCT_CONFIG_V3`、
   `X_IDE_AUTH_PROXY`、`IDE_EDITOR_SERVER_AUTH_TYPE`，不摘就永远走 custom-token，
   生成不出登录 URL。
6. **登录链接要从微信里点开，不要用系统浏览器**。实测：系统浏览器打开会被劫持跳到
   WorkBuddy 小程序、授权中断；把链接发到微信聊天框、再从微信点开，才能走完授权
   （实测 2 分 38 秒落地）。登录途中跳到小程序是正常的，走完即可。
   无论哪种方式，只要 CLI 一直刷 `Waiting for auth...` 且凭据文件不生成，
   就是授权没走完，换另一种打开方式重试。

## 🧪 排错

| 现象 | 处理 |
|---|---|
| `NO_AUTH` | 走第 2 步重新授权 |
| `credential_format: sym-v1` | 换明文凭据，别用桌面端加密那份 |
| `AUTH_REJECTED` / HTTP 401 | 登录态过期，重新授权 |
| URL 只拿到半截 | 终端窗口太窄，见坑位 4 |
| `/login` 界面不出来 | 按键用了 `\n`，或没先过信任提示，见坑位 2、3 |
| 卡在 `Waiting for auth` | 授权没走完，换打开方式重试，见坑位 6 |

## 📋 凭据格式

脚本要的 JSON 结构（明文）：

```json
{
  "auth":    {"accessToken": "<字符串>", "domain": "..."},
  "account": {"uid": "...", "enterpriseId": "..."}
}
```

`accessToken` 是**字符串**才是明文可用；是 `{"$wbEncrypted":1,"envelope":...}`
这种对象就是 `sym-v1` 加密的，Linux 解不开。桌面端写的 `workbuddy-desktop.info`
多半是加密的，CodeBuddy CLI / VS Code 扩展写的
`Tencent-Cloud.coding-copilot.info` 才是明文。

## ✅ 实测记录

2026-10-11 在 WorkBuddy 手机 App 所连的云端沙箱
（`CLIENT_INFO_IDE_TYPE=WorkBuddy_APP`）完整跑通：

生成链接 → 链接发到微信聊天框、从微信点开登录 → 约 2 分 38 秒凭据落地 →
`doctor` 返回 `plaintext` → `auto` 返回 `ALREADY`（今日已签，今日 +100、
连续 12 天、累计 1200 积分，均为真实数据）。

同一链接用系统浏览器打开则失败（被劫持跳小程序）。
