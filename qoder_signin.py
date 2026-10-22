"""Qoder CN 每日 100 Credits 自动领取脚本。

作者：88lin
仓库：https://github.com/88lin/workbuddy-auto-signin
协议：MIT

Qoder CN 个人用户每天可在桌面端领取 100 通用 Credits。活动按 UTC+8 每天
10:00 刷新，没有补领。本脚本只读取本机 Qoder CN 桌面端的登录态，零内置密钥：

  GET  {base}/sash/api/v1/me/campaigns
  POST {base}/sash/api/v1/me/campaigns/{campaignId}/claim      （幂等）

凭据解密只用 Python 标准库 + Windows 自带的 DPAPI / CNG；装了 cryptography
时优先用它，但不强依赖。任何模式下都不会打印令牌，也不会改写登录态文件。

用法：
  python qoder_signin.py auto      # 领取今日 Credits，输出一行 JSON
  python qoder_signin.py silent    # 同 auto，结果写 qoder_signin.log（配合 pythonw）
  python qoder_signin.py status    # 只查活动列表，不领取
  python qoder_signin.py doctor    # 离线检查登录态与设备组件，不联网
  python qoder_signin.py auto --dry-run   # 只报告会领到什么
  python qoder_signin.py auto --once      # 不做网络重试

退出码：0 已领/今日已领/无活动；1 失败；2 没有登录态或参数错误。
"""

import base64
import glob
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0.0"

# 活动固定按 UTC+8 刷新，与运行机器的本地时区无关。
TZ_CN = timezone(timedelta(hours=8))
DEFAULT_BASE_URL = "https://openapi.qoder.com.cn"
AUTH_DIR_GLOB = "com.qodercn.app*"
AUTH_FILE = "auth.v1.dat"
LOCAL_STATE_FILE = "Local State"

# 缺这个头，服务端会返回空的活动列表。
BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Qoder/claim",
    "Cosy-ClientType": "10",
}

REQUEST_TIMEOUT = 20
# 重试只针对网络错误、429 和 5xx；401/403/业务错误重试也不会变好。
RETRY_DELAYS = (5, 15, 30)
# 计划任务的 ExecutionTimeLimit 是 PT5M；留 60s 给解释器启动和收尾。
BUDGET_SECONDS = 240.0

IS_WINDOWS = sys.platform == "win32"
_started_at = time.monotonic()
_sensitive_values = set()
_risk_cache = (0.0, {})
_risk_cache_root = None

FAILURE_RESULTS = (
    "ERROR", "NETWORK", "TIMEOUT", "NO_AUTH", "AUTH_ERROR",
    "AUTH_REJECTED", "FORBIDDEN", "PENDING",
)
QUIET_RESULTS = ("ALREADY", "INACTIVE")


class QoderAuthError(Exception):
    """登录态不可用。looked_in 用于诊断输出，不含令牌。"""

    def __init__(self, message, looked_in=None):
        super().__init__(message)
        self.looked_in = list(looked_in or [])


class NoAuthError(QoderAuthError):
    pass


class AuthFormatError(QoderAuthError):
    pass


# ---------------------------------------------------------------- 本地登录态

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    class _AeadInfo(ctypes.Structure):  # BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO
        _fields_ = [
            ("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
            ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)), ("cbNonce", wt.ULONG),
            ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)), ("cbAuthData", wt.ULONG),
            ("pbTag", ctypes.POINTER(ctypes.c_ubyte)), ("cbTag", wt.ULONG),
            ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)), ("cbMacContext", wt.ULONG),
            ("cbAAD", wt.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", wt.ULONG),
        ]


def _dpapi_unprotect(data):
    src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)
    ):
        raise AuthFormatError("DPAPI 解不开密钥（%s）——需要同一 Windows 用户且已登录桌面"
                              % ctypes.WinError())
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _decrypt_gcm_cng(key, nonce, blob):
    """AES-GCM 解密，走 Windows 自带的 CNG，不依赖 cryptography。"""
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    ct, tag = blob[:-16], blob[-16:]

    def chk(res, what):
        if res != 0:
            raise AuthFormatError("CNG %s 失败 NTSTATUS=0x%08x" % (what, res & 0xFFFFFFFF))

    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    try:
        chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
        mode = "ChainingModeGCM"
        chk(bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0),
            "SetProperty")
        kbuf = ctypes.create_string_buffer(key, len(key))
        chk(bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0,
                                              kbuf, len(key), 0), "GenKey")
        nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        info = _AeadInfo()
        info.cbSize = ctypes.sizeof(_AeadInfo)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = len(tag)
        ibuf = ctypes.create_string_buffer(ct, len(ct))
        obuf = ctypes.create_string_buffer(len(ct))
        done = ctypes.c_ulong()
        chk(bcrypt.BCryptDecrypt(h_key, ibuf, len(ct), ctypes.byref(info), None, 0,
                                 obuf, len(ct), ctypes.byref(done), 0), "Decrypt")
        return obuf.raw[:done.value]
    finally:
        if h_key:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def _decrypt_gcm(key, nonce, blob):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return _decrypt_gcm_cng(key, nonce, blob)
    try:
        return AESGCM(key).decrypt(nonce, blob, None)
    except Exception as exc:
        raise AuthFormatError("AES-GCM 认证失败（%s）" % type(exc).__name__)


def find_auth_dirs():
    override = os.environ.get("QODER_AUTH_DIR")
    if override:
        return [Path(override).expanduser()]
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    try:
        roots = sorted(glob.glob(str(appdata / AUTH_DIR_GLOB)),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        roots = []
    return [Path(r) for r in roots]


def _read_auth_store(root):
    raw = (root / AUTH_FILE).read_bytes()
    if raw[:3] != b"v10":
        raise AuthFormatError("%s 格式未知（%r）" % (AUTH_FILE, raw[:3]))
    if len(raw) < 3 + 12 + 16 + 2:
        raise AuthFormatError("%s 过短，可能正被客户端改写" % AUTH_FILE)

    state = json.loads((root / LOCAL_STATE_FILE).read_text(encoding="utf-8"))
    crypt = state.get("os_crypt") if isinstance(state, dict) else None
    if not isinstance(crypt, dict):
        raise AuthFormatError("%s 缺少 os_crypt 配置" % LOCAL_STATE_FILE)
    key_blob = base64.b64decode(crypt.get("encrypted_key") or "")
    if key_blob[:5] != b"DPAPI":
        raise AuthFormatError("%s 的 os_crypt 结构变了" % LOCAL_STATE_FILE)
    key = _dpapi_unprotect(key_blob[5:])
    plain = _decrypt_gcm(key, raw[3:15], raw[15:])
    sess = json.loads(plain.decode("utf-8"))
    if not isinstance(sess, dict):
        raise AuthFormatError("会话内容不是 JSON 对象")
    token = sess.get("token")
    if not isinstance(token, str) or not token.strip():
        raise AuthFormatError("解密成功但会话里没有 token")
    return sess


def _read_auth_store_retry(root, attempts=3):
    """客户端可能正在重写凭据，读到半截就短暂重试。"""
    last = None
    for i in range(attempts):
        try:
            return _read_auth_store(root)
        except (ValueError, OSError, AuthFormatError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(0.25)
    raise last


def load_session():
    if not IS_WINDOWS:
        raise NoAuthError("自动读取 Qoder CN 登录态只支持 Windows；其他平台请设置 QODER_TOKEN")
    dirs = find_auth_dirs()
    looked_in = [str(d) for d in dirs]
    if not dirs:
        # 注意：消息里的 %APPDATA% 必须写成 %%APPDATA%%——这行只在没找到
        # 登录态目录时执行，% 格式化运算符会把单个 %A 当格式字符直接抛
        # ValueError，把干净的 NO_AUTH 变成“脚本运行异常”。装了桌面端的
        # 机器走不到这行，所以这个 bug 在正常环境不会暴露。
        raise NoAuthError("没找到 %%APPDATA%%\\%s，请先安装并登录 Qoder CN 桌面端"
                          % AUTH_DIR_GLOB, looked_in)

    errors = []
    saw_auth_file = False
    for root in dirs:
        if not (root / AUTH_FILE).is_file():
            errors.append("%s: 未登录" % root.name)
            continue
        saw_auth_file = True
        try:
            sess = _read_auth_store_retry(root)
        except AuthFormatError as exc:
            errors.append("%s: %s" % (root.name, exc))
            continue
        except (OSError, ValueError) as exc:
            errors.append("%s: %s" % (root.name, type(exc).__name__))
            continue
        sess["_auth_dir"] = str(root)
        return sess

    detail = "；".join(errors)
    if saw_auth_file:
        raise AuthFormatError("Qoder CN 登录态读取失败（%s），请重新登录桌面端后重试" % detail,
                              looked_in)
    raise NoAuthError("Qoder CN 桌面端尚未登录（%s）" % detail, looked_in)


def _parse_iso(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _token_expired(sess):
    expires = _parse_iso(sess.get("expiresAt"))
    return expires is not None and expires <= datetime.now(timezone.utc)


# ---------------------------------------------------------------- 设备标识
#
# 服务端更愿意给带设备标识的请求下发活动。标识不编造：直接运行 Qoder CN
# 安装目录里自带的 runtime-info.exe，取客户端平时使用的那一份。找不到组件
# 就退回普通请求，不影响主流程。

UMID_REL_PATH = ("resources", "umid", "runtime-info.exe")


def _registry_roots():
    roots = []
    if not IS_WINDOWS:
        return roots
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        name = str(winreg.QueryValueEx(sub, "DisplayName")[0]).lower()
                        if "qoder" not in name:
                            continue
                        try:
                            loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0]).strip()
                        except OSError:
                            loc = ""
                        if loc:
                            roots.append(Path(loc))
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)
    return roots


def _running_roots():
    roots = []
    if not IS_WINDOWS:
        return roots
    try:
        kernel32 = ctypes.windll.kernel32

        class Entry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("th32Threads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (0, -1):
            return roots
        try:
            entry = Entry32()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            while ok:
                exe_name = entry.szExeFile.decode("utf-8", "replace").lower()
                if exe_name in ("qoder cn.exe", "qoder.exe"):
                    handle = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
                    if handle:
                        try:
                            buf = ctypes.create_unicode_buffer(32768)
                            size = wt.DWORD(len(buf))
                            if kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                                   ctypes.byref(size)):
                                roots.append(Path(buf.value[:size.value]).parent)
                        finally:
                            kernel32.CloseHandle(handle)
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        pass
    return roots


def _umid_candidates():
    override = os.environ.get("QODER_UMID_EXE")
    if override:
        return [Path(override).expanduser()]
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    prog86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    roots = [
        local / "Programs" / "Qoder CN",
        prog / "Qoder CN",
        prog86 / "Qoder CN",
        local / "Programs" / "Qoder",
        prog / "Qoder",
        prog86 / "Qoder",
    ]
    roots += _registry_roots() + _running_roots()
    seen, uniq = set(), []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(root)
    return [root.joinpath(*UMID_REL_PATH) for root in uniq]


def _machine_id(auth_dir):
    candidates = [Path(auth_dir)] if auth_dir else []
    candidates += [d for d in find_auth_dirs() if d not in candidates]
    for root in candidates:
        try:
            value = (root / "auth.machine-id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _run_umid(exe):
    try:
        proc = subprocess.run(
            [str(exe), "--account-stdin"], input=b"", capture_output=True,
            timeout=15, cwd=str(exe.parent),
            creationflags=0x08000000 if IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def risk_headers(auth_dir=None):
    global _risk_cache, _risk_cache_root
    now = time.time()
    if _risk_cache_root == auth_dir and now - _risk_cache[0] < 1800:
        return dict(_risk_cache[1])

    headers = {}
    if IS_WINDOWS:
        arch = {"amd64": "x86_64", "x86_64": "x86_64",
                "aarch64": "aarch64", "arm64": "aarch64"}.get(
                    platform.machine().lower(), "x86_64")
        headers["Cosy-MachineOS"] = "%s_windows" % arch
        headers["Cosy-MachineHostname"] = socket.gethostname()

    exe = next((p for p in _umid_candidates() if p.is_file()), None)
    if exe:
        try:
            manifest = json.loads((exe.parent.parent / "build-manifest.json")
                                  .read_text(encoding="utf-8"))
            version = manifest.get("productVersion")
            if version:
                headers["Cosy-Version"] = str(version)
        except (OSError, ValueError):
            pass
        identity = _run_umid(exe)
        for src, dst in (("machineToken", "Cosy-MachineToken"),
                         ("machineCode", "Cosy-MachineCode"),
                         ("machineType", "Cosy-MachineType")):
            if identity.get(src):
                headers[dst] = str(identity[src])

    mid = _machine_id(auth_dir)
    if mid:
        headers["Cosy-MachineId"] = mid

    _risk_cache = (now, dict(headers))
    _risk_cache_root = auth_dir
    return headers


# ---------------------------------------------------------------- 服务端

class Api:
    def __init__(self, token, base_url, risk=None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.risk = risk or {}

    def _raw(self, path, method):
        url = self.base_url + path
        request = urllib.request.Request(
            url, method=method,
            headers={**BASE_HEADERS, **self.risk, "Authorization": "Bearer %s" % self.token},
            data=b"" if method == "POST" else None,
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read(1 << 20)
                code = resp.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(1 << 20)
            except OSError:
                raw = b""
            code = exc.code
        except (urllib.error.URLError, OSError) as exc:
            return 0, None, str(exc)

        text = raw.decode("utf-8", "replace")
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return code, body, None

    def call(self, path, method="GET", retry=True):
        attempt = 0
        while True:
            code, body, err = self._raw(path, method)
            retryable = code == 0 or code == 429 or 500 <= code < 600
            if not retryable or not retry or attempt >= len(RETRY_DELAYS):
                return code, body, err
            delay = RETRY_DELAYS[attempt]
            if _remaining() <= delay + REQUEST_TIMEOUT:
                return code, body, err
            time.sleep(delay)
            attempt += 1

    def campaigns(self, retry=True):
        return self.call("/sash/api/v1/me/campaigns", retry=retry)

    def claim(self, campaign_id, retry=True):
        return self.call("/sash/api/v1/me/campaigns/%s/claim" % campaign_id,
                         method="POST", retry=retry)


def _remaining():
    return BUDGET_SECONDS - (time.monotonic() - _started_at)


def _campaigns(payload):
    if not isinstance(payload, dict):
        return []
    rows = payload.get("campaigns")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _amount(campaign):
    benefit = campaign.get("benefit")
    if isinstance(benefit, dict):
        amount = benefit.get("amount")
        if isinstance(amount, (int, float)):
            return amount
    return None


def _validity(campaign):
    benefit = campaign.get("benefit")
    validity = benefit.get("validity") if isinstance(benefit, dict) else None
    if isinstance(validity, dict) and validity.get("mode") == "RELATIVE_DAYS":
        days = validity.get("days")
        if days:
            return "领取后 %s 天有效" % days
    return ""


def _fmt_ts(value):
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value, TZ_CN).strftime("%m-%d %H:%M")


def _window(campaign):
    start, end = _fmt_ts(campaign.get("startAt")), _fmt_ts(campaign.get("endAt"))
    if start and end:
        return "%s ~ %s" % (start, end)
    return ""


def _is_active(campaign, now):
    start = campaign.get("startAt")
    end = campaign.get("endAt")
    if isinstance(start, (int, float)) and now < start:
        return False
    if isinstance(end, (int, float)) and now >= end:
        return False
    return True


def select_campaigns(payload, now):
    """把活动列表分成：可领取 / 本窗口已领 / 其它发币活动。"""
    benefit = [c for c in _campaigns(payload) if c.get("actionType") == "CLAIM_BENEFIT"]
    active = [c for c in benefit if _is_active(c, now)]
    todo = [c for c in active if c.get("claimStatus") == "CLAIMABLE"]
    claimed = [c for c in active if c.get("claimStatus") == "CLAIMED"]
    return {"benefit": benefit, "active": active, "todo": todo, "claimed": claimed}


def _credits_text(total):
    return "%s Credits" % total if total is not None else "Credits"


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _short(value, limit=200):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _result(name, report, **extra):
    out = {"result": name, "report": report, "needs_attention": name in FAILURE_RESULTS}
    out.update(extra)
    return out


def _claim_all(api, campaigns, retry):
    total = None
    keys = []
    failures = []
    replayed_any = False
    last_http = None
    for campaign in campaigns:
        key = campaign.get("campaignKey") or campaign.get("campaignId") or "?"
        code, body, err = api.claim(campaign.get("campaignId"), retry=retry)
        last_http = code
        if code == 401:
            return _result("AUTH_REJECTED",
                           "领取时登录态被拒绝（HTTP 401），请打开 Qoder CN 桌面端重新登录",
                           http=code, campaign=key)
        if code == 403:
            return _result("FORBIDDEN",
                           "领取被服务端拒绝（HTTP 403），请确认账号资格后重试",
                           http=code, campaign=key)
        data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) \
            else body
        ok = code == 200 and isinstance(data, dict) and data.get("status") == "CLAIMED"
        if not ok:
            detail = err or body
            failures.append("%s: HTTP %s %s" % (key, code, _short(detail, 120)))
            continue
        amount = _amount(data)
        if amount is None:
            amount = _amount(campaign)
        total = _sum([total, amount])
        keys.append(key)
        if data.get("replayed"):
            replayed_any = True

    if failures:
        return _result("ERROR", "领取失败：%s" % "；".join(failures),
                       http=last_http, campaign=", ".join(keys) or None, credits=total)
    report = "成功领取 %s" % _credits_text(total)
    if keys:
        report += "（%s" % "、".join(keys)
        if replayed_any:
            report += "，幂等重复"
        report += "）"
    valid = _validity(campaigns[0]) if campaigns else ""
    if valid:
        report += "，" + valid
    return _result("CLAIMED", report, http=last_http,
                   campaign=", ".join(keys) or None, credits=total)


def claim_once(api, retry=True, dry_run=False):
    code, payload, err = api.campaigns(retry=retry)
    if code == 401:
        return _result("AUTH_REJECTED",
                       "登录态被服务端拒绝（HTTP 401），请打开 Qoder CN 桌面端重新登录",
                       http=code)
    if code == 403:
        return _result("FORBIDDEN", "查询被服务端拒绝（HTTP 403），请稍后重试", http=code)
    if code == 0:
        return _result("NETWORK", "网络请求失败：%s" % _short(err), http=0)
    if code != 200 or not isinstance(payload, dict):
        return _result("ERROR", "查询活动失败：HTTP %s %s" % (code, _short(payload)),
                       http=code)

    picked = select_campaigns(payload, time.time())
    todo, claimed = picked["todo"], picked["claimed"]
    if todo:
        amount = _sum(_amount(c) for c in todo)
        keys = "、".join(c.get("campaignKey") or "?" for c in todo)
        if dry_run:
            return _result("DRY_RUN",
                           "[dry-run] 可领取 %s（%s）" % (_credits_text(amount), keys),
                           http=code, campaign=keys, credits=amount)
        return _claim_all(api, todo, retry=retry)

    if claimed:
        amount = _sum(_amount(c) for c in claimed)
        keys = "、".join(c.get("campaignKey") or "?" for c in claimed)
        report = "本周期已领取 %s（%s" % (_credits_text(amount), keys)
        window = _window(claimed[0])
        if window:
            report += "，窗口 %s" % window
        report += "）"
        return _result("ALREADY", report, http=code, campaign=keys, credits=amount,
                       window=window or None)

    if picked["active"]:
        rows = []
        for campaign in picked["active"]:
            rows.append("%s status=%s" % (campaign.get("campaignKey") or "?",
                                          campaign.get("claimStatus") or "?"))
        return _result("PENDING", "活动存在但当前不可领取：%s" % "；".join(rows),
                       http=code)

    return _result("INACTIVE", "当前没有可领取的 Credits 活动（活动可能已结束）", http=code)


# ---------------------------------------------------------------- 输出

def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _redact(payload):
    for secret in _sensitive_values:
        payload = payload.replace(secret, "[REDACTED]")
    return payload


def emit(out, action, quiet=False):
    payload = _redact(json.dumps(out, ensure_ascii=False, default=str))
    silent = str(action).startswith("silent")
    if not silent and sys.stdout is not None:
        try:
            print(payload)
            return
        except Exception:
            pass
    if quiet and not _env_flag("QODER_SIGNIN_LOG_EMPTY"):
        return

    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payload)
    default_log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "qoder_signin.log")
    for path in (os.environ.get("QODER_SIGNIN_LOG") or default_log, default_log):
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
            return
        except Exception:
            continue
    try:
        print(payload)
    except Exception:
        pass


def _resolve_token():
    token = (os.environ.get("QODER_TOKEN") or "").strip()
    if token:
        return token, {"source": "env", "expiresAt": None, "_auth_dir": None}
    sess = load_session()
    token = sess.get("token")
    _sensitive_values.add(token)
    return token, sess


def _auth_failure(exc, action):
    if isinstance(exc, NoAuthError):
        out = _result("NO_AUTH", str(exc), looked_in=exc.looked_in or None)
        emit(out, action)
        return 2
    out = _result("AUTH_ERROR", str(exc), looked_in=exc.looked_in or None)
    emit(out, action)
    return 1


def run_doctor():
    token = (os.environ.get("QODER_TOKEN") or "").strip()
    out = {"result": "OK", "version": VERSION, "platform": sys.platform,
           "token_source": "env" if token else "desktop", "needs_attention": False}
    if token:
        _sensitive_values.add(token)
        out["report"] = "使用环境变量 QODER_TOKEN（无法离线校验有效期）"
        return out

    try:
        sess = load_session()
    except NoAuthError as exc:
        out.update(_result("NO_AUTH", str(exc), looked_in=exc.looked_in or None))
        return out
    except QoderAuthError as exc:
        out.update(_result("AUTH_ERROR", str(exc), looked_in=exc.looked_in or None))
        return out

    token = sess["token"]
    _sensitive_values.add(token)
    expires = _parse_iso(sess.get("expiresAt"))
    out.update({
        "auth_dir": sess.get("_auth_dir"),
        "token_expires_at": sess.get("expiresAt"),
        "decrypted": True,
    })
    exe = next((p for p in _umid_candidates() if p.is_file()), None)
    out["device_component"] = str(exe) if exe else None
    out["device_identity"] = bool(risk_headers(sess.get("_auth_dir"))
                                  .get("Cosy-MachineToken"))

    if _token_expired(sess):
        out.update(_result("AUTH_REJECTED",
                           "登录态已于 %s 过期，请打开 Qoder CN 桌面端重新登录"
                           % sess.get("expiresAt")))
        return out

    report = "登录态可读"
    if expires:
        report += "，有效期至 %s" % expires.astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M")
    if not out["device_identity"]:
        report += "；未取到设备标识，查询仍会尝试（通常不影响）"
    out["report"] = report
    return out


def run_status(api):
    code, payload, err = api.campaigns(retry=True)
    if code == 401:
        return _result("AUTH_REJECTED",
                       "登录态被服务端拒绝（HTTP 401），请打开 Qoder CN 桌面端重新登录",
                       http=code), 1
    if code == 403:
        return _result("FORBIDDEN", "查询被服务端拒绝（HTTP 403）", http=code), 1
    if code == 0:
        return _result("NETWORK", "网络请求失败：%s" % _short(err), http=0), 1
    if code != 200 or not isinstance(payload, dict):
        return _result("ERROR", "查询活动失败：HTTP %s %s" % (code, _short(payload)),
                       http=code), 1

    rows = []
    now = time.time()
    for campaign in _campaigns(payload):
        rows.append({
            "campaign": campaign.get("campaignKey"),
            "action": campaign.get("actionType"),
            "status": campaign.get("claimStatus"),
            "credits": _amount(campaign),
            "active": _is_active(campaign, now),
            "window": _window(campaign),
        })
    out = _result("OK", "共 %s 个活动" % len(rows), http=code, campaigns=rows,
                  show_campaign=payload.get("showCampaign"),
                  claimable=payload.get("claimable"))
    return out, 0


def run(action, dry_run=False, once=False, base_url=None):
    if action not in ("auto", "silent", "status", "doctor"):
        out = _result("ERROR", "未知命令：%s（可用：auto / silent / status / doctor）" % action)
        emit(out, action)
        return 2

    if action == "doctor":
        out = run_doctor()
        emit(out, action)
        if out.get("result") == "OK":
            return 0
        return 2 if out.get("result") == "NO_AUTH" else 1

    try:
        token, sess = _resolve_token()
    except QoderAuthError as exc:
        return _auth_failure(exc, action)

    if _token_expired(sess):
        out = _result("AUTH_REJECTED",
                      "登录态已于 %s 过期，请打开 Qoder CN 桌面端重新登录"
                      % sess.get("expiresAt"), token_expires_at=sess.get("expiresAt"))
        emit(out, action)
        return 1

    api = Api(token, base_url or os.environ.get("QODER_BASE_URL") or DEFAULT_BASE_URL,
              risk_headers(sess.get("_auth_dir")))

    if action == "status":
        out, code = run_status(api)
        emit(out, action)
        return code

    out = claim_once(api, retry=not once, dry_run=dry_run)
    if sess.get("expiresAt"):
        out["token_expires_at"] = sess.get("expiresAt")
    quiet = out.get("result") in QUIET_RESULTS and not out.get("needs_attention")
    emit(out, action, quiet=quiet)
    return 0 if out.get("result") in ("CLAIMED", "ALREADY", "INACTIVE", "DRY_RUN") else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    once = "--once" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--once")]
    action = argv[0] if argv else "auto"
    try:
        return run(action, dry_run=dry_run, once=once)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit({"result": "ERROR",
              "report": "脚本运行异常（%s）" % type(exc).__name__,
              "needs_attention": True}, action)
        return 2


if __name__ == "__main__":
    sys.exit(main())
