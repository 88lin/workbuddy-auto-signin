# WorkBuddy 统一控制台（hub.py）

把**换号、看账号状态/当前积分、会话自动跟随**合成一个工具。不改动上游 `signin.py` 的签到逻辑，两个定时任务照常跑。

## 打开方式

- 桌面快捷方式 **「WorkBuddy统一控制台」**
- 或浏览器访问 `http://127.0.0.1:8765/`

守护进程由开机自启项 `WorkBuddy统一控制台.lnk` 拉起，无控制台窗口。

## 面板上有什么

- **账号表**：每个账号的**当前可用积分余额**（来自账单套餐剩余，不是签到活动累计）、最近一笔积分到期时间、名下会话数、凭据到期时间、是否当前登录
- **切换按钮**：一键冷切换账号 + 把会话归并到新账号
- **立即同步会话**：不换号，只把其他账号的会话归到当前账号
- **刷新余额**：强制重新查服务端余额（平时 5 分钟缓存一次）
- **暂停/恢复监控**：临时关掉自动跟随，不动数据
- **打开备份目录 / 打开完整日志**
- 底部实时日志，状态、错误都能看到

## 命令

```powershell
python hub.py                 # 启动守护进程 + 面板
python hub.py status          # 打印状态 JSON（含余额）
python hub.py switch acc2     # 切号（若守护在跑则走队列，不会并发迁移）
python hub.py sync            # 只同步会话
python hub.py balance         # 强制刷新余额
python hub.py self-test       # 自检
```

启动/停止：`启动控制台.cmd` / `停止控制台.cmd`。

## 关键口径

- **当前积分**：`POST https://www.codebuddy.cn/v2/billing/meter/get-user-resource`，取各套餐剩余额度（`CycleCapacityRemain` 钳位到 `CycleCapacitySize`，脏数据用 `CycleCapacityUsed` 反修正；无周期字段回退 `CapacityRemain`），逐条钳非负后求和。与官方客户端口径一致。
- 签到活动的 `total_credits` 是赛季累计，**不是余额**，面板不把它当余额展示。
- 余额和到期时间来自同一次请求，一起缓存 5 分钟，避免高频查询。

## 换号流程

1. 校验目标账号快照 uid 与清单一致
2. 风控保护：距上次换号不足 180 秒直接拒绝
3. 关闭国内版/国际版客户端
4. 备份当前 `workbuddy-desktop.info` 到 `backups/`，清掉 `.logged-out` 标记
5. 写回目标账号快照并回读 uid 校验
6. 调用 `workbuddy-account-migrate/scripts/migrate.py` 把其他账号会话归到目标账号
7. 重开客户端，等 10 秒回读 `account-snapshot.json` 校验 uid
8. 同步 `signin.py` 的 active 指针（不碰客户端登录态）

手动在客户端换号时，守护进程检测到 uid 变化会自动走第 6、7 步。

## 稳定性设计

- 单实例系统文件锁，重复启动直接退出；进程崩溃自动释放
- 所有子进程调用带 `CREATE_NO_WINDOW`，`pythonw` 常驻不会闪 CMD 窗口
- 换号最小间隔 180 秒；余额查询串行 + 1 秒退避 + 5 分钟缓存
- 状态机写入 `hub-state.json`（原子替换），失败原因落到 `last_error` 并在面板显示
- 命令行 `switch/sync/balance` 通过本地 HTTP 队列交给守护进程，不会起第二个进程并发改库

## 注意

- 换号瞬间客户端会被强制关闭重开，未保存的编辑会丢失
- 余额是服务端实时查询，失败时面板该行会显示错误而不是显示 0
- 换号/迁移备份：`workbuddy-auto-signin/backups/`（登录态）+ `~/.workbuddy/migrate_backups/`（数据库）
