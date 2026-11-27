"""配置与路径：集中定义，支持环境变量覆盖以便测试隔离。

本模块不导入 signin，也不做任何 I/O，仅提供常量与路径计算。
"""
import os
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
HOME = Path.home()
DATA_DIR = Path(os.environ.get("WORKBUDDY_HUB_DATA_DIR", str(HOME / ".workbuddy")))

SNAPSHOT = DATA_DIR / "storage" / "skeleton" / "account-snapshot.json"
DB_PATH = DATA_DIR / "workbuddy.db"

BACKUPS = Path(os.environ.get("WORKBUDDY_HUB_BACKUPS", str(BASE / "backups")))
MIGRATE = Path(os.environ.get(
    "WORKBUDDY_HUB_MIGRATE",
    str(BASE.parent / "wb会话迁移" / "workbuddy-account-migrate" / "scripts" / "migrate.py")))
LOG = Path(os.environ.get("WORKBUDDY_HUB_LOG", str(BASE / "hub.log")))
STATE_FILE = Path(os.environ.get("WORKBUDDY_HUB_STATE", str(BASE / "hub-state.json")))
DASHBOARD = Path(os.environ.get("WORKBUDDY_HUB_DASHBOARD", str(BASE / "hub-dashboard.html")))
ASSETS = Path(os.environ.get("WORKBUDDY_HUB_ASSETS", str(BASE / "assets" / "dashboard")))
LOCK = Path(os.environ.get("WORKBUDDY_HUB_LOCK", str(BASE / "hub.lock")))

PORT = int(os.environ.get("WORKBUDDY_HUB_PORT", "8765"))
BALANCE_TTL = int(os.environ.get("WORKBUDDY_HUB_BALANCE_TTL", "300"))
MIN_SWITCH_INTERVAL = int(os.environ.get("WORKBUDDY_HUB_SWITCH_INTERVAL", "180"))
POLL_SECONDS = 5
SETTLE_SECONDS = 8
COOLDOWN_SECONDS = 120
STATE_TTL_SECONDS = 1.0  # external_state 聚合快照有效期，抑制面板轮询重复开销
HTTP_BODY_LIMIT = 4096
CLIENT_NAMES = ("WorkBuddy.exe", "WorkBuddyAI.exe")
BILLING_URL = "https://www.codebuddy.cn/v2/billing/meter/get-user-resource"
STATE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 状态文件里出现这些 status 时，说明上次守护进程可能在换号中途退出。
SWITCHING_STATUS = "switching"
