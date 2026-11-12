"""WorkBuddy 每日签到自动领取脚本。

作者：88lin
仓库：https://github.com/88lin/workbuddy-auto-signin
协议：MIT

签到接口系从桌面端逆向所得，服务端改一版就可能失效——修复都会推到上面这个仓库。
顺手点个 ⭐ Star，等哪天连签莫名其妙断了，你能一秒把它找回来。

读取本机 WorkBuddy 桌面端的登录会话，调用其签到接口自动领取每日积分：
  POST {endpoint}/v2/billing/meter/checkin-activity-status  查询签到状态
  POST {endpoint}/v2/billing/meter/daily-checkin            领取今日积分

响应契约：
  - 领取成功 : 含 credit 字段，如 {"credit": 100}
  - 今日已签 : null 或 HTTP 400 + {"code":10001,"msg":"今天已签到，请明天再来"}
               幂等，两种形态都按"已签"处理，不计失败
  - 业务错误 : {"code": ..., "msg": ...}
  - 认证拒绝 : HTTP 401；HTTP 403 单独作为权限/业务拒绝处理

凭据文件由桌面端登录后自动写入；脚本按平台自动探测，或用环境变量
WORKBUDDY_AUTH_FILE 指定。任何模式下都不会打印令牌，可安全分享。

用法：
  python signin.py auto           # 每日自动化：签到 + 成长中心（礼物/任务/补登/兑换/抽奖/Buddy）
  python signin.py silent         # 同 auto，但结果写日志文件而非 stdout（配合 pythonw.exe 静默运行）
  python signin.py growth         # 仅成长中心（不签到）
  python signin.py silent-poll    # 轮询：补签（未签才签）+ 成长中心，空跑不写日志
  python signin.py silent-growth  # silent-poll 的旧名，行为完全相同（老计划任务仍可用）
  python signin.py doctor         # 离线检查凭据格式与运行时能力，不解密、不联网
  python signin.py status         # 仅查签到状态（调试）
  python signin.py claim          # 仅领取签到（调试，幂等）
  python signin.py all            # 查签到状态 + 领取（调试）

多账号（一个账号跑完自动切下一个）：
  python signin.py bind work      # 把客户端当前登录的账号绑定为 work（登录另一个账号后可再 bind 一个）
  python signin.py accounts       # 查看已绑定账号、当前账号与各自最近一次结果
  python signin.py use work       # 切换当前账号（轮转时排在最前）
  python signin.py unbind work    # 解绑（同时删除该账号的凭据副本）
  python signin.py auto --account work      # 只跑 work
  python signin.py auto --account current   # 只跑客户端当前登录账号（忽略账号清单）

绑定了多个账号后，auto / silent / growth / silent-poll / silent-growth 会按"一个账号跑完
再切下一个"的顺序依次处理全部账号，任一账号失败都不会中断其余账号。桌面端同一时间只能
登录一个账号，所以每个账号的凭据是在 bind 时做的快照；要更新某个账号的凭据（重新登录后），
再执行一次 bind <名字> --force。
"""

import base64
import json
import math
import ntpath
import os
import plistlib
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from contextlib import closing
from datetime import date, datetime

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")
# Linux 没有桌面端，入口是 CodeBuddy CLI；它写出的凭据文件名不同、放在 XDG 数据目录
# （issue #4 实测：~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info，
#  JSON 结构与桌面端一致，签到/成长中心接口全部照常工作）
CLI_AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth",
                                 "Tencent-Cloud.coding-copilot.info")

# 伪 HTTP 码：区分"没拿到响应"的两种原因
CODE_NO_NETWORK = -1   # 连不上/超时
CODE_BUDGET_OUT = -2   # 本次运行的时间预算已耗尽，主动放弃后续请求

# 计划任务跑满 ExecutionTimeLimit 会被系统直接杀掉，届时 emit 还没执行，当天日志整条
# 丢失。这里自设更小的预算，确保总能走到写日志那一步。两个任务的时限不同，上限也必须
# 分开算：签到任务 PT10M、轮询任务 PT5M，各留 60s 给解释器启动和收尾。
# 这四个常量与 install-windows.ps1 里的 ExecutionTimeLimit 一一对应，改一处就要改另一处。
DEFAULT_BUDGET_SECONDS = 420.0
MAX_BUDGET_SECONDS = 540.0          # 签到任务 PT10M = 600s - 60s
# 轮询任务如今也负责补签（见 run_daily），预算比"只跑成长中心"时期宽一些，好让
# 冷启动重试跑得完；但仍远小于 PT5M，跑不完就早收尾、四小时后再来。
POLL_BUDGET_SECONDS = 180.0
POLL_MAX_BUDGET_SECONDS = 240.0     # 轮询任务 PT5M = 300s - 60s
# 每轮最多用掉一张补登卡；仅依据服务端活跃日历中明确的当月断登日期操作。
MAKEUP_MAX_PER_RUN = 1
REQUEST_TIMEOUT = 30
# 网络类失败的退避节奏（秒）。定时任务最容易撞上的就是"刚开机/刚唤醒"：WiFi 重连、
# DHCP 续租、VPN 拨通往往要几十秒，而原来的策略是"5 秒后再试一次"——两次都撞在同
# 一堵墙上，420 秒预算只花掉 5 秒就判了当天死刑。退避到分钟级才真正跨得过这个窗口：
# 最多 6 次尝试摊开约 3.5 分钟，仍在签到任务的预算内。实际跑几轮由剩余预算决定
# （见 _request_with_retry 的守卫），轮询任务预算短，会自动少跑几轮。
NETWORK_RETRY_DELAYS = (5, 15, 30, 60, 90)
# 5xx 是服务端抖动，不是本机网络没就绪，短促重试即可——干等几分钟既救不了它，
# 还会把预算耗光，让后面的成长中心一个都跑不成。
SERVER_RETRY_DELAYS = (3, 10)
_started_at = None
_budget_seconds = DEFAULT_BUDGET_SECONDS
_config_warning = None   # 配置非法时的告警，由 emit 统一带进输出

# 轮询类命令：预算更短，且空跑不落盘。silent-growth 是 silent-poll 的旧名，
# 已安装的计划任务还在用它，必须继续认。
POLL_ACTIONS = ("silent-poll", "silent-growth")

# ---- 多账号 ----
# 账号清单与凭据副本默认放在脚本同目录的 accounts/ 下（可用 WORKBUDDY_ACCOUNTS_DIR 覆盖）。
# 桌面端同一时刻只保存一个账号的令牌，所以每个账号的凭据是 bind 时的快照；脚本按快照
# 依次请求，不需要真的在客户端里来回切登录。
ACCOUNTS_DIRNAME = "accounts"
ACCOUNTS_REGISTRY_NAME = "accounts.json"
ACCOUNTS_VERSION = 1
# 账号名会用于生成凭据副本文件名，只放行"中英文/数字/点/下划线/短横线"，且首字符不能是
# 符号：这样 "."、".."、"-x"（会被当成命令行参数）这类名字在入口就被挡掉。
ACCOUNT_NAME_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff._-]{0,31}")
# 剩余预算不足以再跑完一个账号时，宁可把它留给下一轮，也不要跑到一半被任务时限强杀。
ACCOUNT_MIN_SLICE = 30.0

AUTH_HELPER_TIMEOUT = 10.0
AUTH_INPUT_LIMIT = 65536
AUTH_OUTPUT_LIMIT = 65536
TOKEN_LIMIT = 32768
_sensitive_values = set()
AUTH_REASONS = {
    "INVALID_FORMAT": "登录凭据格式无效，请检查凭据来源和客户端版本",
    "UNSUPPORTED_ENVELOPE": "尚不支持此加密凭据格式，请更新脚本；重新登录不会改变加密格式",
    "RUNTIME_NOT_FOUND": "未找到 WorkBuddy 客户端，请用 WORKBUDDY_EXE 指定其可执行文件",
    "INVALID_RUNTIME_PATH": "WORKBUDDY_EXE 不是可用的可执行文件，请核对路径",
    "RUNTIME_UNAVAILABLE": "客户端运行时不支持所需的原生存储接口，请检查客户端和脚本版本",
    "KEY_MISMATCH": "凭据与所选客户端的密钥不匹配，请用 WORKBUDDY_EXE 指定对应客户端",
    "DECRYPT_FAILED": "加密凭据认证失败，请检查客户端版本及凭据是否完整",
    "HELPER_TIMEOUT": "凭据处理超时，已停止子进程，请稍后重试",
    "HELPER_PROTOCOL": "客户端凭据助手返回无效结果，请检查客户端和脚本版本",
}

# Fixed code only in argv; the encrypted field travels over stdin. Build material
# never leaves this process. Only sym-v1 / field / suite 1 is supported.
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const token = utf8(plaintext);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      failure('INVALID_FORMAT');
    return token;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, accessToken: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


class AuthError(Exception):
    """Only fixed, non-sensitive messages may cross the credential boundary."""

    def __init__(self, reason, result="AUTH_ERROR"):
        self.reason = reason
        self.result = result
        super().__init__(AUTH_REASONS.get(reason, "本地未找到有效登录会话，请先登录客户端"))

    def output(self):
        return {"result": self.result, "reason": self.reason,
                "report": str(self), "needs_attention": True}


def _valid_token(token):
    return (isinstance(token, str) and 0 < len(token) <= TOKEN_LIMIT
            and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is not None)


def _validate_session(session):
    if not isinstance(session, dict):
        raise AuthError("INVALID_FORMAT")
    for field in ("auth", "account"):
        if session.get(field) is None:
            raise AuthError("MISSING_SESSION", "NO_SESSION")
        if not isinstance(session[field], dict):
            raise AuthError("INVALID_FORMAT")
    if session["auth"].get("accessToken") in (None, "") or session["account"].get("uid") in (None, ""):
        raise AuthError("MISSING_SESSION", "NO_SESSION")
    for source, fields in ((session["account"], ("uid", "enterpriseId")),
                           (session["auth"], ("domain",))):
        for field in fields:
            value = source.get(field)
            if value not in (None, "") and (not isinstance(value, str) or
                                      re.fullmatch(r"[\x21-\x7e]{1,2048}", value) is None):
                raise AuthError("INVALID_FORMAT")
    endpoint = session["auth"].get("endpoint")
    if endpoint in (None, ""):
        endpoint = DEFAULT_ENDPOINT
    if not isinstance(endpoint, str) or re.search(r"[\s\x00-\x1f\x7f]", endpoint):
        raise AuthError("INVALID_FORMAT")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        parsed.port  # Validate an explicitly supplied port without logging the URL.
    except ValueError:
        raise AuthError("INVALID_FORMAT") from None


def _token_format(value):
    if isinstance(value, str):
        if not _valid_token(value):
            raise AuthError("INVALID_FORMAT")
        return "plaintext"
    if not isinstance(value, dict):
        raise AuthError("INVALID_FORMAT")
    if (set(value) != {"$wbEncrypted", "envelope"} or
            type(value.get("$wbEncrypted")) is not int or value["$wbEncrypted"] != 1):
        raise AuthError("UNSUPPORTED_ENVELOPE")
    try:
        encoded = value["envelope"]
        if not isinstance(encoded, str) or not 0 < len(encoded) <= AUTH_INPUT_LIMIT - 1024:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise ValueError()
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or type(envelope.get("suite")) is not int:
            raise ValueError()
        if envelope["suite"] != 1:
            raise AuthError("UNSUPPORTED_ENVELOPE")
        if set(envelope) != {"suite", "keyId", "nonce", "authTag", "ciphertext"}:
            raise ValueError()
        if not isinstance(envelope["keyId"], str) or not re.fullmatch(r"[0-9a-f]{16}", envelope["keyId"]):
            raise ValueError()
        for field, length in (("nonce", 12), ("authTag", 16), ("ciphertext", None)):
            data = envelope[field]
            if not isinstance(data, str):
                raise ValueError()
            decoded = base64.b64decode(data, validate=True)
            if base64.b64encode(decoded).decode("ascii") != data or (length is not None and len(decoded) != length):
                raise ValueError()
        return "sym-v1"
    except (ValueError, TypeError, KeyError):
        raise AuthError("INVALID_FORMAT") from None


def _mac_runtime(bundle):
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _registry_text(registry, key, field):
    """Read only string registry values; expand REG_EXPAND_SZ using Windows."""
    try:
        value, kind = registry.QueryValueEx(key, field)
        if not isinstance(value, str) or kind not in (registry.REG_SZ, registry.REG_EXPAND_SZ):
            return None
        if kind == registry.REG_EXPAND_SZ:
            value = registry.ExpandEnvironmentStrings(value)
        return value
    except OSError:
        return None


def _registry_runtime_path(value, icon=False):
    """Parse a directory or DisplayIcon; never interpret an uninstall command."""
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    value = value.strip()
    if value.startswith('"'):
        suffix = r"(?:\s*,\s*-?\d+)?" if icon else ""
        match = re.fullmatch(r'"([^"]+)"' + suffix, value)
        if not match:
            return None
        value = match[1]
    elif '"' in value:
        return None
    elif icon:
        # Only remove the trailing resource index; commas can belong to folders.
        value = re.sub(r",\s*-?\d+$", "", value).rstrip()
    if not ntpath.splitdrive(value)[0] or not ntpath.isabs(value):
        return None
    if not icon:
        value = ntpath.join(value, "WorkBuddy.exe")
    value = ntpath.normpath(value)
    if ntpath.basename(value).casefold() != "workbuddy.exe":
        return None
    return value


def _windows_registry_runtimes():
    """Yield existing clients from user/machine uninstall entries, lazily.

    Keep the import and all platform-specific constants inside this Windows-only
    fallback so importing the script on macOS/Linux needs no winreg module.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    seen = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for index in range(count):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index),
                                                0, winreg.KEY_READ | view) as entry:
                                name = _registry_text(winreg, entry, "DisplayName")
                                if not name or not re.fullmatch(
                                        r"WorkBuddy(?:\s+\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)?",
                                        name.strip(), re.IGNORECASE):
                                    continue
                                # A stale InstallLocation must not mask a valid DisplayIcon.
                                for field in ("InstallLocation", "DisplayIcon"):
                                    path = _registry_runtime_path(
                                        _registry_text(winreg, entry, field), icon=field == "DisplayIcon")
                                    if path and ntpath.normcase(path) not in seen:
                                        seen.add(ntpath.normcase(path))
                                        if os.path.isfile(path):
                                            yield path
                        except OSError:
                            # Deleted entries and access-denied subkeys are normal.
                            continue
            except OSError:
                continue


def find_workbuddy_runtime():
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or (os.name != "nt" and not os.access(path, os.X_OK)):
            raise AuthError("INVALID_RUNTIME_PATH")
        return path
    home = os.path.expanduser("~")
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.append(os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"))
        for name in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(name):
                candidates.append(os.path.join(os.environ[name], "WorkBuddy", "WorkBuddy.exe"))
    elif sys.platform == "darwin":
        candidates = [_mac_runtime(os.path.join(root, "WorkBuddy.app"))
                      for root in ("/Applications", os.path.join(home, "Applications"))]
    for path in candidates:
        if path and os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK)):
            return os.path.abspath(path)
    if sys.platform == "win32":
        with closing(_windows_registry_runtimes()) as entries:
            registered = next(entries, None)
        if registered:
            return registered
    raise AuthError("RUNTIME_NOT_FOUND")


def _run_auth_helper(exe, request):
    """Bound every pipe and deadline; never include captured data in exceptions."""
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthError("INVALID_FORMAT")
    timeout = min(AUTH_HELPER_TIMEOUT, _budget_left())
    if timeout <= 0:
        raise AuthError("HELPER_TIMEOUT")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        process = subprocess.Popen([exe, "-e", AUTH_HELPER_JS], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise AuthError("RUNTIME_UNAVAILABLE") from None
    output = {}
    failed = threading.Event()

    def read_pipe(name, pipe, limit):
        try:
            captured = pipe.read(limit + 1)
            if len(captured) > limit:
                failed.set()
                process.kill()
            else:
                output[name] = captured
        except OSError:
            failed.set()

    def write_pipe():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError:
            failed.set()

    workers = [threading.Thread(target=read_pipe, args=("stdout", process.stdout, AUTH_OUTPUT_LIMIT), daemon=True),
               threading.Thread(target=read_pipe, args=("stderr", process.stderr, 8192), daemon=True),
               threading.Thread(target=write_pipe, daemon=True)]
    deadline = time.monotonic() + timeout
    try:
        for worker in workers:
            worker.start()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AuthError("HELPER_TIMEOUT") from None
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise AuthError("HELPER_TIMEOUT")
        if failed.is_set():
            raise AuthError("HELPER_PROTOCOL")
        try:
            reply = json.loads(output.get("stdout", b"").decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AuthError("HELPER_PROTOCOL") from None
        if not isinstance(reply, dict) or type(reply.get("version")) is not int or reply["version"] != 1:
            raise AuthError("HELPER_PROTOCOL")
        if reply.get("ok") is False and process.returncode == 1:
            reason = reply.get("reason")
            raise AuthError(reason if isinstance(reason, str) and reason in AUTH_REASONS else "HELPER_PROTOCOL")
        if reply.get("ok") is not True or process.returncode != 0:
            raise AuthError("HELPER_PROTOCOL")
        if request["operation"] == "decrypt":
            if set(reply) != {"version", "ok", "accessToken"} or not _valid_token(reply.get("accessToken")):
                raise AuthError("HELPER_PROTOCOL")
        elif (set(reply) != {"version", "ok", "electron"} or not isinstance(reply.get("electron"), str)
              or re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", reply["electron"]) is None):
            raise AuthError("HELPER_PROTOCOL")
        return reply
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for worker in workers:
            if worker.ident is not None:
                worker.join(1)
        # Avoid blocking on a pipe held by an incompatible runtime's descendant.
        if not any(worker.is_alive() for worker in workers):
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def resolve_session(session):
    _validate_session(session)
    token = session["auth"]["accessToken"]
    if _token_format(token) != "plaintext":
        token = _run_auth_helper(find_workbuddy_runtime(), {"operation": "decrypt", "value": token})["accessToken"]
    _sensitive_values.add(token)
    return dict(session, auth=dict(session["auth"], accessToken=token))


def run_doctor(session):
    _validate_session(session)
    kind = _token_format(session["auth"]["accessToken"])
    out = {"result": "AUTH_READY", "credential_format": kind, "needs_attention": False,
           "report": "本地凭据格式有效；未验证服务端登录状态", "online_checked": False}
    if kind == "sym-v1":
        runtime = find_workbuddy_runtime()
        probe = _run_auth_helper(runtime, {"operation": "probe"})
        out.update(runtime=runtime, electron_version=probe["electron"],
                   report="已识别加密凭据，运行时具备解密能力；未提取密钥、未验证解密或服务端登录状态")
    return out


def _auth_failure(code):
    if code == 401:
        return {"result": "AUTH_REJECTED", "http": code, "needs_attention": True,
                "report": "服务端拒绝认证（HTTP 401），请检查客户端登录状态、凭据对应的服务地址及脚本版本"}
    return {"result": "FORBIDDEN", "http": code, "needs_attention": True,
            "report": "服务端拒绝此操作（HTTP 403），请检查账号权限或活动条件；不能据此判断登录过期"}


def _parse_budget(default=DEFAULT_BUDGET_SECONDS, maximum=MAX_BUDGET_SECONDS):
    """解析预算环境变量。非法值/越界值一律夹到安全区间——绝不能在这里抛异常。

    这段逻辑曾写在模块顶层，WORKBUDDY_BUDGET_SECONDS=abc 会让进程在 main() 的
    try/except 生效之前就崩掉，silent 模式下当天日志整条为空。

    上限按调用方传入的 maximum 算：轮询任务的 ExecutionTimeLimit 比签到任务短，
    共用一个 540s 的上限等于对轮询任务没有上限，跑穿一样会被杀在写日志之前。
    """
    raw = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
    if not raw:
        return default, None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default, "WORKBUDDY_BUDGET_SECONDS=%r 不是数字，已回落 %s 秒" % (
            raw, int(default))
    if val <= 0:
        # "0" 是非空字符串，用 `or` 兜不住；且 0 会让每个请求都直接放弃，脚本永久失效
        return default, "WORKBUDDY_BUDGET_SECONDS=%s 必须为正数，已回落 %s 秒" % (
            raw, int(default))
    if val > maximum:
        # 上限同样是硬要求：预算大于任务时限就等于没有预算，黑洞式超时会把进程跑到被强杀
        return maximum, ("WORKBUDDY_BUDGET_SECONDS=%s 超过本命令上限，已夹到 %s 秒"
                         "（须小于该定时任务的 ExecutionTimeLimit）") % (
            raw, int(maximum))
    return val, None


def _start_budget(action=None, accounts=1):
    """启动预算时钟；配置非法时记下告警，由 emit 带进输出而不是静默回落。

    轮询类命令（silent-poll / silent-growth）用更短的默认值和更低的上限，用户显式设置
    环境变量时仍以环境变量为准，但同样夹在该命令自己的上限内。

    accounts > 1 时把默认值抬到"每个账号至少 ACCOUNT_MIN_SLICE 秒"，否则 6 个账号
    分 180 秒每人才 30 秒，连一次冷启动重试都跑不完。上限仍是任务时限，不会被抬穿。
    """
    global _started_at, _budget_seconds, _config_warning
    if action in POLL_ACTIONS:
        default, maximum = POLL_BUDGET_SECONDS, POLL_MAX_BUDGET_SECONDS
    else:
        default, maximum = DEFAULT_BUDGET_SECONDS, MAX_BUDGET_SECONDS
    count = max(1, int(accounts or 1))
    if count > 1:
        default = max(default, min(maximum, ACCOUNT_MIN_SLICE * count))
    _budget_seconds, _config_warning = _parse_budget(default, maximum)
    _started_at = time.monotonic()


def _budget_left():
    """本次运行还剩多少秒可用于网络请求。"""
    if _started_at is None:
        return _budget_seconds
    return _budget_seconds - (time.monotonic() - _started_at)


def _budget_snapshot():
    return _started_at, _budget_seconds


def _budget_restore(snapshot):
    global _started_at, _budget_seconds
    _started_at, _budget_seconds = snapshot


def _budget_slice(seconds):
    """把当前预算换成"某个账号的时间片"，退出时由 _budget_restore 还原总预算。

    多账号轮转必须在同一份总预算内切分：每个账号看到的 _budget_left() 是它自己的
    时间片，这样内部所有"预算耗尽就跳过"的判断无需改动，而账号之间不会互相透支。
    """
    global _started_at, _budget_seconds
    _started_at = time.monotonic()
    _budget_seconds = max(0.0, float(seconds))


def find_auth_file():
    """按平台探测 WorkBuddy 桌面端写出的登录凭据文件，支持环境变量覆盖。

    返回 (path_or_None, looked_in) —— looked_in 始终是脚本实际检查过的路径列表，
    供 NO_AUTH 报错时显示，避免与代码实现漂移。
    """
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        # 环境变量优先，但文件不存在时单独报告，不给无关建议
        return (override if os.path.exists(override) else None), [override]
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    candidates = [
        os.path.join(local, AUTH_BASENAME),                                  # Windows 桌面端
        os.path.join(home, "Library", "Application Support", AUTH_BASENAME),  # macOS 桌面端
        os.path.join(xdg_data, CLI_AUTH_BASENAME),                           # Linux CodeBuddy CLI
        os.path.join(home, ".config", AUTH_BASENAME),                        # Linux（旧猜测，保留）
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),  # 兜底
    ]
    for c in candidates:
        if os.path.exists(c):
            return c, candidates
    return None, candidates


def read_credential_bytes(auth_file, attempts=3, delay=2.0):
    """读取凭据文件原始字节（bind 要原样复制，不能边读边解析）。

    WorkBuddy 客户端刷新 token 时会短暂独占凭据文件，恰好在那一刻读取就是
    PermissionError（实测：定时任务两次撞锁、整轮直接放弃）。锁是瞬时的，
    等两秒再试即可；重试仍失败才真正报错。
    """
    last = None
    for i in range(attempts):
        try:
            with open(auth_file, "rb") as f:
                return f.read()
        except PermissionError as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last


def load_session(auth_file):
    with open(auth_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_session_retry(auth_file, attempts=3, delay=2.0):
    """带重试的凭据读取；重试策略与 read_credential_bytes 一致。"""
    last = None
    for i in range(attempts):
        try:
            return load_session(auth_file)
        except PermissionError as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last


# ---------------------------------------------------------------------------
# 多账号：账号清单、凭据快照与轮转
# ---------------------------------------------------------------------------
# 桌面端同一时刻只保存一个账号的令牌，切换账号必须重新登录。所以每个账号的凭据是在
# bind 时做的一份快照（原样复制的 *.info，令牌仍是客户端加密信封，明文从不落盘）。
# 定时任务直接按快照依次请求，不需要、也不会去动客户端当前登录的账号。

class AccountsError(Exception):
    """账号管理相关的固定错误；消息里绝不带令牌。"""

    def __init__(self, result, report, **extra):
        self.result = result
        self.report = report
        self.extra = extra
        super().__init__(report)

    def output(self):
        out = {"result": self.result, "report": self.report, "needs_attention": True}
        out.update(self.extra)
        return out


def accounts_dir():
    """账号清单目录：WORKBUDDY_ACCOUNTS_DIR 优先，否则脚本同目录的 accounts/。"""
    override = (os.environ.get("WORKBUDDY_ACCOUNTS_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ACCOUNTS_DIRNAME)


def _registry_path():
    return os.path.join(accounts_dir(), ACCOUNTS_REGISTRY_NAME)


def _empty_registry():
    return {"version": ACCOUNTS_VERSION, "active": None, "accounts": []}


def _valid_account_name(name):
    return isinstance(name, str) and ACCOUNT_NAME_RE.fullmatch(name) is not None


def _atomic_write(path, data, mode=0o600):
    """原子写文件：先写同目录临时文件再 os.replace，避免中断留下半截 JSON。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)   # Windows 上基本无效；POSIX 上确保 0600
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _account_entry(item):
    """校验清单里的单条记录；任何结构问题都当作清单损坏，绝不猜。"""
    if not isinstance(item, dict):
        raise AccountsError("ERROR", "账号清单格式无效（记录不是对象）")
    name = item.get("name")
    if not _valid_account_name(name):
        raise AccountsError("ERROR", "账号清单里有无效的账号名：%r" % (name,))
    credential = item.get("credential")
    if (not isinstance(credential, str) or not credential
            or os.path.basename(credential) != credential or credential in (".", "..")
            or "\\" in credential or ":" in credential):
        raise AccountsError("ERROR", "账号「%s」的凭据文件名无效" % name)

    def text(field, limit):
        value = item.get(field)
        if value in (None, ""):
            return ""
        if (not isinstance(value, str) or len(value) > limit
                or re.search(r"[\x00-\x1f\x7f]", value)):
            raise AccountsError("ERROR", "账号「%s」的字段 %s 无效" % (name, field))
        return value

    return {"name": name, "uid": text("uid", 256), "endpoint": text("endpoint", 2048),
            "credential": credential, "bound_at": text("bound_at", 64),
            "last_run_at": text("last_run_at", 64), "last_result": text("last_result", 64),
            "last_code": as_int(item.get("last_code"), 0)}


def _registry_from(data):
    if not isinstance(data, dict):
        raise AccountsError("ERROR", "账号清单格式无效（不是 JSON 对象）")
    version = data.get("version")
    if type(version) is not int or version != ACCOUNTS_VERSION:
        raise AccountsError("ERROR", "账号清单版本不受支持（%r），请删除清单后重新 bind"
                            % (version,))
    raw = data.get("accounts", [])
    if not isinstance(raw, list):
        raise AccountsError("ERROR", "账号清单格式无效（accounts 不是数组）")
    entries = []
    names = set()
    for item in raw:
        entry = _account_entry(item)
        if entry["name"] in names:
            raise AccountsError("ERROR", "账号清单里有重复的账号名「%s」" % entry["name"])
        names.add(entry["name"])
        entries.append(entry)
    active = data.get("active")
    if active not in names:
        active = entries[0]["name"] if entries else None
    return {"version": ACCOUNTS_VERSION, "active": active, "accounts": entries}


def load_registry():
    """读账号清单；文件不存在视为"还没绑定账号"，内容损坏则明确报错。"""
    path = _registry_path()
    if not os.path.exists(path):
        return _empty_registry()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise AccountsError("ERROR", "账号清单读取失败（%s: %s），请修复或删除 %s"
                            % (type(e).__name__, e, path)) from None
    return _registry_from(data)


def save_registry(registry):
    _atomic_write(_registry_path(),
                  (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def credential_path(entry):
    return os.path.join(accounts_dir(), entry["credential"])


def find_account(name):
    registry = load_registry()
    for entry in registry["accounts"]:
        if entry["name"] == name:
            return entry
    raise AccountsError("ERROR", "未绑定账号「%s」；用 signin.py accounts 查看已绑定账号" % name)


def _uid_hint(uid):
    """对外展示只给 uid 前缀：够区分账号，又不把完整身份标识写进日志。"""
    return (uid[:8] + "…") if uid else ""


def order_accounts(registry):
    """轮转顺序：当前账号排最前，其余按"上次运行时间"从早到晚，保证每个账号都轮到。

    last_run_at 是 ISO 时间串，同一格式下字典序即时间序；没跑过的排最前。
    """
    accounts = list(registry["accounts"])
    active = registry.get("active")
    rest = [a for a in accounts if a["name"] != active]
    rest.sort(key=lambda a: (a.get("last_run_at") or "", a["name"]))
    return [a for a in accounts if a["name"] == active] + rest


def _expires_text(value):
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(stamp) or stamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(stamp / 1000.0).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _session_expired(session):
    """凭据快照是否已过期；过期返回展示用时间串，未过期/无法判断返回 None。

    快照里的 expiresAt 是客户端上次刷新时写下的，过了这个时间令牌必然失效，
    提前判掉可以省掉一次注定 401 的请求，也能把"该重新登录哪个账号"说清楚。
    """
    auth = session.get("auth") if isinstance(session, dict) else None
    if not isinstance(auth, dict):
        return None
    text = _expires_text(auth.get("expiresAt"))
    try:
        expires = float(auth.get("expiresAt"))
    except (TypeError, ValueError, OverflowError):
        return None
    if text and math.isfinite(expires) and expires <= time.time() * 1000:
        return text
    return None


def bind_account(name, source=None, force=False):
    """把一份登录凭据绑定为账号快照；返回可汇报的摘要（绝不含令牌）。"""
    if not _valid_account_name(name):
        raise AccountsError("ERROR", "账号名无效：只能用中英文、数字、点、下划线、短横线，"
                                     "1-32 个字符，且不能以符号开头")
    path = source or find_auth_file()[0]
    if not path:
        raise AccountsError("NO_AUTH", "未找到当前登录凭据，请先登录 WorkBuddy 桌面端，"
                                       "或用 --from 指定凭据文件")
    if not os.path.isfile(path):
        raise AccountsError("NO_AUTH", "凭据文件不存在：%s" % path)
    try:
        raw = read_credential_bytes(path)
    except OSError as e:
        raise AccountsError("ERROR", "读取凭据文件失败（%s），请确认客户端没有独占锁定该文件"
                            % type(e).__name__) from None
    try:
        session = json.loads(raw.decode("utf-8"))
        _validate_session(session)
        _token_format(session["auth"]["accessToken"])
    except AuthError as e:
        raise AccountsError("ERROR", "这份凭据不能用来绑定账号：%s" % e) from None
    except (ValueError, UnicodeError):
        raise AccountsError("ERROR", "凭据文件不是合法 JSON：%s" % path) from None

    uid = session["account"]["uid"]
    endpoint = ((session.get("auth") or {}).get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")
    registry = load_registry()
    for other in registry["accounts"]:
        if other["name"] == name:
            if not force:
                raise AccountsError("ERROR", "账号「%s」已绑定；要更新凭据请加 --force" % name)
        elif other["uid"] == uid and not force:
            raise AccountsError("ERROR", "该登录账号已绑定为「%s」；如需重复绑定请加 --force"
                                % other["name"])

    credential = urllib.parse.quote(name, safe="") + ".info"
    entry = {"name": name, "uid": uid, "endpoint": endpoint, "credential": credential,
             "bound_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "last_run_at": "", "last_result": "", "last_code": 0}
    _atomic_write(os.path.join(accounts_dir(), credential), raw)
    accounts = [a for a in registry["accounts"] if a["name"] != name]
    accounts.append(entry)
    registry["accounts"] = accounts
    if not registry.get("active"):
        registry["active"] = name
    save_registry(registry)
    return {"account": name, "uid": _uid_hint(uid), "endpoint": endpoint,
            "credential": os.path.join(accounts_dir(), credential),
            "active": registry["active"],
            "report": "已绑定账号「%s」（uid %s），当前共 %d 个账号%s" % (
                name, _uid_hint(uid) or "-", len(accounts),
                "；当前账号是「%s」" % registry["active"] if registry["active"] else "")}


def use_account(name):
    registry = load_registry()
    entry = next((a for a in registry["accounts"] if a["name"] == name), None)
    if entry is None:
        raise AccountsError("ERROR", "未绑定账号「%s」；用 signin.py accounts 查看已绑定账号" % name)
    registry["active"] = name
    save_registry(registry)
    return {"account": name, "uid": _uid_hint(entry["uid"]), "active": name,
            "report": "当前账号已切换为「%s」" % name}


def unbind_account(name):
    registry = load_registry()
    entry = next((a for a in registry["accounts"] if a["name"] == name), None)
    if entry is None:
        raise AccountsError("ERROR", "未绑定账号「%s」；用 signin.py accounts 查看已绑定账号" % name)
    registry["accounts"] = [a for a in registry["accounts"] if a["name"] != name]
    if registry.get("active") == name:
        registry["active"] = registry["accounts"][0]["name"] if registry["accounts"] else None
    save_registry(registry)
    removed = False
    try:
        os.unlink(credential_path(entry))
        removed = True
    except OSError:
        pass
    return {"account": name, "credential_removed": removed, "active": registry.get("active"),
            "report": "已解绑账号「%s」，还剩 %d 个账号" % (name, len(registry["accounts"]))}


def _credential_expiry_text(path):
    try:
        session = load_session(path)
    except (OSError, ValueError, UnicodeError):
        return ""
    auth = session.get("auth") if isinstance(session, dict) else None
    return _expires_text(auth.get("expiresAt") if isinstance(auth, dict) else None)


def list_accounts():
    registry = load_registry()
    rows = []
    for entry in registry["accounts"]:
        rows.append({
            "name": entry["name"],
            "uid": _uid_hint(entry["uid"]),
            "endpoint": entry["endpoint"],
            "active": entry["name"] == registry.get("active"),
            "credential_exists": os.path.exists(credential_path(entry)),
            "expires_at": _credential_expiry_text(credential_path(entry)),
            "last_run_at": entry["last_run_at"],
            "last_result": entry["last_result"],
        })
    if rows:
        report = "已绑定 %d 个账号：%s" % (len(rows), "，".join(
            ("%s（当前）" if row["active"] else "%s") % row["name"] for row in rows))
    else:
        report = "还没有绑定任何账号；先登录 WorkBuddy 桌面端，再运行 signin.py bind <账号名>"
    return {"count": len(rows), "active": registry.get("active"), "accounts": rows,
            "directory": accounts_dir(), "report": report}


def _missing_auth_error():
    """凭据完全找不到时的 NO_AUTH；保留"环境变量指错了"这个更有用的诊断。"""
    _, looked_in = find_auth_file()
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        report = "WORKBUDDY_AUTH_FILE 指向的文件不存在：%s" % override
    else:
        report = ("未找到 WorkBuddy 登录凭据。请先在本机登录 WorkBuddy 桌面端；"
                  "或设置环境变量 WORKBUDDY_AUTH_FILE 指向 workbuddy-desktop.info。")
    return AccountsError("NO_AUTH", report, looked_in=looked_in)


def credential_plan(explicit=None, rotate=True):
    """决定这一轮要跑哪些凭据。返回 ([(label, path, entry)], warnings)。

    rotate=True（auto / silent / growth / 轮询）：清单里有账号就按轮转顺序全部跑；
    没有账号清单时退回"客户端当前登录"这一份，行为与加入多账号之前完全一致。
    rotate=False（status / claim / all / doctor）：只跑一个——显式指定的账号，否则
    清单里的当前账号，最后才退回客户端当前登录。

    explicit 取 "current"/"client" 表示忽略清单、只用客户端当前登录。
    """
    warnings = []
    if explicit:
        if explicit.strip().lower() in ("current", "client"):
            path, _ = find_auth_file()
            if not path:
                raise _missing_auth_error()
            return [(None, path, None)], warnings
        entry = find_account(explicit)
        return [(entry["name"], credential_path(entry), entry)], warnings

    try:
        registry = load_registry()
    except AccountsError as e:
        # 清单坏了不能把当天的签到一起拖死：退回当前登录账号，并把问题写进汇报。
        path, looked_in = find_auth_file()
        if not path:
            raise AccountsError("NO_AUTH", "%s；同时未找到客户端登录凭据" % e.report,
                                looked_in=looked_in) from None
        warnings.append("%s；本次只跑客户端当前登录账号" % e.report)
        return [(None, path, None)], warnings

    if not registry["accounts"]:
        path, _ = find_auth_file()
        if not path:
            raise _missing_auth_error()
        return [(None, path, None)], warnings

    entries = order_accounts(registry)
    if not rotate:
        active = registry.get("active")
        entries = [a for a in entries if a["name"] == active][:1] or entries[:1]
    contexts = []
    seen = set()
    for entry in entries:
        # 同一个登录身份被绑成两个名字时只跑一次：重复签到幂等，但白花一次预算。
        if entry["uid"] and entry["uid"] in seen:
            warnings.append("账号「%s」与前面的账号是同一个登录身份，已跳过" % entry["name"])
            continue
        seen.add(entry["uid"])
        path = credential_path(entry)
        contexts.append((entry["name"], path if os.path.exists(path) else None, entry))
    return contexts, warnings


def _record_account_run(name, out, code):
    """把本次结果记回清单，供下一次轮转排序与 accounts 展示；失败不影响主流程。"""
    if not name:
        return
    try:
        registry = load_registry()
    except AccountsError:
        return
    for entry in registry["accounts"]:
        if entry["name"] == name:
            entry["last_run_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            entry["last_result"] = str(out.get("result") or "UNKNOWN")[:64]
            entry["last_code"] = as_int(code, 0)
            break
    else:
        return
    try:
        save_registry(registry)
    except Exception:
        pass


def build_headers(session):
    _validate_session(session)
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not _valid_token(token):
        raise AuthError("INVALID_FORMAT")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "X-User-Id": uid,
        "User-Agent": "WorkBuddy",
    }
    if account.get("enterpriseId"):
        headers["X-Enterprise-Id"] = account["enterpriseId"]
        headers["X-Tenant-Id"] = account["enterpriseId"]
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    return headers


def _request(url, headers, method="GET", payload=None, timeout=REQUEST_TIMEOUT):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except urllib.error.URLError as e:
        return CODE_NO_NETWORK, {"error": str(e.reason)}
    except Exception as e:
        return CODE_NO_NETWORK, {"error": str(e)}


def post(url, headers, payload=None, retry=False):
    """POST 默认不重试：抽奖/领奖等写操作若在服务端处理完成后才超时，重试会重复提交。"""
    return _request_with_retry(url, headers, method="POST", payload=payload, retry=retry)


def get(url, headers):
    return _request_with_retry(url, headers, method="GET", retry=True)


def _retry_delays(code):
    """该失败码对应的退避节奏；空元组表示"重试也没用"，立刻如实返回。

    只有这两类值得再试：本机网络没就绪（-1）、服务端抖动（5xx）。
    4xx 是业务规则或参数问题，重试一百次也是同一个答案；CODE_BUDGET_OUT 更是
    连请求都没发出去，再试只会离被强杀更近一步。
    """
    if code == CODE_NO_NETWORK:
        return NETWORK_RETRY_DELAYS
    if code >= 500:
        return SERVER_RETRY_DELAYS
    return ()


def _request_with_retry(url, headers, method="GET", payload=None, retry=True):
    """带时间预算的请求：失败按退避节奏重试，超时上限随剩余预算收缩，预算不足则直接放弃。

    返回 CODE_BUDGET_OUT 表示"没发出去，因为再发就要超出任务时限了"——调用方据此提前
    收尾，保证 emit 一定能执行到。

    退避节奏见 NETWORK_RETRY_DELAYS：早期版本固定"重试 1 次、隔 5 秒"，对定时任务
    最常见的失败场景（刚开机/唤醒，网络还要几十秒才就绪）几乎是无效重试。
    """
    if _budget_left() <= 1:
        return CODE_BUDGET_OUT, {"error": "已达本次运行时间预算，跳过剩余请求"}

    code, body = _request(url, headers, method=method, payload=payload,
                          timeout=max(1, min(REQUEST_TIMEOUT, _budget_left())))
    if not retry:
        return code, body

    # 退避进度按"失败类型"各记一份，而不是一个全局计数。
    # 失败类型会在重试途中变化：典型的是冷启动——前几次网络不可达（网卡刚连上），
    # 之后转成 500（代理还没就绪）。全局计数会让这种 5xx 撞上已经用光的计数、
    # 一次都重试不到（网络类已推进到 2，而 5xx 的节奏只有 2 项）。按节奏分桶后，
    # 每类各自从头走自己的退避表。总尝试次数仍有界：至多 len(两类节奏之和)+1 次。
    attempts = {}
    while True:
        delays = _retry_delays(code)
        if not delays:
            return code, body
        used = attempts.get(delays, 0)
        if used >= len(delays):
            return code, body
        delay = delays[used]
        attempts[delays] = used + 1
        # 一轮重试最坏要占掉 delay + 一整个超时，预算不够就别开始：宁可现在如实返回
        # 失败，也不能跑穿任务时限被系统强杀——那样连日志都写不出来，当天记录整条丢失。
        if _budget_left() <= delay + REQUEST_TIMEOUT:
            return code, body
        time.sleep(delay)
        code, body = _request(url, headers, method=method, payload=payload,
                              timeout=max(1, min(REQUEST_TIMEOUT, _budget_left())))


def _is_hard_failure(code):
    """是否属于"需要人关注"的失败。

    5xx 与"没拿到响应"算硬失败；4xx 绝大多数是业务规则（如派 Buddy 已达每日上限、
    活动已结束），属于每天的正常状态，若计入退出码会让计划任务天天报红。
    """
    return code >= 500 or code in (CODE_NO_NETWORK, CODE_BUDGET_OUT)


def _http_label(code):
    """把伪 HTTP 码翻译成人话；-1/-2 是脚本自定义的"没拿到响应"标记。"""
    if code == CODE_NO_NETWORK:
        return "网络不可达"
    if code == CODE_BUDGET_OUT:
        return "时间预算耗尽"
    return "HTTP %s" % code


def _is_no_chance(msg):
    """抽奖失败是否只是"没有次数"——这是常态，不是故障。

    服务端对"次数为 0"返回 400 + `insufficient lottery chance balance`，和真正的
    参数错误（`invalid request`）同为 400，只看状态码会把两者混为一谈：把常态记成
    失败，轮询就会每轮强制落盘、还会把失败数算进汇报。
    """
    m = str(msg or "").lower()
    if not m:
        return False
    if "insufficient" in m or "not enough" in m:
        return "chance" in m or "balance" in m
    return "no chance" in m


def _is_unknown_tier(code, body):
    """连登兑换是否因为"tier 这个值本身不认识"被拒——用于判断要不要换一种写法重试。

    /redeem 的 tier 是**档位标识字符串**（"7d"/"14d"/"28d"），权威来源是
    GET /streak 的 redemption_status.tiers[].tier。接口哪天改回收天数，脚本就会
    三档全废且看不出原因，所以保留这条兜底：档位标识被判 unknown tier 时退回天数
    再试一次。这类 400 发生在参数校验阶段，服务端没兑换任何东西，重试不会重复领取；
    `invalid request` 这类业务拒绝不在此列，不能重试。

    历史教训：2026-09-11 曾据"传天数返回 invalid request"误判 tier 要收数字，
    导致兑换模块连坏三个版本——"业务拒绝"的错误文案不能反推参数格式正确，
    必须拿到一次成功响应才算验证过。
    """
    if code != 400:
        return False
    m = str(dig(body, "msg") or "").lower()
    return "tier" in m and ("unknown" in m or "unsupported" in m or "invalid" in m)


def _is_tier_locked(code, body):
    """未解锁档位：403 + 「连续登录天数不足」——这是常态，不是故障。

    不加这条的话，未解锁档位会被计进 failures，轮询每轮都判定"有失败"从而强制
    落盘，真正有价值的记录会被这类常态信息淹没。
    """
    if code != 403:
        return False
    m = str(dig(body, "msg") or "")
    return "天数不足" in m or "不足" in m


def dig(obj, key):
    """在可能被 data/result 包裹的响应里找字段，兼容信封结构。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


def _api_success(code, body):
    """同时检查 HTTP 状态和 JSON 业务码，避免把业务失败当作状态转换成功。"""
    business_code = body.get("code") if isinstance(body, dict) else None
    return 200 <= code < 300 and (
        business_code is None or (type(business_code) is int and business_code == 0))


def _makeup_candidates(streak_body, heatmap_body):
    """从日历选断登日期；makeup_dates 是已补登历史，不能作为待办清单。

    对齐官方成长中心：当月、今天之前、活动上线之后、score == 0。
    使用 heatmap.today 的服务端日期，避免本机时区把月初/午夜算错。
    日历缺项不推测为零；结构异常时整段停止，不能靠猜测消耗补登卡。
    """
    def parse_date(value):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("补登日期格式无效")
        return date.fromisoformat(value)

    today_obj = dig(heatmap_body, "today")
    if not isinstance(today_obj, dict):
        raise ValueError("活跃日历缺少服务端日期")
    today = parse_date(today_obj.get("date"))
    launch = parse_date(dig(streak_body, "launch_date"))
    start = max(today.replace(day=1), launch, date(2026, 6, 17))
    streak = dig(streak_body, "streak")
    if not isinstance(streak, dict):
        raise ValueError("连登状态格式无效")
    made_up = streak.get("makeup_dates", [])
    if not isinstance(made_up, list):
        raise ValueError("已补登记录格式无效")
    made_up = {parse_date(value) for value in made_up}
    cells = dig(heatmap_body, "cells")
    if not isinstance(cells, list):
        raise ValueError("活跃日历缺少日期列表")
    scores = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("活跃日历记录格式无效")
        day = parse_date(cell.get("date"))
        score = cell.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
            raise ValueError("活跃日历分数无效")
        if day in scores and scores[day] != score:
            raise ValueError("活跃日历同一日期的记录冲突")
        scores[day] = score
    # 最近的断点优先，以便尽早恢复当前连登；每轮仍受 MAKEUP_MAX_PER_RUN 限制。
    return sorted((day.isoformat() for day, score in scores.items()
                   if start <= day < today and score == 0 and day not in made_up), reverse=True)


def fmt_credit(v):
    """积分显示用：能转 int 就转，否则原样返回（OverflowError 同理，见 as_int）。"""
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return v


def as_int(v, default=0):
    """把可能为字符串的数字安全地转成 int，避免与数值比较/累加时抛异常。

    OverflowError 必须一并捕获：json.loads 默认接受 Infinity，服务端返回该字面量时
    v 已经是 float('inf')，int(v) 抛的是 OverflowError 而非 ValueError。
    """
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(v))  # 兼容 "1.5"/"1e3" 这类数字串，宁可截断也不把真实数值丢成 0
    except (TypeError, ValueError, OverflowError):
        return default


def _first_int(body, key, fallback=0):
    """优先取响应里的实际数值，挖不到才用回落值。

    任务列表里的 reward_credit 只是活动配置，与服务端这次实际发放的可能不同；
    上报按响应值才不会虚报，响应里没有时才退回配置值。
    """
    if isinstance(body, dict):
        v = dig(body, key)
        if v is not None:
            return as_int(v)
    return as_int(fallback)


def _fmt_eta(arrive_at, server_now):
    """把服务端返回的 Unix 时间戳换算成"还有多久回来"。

    任一时间戳缺失/非法就返回空串——这是纯展示信息，绝不能因为它让整轮执行失败。
    json.loads 默认接受 Infinity/NaN，非有限值同样按缺失处理，否则会汇报出
    "约 inf 小时后回"这种话。
    """
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(left):
        return ""
    if left <= 0:
        return "，已到达待领取"
    # 先算分钟再决定用哪个量纲：直接按 left < 3600 分档会让 3599s 显示成"约 60 分钟"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return "，约 %d 分钟后回" % max(1, minutes)
    return "，约 %.1f 小时后回" % (left / 3600.0)


def _env_flag(name):
    """开关型环境变量是否为真；空串与 0/false/no/off 一律视为关。"""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _client_token(prefix="u"):
    """活动接口（抽奖/连登兑换）要求的防重放 token。

    官方前端用 `crypto.randomUUID()` 拼成 "u-<uuid>"，服务端只做幂等去重、
    不校验格式；缺了它 /lottery/draw 直接 400（实测过）。
    """
    return "%s-%s" % (prefix, uuid.uuid4())


# 连登兑换各档奖励的官方文案（2026-09 活动版本）。兑换成功的汇报优先用
# 服务端返回的实际明细，挖不到字段时才回落到这里——活动改版时以响应为准。
_REDEEM_REWARDS = {
    "7d":  "+2 能量 +1 补登卡 +1 次抽奖",
    "14d": "+50 积分 +3 能量 +1 补登卡 +1 次抽奖",
    "28d": "+150 积分 +5 能量 +1 补登卡 +1 次抽奖",
}

# 连登兑换三档：(档位标识, /redeem/summary 的状态字段前缀, 展示名, 天数)
# tier 的口径是档位标识字符串，天数只用于兜底重试时退回旧写法。
_REDEEM_TIERS = (
    ("7d",  "starter",   "入门", 7),
    ("14d", "advanced",  "进阶", 14),
    ("28d", "legendary", "巅峰", 28),
)


def _redeem_reward_desc(body, tier):
    """兑换成功的奖励描述：优先拼服务端实发的 *_granted，挖不到才回落官方文案。

    注意实发字段名带 _granted 后缀（credit_granted / energy_granted /
    cards_granted / chances_granted）；直接读 `credit` 恒为空，会把兑换所得漏计。
    """
    bits = []
    credit = as_int(dig(body, "credit_granted"), 0)
    energy = as_int(dig(body, "energy_granted"), 0)
    cards = as_int(dig(body, "cards_granted"), 0)
    chances = as_int(dig(body, "chances_granted"), 0)
    if credit:
        bits.append("+%s 积分" % fmt_credit(credit))
    if energy:
        bits.append("+%s 能量" % fmt_credit(energy))
    if cards:
        bits.append("+%s 补登卡" % fmt_credit(cards))
    if chances:
        bits.append("+%s 次抽奖" % fmt_credit(chances))
    if bits:
        return "（%s）" % " ".join(bits)
    return "（%s）" % _REDEEM_REWARDS.get(tier, "奖励已到账")


def _dumps(out):
    """序列化汇报内容；default=str 兜住意外混入的非 JSON 类型，绝不让唯一的输出通道崩掉。"""
    try:
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception:
        return repr(out)


def emit(out, action):
    """silent 类模式写日志文件，其余模式打印到 stdout；本函数保证不抛异常。

    判定用前缀而不是等号：silent-poll / silent-growth 同样是无窗口跑的，pythonw 下
    sys.stdout 是 None，print 会静默成功（不抛异常），于是"打到 stdout"等于扔进黑洞——
    NO_AUTH、NO_SESSION 这类最需要人处理的结果会一条不剩地消失。

    ERROR 两条通道都走：计划任务里若把命令名写错（silent 拼成 slient 之类），
    action 不带 silent 前缀，走 stdout 就等于扔进黑洞，无窗口运行下这次失败再没有
    任何痕迹——正是本脚本想杜绝的"当天日志整条丢失"。
    """
    if isinstance(out, dict):
        out = dict(out)
        out.setdefault("needs_attention", out.get("result") in (
            "ERROR", "UNKNOWN", "NETWORK", "TIMEOUT", "NO_AUTH", "NO_SESSION",
            "AUTH_ERROR", "AUTH_REJECTED", "FORBIDDEN"))
    if _config_warning and isinstance(out, dict):
        out = dict(out, config_warning=_config_warning)
    payload = _dumps(out)
    for value in _sensitive_values:
        payload = payload.replace(value, "[REDACTED]")
    is_error = isinstance(out, dict) and out.get("result") == "ERROR"
    if not str(action).startswith("silent"):
        try:
            print(payload)
            if not is_error:
                return
        except Exception:
            pass  # stdout 不可用（编码/管道问题）时退到日志，至少不把结果丢掉

    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payload)
    default_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signin.log")
    # 自定义路径不可写时回退到脚本同目录，避免无窗口运行下结果彻底丢失
    for path in (os.environ.get("WORKBUDDY_SIGNIN_LOG") or default_log, default_log):
        try:
            with open(path, "a", encoding="utf-8") as lf:
                lf.write(line)
            return
        except Exception:
            continue


def _is_already_checked_in(cbody):
    """领取接口返回是否表示"今日已签"（兼容 null 与 400+code10001）。"""
    if cbody is None:
        return True
    if isinstance(cbody, dict):
        msg = cbody.get("msg") or ""
        if cbody.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _already_report(status, via=None):
    """根据状态构造"今日已签"汇报 dict。"""
    today_credit = dig(status, "today_credit") or dig(status, "daily_credit")
    streak_days = dig(status, "streak_days")
    total_credits = dig(status, "total_credits")
    is_streak_day = dig(status, "is_streak_day")
    next_streak_day = dig(status, "next_streak_day")
    inner = []
    if today_credit is not None:
        inner.append("今日 +%s" % fmt_credit(today_credit))
    if streak_days is not None:
        inner.append("连续 %s 天" % streak_days)
    if total_credits is not None:
        inner.append("累计 %s 积分" % fmt_credit(total_credits))
    prefix = via or "今日已签过"
    report = "%s（%s）" % (prefix, "，".join(inner)) if inner else prefix
    return {
        "result": "ALREADY",
        "report": report,
        "today_credit": today_credit,
        "streak_days": streak_days,
        "total_credits": total_credits,
        "is_streak_day": is_streak_day,
        "next_streak_day": next_streak_day,
    }


def run_growth(headers, endpoint):
    """成长中心自动化：领旅行礼物→领任务→补登→连登兑换→开盲盒→能量开 Buddy→派出→汇报。

    各子步骤单独 try，一段失败不影响其余领取；认证或权限拒绝时结束本轮。
    """
    base = endpoint + "/v2/activity/growth"
    parts = []
    credits_gained = 0
    failures = 0        # 全部失败项，仅用于汇报
    hard_failures = 0   # 其中"需要关注"的那些，只有它们影响退出码
    successes = 0

    def _check_auth(code):
        """已知业务 403 由调用方先处理，其余认证/权限拒绝结束本轮。"""
        return code in (401, 403)

    def _stop_for_auth(code):
        # 派出已移到奖励领取之后；即使被拒，也必须保留本轮已到账的奖励。
        out = _auth_failure(code)
        out["report"] = "；".join(parts + [out["report"]])
        out["credits_gained"] = credits_gained
        return 1, out

    def _note_http(code, body, label, required=False):
        """前置查询接口非 2xx 时的统一记录；返回 True 表示调用方应跳过后续处理。

        硬失败（5xx / 网络不可达 / 预算耗尽）必须计入 failures：否则整轮
        successes=0 且 failures=0 会被判成 idle，轮询既不写日志又返回 0，
        服务端故障彻底无声无息。旅行模块早就这么做了，其余各段没跟上。

        一般查询的 4xx 只进报告、不计失败。影响写操作的前置查询用 required=True：
        查询失败或业务码非零时必须提醒，不能把未知状态解释为可安全操作。
        """
        nonlocal failures, hard_failures
        if 200 <= code < 300 and (not required or _api_success(code, body)):
            return False
        reason = _http_label(code)
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("msg") or "")
        # 状态码与服务端给的详情都保留：只知道 500 无法判断影响，只知道 "timed out"
        # 又看不出是网络还是服务端，轮询日志里这两者都想要
        parts.append("%s失败：%s" % (label, "%s（%s）" % (reason, detail)
                                     if detail and detail != reason else (detail or reason)))
        if required or _is_hard_failure(code):
            failures += 1
            hard_failures += 1
        return True

    # --- 1. Buddy 旅行：先领已有礼物，派出放到能量开 Buddy 之后 ---
    # 开 Buddy 会覆盖旅行记录；只有明确 idle 才能开，状态未知也不能放行。
    travel = None
    daily_limit = False
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        if scode == CODE_BUDGET_OUT:
            return 1, {"result": "TIMEOUT",
                       "report": "时间预算耗尽，成长中心跳过，下次自动重试"}
        if scode == CODE_NO_NETWORK:
            # 网络不可达就立刻收手，别把后续 5 个接口的重试+等待全跑一遍
            return 1, {"result": "NETWORK",
                       "report": "网络不可达，成长中心跳过（%s）" % (sbody.get("error") or "")}
        if _check_auth(scode):
            return 1, _auth_failure(scode)
        if not _note_http(scode, sbody, "查旅行状态", required=True):
            state = dig(sbody, "state")
            if state in ("idle", "traveling", "arrived"):
                travel = state
                daily_limit = bool(dig(sbody, "daily_limit_reached"))
            else:
                parts.append("旅行状态无法识别，跳过开 Buddy 与派出")
                failures += 1
                hard_failures += 1
        if travel == "arrived":
            record_id = dig(sbody, "record_id")
            ccode, cbody = post(base + "/buddy/travel/claim", headers, {"record_id": record_id})
            if _check_auth(ccode):
                return 1, _auth_failure(ccode)
            if _api_success(ccode, cbody) and dig(cbody, "reward_credit") is not None:
                got = as_int(dig(cbody, "reward_credit"))
                credits_gained += got
                parts.append("领旅行礼物 +%s 积分" % fmt_credit(got))
                successes += 1
                travel = "idle"  # 确认领取成功后，才允许开 Buddy 或派出。
            else:
                # 领失败：带出业务 msg，不再说"HTTP 200"；也不派 Buddy 出发，避免覆盖未领取的奖励
                msg = (dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                parts.append("领旅行礼物失败：%s" % (msg or "HTTP %s" % ccode))
                failures += 1
                hard_failures += _is_hard_failure(ccode)
        if travel == "idle" and daily_limit:
            # 今日名额已用完：直接收手，不碰 config/depart，省掉两个请求和一条必然的失败
            parts.append("今日旅行名额已用完")
        elif travel == "traveling":
            location = dig(sbody, "location")
            loc_name = location.get("name", "?") if isinstance(location, dict) else "?"
            # arrive_at / server_now 是服务端时间戳，比本地时钟可靠
            parts.append("Buddy 旅行中（%s%s）" % (
                loc_name, _fmt_eta(dig(sbody, "arrive_at"), dig(sbody, "server_now"))))
    except Exception as e:
        parts.append("旅行模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 2. 任务领奖（放在抽奖前：任务送的抽奖机会/能量，后面马上能用上）---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，任务领奖跳过")
    else:
        try:
            tcode, tbody = get(base + "/tasks", headers)
            if _check_auth(tcode):
                return 1, _auth_failure(tcode)
            if not _note_http(tcode, tbody, "查任务列表"):
                tasks = dig(tbody, "tasks") or []
                # 真实契约（2026-09 从桌面端成长中心 H5 的 growthSpace chunk 读出）：
                #   accept_status: not_accepted | accepted | in_progress | completed | claimed
                #   接单 POST /tasks/accept  body {"task_codes": [code, ...]}   ← 复数数组
                #        （旧的单数 {"task_code": x} 在新服务端一律 400 invalid request）
                #   领奖 POST /tasks/{task_code}/claim   ← 路径带 code、body 空
                #        （旧版拿 /tasks/accept 当领奖用，同样 400）
                titles = {t.get("task_code"): t.get("title", t.get("task_code")) for t in tasks}
                pending = [t.get("task_code") for t in tasks
                           if t.get("task_code") and not t.get("locked")
                           and t.get("accept_status") == "not_accepted"]
                for i in range(0, len(pending), 20):   # 分批，别把 body 撑大
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余任务下次再接单")
                        break
                    batch = pending[i:i + 20]
                    acode, abody = post(base + "/tasks/accept", headers,
                                        {"task_codes": batch})
                    if _check_auth(acode):
                        return 1, _auth_failure(acode)
                    # 逐条读 results：接单失败必须报出来（常见
                    # "prerequisite not met: first_buddy (no buddy instance found)"），
                    # 静默吞掉的话接口坏了也没人知道——轮询空跑是不写日志的。
                    results = dig(abody, "results")
                    if not isinstance(results, list):
                        results = [{"task_code": c,
                                    "status": "ok" if 200 <= acode < 300 else "error",
                                    "message": dig(abody, "msg")} for c in batch]
                    for r in results:
                        title = titles.get(r.get("task_code"), r.get("task_code"))
                        if r.get("status") == "error":
                            parts.append("领取任务「%s」失败：%s" % (
                                title, r.get("message") or "HTTP %s" % acode))
                            failures += 1
                            hard_failures += _is_hard_failure(acode)
                        else:
                            parts.append("领取任务「%s」（进度开始计）" % title)
                            successes += 1
                for t in tasks:
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余任务奖下次再领")
                        break
                    if t.get("locked") or t.get("accept_status") != "completed":
                        continue   # 只有 completed 才发奖，且走独立路径
                    code = t.get("task_code")
                    title = titles.get(code, code)
                    try:
                        ccode, cbody = post(base + "/tasks/%s/claim" % code, headers, {})
                        if _check_auth(ccode):
                            return 1, _auth_failure(ccode)
                        if 200 <= ccode < 300 and not dig(cbody, "already_claimed"):
                            # 优先用服务端实发数值；列表里的 reward_credit 只是活动配置，
                            # 改版时会和实发对不上，挖不到才回落到列表值。
                            rc = _first_int(cbody, "credit", t.get("reward_credit"))
                            re_ = _first_int(cbody, "energy", t.get("reward_energy"))
                            credits_gained += rc
                            parts.append("领任务奖「%s」+credit%s+energy%s" % (title, rc, re_))
                            successes += 1
                        elif 200 <= ccode < 300:
                            parts.append("任务奖「%s」已领过" % title)
                        else:
                            parts.append("领任务奖「%s」失败：%s" % (
                                title, dig(cbody, "msg") or "HTTP %s" % ccode))
                            failures += 1
                            hard_failures += _is_hard_failure(ccode)
                    except Exception as e:
                        parts.append("领任务奖「%s」异常（%s: %s）" % (
                            code, type(e).__name__, e))
                        failures += 1
                        hard_failures += 1
        except Exception as e:
            parts.append("任务模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 3. 补登卡：断登自动补一张，保住连登 ---
    # 官方规则：补登卡上限 4 张、仅可补救当月断登。/streak 的 makeup_dates
    # 是已经补登的日期；真正的断点来自 /heatmap 中 score == 0 的历史日期。
    # 放在连登兑换之前：补登会改变连登天数，先补，兑换才能拿到最新解锁状态。
    # 2026-10-01 核对官方 GrowthCenterPage / AllTasksPage 与真实只读日历。
    # 保留每轮最多一张、写请求不重试的限制；空白或异常数据不会触发补登。
    streak_body = None    # 复用给第 7 段的展示值，避免同一轮打两次 /streak
    streak_stale = False  # 补登成功会改变连签天数，此时必须重新取
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，补登跳过")
    else:
        try:
            mcode, mbody = get(base + "/streak", headers)
            if _check_auth(mcode):
                return 1, _auth_failure(mcode)
            if not _note_http(mcode, mbody, "查连登状态", required=True):
                streak_body = mbody
                # 余额兼容两种形状：{"makeup_cards":{"balance":2}} 与 {"makeup_cards":2}。
                # 只认前者的话，接口是后者时整个补登会一声不响地永不执行。
                cards_obj = dig(mbody, "makeup_cards")
                cards = as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) \
                    else as_int(cards_obj)
                dates = []
                if cards > 0:
                    hcode, hbody = get(base + "/heatmap", headers)
                    if _check_auth(hcode):
                        return 1, _auth_failure(hcode)
                    if not _note_http(hcode, hbody, "查补登日历", required=True):
                        dates = _makeup_candidates(mbody, hbody)
                if dates:
                    made_up = 0
                    for d in dates[:min(cards, MAKEUP_MAX_PER_RUN)]:
                        if _budget_left() <= 0:
                            parts.append("时间预算耗尽，剩余补登下次再做")
                            break
                        ucode, ubody = post(base + "/makeup-cards/use", headers,
                                            {"target_date": d})
                        if _check_auth(ucode):
                            return 1, _auth_failure(ucode)
                        if _api_success(ucode, ubody):
                            cards -= 1
                            made_up += 1
                            streak_stale = True
                            # 优先报服务端给的余额，本地递减只是接口没给时的兜底
                            left_obj = dig(ubody, "makeup_cards")
                            left_cards = as_int(left_obj.get("balance"), cards) \
                                if isinstance(left_obj, dict) else as_int(left_obj, cards)
                            parts.append("补登 %s（剩 %s 张卡）" % (d, left_cards))
                            cards = max(0, left_cards)
                            successes += 1
                        else:
                            msg = dig(ubody, "msg") or ""
                            if ucode == 400 and str(msg).strip().lower() == "date is not broken, no makeup needed":
                                # 查询后被客户端或另一轮补上，是正常竞争；不再当作失败刷日志。
                                parts.append("%s 已活跃或已补登，无需再次补登" % d)
                                break
                            parts.append("补登 %s 失败：%s" % (d, msg or "HTTP %s" % ucode))
                            failures += 1
                            hard_failures += 1
                            break
                    if made_up and len(dates) > made_up and cards > 0:
                        parts.append("另有 %s 天可补、剩 %s 张卡，下轮继续" % (
                            len(dates) - made_up, cards))
        except Exception as e:
            parts.append("补登模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 4. 连登奖励兑换（入门/进阶/巅峰三档，附积分/能量/补登卡/抽奖机会）---
    # 入门 7 天、进阶 14 天、巅峰 28 天解锁。summary 只报 claimed/locked 两种
    # 已见状态；"非 claimed 且非 locked"即视为可兑换去尝试，被服务端拒绝时按
    # 普通业务失败处理（不算硬失败）。
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，连登兑换跳过")
    else:
        try:
            rcode, rbody = get(base + "/redeem/summary", headers)
            if _check_auth(rcode):
                return 1, _auth_failure(rcode)
            if not _note_http(rcode, rbody, "查连登兑换"):
                # tier 传**档位标识**（"7d"/"14d"/"28d"），不是天数也不是档位名：
                # 实测传 "starter"/"7"/7 分别得到 unknown tier / unknown tier /
                # invalid request，只有 "7d" 会 200 兑换成功。权威来源是 GET /streak
                # 的 redemption_status.tiers[].tier；状态字段仍读 /redeem/summary
                # 的 starter/advanced/legendary_status，两者一一对应。
                for tier, status_key, label, days in _REDEEM_TIERS:
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余连登兑换下次再领")
                        break
                    status = dig(rbody, status_key + "_status")
                    # 字段缺失（None）同样跳过：接口改版时不该让脚本对三档无脑 POST。
                    # 注意别在这里再加 *_count 之类的"双保险"：实测响应里
                    # starter_count=1 与 total_consumed=0 并存，count 到底是
                    # "已兑换次数"还是"可兑换次数"并不确定，猜错就会把整段静默关掉。
                    if not status or status in ("claimed", "locked"):
                        continue
                    c2code, c2body = post(base + "/redeem", headers,
                                          {"tier": tier, "client_token": _client_token()})
                    # 档位标识被判为未知时退回天数再试一次：这类 400 是参数校验阶段
                    # 的拒绝，服务端没兑换任何东西，重试不会重复领取
                    if _is_unknown_tier(c2code, c2body):
                        c2code, c2body = post(base + "/redeem", headers,
                                              {"tier": days, "client_token": _client_token()})
                    # 403「连登天数不足」是业务常态，必须**先于** _check_auth 判断：
                    # _check_auth 会结束认证/权限拒绝的流程，若让它先跑，未解锁档位
                    # 会被当成权限拒绝并直接中止整个成长中心。
                    if _is_tier_locked(c2code, c2body):
                        parts.append("连登兑换「%s」未解锁（连登天数不足）" % label)
                        continue
                    if _check_auth(c2code):
                        return 1, _auth_failure(c2code)
                    if 200 <= c2code < 300:
                        credits_gained += as_int(dig(c2body, "credit_granted"))
                        parts.append("连登兑换「%s」%s" % (label, _redeem_reward_desc(c2body, tier)))
                        successes += 1
                    else:
                        msg = dig(c2body, "msg") or ""
                        parts.append("连登兑换「%s」失败：%s" % (label, msg or "HTTP %s" % c2code))
                        failures += 1
                        hard_failures += _is_hard_failure(c2code)
        except Exception as e:
            parts.append("连登兑换模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 5. 盲盒/抽奖（draw 必须带 client_token，缺了会 400）---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，盲盒跳过")
    else:
        try:
            lcode, lbody = get(base + "/lottery/chances", headers)
            if _check_auth(lcode):
                return 1, _auth_failure(lcode)
            chances = 0 if _note_http(lcode, lbody, "查抽奖机会") else as_int(dig(lbody, "balance"))
            if chances > 0:
                dcode, dbody = post(base + "/lottery/draw", headers,
                                    {"client_token": _client_token()})
                if _check_auth(dcode):
                    return 1, _auth_failure(dcode)
                if 200 <= dcode < 300:
                    prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
                    if not isinstance(prize, str):
                        # prize 可能是对象/数字；直接 += 会抛 TypeError，把一次已经中了的
                        # 抽奖变成"模块异常"，奖品名和下面这句提醒双双丢失
                        prize = str(prize)
                    # 奖池含实物周边（冰箱贴/胸针/杯子），中奖后要用户自己去填收件信息，
                    # 脚本代填不了也绝不该代填——但必须提醒，否则奖品会卡在未填地址状态。
                    if dig(dbody, "need_address") or dig(dbody, "require_address"):
                        prize += "（实物奖，需到成长中心填写收件信息）"
                    parts.append("开盲盒获得：%s" % prize)
                    successes += 1
                    # 一轮只开一次：这条写路径不可逆，剩下的机会留给下一轮更稳妥
                    if chances > 1:
                        parts.append("还剩 %s 次抽奖机会，下轮继续" % (chances - 1))
                else:
                    msg = dig(dbody, "msg") or ""
                    if _is_no_chance(msg):
                        # 次数为 0 是常态（次数来自连登兑换），不是故障：计入失败会让
                        # 轮询每轮强制落盘，还会把常态算进失败统计
                        parts.append("开盲盒：%s" % (msg or "无抽奖机会"))
                    else:
                        parts.append("开盲盒失败：%s" % (msg or "HTTP %s" % dcode))
                        failures += 1
                        hard_failures += _is_hard_failure(dcode)
        except Exception as e:
            parts.append("盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 6. Buddy 盲盒（能量攒够 cost_per_open 就开；能量没有其它消耗出口）---
    if travel != "idle":
        parts.append("Buddy 盲盒跳过：%s" % (
            "旅行尚未结束或礼物尚未领到" if travel in ("traveling", "arrived") else "旅行状态未知"))
    elif _budget_left() <= 0:
        parts.append("时间预算耗尽，Buddy 盲盒跳过")
    else:
        try:
            qcode, qbody = get(base + "/buddy/quota", headers)
            if _check_auth(qcode):
                return 1, _auth_failure(qcode)
            if not _note_http(qcode, qbody, "查 Buddy 能量"):
                affordable = as_int(dig(qbody, "affordable"))
                max_open = as_int(dig(qbody, "max_open_count"), 1) or 1
                if affordable > 0:
                    count = min(affordable, max_open)
                    ocode, obody = post(base + "/buddy/open", headers,
                                        {"count": count, "client_token": _client_token()})
                    if _check_auth(ocode):
                        return 1, _auth_failure(ocode)
                    if 200 <= ocode < 300:
                        name = dig(obody, "buddy") or dig(obody, "name") or dig(obody, "buddies")
                        if not isinstance(name, str):
                            name = "新 Buddy"
                        parts.append("开 Buddy 盲盒 ×%s（%s）" % (count, name))
                        successes += 1
                    else:
                        msg = dig(obody, "msg") or ""
                        parts.append("开 Buddy 盲盒失败：%s" % (msg or "HTTP %s" % ocode))
                        failures += 1
                        hard_failures += _is_hard_failure(ocode)
        except Exception as e:
            parts.append("Buddy 盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 6b. 最后派出：既不覆盖在途礼物，也让每天只跑一次的用户有机会开 Buddy ---
    if travel == "idle" and not daily_limit:
        if _budget_left() <= 0:
            parts.append("时间预算耗尽，派 Buddy 跳过")
        else:
            try:
                ccode, cbody = get(base + "/buddy/travel/config", headers)
                if _check_auth(ccode):
                    return _stop_for_auth(ccode)
                if not _note_http(ccode, cbody, "查旅行配置", required=True):
                    locs = dig(cbody, "locations")
                    if locs and isinstance(locs[0], dict):
                        dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                            {"location_id": locs[0].get("id")})
                        msg = str(dig(dbody, "msg") or "")
                        if dcode == 403 and msg.strip().lower() == "no active buddy":
                            parts.append("派 Buddy 跳过：尚未领养 Buddy")
                        elif _check_auth(dcode):
                            return _stop_for_auth(dcode)
                        elif _api_success(dcode, dbody):
                            location = dig(dbody, "location")
                            location = location if isinstance(location, dict) else {}
                            loc_name = location.get("name", "?")
                            dur = dig(dbody, "duration_hours") or location.get("duration_hours", "?")
                            parts.append("派 Buddy 去%s（%s 小时后回）" % (loc_name, dur))
                            successes += 1
                        else:
                            parts.append("派 Buddy 失败：%s" % (msg or "HTTP %s" % dcode))
                            failures += 1
                            hard_failures += _is_hard_failure(dcode)
            except Exception as e:
                parts.append("派 Buddy 模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

    # --- 7. 能量 & 连签状态（纯展示值，预算不够就直接不取，不计失败）---
    energy = None
    streak_days = None
    if _budget_left() > 0:
        try:
            ecode, ebody = get(base + "/energy", headers)
            if not _check_auth(ecode):
                energy = dig(ebody, "balance") if (200 <= ecode < 300) else None
        except Exception:
            pass

    # 连签天数：第 3 段已经取过 /streak，没补登过就直接复用，别在同一轮里打两次同一个接口。
    # 补登成功会改变天数（streak_stale），那时才有必要重新取。
    try:
        if streak_body is not None and not streak_stale:
            streak_obj = dig(streak_body, "streak") or {}
            streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
        elif _budget_left() > 0:
            scode2, sbody2 = get(base + "/streak", headers)
            if not _check_auth(scode2):
                streak_obj = dig(sbody2, "streak") or {}
                streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
    except Exception:
        pass

    tail = []
    if energy is not None:
        tail.append("能量 %s" % energy)
    if streak_days is not None:
        tail.append("连签 %s 天" % streak_days)
    if credits_gained:
        tail.append("本次 +共 %s 积分" % credits_gained)

    if parts:
        report = "；".join(parts)
    elif failures:
        report = "成长中心各步骤均失败"
    else:
        report = "成长中心无可领取项"
    if tail:
        report += "（%s）" % "，".join(tail)

    # 已获得的奖励仍保留在汇报里，但不能掩盖其他步骤需要处理的失败。
    # 已识别的正常业务状态（如旅行名额用完、日期无需补登）不会计入 hard_failures。
    result_code = 1 if hard_failures else 0
    # idle = 这一轮既没领到东西也没出错，纯空跑（Buddy 还在路上 / 今日名额已用完 /
    # 确实没有可领项）。轮询任务靠它决定要不要写日志。
    idle = (successes == 0 and failures == 0)
    return result_code, {"result": "GROWTH", "report": report, "credits_gained": credits_gained,
                         "energy": energy, "streak_days": streak_days, "idle": idle,
                         **({"failures": failures} if failures else {})}


def run_auto(headers, endpoint):
    """每日自动化主逻辑：查状态→未签才领→返回一行汇报。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)

    if scode == CODE_BUDGET_OUT:
        return 1, {
            "result": "TIMEOUT",
            "report": "已达本次运行时间预算，签到跳过，下次自动重试",
        }
    if scode == CODE_NO_NETWORK:
        return 1, {
            "result": "NETWORK",
            "report": "网络不可达，签到跳过，下次自动重试（%s）" % (sbody.get("error") or ""),
            "error": sbody.get("error"),
        }
    if scode in (401, 403):
        return 1, _auth_failure(scode)
    if not (200 <= scode < 300):
        return 1, {
            "result": "ERROR",
            "report": "签到接口返回异常（HTTP %s），请重新登录客户端或稍后重试" % scode,
            "http": scode,
            "status_body": sbody,
        }

    status = sbody if isinstance(sbody, dict) else {}
    active = dig(status, "active")
    activity_name = dig(status, "activity_name")

    if active is False:
        report = "签到活动未开启" + ("（%s）" % activity_name if activity_name else "")
        return 0, {"result": "INACTIVE", "report": report, "active": False}

    if dig(status, "today_checked_in") in (True, 1):
        return 0, _already_report(status)

    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers, retry=True)

    if ccode in (CODE_NO_NETWORK, CODE_BUDGET_OUT):
        return 1, {
            "result": "NETWORK" if ccode == CODE_NO_NETWORK else "TIMEOUT",
            "report": "领取请求未能送达，下次自动重试（%s）" % (
                (cbody.get("error") or "") if isinstance(cbody, dict) else ""),
        }

    # 认证或权限拒绝先于“已签”判断。
    if ccode in (401, 403):
        return 1, _auth_failure(ccode)

    if _is_already_checked_in(cbody):
        scode2, sbody2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)
        fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
        return 0, _already_report(fresh, via="今日已签过（服务端判定已领取）")

    credit = dig(cbody, "credit")
    if credit is not None:
        scode2, sbody2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)
        fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
        streak_days = dig(fresh, "streak_days") or dig(status, "streak_days")
        total_credits = dig(fresh, "total_credits")
        is_streak_day = dig(fresh, "is_streak_day")
        next_streak_day = dig(fresh, "next_streak_day")
        bonus = "，且为连签奖励日" if is_streak_day else ""
        cum = "，累计 %s 积分" % fmt_credit(total_credits) if total_credits is not None else ""
        # streak_days 缺失时不要把 None 打进文案
        streak = "（连续 %s 天%s）" % (streak_days, cum) if streak_days is not None else (
            "（%s）" % cum.lstrip("，") if cum else "")
        report = "成功领取 %s 积分%s%s" % (fmt_credit(credit), bonus, streak)
        return 0, {
            "result": "CLAIMED",
            "report": report,
            "credit": credit,
            "streak_days": streak_days,
            "total_credits": total_credits,
            "is_streak_day": is_streak_day,
            "next_streak_day": next_streak_day,
        }

    if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
        msg = cbody.get("msg") or ("code %s" % cbody.get("code"))
        return 1, {
            "result": "ERROR",
            "report": "领取失败：%s（HTTP %s）" % (msg, ccode),
            "http": ccode,
            "claim_body": cbody,
        }

    return 1, {
        "result": "UNKNOWN",
        "report": "未识别的领取返回，请检查接口：%s" % json.dumps(cbody, ensure_ascii=False)[:200],
        "http": ccode,
        "claim_body": cbody,
    }


def run_daily(headers, endpoint):
    """一轮完整的日常：查状态→未签才领→再跑成长中心。返回 (退出码, 输出, 是否空跑)。

    这是 auto / silent / silent-poll 三条路径的共用实现。轮询之所以要跑它而不是
    "只跑成长中心"，是因为签到原本一天只有 00:05 这一次机会：那一次撞上关机、
    睡眠、或刚开机网络还没就绪，当天就再无补救，连签直接断。让每一次轮询都带上
    签到，等于一天七次机会，且不会重复领取——接口幂等，已签会直接返回 ALREADY。

    第三个返回值是给轮询用的静默判据：这一轮没有任何值得一提的事（已签 + 成长
    中心无可领取项）。定时任务不关心它，照写日志；轮询靠它决定要不要落盘。
    """
    code, out = run_auto(headers, endpoint)
    # 网络本就不可达时不必再跑成长中心的一串请求（每个都要重试+等待），也免得
    # 汇报出"无可领取项"这种假的安心话
    if out.get("result") in ("NETWORK", "TIMEOUT"):
        out["growth"] = "网络不可达或时间预算耗尽，成长中心跳过"
        out["growth_result"] = out["result"]
        return code, out, False
    # 认证/权限被拒绝时，停止使用同一凭据继续请求。
    if out.get("result") in ("NO_SESSION", "AUTH_REJECTED", "FORBIDDEN"):
        out["growth"] = "认证或权限检查未通过，成长中心跳过"
        out["growth_result"] = out["result"]
        return code, out, False
    # 签到后顺带跑成长中心；它出任何问题都不能吞掉签到已成功的事实
    try:
        gcode, gout = run_growth(headers, endpoint)
    except AuthError as e:
        gcode, gout = 1, e.output()
    except Exception as e:
        gcode, gout = 1, {"result": "ERROR",
                          "report": "成长中心异常（%s: %s）" % (type(e).__name__, e)}
    out["growth"] = gout.get("report")
    out["growth_result"] = gout.get("result")
    if gout.get("credits_gained") or gcode != 0:
        out["report"] += "；" + gout["report"]
    # 成长中心有需要处理的失败即返回非零；保留签到成功结果，同时透传提醒。
    if gcode != 0 and code == 0:
        code = gcode
    out["needs_attention"] = code != 0
    quiet = out.get("result") in ("ALREADY", "INACTIVE") and bool(gout.get("idle"))
    return code, out, quiet


KNOWN_ACTIONS = ("auto", "silent", "growth", "silent-poll", "silent-growth",
                 "status", "claim", "all", "doctor",
                 "accounts", "bind", "use", "unbind", "help")
# 账号管理命令：只操作账号清单，不联网。
ACCOUNT_ACTIONS = ("accounts", "bind", "use", "unbind")
# 轮转命令：绑定多个账号时，一个账号跑完自动切下一个。
ROTATION_ACTIONS = ("auto", "silent", "growth", "silent-poll", "silent-growth")


def _help_output():
    return {"result": "HELP", "usage": [
        "signin.py [auto|silent|growth|silent-poll|silent-growth|doctor|status|claim|all] "
        "[--account <账号名|current>]",
        "signin.py bind <账号名> [--from <凭据文件>] [--force]",
        "signin.py accounts",
        "signin.py use <账号名>",
        "signin.py unbind <账号名>",
    ], "report": "绑定多个账号后，auto/silent/growth/轮询会依次跑完全部账号；"
                 "每个账号的凭据在 bind 时快照，重新登录后执行 bind <名字> --force 更新"}


def _parse_argv(argv):
    """把命令行拆成 (action, account, source, force)。

    支持两种写法：`signin.py auto --account work` 与 `signin.py --account work auto`；
    bind/use/unbind 的账号名也可以直接跟在命令后面。参数出错抛 ValueError，
    由 main() 统一变成一条 ERROR 汇报（silent 模式同样落盘）。
    """
    args = list(argv)
    account = source = None
    force = False
    positional = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--account", "-a"):
            index += 1
            if index >= len(args):
                raise ValueError("--account 需要一个账号名")
            account = args[index]
        elif arg.startswith("--account="):
            account = arg.split("=", 1)[1]
        elif arg in ("--from", "-f"):
            index += 1
            if index >= len(args):
                raise ValueError("--from 需要一个凭据文件路径")
            source = args[index]
        elif arg.startswith("--from="):
            source = arg.split("=", 1)[1]
        elif arg == "--force":
            force = True
        elif arg in ("-h", "--help", "help"):
            return "help", None, None, False
        elif arg.startswith("-"):
            raise ValueError("未知参数：%s" % arg)
        else:
            positional.append(arg)
        index += 1

    action = positional[0] if positional else "auto"
    name = account
    if action in ACCOUNT_ACTIONS:
        if len(positional) > 1:
            name = positional[1]
        if len(positional) > 2:
            raise ValueError("参数过多：%s" % " ".join(positional[2:]))
    elif len(positional) > 1:
        raise ValueError("参数过多：%s" % " ".join(positional[1:]))
    if force and action != "bind":
        raise ValueError("--force 只用于 bind")
    if source and action != "bind":
        raise ValueError("--from 只用于 bind")
    return action, name, source, force


def main(argv=None):
    """薄壳：只负责取命令 + 兜住一切异常，保证 silent 模式下结果必定落盘。"""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        action, account, source, force = _parse_argv(args)
    except ValueError as e:
        emit({"result": "ERROR", "report": "命令行参数无效：%s" % e, "needs_attention": True},
             args[0] if args else "auto")
        return 2
    try:
        return _run(action, account, source, force)
    except AuthError as e:
        emit(e.output(), action)
        return 1
    except AccountsError as e:
        emit(e.output(), action)
        return 2 if e.result == "NO_AUTH" else 1
    except Exception as e:
        # 无窗口运行下任何未捕获异常都会让当天的失败无痕消失，这里是最后一道防线
        emit({"result": "ERROR", "report": "脚本运行异常（%s）" % type(e).__name__,
              "needs_attention": True}, action)
        return 2


def _run_account_command(action, name, source, force):
    """accounts / bind / use / unbind：只操作账号清单，不联网。"""
    try:
        if action == "accounts":
            out = dict(list_accounts(), result="ACCOUNTS")
        elif action == "bind":
            if not name:
                raise AccountsError("ERROR", "用法：signin.py bind <账号名> "
                                             "[--from <凭据文件>] [--force]")
            out = dict(bind_account(name, source, force), result="BOUND")
        elif action == "use":
            if not name:
                raise AccountsError("ERROR", "用法：signin.py use <账号名>")
            out = dict(use_account(name), result="ACTIVE")
        else:
            if not name:
                raise AccountsError("ERROR", "用法：signin.py unbind <账号名>")
            out = dict(unbind_account(name), result="UNBOUND")
    except AccountsError as e:
        emit(e.output(), action)
        return 2 if e.result == "NO_AUTH" else 1
    emit(out, action)
    return 0


def _dispatch(action, headers, endpoint):
    """执行单账号动作，返回 (code, out, quiet, steps)。steps 只有调试命令会用到。"""
    if action in ("auto", "silent"):
        code, out, quiet = run_daily(headers, endpoint)
        return code, out, quiet, []

    if action in POLL_ACTIONS:
        # 轮询 = 补签 + 成长中心，结果写日志文件（emit 按 silent 前缀判定落盘）。
        # 早期版本这里只跑成长中心、刻意不碰签到接口，理由是"签到一天一次就够"；
        # 但那样一来 00:05 那次一旦失败（关机/睡眠/刚开机网络没就绪），当天就再无
        # 第二次机会。现在每次轮询都先查一次签到状态，未签才补——已签的代价只是
        # 一个查询请求，换来的是一天七次机会。
        code, out, quiet = run_daily(headers, endpoint)
        out["trigger"] = "poll"
        return code, out, quiet, []

    if action == "growth":
        code, out = run_growth(headers, endpoint)
        out["needs_attention"] = code != 0
        return code, out, False, []

    # 以下为交互式调试命令，输出原始返回。走 emit 而非 print，这样非法配置的
    # config_warning 同样能带出来（这些命令不会是 silent，仍然打到 stdout）
    steps = []
    if action in ("status", "all"):
        scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status",
                            headers, retry=True)
        if scode in (401, 403):
            out = dict(_auth_failure(scode), step="status")
            steps.append(out)
            return 1, out, False, steps
        out = {"step": "status", "http": scode, "body": sbody,
               "needs_attention": not 200 <= scode < 300}
        steps.append(out)
        if not 200 <= scode < 300:
            return 1, out, False, steps

    if action in ("claim", "all"):
        ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers, retry=True)
        if ccode in (401, 403):
            out = dict(_auth_failure(ccode), step="claim")
            steps.append(out)
            return 1, out, False, steps
        success = 200 <= ccode < 300 or (ccode == 400 and _is_already_checked_in(cbody))
        out = {"step": "claim", "http": ccode, "body": cbody, "needs_attention": not success}
        steps.append(out)
        return (0 if success else 1), out, False, steps

    return 0, {"result": "OK"}, False, steps


def _run_one_account(action, auth_file, label=None):
    """跑单个账号的凭据；返回 (code, out, quiet, steps)，所有异常都转成可汇报结果。"""
    if auth_file is None:
        return 1, {"result": "MISSING", "needs_attention": True,
                   "report": "该账号的凭据副本已丢失，请重新登录该账号后执行 bind --force"}, False, []

    try:
        session = load_session_retry(auth_file)
    except json.JSONDecodeError as e:
        # JSONDecodeError 是 ValueError 的子类，必须先于下面的分支捕获，否则会被误归类
        return 2, {"result": "ERROR", "needs_attention": True,
                   "report": "登录凭据文件不是合法 JSON（%s），请重新登录 WorkBuddy 桌面端" % e}, False, []
    except ValueError as e:
        # 文件编码损坏（UnicodeDecodeError 也是 ValueError 子类）等情形
        return 2, {"result": "ERROR", "needs_attention": True,
                   "report": "登录凭据文件内容损坏（%s: %s），请重新登录 WorkBuddy 桌面端"
                             % (type(e).__name__, e)}, False, []
    except Exception as e:
        # 无读取权限等其它 IO 问题
        return 2, {"result": "ERROR", "needs_attention": True,
                   "report": "读取登录凭据失败（%s: %s），请重新登录 WorkBuddy 桌面端"
                             % (type(e).__name__, e)}, False, []

    try:
        if action == "doctor":
            return 0, run_doctor(session), False, []
        expired = _session_expired(session)
        if expired:
            hint = ("signin.py bind %s --force" % label) if label else "bind --force"
            return 1, {"result": "EXPIRED", "needs_attention": True,
                       "report": "该账号的登录凭据已于 %s 过期，请重新登录该账号后执行 %s"
                                 % (expired, hint)}, False, []
        session = resolve_session(session)
        headers = build_headers(session)
    except AuthError as e:
        return 1, e.output(), False, []

    endpoint = ((session.get("auth") or {}).get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")
    # 只有具名账号（绑定过的）才带 uid 提示：没绑账号时输出与原来保持一致。
    out_entry = {"uid_hint": _uid_hint(session["account"].get("uid"))} if label else {}
    code, out, quiet, steps = _dispatch(action, headers, endpoint)
    out = dict(out_entry, **out) if isinstance(out, dict) else out
    return code, out, quiet, steps


def _run_rotation(action, contexts, warnings):
    """多账号串行：一个账号跑完再切下一个；单个账号失败不中断其余账号。

    预算在同一份总预算里按"剩余时间 / 剩余账号数"切片（见 _budget_slice），
    切出来的时间片不够开一个新账号时，剩下的账号记 DEFERRED 留给下一轮——
    绝不能让定时任务跑穿 ExecutionTimeLimit 被系统强杀，那样连日志都写不出来。
    """
    total = len(contexts)
    results = []
    overall = 0
    quiet_all = True
    credits = 0
    total_state = _budget_snapshot()
    for index, (label, path, _) in enumerate(contexts):
        remaining = _budget_left()
        if remaining < ACCOUNT_MIN_SLICE:
            for rest_label, _, _ in contexts[index:]:
                results.append({"account": rest_label or "客户端当前登录", "result": "DEFERRED",
                                "code": 0, "report": "时间预算不足，留到下一轮"})
            quiet_all = False
            break
        _budget_slice(remaining / (total - index))
        try:
            code, out, quiet, _ = _run_one_account(action, path, label)
        except Exception as e:
            # 一个账号的意外异常绝不能带走其余账号——"切到下一个账号"正是这个功能的意义。
            code, quiet = 2, False
            out = {"result": "ERROR", "needs_attention": True,
                   "report": "该账号执行异常（%s: %s）" % (type(e).__name__, e)}
        finally:
            _budget_restore(total_state)
        quiet_all = quiet_all and quiet
        if code != 0:
            overall = 1
        credits += as_int(out.get("credits_gained"), 0)
        row = {"account": label or "客户端当前登录", "result": out.get("result"),
               "code": code, "report": out.get("report")}
        if out.get("uid_hint"):
            row["uid"] = out["uid_hint"]
        results.append(row)
        _record_account_run(label, out, code)

    out = {"result": "ALL", "total": total, "accounts": results, "credits_gained": credits,
           "needs_attention": overall != 0,
           "report": "共 %d 个账号 → %s" % (total, "；".join(
               "[%s] %s" % (row["account"], row["report"]) for row in results))}
    if warnings:
        out["account_warnings"] = warnings
    if action in POLL_ACTIONS:
        # 空跑默认不落盘——一天要跑好几轮，全写进去只会把真正有价值的记录
        # 淹没在一串"Buddy 旅行中"里。要看完整过程就设 WORKBUDDY_GROWTH_LOG_EMPTY=1。
        out["trigger"] = "poll"
        if (not quiet_all) or _env_flag("WORKBUDDY_GROWTH_LOG_EMPTY"):
            emit(out, action)
        return overall
    emit(out, action)
    return overall


def _run(action, account=None, source=None, force=False):
    name = account
    # auto-all / silent-all 是显式写法，语义与绑定多账号后的 auto / silent 相同。
    if action in ("auto-all", "silent-all"):
        action = "silent" if action == "silent-all" else "auto"
    _start_budget(action)
    if action not in KNOWN_ACTIONS:
        emit({"result": "ERROR",
              "report": "未知命令：%s（可用：%s）" % (action, " / ".join(KNOWN_ACTIONS))},
             action)
        return 2

    if action in ACCOUNT_ACTIONS:
        return _run_account_command(action, name, source, force)

    if action == "help":
        emit(_help_output(), action)
        return 0

    try:
        contexts, warnings = credential_plan(name, rotate=action in ROTATION_ACTIONS)
    except AccountsError as e:
        emit(e.output(), action)
        return 2 if e.result == "NO_AUTH" else 1

    if not contexts:
        emit({"result": "NO_ACCOUNTS", "report": "账号清单里没有可执行的账号；"
                                                "请先登录 WorkBuddy 桌面端后运行 signin.py bind <账号名>",
              "needs_attention": True}, action)
        return 1

    if len(contexts) > 1:
        # 多账号：预算要按账号数放大（默认值），再在账号之间切片。
        _start_budget(action, len(contexts))
        return _run_rotation(action, contexts, warnings)

    label, path, _ = contexts[0]
    code, out, quiet, steps = _run_one_account(action, path, label)
    _record_account_run(label, out, code)
    if label:
        out.setdefault("account", label)
    if warnings:
        out["account_warnings"] = warnings
    for step in steps:
        emit(step, action)
    if not steps:
        if action in POLL_ACTIONS:
            if (not quiet) or _env_flag("WORKBUDDY_GROWTH_LOG_EMPTY"):
                emit(out, action)
        else:
            emit(out, action)
    return code

if __name__ == "__main__":
    sys.exit(main())
