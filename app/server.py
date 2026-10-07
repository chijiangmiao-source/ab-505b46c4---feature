#!/usr/bin/env python3
"""深空地面站 —— 授权族轮换 HTTP 服务（仅标准库）。

路由：
  GET  /                              操作员页面
  GET  /health                        健康检查（宿主机端口可在 compose 中配置）
  POST /api/families                  创建绑定终端标识的授权族
  GET  /api/terminals/<tid>/family         查询授权族状态（含撤销原因）
  GET  /api/terminals/<tid>/rotation-chain 查询轮换链审计（按发生顺序，仅凭证指纹）
  GET  /api/families/<family_id>           按授权族 ID 查询
  POST /api/rotate                    以旧凭证 + 稳定轮换标识发起轮换（幂等）
  GET  /api/admin/faults              查看故障注入开关
  POST /api/admin/faults              设置故障注入开关 {"drop_after_commit": bool}
  POST /api/admin/shutdown            退出服务进程（由守护循环/compose 重启，用于演练）
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import service
from storage import Storage

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = (BASE_DIR / "static" / "index.html").read_bytes()

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "app.db"))

STORAGE = None  # main() 中初始化

# 故障注入开关（进程内存态，服务重启后自动清除）。
FAULTS = {"drop_after_commit": False}
FAULTS_LOCK = threading.Lock()


def fault_enabled(name):
    with FAULTS_LOCK:
        return FAULTS[name]


def set_fault(name, value):
    with FAULTS_LOCK:
        FAULTS[name] = bool(value)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DeepSpaceGroundStation/1.0"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    # ---------- 工具 ----------

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _drop_response(self):
        """断回应故障：轮换已提交，但直接关闭连接、不送达任何响应。"""
        print("[fault] drop_after_commit: rotation committed, dropping response",
              flush=True)
        self.close_connection = True

    # ---------- GET ----------

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/health":
            return self._send_json(200, {
                "status": "ok",
                "service": "deep-space-ground-station",
                "time": service._now(),
            })
        if path == "/api/admin/faults":
            return self._send_json(200, {"drop_after_commit": fault_enabled("drop_after_commit")})
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "families"]:
            return self._reply_family(service.get_family, parts[2])
        if len(parts) == 4 and parts[:2] == ["api", "terminals"] and parts[3] == "family":
            return self._reply_family(service.get_family_by_terminal, parts[2])
        if len(parts) == 4 and parts[:2] == ["api", "terminals"] and parts[3] == "rotation-chain":
            return self._reply_rotation_chain(parts[2])
        return self._send_json(404, {"error": "not_found"})

    def _reply_family(self, lookup, key):
        try:
            return self._send_json(200, lookup(STORAGE, key))
        except service.UnknownTerminalError:
            return self._send_json(404, {"error": "unknown_terminal"})

    def _reply_rotation_chain(self, terminal_id):
        try:
            return self._send_json(200, service.get_rotation_chain(STORAGE, terminal_id))
        except service.UnknownTerminalError:
            return self._send_json(404, {"error": "unknown_terminal"})

    # ---------- POST ----------

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/families":
            return self._handle_create_family()
        if path == "/api/rotate":
            return self._handle_rotate()
        if path == "/api/admin/faults":
            return self._handle_faults()
        if path == "/api/admin/shutdown":
            return self._handle_shutdown()
        return self._send_json(404, {"error": "not_found"})

    def _handle_create_family(self):
        body = self._read_json()
        if body is None:
            return self._send_json(400, {"error": "bad_json"})
        try:
            result = service.create_family(STORAGE, body.get("terminal_id", ""))
        except ValueError as exc:
            return self._send_json(400, {"error": "bad_request", "detail": str(exc)})
        except service.TerminalAlreadyBoundError:
            return self._send_json(409, {"error": "terminal_already_bound"})
        return self._send_json(201, result)

    def _handle_rotate(self):
        body = self._read_json()
        if body is None:
            return self._send_json(400, {"error": "bad_json"})
        try:
            result = service.rotate(
                STORAGE,
                body.get("terminal_id", ""),
                body.get("credential", ""),
                body.get("rotation_id", ""),
            )
        except ValueError as exc:
            return self._send_json(400, {"error": "bad_request", "detail": str(exc)})
        except service.UnknownTerminalError:
            return self._send_json(404, {"error": "unknown_terminal"})
        except service.InvalidCredentialError:
            return self._send_json(401, {"error": "invalid_credential"})

        # 提交已完成。若启用「断回应」故障，则丢弃响应（客户端只见连接中断）。
        if fault_enabled("drop_after_commit"):
            return self._drop_response()

        status = 200 if result["outcome"] in ("accepted", "replayed") else 409
        return self._send_json(status, result)

    def _handle_faults(self):
        body = self._read_json()
        if body is None:
            return self._send_json(400, {"error": "bad_json"})
        if "drop_after_commit" in body:
            set_fault("drop_after_commit", body["drop_after_commit"])
        print(f"[fault] drop_after_commit = {fault_enabled('drop_after_commit')}",
              flush=True)
        return self._send_json(200, {"drop_after_commit": fault_enabled("drop_after_commit")})

    def _handle_shutdown(self):
        self._send_json(200, {"status": "shutting_down"})

        def _die():
            time.sleep(0.4)  # 让响应先送达
            print("[admin] shutdown requested, restarting service process", flush=True)
            os._exit(0)

        threading.Thread(target=_die, daemon=True).start()


def main():
    global STORAGE
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    STORAGE = Storage(DB_PATH)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"deep-space ground station listening on :{PORT}, db={DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
