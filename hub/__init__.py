"""WorkBuddy 统一控制台（hub）。

模块边界：
  config   路径 / 常量 / 开关（集中管理，便于测试注入）
  state    状态文件原子读写、单实例锁、跨线程命令队列
  clients  客户端进程发现 / 关闭 / 重开
  billing  当前积分余额解析与查询（口径对齐官方客户端）
  service  换号 / 会话同步 / 状态聚合等业务编排
  web      本地 HTTP 面板与 API
  daemon   看守循环
  cli      命令行入口与自检

设计约束：业务逻辑不依赖 HTTP；HTTP 只操作命令队列；所有外部 I/O 可替换。
"""

__all__ = ["config", "state", "clients", "billing", "service", "web", "daemon", "cli"]
