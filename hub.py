"""WorkBuddy 统一控制台入口（薄壳）。

命令与实现分离：CLI 解析在 hub.cli，业务逻辑在 hub.service，HTTP 在 hub.web。
保留本文件名以兼容既有启动方式与快捷方式。
"""
from hub.cli import main

if __name__ == "__main__":
    main()
