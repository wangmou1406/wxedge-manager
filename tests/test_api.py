"""WXEdge 魔方管理器 · API 集成测试（模拟后端，无需 Docker）。

运行:
    python -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app", "main.py")


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.data_dir = tempfile.mkdtemp(prefix="wxedge-test-")
        cls.proc = subprocess.Popen(
            [sys.executable, APP, "--host", "127.0.0.1", "--port", str(cls.port),
             "--data-dir", cls.data_dir, "--sim"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        cls.base = "http://127.0.0.1:%d" % cls.port
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                cls.request("GET", "/api/health")
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.25)
        output = ""
        try:
            output = cls.proc.stdout.read()
        except Exception:
            pass
        raise RuntimeError("测试服务启动失败：\n" + output)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=8)
        except Exception:
            try:
                cls.proc.kill()
            except Exception:
                pass
        import shutil
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @classmethod
    def request(cls, method, path, body=None, token=None, expect=200):
        data = None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Auth-Token"] = token
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(cls.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
        assert status == expect, "期望 HTTP %s，实际 %s：%s" % (expect, status, payload)
        return payload

    # ---------- 基础 ----------
    def test_01_health(self):
        data = self.request("GET", "/api/health")
        self.assertTrue(data["ok"])
        self.assertEqual(data["name"], "wxedge-manager")

    def test_02_overview(self):
        data = self.request("GET", "/api/overview")
        self.assertEqual(data["backend"]["mode"], "sim")
        self.assertIn("cpu", data["host"])
        self.assertEqual(data["instances"], [])

    # ---------- 实例生命周期 ----------
    def test_03_create_actions_delete(self):
        created = self.request("POST", "/api/instances", {
            "name": "测试节点A",
            "cpus": 1.5,
            "memory_mb": 2048,
            "disk_gb": 40,
            "listenf_port": 19999,
            "listen_port": 19999,
            "data_dir": os.path.join(self.data_dir, "instance-a"),
        })
        iid = created["id"]
        self.assertEqual(created["cpus"], 1.5)
        self.assertEqual(created["memory_mb"], 2048)
        overview = self.request("GET", "/api/overview")
        self.assertEqual(len(overview["instances"]), 1)
        # docker run -d 会立即启动容器，模拟后端保持同样语义
        self.assertEqual(overview["instances"][0]["runtime"]["state"], "running")

        started = self.request("POST", "/api/instances/%s/action" % iid, {"action": "start"})
        self.assertEqual(started["runtime"]["state"], "running")
        stopped = self.request("POST", "/api/instances/%s/action" % iid, {"action": "stop"})
        self.assertEqual(stopped["runtime"]["state"], "exited")
        restarted = self.request("POST", "/api/instances/%s/action" % iid, {"action": "restart"})
        self.assertEqual(restarted["runtime"]["state"], "running")

        updated = self.request("PUT", "/api/instances/%s" % iid, {
            "name": "测试节点A2", "cpus": 2, "memory_mb": 3072, "disk_gb": 50,
            "listen_port": 19998,
        })
        self.assertEqual(updated["name"], "测试节点A2")
        self.assertEqual(updated["memory_mb"], 3072)

        logs = self.request("GET", "/api/instances/%s/logs?tail=50" % iid)
        self.assertIn("模拟", logs["text"])

        deleted = self.request("DELETE", "/api/instances/%s?purge=1" % iid)
        self.assertTrue(deleted["purged"])
        self.assertEqual(self.request("GET", "/api/overview")["instances"], [])

    # ---------- 校验 ----------
    def test_04_validation(self):
        self.request("POST", "/api/instances", {"name": ""}, expect=400)
        self.request("POST", "/api/instances", {"name": "X", "cpus": "abc"}, expect=400)
        self.request("POST", "/api/instances", {"name": "X", "memory_mb": 1}, expect=400)
        self.request("POST", "/api/instances", {"name": "X", "data_dir": "relative/path"}, expect=400)
        self.request("POST", "/api/instances", {"name": "X", "listen_port": 80}, expect=400)
        first = self.request("POST", "/api/instances", {"name": "端口占用1", "listen_port": 20001,
                                                        "data_dir": os.path.join(self.data_dir, "p1")})
        second = self.request("POST", "/api/instances", {"name": "端口占用2", "listen_port": 20001,
                                                         "data_dir": os.path.join(self.data_dir, "p2")}, expect=400)
        self.assertIn("占用", second["error"])
        self.request("DELETE", "/api/instances/%s" % first["id"], expect=200)
        # 不存在的实例
        self.request("GET", "/api/instances/wx-404", expect=404)
        self.request("POST", "/api/instances/wx-404/action", {"action": "start"}, expect=404)

    # ---------- 自动配置 ----------
    def test_05_plan_preview_batch(self):
        plan = self.request("POST", "/api/plan", {
            "count": 2, "strategy": "balanced",
            "overrides": {"storage_root": os.path.join(self.data_dir, "batch"),
                          "port_base": 21000, "name_prefix": "批量"},
        })
        self.assertEqual(len(plan["items"]), 2)
        self.assertTrue(all(item["cpus"] > 0 and item["memory_mb"] >= 128 for item in plan["items"]))
        ports = [item["listen_port"] for item in plan["items"]]
        self.assertEqual(len(set(ports)), 2)

        big = self.request("POST", "/api/plan", {
            "count": 25, "strategy": "balanced",
            "overrides": {"storage_root": os.path.join(self.data_dir, "batch-big"), "port_base": 23000},
        })
        self.assertEqual(len(big["items"]), 25)

        fixed = self.request("POST", "/api/plan", {
            "count": 3, "strategy": "balanced",
            "overrides": {"storage_root": os.path.join(self.data_dir, "batch-fixed"),
                          "port_base": 24000, "port_end": 24002,
                          "cpus": 1.5, "memory_mb": 1024, "disk_gb": 40},
        })
        self.assertEqual(len(fixed["items"]), 3)
        self.assertTrue(all(item["cpus"] == 1.5 and item["memory_mb"] == 1024 and item["disk_gb"] == 40 for item in fixed["items"]))
        self.assertEqual(sorted(item["listen_port"] for item in fixed["items"]), [24000, 24001, 24002])

        self.request("POST", "/api/plan", {
            "count": 3,
            "overrides": {"port_base": 24100, "port_end": 24101},
        }, expect=400)

        preview = self.request("POST", "/api/preview", {"instance": {
            "name": "预览", "cpus": 2, "memory_mb": 2048, "disk_gb": 60, "listen_port": 22000,
            "data_dir": os.path.join(self.data_dir, "preview"),
        }})
        self.assertIn("docker", preview["command"])
        self.assertIn("--privileged", preview["args"])
        self.assertIn("--cpus", preview["args"])
        self.assertIn("LISTEN_ADDR=:22000", preview["args"])

        before = len(self.request("GET", "/api/overview")["instances"])
        result = self.request("POST", "/api/batch", {
            "count": 2, "strategy": "eco",
            "overrides": {"storage_root": os.path.join(self.data_dir, "batch2"),
                          "port_base": 21200, "name_prefix": "批量"},
        })
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(result["errors"], [])
        overview = self.request("GET", "/api/overview")
        self.assertEqual(len(overview["instances"]), before + 2)
        for item in result["created"]:
            self.request("DELETE", "/api/instances/%s?purge=1" % item["id"])

    # ---------- 设置 / 令牌 ----------
    def test_06_settings_and_auth(self):
        data = self.request("PUT", "/api/settings", {"poll_seconds": 6, "auto_heal": False})
        self.assertEqual(data["settings"]["poll_seconds"], 6)
        self.assertFalse(data["settings"]["auto_heal"])

        self.request("PUT", "/api/settings", {"auth_token": "secret123"}, token=None)
        self.request("GET", "/api/overview", expect=401)
        authed = self.request("GET", "/api/overview", token="secret123")
        self.assertTrue(authed["settings"]["auth_enabled"])
        self.assertEqual(authed["settings"]["auth_token"], "***")

        wrong = self.request("POST", "/api/auth/verify", token="bad")
        self.assertFalse(wrong["ok"])
        verify = self.request("POST", "/api/auth/verify", token="secret123")
        self.assertTrue(verify["ok"])

        self.request("PUT", "/api/settings", {"auth_token": ""}, token="secret123")
        self.request("GET", "/api/overview")

    # ---------- 存储限额 / 面板端口 ----------
    def test_08_panel_and_quota_settings(self):
        overview = self.request("GET", "/api/overview")
        self.assertIn("panel", overview)
        self.assertIn("bind_port", overview["panel"])

        data = self.request("PUT", "/api/settings", {
            "panel_bind_host": "127.0.0.1", "panel_bind_port": 18080,
            "default_quota_action": "stop",
        })
        self.assertEqual(data["settings"]["panel_bind_host"], "127.0.0.1")
        self.assertEqual(data["settings"]["panel_bind_port"], 18080)
        self.assertEqual(data["settings"]["default_quota_action"], "stop")
        self.request("PUT", "/api/settings", {"panel_bind_port": 80}, expect=400)
        self.request("PUT", "/api/settings", {"default_quota_action": "boom"}, expect=400)
        self.request("PUT", "/api/settings", {"panel_bind_host": "bad host"}, expect=400)

        created = self.request("POST", "/api/instances", {
            "name": "配额测试", "disk_gb": 20, "quota_action": "stop",
            "data_dir": os.path.join(self.data_dir, "quota-test"),
            "listen_port": 23001,
        })
        self.assertEqual(created["quota_action"], "stop")
        self.request("PUT", "/api/instances/%s" % created["id"], {"quota_action": "bogus"}, expect=400)
        overview = self.request("GET", "/api/overview")
        inst = [i for i in overview["instances"] if i["id"] == created["id"]][0]
        runtime = inst["runtime"]
        self.assertEqual(runtime["disk_limit_bytes"], 20 * 1024 ** 3)
        self.assertIn("disk_used_bytes", runtime)
        self.assertIn("disk_usage_pct", runtime)
        self.assertIn("disk_mounted", runtime)
        self.request("DELETE", "/api/instances/%s" % created["id"], expect=200)

    # ---------- 自检 / 任务 ----------
    def test_07_self_check_and_job(self):
        check = self.request("GET", "/api/self-check")
        self.assertTrue(check["checks"])
        names = {item["name"] for item in check["checks"]}
        self.assertIn("Docker CLI", names)
        job = self.request("POST", "/api/pull-image")
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            status = self.request("GET", "/api/jobs/" + job["id"])
            if status["status"] != "running":
                break
            time.sleep(0.3)
        self.assertEqual(status["status"], "done")
        self.request("GET", "/api/jobs/nope", expect=404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
