"""本地 HTTP 面板与 API。

HTTP 层只做参数校验与命令入队，不直接执行换号/迁移；业务逻辑集中在 service。
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import subprocess

from . import config, service
from .state import commands, write_state


def _log_tail(n):
    if not config.LOG.exists():
        return []
    try:
        return config.LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


class HubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 面板关闭导致连接断开，不应污染日志或中断守护进程

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                if config.DASHBOARD.exists():
                    self._send(200, config.DASHBOARD.read_text(encoding="utf-8"),
                               "text/html; charset=utf-8")
                else:
                    self._send(404, "hub-dashboard.html missing", "text/plain; charset=utf-8")
            elif path == "/api/state":
                state = service.external_state()
                state["log"] = _log_tail(60)
                self._json(200, state)
            elif path.startswith("/assets/dashboard/"):
                self._serve_asset(path[len("/assets/dashboard/"):])
            else:
                self._send(404, "not found")
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _serve_asset(self, relative):
        """只服务面板自身静态资源，阻止路径穿越。"""
        root = config.ASSETS.resolve()
        target = (config.ASSETS / relative).resolve()
        if target != root and root not in target.parents:
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8")
        types = {
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        ctype = types.get(target.suffix.lower(), "application/octet-stream")
        try:
            self._send(200, target.read_text(encoding="utf-8"), ctype)
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > config.HTTP_BODY_LIMIT:
                return self._json(400, {"ok": False, "error": "body too large"})
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            query = parse_qs(raw)

            if path == "/api/switch":
                name = (query.get("name") or [""])[0].strip()
                if not name or len(name) > 64 or any(ch in name for ch in "/\\\r\n"):
                    return self._json(400, {"ok": False, "error": "invalid name"})
                commands.put("switch", name)
                return self._json(200, {"ok": True, "msg": "switch queued"})

            if path == "/api/sync":
                commands.put("sync", True)
                return self._json(200, {"ok": True, "msg": "sync queued"})

            if path == "/api/balance":
                commands.put("balance", True)
                return self._json(200, {"ok": True, "msg": "balance refresh queued"})

            if path == "/api/pause":
                write_state(status="paused", paused=True, last_action="paused by user")
                service.invalidate_state_cache()
                return self._json(200, {"ok": True, "msg": "paused"})

            if path == "/api/resume":
                write_state(status="watching", paused=False, last_action="resumed by user")
                service.invalidate_state_cache()
                return self._json(200, {"ok": True, "msg": "resumed"})

            if path == "/api/open/backups":
                if config.BACKUPS.exists():
                    subprocess.Popen(["explorer", str(config.BACKUPS)],
                                     creationflags=config.NO_WINDOW)
                return self._json(200, {"ok": True})

            if path == "/api/open/log":
                if config.LOG.exists():
                    subprocess.Popen(["notepad.exe", str(config.LOG)],
                                     creationflags=config.NO_WINDOW)
                return self._json(200, {"ok": True})

            self._send(404, "not found")
        except Exception as exc:
            self._json(500, {"error": str(exc)})


def create_server(host="127.0.0.1", port=None):
    return ThreadingHTTPServer((host, config.PORT if port is None else port), HubHandler)
