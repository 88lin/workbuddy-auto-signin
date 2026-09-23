# 加密凭据兼容设计与验证

## 目标与边界

保留 `signin.py` 单文件入口和既有计划任务命令。明文凭据只需 Python；新版字段加密使用本机 WorkBuddy 自带运行时，无需额外安装 Python 包或 Node。

本项目读取当前用户的会话，只解密本次请求需要的 access token。登录及续期仍由客户端负责；不解密 refresh token、不改写凭据、不创建明文缓存。

## 处理流程

1. 按现有规则发现凭据文件，保留短暂占用重试；显式路径错误不静默回退。
2. 校验会话、账号及请求头字段。明文 access token 直接使用。
3. 只接受已识别的 `$wbEncrypted: 1`、`sym-v1`、`suite: 1` 字段信封；校验结构、canonical base64、key ID 和 nonce/tag 长度。
4. 加密字段经 stdin 传入隐藏子进程。固定内嵌 JS 在客户端运行时中获取 build material，通过 Node 标准 `crypto` 完成 AES-256-GCM 认证解密。
5. 密钥只留在子进程内；stdout 仅返回受约束的 JSON 结果。Python 校验结果后构造请求头，token 只保留在进程内存。
6. 401 按认证拒绝处理，未知 403 按权限拒绝处理；已知“兑换档位未解锁”优先按业务规则跳过。认证或权限拒绝后停止本轮后续业务请求。

## 进程与日志约束

- `shell=False`，执行文件使用绝对路径；Windows 使用隐藏子进程选项。
- 动态凭据不进入命令行或环境变量；去除不必要的 Node/Electron 运行时环境注入项。
- 助手上限 10 秒，且不能超过本轮剩余预算；超时终止并回收直接子进程。
- stdin/stdout 各限制 64 KiB，stderr 限制 8 KiB；超量输出停止处理。
- 密钥、token 和完整信封不写临时文件。原始助手输出和异常不直接进入日志，失败使用固定原因码。
- 已使用的 access token 在最终输出中统一脱敏；所有静默模式都记录认证错误。
- 非幂等写请求的重试策略保持不变；本次改动不引入自动刷新或请求重放。

## 平台与诊断

| 路径 | 实现与证据 |
|---|---|
| Windows 明文 | 原路径，单元测试覆盖 |
| Windows 字段信封 | 本机 WorkBuddy 5.6.2 真实解密与只读状态请求通过 |
| macOS 明文 | 原路径，跨平台测试覆盖 |
| macOS 字段信封 | 从 `.app/Contents/Info.plist` 发现实际可执行文件；同一桥接协议，客户端实机验收待完成 |
| Linux CodeBuddy CLI 明文 | 保留 XDG 数据路径，测试覆盖 |
| 其他加密格式/运行时 | 清晰报错，不推测其兼容性 |

可用 `WORKBUDDY_EXE` 指定对应客户端的可执行文件。自动发现多个安装时按固定顺序选择；密钥不匹配时报告原因，由显式配置选择对应版本，不循环尝试其他账号来源。

`doctor` 只检查格式、运行时原生 binding 和 AES-GCM 能力，报告所选路径与 Electron 版本。它不提取密钥、不验证真实解密、不联网。`status` 用于验证真实会话的服务端认证，失败返回非零退出码。

客户端私有 binding 和字段协议未来可能变化。支持判断以格式、运行时能力和实际验证为准，不承诺后续版本自动兼容。

## 验证方法

运行 `python -m unittest discover -s tests -v`。Python 测试使用标准库；Node 仅用作内嵌 JS 的测试宿主，不是生产安装依赖。

固定夹具 `tests/fixtures/sym-v1.json` 全部为合成数据，由独立 Python AESGCM 实现生成，测试运行时不依赖 cryptography。测试包含明文回归、信封格式和篡改、无关 refresh token、助手限流与超时、缺失运行时、日志脱敏、非零退出码、业务 403、跨平台路径和 Windows pythonw 静默日志。

CI 在 Linux、Windows、macOS 的 Python 3.10/3.13 上运行相同测试。普通 CI 不安装 WorkBuddy，因此不能替代客户端实机集成验证。

2026-09-23 本机集成验收：WorkBuddy 5.6.2 字段信封成功解密；只读 `checkin-activity-status` 返回 HTTP 200；调用前后原凭据文件字节一致；未调用领取、抽奖或兑换接口。
