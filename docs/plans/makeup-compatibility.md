# 补登日期契约与回归验证

## 根因

旧脚本把 `GET /v2/activity/growth/streak` 中的 `streak.makeup_dates` 当作待补日期。官方成长中心实际上用该列表标注“已补签”；待补日期由活跃日历与日期限制共同确定。因此旧脚本既会反复提交已补登日期，也会在补登历史为空时漏掉真正的断登。

2026-10-01 核对的官方公开前端：

- [GrowthCenterPage](https://download.codebuddy.cn/web/usercenter/00259587558f692132209f5249d62317c65520b6/assets/GrowthCenterPage-BZwm0xmj.js)：`makeupDates` 转为集合，命中时显示“已补签”；日历用 `score > 0` 标记活跃，过去的零分日期标为 `broken`。
- [AllTasksPage](https://download.codebuddy.cn/web/usercenter/00259587558f692132209f5249d62317c65520b6/assets/AllTasksPage-Cs7k60T3.js)：`canMakeup` 限制为同一自然月、今天之前、不早于 `launch_date` 及活动起始日 `2026-06-17`；缺少上线日期时禁止补登。
- [growthSpace 接口](https://download.codebuddy.cn/web/usercenter/00259587558f692132209f5249d62317c65520b6/assets/growthSpace-EIUE4QaA.js)：查询 `/activity/growth/heatmap`，补登提交 `/activity/growth/makeup-cards/use`，请求体为 `{"target_date": "YYYY-MM-DD"}`。

## 实现

1. 先查询连登状态与卡余额；无卡时不额外查询日历。
2. 有卡时读取 `/v2/activity/growth/heatmap`，使用 `today.date` 作为日期边界，避免本机时区差异。
3. 只接受有效 ISO 日期与明确的非负数字分数；仅选择当月历史零分日期，并排除已补登记录。未出现在日历中的日期不推测为断登。冲突记录、未知结构及查询失败均停止本轮补登并报错。
4. 从最近的断点开始，每轮最多使用一张卡。补登写请求不自动重试，不增加本地缓存或账号状态文件。
5. HTTP 成功还要检查 JSON 业务码；未知拒绝作为需处理的失败。明确的 `400 + date is not broken, no makeup needed` 表示查询后状态已变化，按无需操作处理。
6. 补登成功后重新查询连登展示值；兑换步骤保留原有查询流程。成长中心的硬失败不能被其他步骤成功掩盖，已领取积分仍保留，`report` 附失败原因，退出码非零且 `needs_attention` 为真。

## 验证

运行 `python -m unittest discover -s tests -v`。新增测试全部使用合成数据和模拟网络，覆盖历史日期误用、首次补登、当月与上线边界、最近断点优先、每轮数量上限、日历缺项和异常、业务错误、并发状态变化、部分成功及两种静默轮询命令的日志。

本机 WorkBuddy 5.7.3 的真实只读状态接口、连登和活跃日历均可访问；历史被反复提交的日期已有正分数，符合“无需补登”的服务端拒绝。测试不会为验证主动制造断登或消耗补登卡；真实成功扣卡路径仍需在实际存在可补日期时观察。

2026-10-01 修复后验收：69 项测试全部通过，`git diff --check` 通过。在请求层阻止所有业务写入后运行 `run_daily`，9 个真实查询均返回 HTTP 200，结果为 `ALREADY`、退出码 0、`needs_attention: false`、`quiet: true`；未尝试业务写入，调用前后凭据文件字节一致。
