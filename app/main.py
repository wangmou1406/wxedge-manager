"""WXEdge 魔方管理器 · Web 服务入口（纯 Python 标准库，无第三方依赖）。

用法:
    python3 app/main.py --host 0.0.0.0 --port 8080 --data-dir ./data
"""
import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from manager import ApiError, Manager, VERSION  # noqa: E402

STATIC_DIR = os.path.join(BASE_DIR, "static")
LOG = logging.getLogger("wxedge-manager")


def json_response(handler, code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "WXEdgeManager/" + VERSION
    protocol_version = "HTTP/1.1"
    manager = None

    def log_message(self, fmt, *args):
        LOG.debug("%s - " + fmt, self.address_string(), *args)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        self._query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api" or path.startswith("/api/"):
                if not self._authorized(path):
                    raise ApiError("需要访问令牌：请在登录框输入，或携带 X-Auth-Token 请求头", 401)
                payload = self._read_json() if method in ("POST", "PUT", "DELETE") else {}
                self._route_api(method, path, payload)
            else:
                self._serve_static(path)
        except ApiError as exc:
            json_response(self, exc.status, {"ok": False, "error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            LOG.exception("请求处理失败: %s", exc)
            json_response(self, 500, {"ok": False, "error": "服务器内部错误：%s" % exc})

    def _authorized(self, path):
        settings = self.manager.store.settings()
        token = settings.get("auth_token") or ""
        if not token:
            return True
        if path in ("/api/health", "/api/auth/verify"):
            return True
        supplied = (self.headers.get("X-Auth-Token") or "").strip()
        if not supplied:
            values = self._query.get("token") or []
            supplied = (values[0] if values else "").strip()
        return supplied == token

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ApiError("请求体过大", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("请求体不是合法的 JSON")
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return data

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if not rel or ".." in rel.split("/") or rel.startswith("/"):
            self._not_found()
            return
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.normpath(STATIC_DIR) + os.sep):
            self._not_found()
            return
        if not os.path.isfile(full):
            if not os.path.splitext(rel)[1]:
                full = os.path.join(STATIC_DIR, "index.html")
            else:
                self._not_found()
                return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        charset = "; charset=utf-8" if (ctype.startswith("text/") or ctype in (
            "application/javascript", "application/json", "image/svg+xml")) else ""
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._not_found()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype + charset)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _not_found(self):
        body = b"404 Not Found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, payload):
        json_response(self, 200, payload)

    def _route_api(self, method, path, payload):
        mgr = self.manager
        data = payload or {}
        if method == "GET" and path == "/api/health":
            self._send({"ok": True, "name": "wxedge-manager", "version": VERSION,
                        "auth_required": bool(mgr.store.settings().get("auth_token")),
                        "bind_host": mgr.host, "bind_port": mgr.port,
                        "time": time.time()})
            return
        if method == "POST" and path == "/api/system/restart":
            settings = mgr.store.settings()
            self._send({
                "ok": True, "restarting": True,
                "bind_host": settings.get("panel_bind_host") or "0.0.0.0",
                "bind_port": int(settings.get("panel_bind_port") or 8080),
            })
            threading.Thread(target=_restart_process, args=(mgr.data_dir,), daemon=True).start()
            return
        if method == "POST" and path == "/api/auth/verify":
            token = mgr.store.settings().get("auth_token") or ""
            supplied = (self.headers.get("X-Auth-Token") or "").strip()
            self._send({"ok": (not token) or supplied == token})
            return
        if method == "GET" and path == "/api/storage-paths":
            self._send(mgr.storage_paths())
            return
        if method == "GET" and path == "/api/overview":
            self._send(mgr.overview())
            return
        if method == "GET" and path == "/api/instances":
            runtime = mgr.refresh_runtime(force=True)
            self._send({"instances": [mgr._public_instance(i, runtime) for i in mgr.store.instances()],
                        "time": time.time()})
            return
        if method == "POST" and path == "/api/instances":
            self._send(mgr.create_instance(data))
            return
        if method == "POST" and path == "/api/batch":
            self._send(mgr.batch_create(data.get("count", 1), data.get("strategy", "balanced"),
                                        data.get("overrides") or {}))
            return
        if method == "POST" and path == "/api/plan":
            self._send(mgr.plan_batch(data.get("count", 1), data.get("strategy", "balanced"),
                                      data.get("overrides") or {}))
            return
        if method == "POST" and path == "/api/preview":
            self._send(mgr.preview(data.get("instance") or data))
            return
        if method == "GET" and path == "/api/settings":
            self._send({"settings": mgr._public_settings(mgr.store.settings())})
            return
        if method == "PUT" and path == "/api/settings":
            self._send({"settings": mgr.update_settings(data)})
            return
        if method == "GET" and path == "/api/self-check":
            self._send(mgr.self_check())
            return
        if method == "POST" and path == "/api/pull-image":
            self._send(mgr.start_pull_job())
            return
        if method == "GET" and path == "/api/activity":
            self._send({"activity": mgr.store.snapshot().get("activity", [])[:80]})
            return
        match = re.match(r"^/api/jobs/([A-Za-z0-9\-_]+)$", path)
        if method == "GET" and match:
            self._send(mgr.get_job(match.group(1)))
            return
        match = re.match(r"^/api/instances/([A-Za-z0-9\-_]+)/logs$", path)
        if method == "GET" and match:
            values = self._query.get("tail") or ["200"]
            try:
                tail = int(values[0])
            except (TypeError, ValueError):
                tail = 200
            self._send(mgr.instance_logs(match.group(1), tail))
            return
        match = re.match(r"^/api/instances/([A-Za-z0-9\-_]+)/action$", path)
        if method == "POST" and match:
            self._send(mgr.action(match.group(1), str(data.get("action", ""))))
            return
        match = re.match(r"^/api/instances/([A-Za-z0-9\-_]+)$", path)
        if match:
            iid = match.group(1)
            if method == "GET":
                self._send(mgr.instance_detail(iid))
                return
            if method == "PUT":
                self._send(mgr.update_instance(iid, data))
                return
            if method == "DELETE":
                values = self._query.get("purge") or ["0"]
                purge = str(values[0]).lower() in ("1", "true", "yes")
                self._send(mgr.delete_instance(iid, purge))
                return
        raise ApiError("接口不存在：%s %s" % (method, path), 404)


def _restart_process(data_dir):
    """在当前进程内 exec 重启，重新读取面板端口设置。"""
    time.sleep(0.8)
    script = os.path.join(BASE_DIR, "main.py")
    os.execv(sys.executable, [sys.executable, script, "--data-dir", data_dir])


def main():
    parser = argparse.ArgumentParser(description="WXEdge 魔方管理器（网心云容器多开管理 WebUI）")
    parser.add_argument("--host", default=os.environ.get("WXEDGE_HOST"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ["WXEDGE_PORT"]) if os.environ.get("WXEDGE_PORT") else None)
    parser.add_argument("--data-dir", default=os.environ.get(
        "WXEDGE_DATA_DIR", os.path.join(os.path.dirname(BASE_DIR), "data")))
    parser.add_argument("--sim", action="store_true", help="强制使用模拟后端（不操作真实 Docker）")
    parser.add_argument("--token", default="", help="启动时设置访问令牌（至少 6 位）")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mgr = Manager(os.path.abspath(args.data_dir), sim_override=args.sim,
                  host=args.host or "0.0.0.0", port=args.port or 8080)
    if args.token:
        try:
            mgr.update_settings({"auth_token": args.token})
        except ApiError as exc:
            LOG.error("设置访问令牌失败：%s", exc.message)

    # 面板监听：命令行/环境变量 > 状态文件中的设置（命令行参数会写回设置）
    settings0 = mgr.store.settings()
    seed = {}
    if args.host is not None:
        bind_host = args.host
        if bind_host != settings0.get("panel_bind_host"):
            seed["panel_bind_host"] = bind_host
    else:
        bind_host = settings0.get("panel_bind_host") or "0.0.0.0"
    if args.port is not None:
        bind_port = args.port
        if bind_port != settings0.get("panel_bind_port"):
            seed["panel_bind_port"] = bind_port
    else:
        try:
            bind_port = int(settings0.get("panel_bind_port") or 8080)
        except (TypeError, ValueError):
            bind_port = 8080
    if not (1 <= bind_port <= 65535):
        LOG.error("面板端口不合法：%s", bind_port)
        sys.exit(1)
    if seed:
        try:
            mgr.store.set_settings(seed)
        except Exception:
            LOG.exception("写入面板监听设置失败")
    mgr.host, mgr.port = bind_host, bind_port

    Handler.manager = mgr
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((bind_host, bind_port), Handler)
    httpd.daemon_threads = True
    bstate = mgr.backend_state()
    LOG.info("WXEdge 魔方管理器 v%s 已启动", VERSION)
    LOG.info("WebUI: http://%s:%d  数据目录: %s",
             ("127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host),
             bind_port, mgr.data_dir)
    LOG.info("后端模式: %s%s", bstate["mode"], ("（模拟）" if bstate["mode"] == "sim" else ""))
    if bstate.get("warning"):
        LOG.warning("%s", bstate["warning"])
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("收到中断信号，正在退出")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        mgr.shutdown()


if __name__ == "__main__":
    main()
