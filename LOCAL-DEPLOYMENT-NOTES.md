# 本机部署与运维手记（接手必读）

本文件记录 `workbuddy-auto-signin` 在本机（Windows）的实际部署、多账号配置、换号方法与踩坑经验。
上游 `README.md` 讲功能，本文件只讲**这台机器上真实做过什么、为什么这么做、出问题怎么恢复**。

## 0. 一句话现状

- 部署方式：Windows 任务计划程序 + `pythonw.exe` 静默运行（**非 Docker**）。
- 已绑定 **3 个账号**：`acc1`（QQ，`bc698847…`）、`acc2`（微信，`dc2e5864…`）、`acc3`（微信，`f45819db…`）。
- 客户端当前登录身份：**acc1**（通过凭据快照还原，未重新扫码）。
- 两个定时任务均 `Ready`、退出码 0；多账号轮转已验证生效。

## 1. 环境与部署

| 项 | 值 |
|---|---|
| 仓库 | `F:\Project\workbuddy-auto-signin`（git clone，未改上游代码） |
| Python | `F:\Python\versions\cpython-3.12-windows-x86_64-none\python.exe` / `pythonw.exe` |
| 凭据目录 | `C:\Users\TTW\AppData\Local\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| 账号快照 | `F:\Project\workbuddy-auto-signin\accounts\`（含登录态副本，**禁止提交 git / 同步网盘**） |
| 日志 | `F:\Project\workbuddy-auto-signin\signin.log` |

部署命令（在仓库目录、用当前 Windows 账号）：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

**坑 1：安装脚本找不到 `pythonw.exe`。**
本机 `python` 是 `C:\Users\TTW\.local\bin\python.exe` 的 uv shim，旁边没有 `pythonw.exe`；真实运行时在 `F:\Python\versions\cpython-3.12-windows-x86_64-none\`。
已在 `install-windows.ps1` 顶部设置：

```powershell
$ManualPythonw = "F:\Python\versions\cpython-3.12-windows-x86_64-none\pythonw.exe"
```

重装/换机器时若自动探测失败，改这一行即可。

## 2. 定时任务（当前注册状态）

| 任务 | 触发 | 动作 | 补跑 | 时限 |
|---|---|---|---|---|
| `WorkBuddyAutoSignin` | 每天 00:05 | `pythonw.exe signin.py silent` | `StartWhenAvailable=True` | 10 分钟 |
| `WorkBuddyGrowthPoll` | 01/05/09/13/17/21 点（6 个独立触发器） | `pythonw.exe signin.py silent-poll` | 同上 | 5 分钟 |

- 刻意用 **6 个独立 Daily 触发器**，不用「每 4 小时重复」：重复实例错过后不会补跑，独立触发器开机后会补。
- 一天共 7 次签到机会；每轮先查状态、未签才补，幂等不会重复领。
- **无法保证的情况**：整天不开机、凭据过期未 rebind、上游接口改版。

查看状态：

```powershell
Get-ScheduledTaskInfo -TaskName "WorkBuddyAutoSignin"
Get-ScheduledTaskInfo -TaskName "WorkBuddyGrowthPoll"
Get-Content "F:\Project\workbuddy-auto-signin\signin.log" -Tail 5
```

卸载：

```powershell
Unregister-ScheduledTask -TaskName "WorkBuddyAutoSignin" -Confirm:$false
Unregister-ScheduledTask -TaskName "WorkBuddyGrowthPoll" -Confirm:$false
```

## 3. 多账号

绑定时 WorkBuddy 桌面端同一时刻只能登录一个账号，所以每个账号的凭据是在 `bind` 时做**快照**存进 `accounts/`。

```powershell
python signin.py bind <名字>          # 客户端登录该账号后执行
python signin.py bind <名字> --force  # 重新登录后更新快照
python signin.py accounts             # 查看清单、uid 前缀、有效期、最近结果
python signin.py unbind <名字>        # 解绑并删除快照
python signin.py auto --account acc2  # 只跑指定账号
python signin.py status --account current  # 只查客户端当前登录身份的只读状态
```

- `auto / silent / silent-poll` 会按「上次运行时间」轮转全部账号；单账号失败不影响其他账号。
- **`use <名字>` 只改脚本清单里的 `active` 指针**，不碰客户端登录态，别当成客户端换号。
- 快照有效期约一年（当前三个账号均到 2026-12-24），到期后需重新登录并 `bind <名字> --force`。

## 4. 客户端换号（凭据快照还原，免扫码）

已实测可行。参考了 `xmgzxmgz/workbuddy-account-hub`、`Danjack85/workbuddy-switch` 的做法。

### 原理与约束

- 登录态就是 `workbuddy-desktop.info`；把目标账号的快照写回去，客户端重启后即认为已登录。
- **必须冷切换**：运行中的客户端持有该文件，热切换写了会被覆盖回去。
- 客户端切换账号只写本地会话、**不调用服务端注销**，所以其他账号的快照不会被作废，可反复互切。
- 切换前整份备份、写回后回读 uid 校验；`allAccounts` 应合并而非覆盖（整份替换时该账号的列表会成为唯一项，切回其他号用对应快照再写回即可）。
- 登出标记 `<同名>.logged-out` 若存在会导致登录态被忽略，切换前确认无此文件。

### 操作步骤

```powershell
# 1) 备份当前登录态
$auth = "C:\Users\TTW\AppData\Local\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"
Copy-Item $auth "F:\Project\workbuddy-auto-signin\backups\workbuddy-desktop.info.before-$(Get-Date -Format yyyyMMdd-HHmmss)"

# 2) 结束全部 WorkBuddy 进程（先 CloseMainWindow，再强杀残留），确认进程数为 0

# 3) 把目标账号快照写回（示例为 acc1）
Copy-Item "F:\Project\workbuddy-auto-signin\accounts\acct-b44b....info" $auth -Force

# 4) 回读校验 uid，然后启动 WorkBuddy.exe，等待 10 秒再核对一次 uid 未被覆盖
```

### 风控边界

- 签到/查积分是幂等的常规功能，必须**串行执行**、失败退避，不要并发或高频刷。
- 官方网关拦截非浏览器 UA（403「请求不合法」）；脚本已处理。
- 真正危险的是短时间频繁换号、以及网关转发/绕过计费类用法——本项目不涉及。
- 令牌有效期以 `expiresAt` 为准（实测约 365 天），过期后需扫码登录一次。

## 5. CMD 弹窗问题（非签到脚本）

游戏时弹出的 CMD 来自另一个项目 `NoobtyVideoD-SyncProductionData`（每 6 小时一次，原动作是 `cmd /c ... && node ...`，`Hidden=False`）。
签到脚本本身用 `pythonw.exe` + `Hidden=True`，不弹窗。

**关键点：任务计划程序的 `Hidden` 开关只隐藏任务条目，不阻止控制台窗口弹出。**

已改为隐藏启动器：

- `F:\Project\Noobty_VideoD\tools\sync-hidden.vbs`：`ws.Run cmd, 0, True`（窗口样式 0 = 完全隐藏，True = 等待结束）
- 任务动作：`wscript.exe "F:\Project\Noobty_VideoD\tools\sync-hidden.vbs"`
- 原任务 XML 备份：`backups\NoobtyVideoD-SyncProductionData-before-hidden.xml`

**坑 2：VBS 里写中文注释会在 GBK 代码页下被误读**，导致 `Object required: 'ws'` 之类的解析错误且静默失败。该文件必须保持纯 ASCII。

**坑 3：PowerShell 用 `& wscript.exe` 不会等待 GUI 进程**，检查输出会误判为没执行；验证时用 `Start-Process -Wait`，或用 `cscript.exe //nologo` 直接看错误。

## 6. 备份清单

| 文件 | 用途 |
|---|---|
| `backups\NoobtyVideoD-SyncProductionData-before-hidden.xml` | 同步任务改隐藏前的原始任务定义 |
| `backups\workbuddy-desktop.info.before-acc1-20261127-185448` | 切到 acc1 前的客户端登录态；可回滚到 f45819db（acc3） |
| `accounts\*.info` + `accounts.json` | 三个账号的凭据快照与清单 |

回滚客户端登录态：结束客户端 → 用备份文件覆盖 `workbuddy-desktop.info` → 启动并回读 uid。

## 7. 常见现象速查

| 现象 | 处理 |
|---|---|
| `doctor` 显示 `AUTH_READY` | 只说明凭据格式和解密运行时可用，未验证服务端 |
| 日志 `ALREADY` | 幂等正常，今天已签 |
| `prerequisite not met: first_buddy` | 该账号没领养 Buddy，成长奖励领不到；签到不受影响。盲盒可能开出 Buddy 后自愈 |
| 某账号 `EXPIRED` / `AUTH_REJECTED` | 重新登录该账号后 `bind <名> --force` |
| `DEFERRED` | 时间预算不够，下一轮优先补跑，正常 |
| 404 / 403 网关拒绝 | 检查 UA 与账号权限，非脚本故障 |
| 想看原始返回 | `python signin.py status --account <名>` |

## 8. 参考项目（换号/风控实现）

- `xmgzxmgz/workbuddy-account-hub`：`allAccounts` 合并、切换前备份、只换登录态不动会话库。
- `Danjack85/workbuddy-switch`：冷切换流程、写回后回读校验、登出标记处理、签到串行避免风控。
- `ddGuo/dsh-workbuddy-switcher`：凭据归档 + 轮询生效（约 30 秒）。

---

*最后更新：2026-11-27。所有路径均为本机绝对路径，换机器需按实际情况调整。*
