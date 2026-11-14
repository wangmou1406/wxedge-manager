"""模拟后端：没有 Docker 时也能完整演示/测试 WebUI。"""
import hashlib
import json
import os
import random
import threading
import time


class SimBackend:
    name = "sim"

    def __init__(self, runtime_path):
        self.runtime_path = runtime_path
        self._lock = threading.RLock()
        self._containers = self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(self.runtime_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.runtime_path), exist_ok=True)
            tmp = self.runtime_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._containers, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.runtime_path)
        except OSError:
            pass

    # ---------- 连接 ----------
    def info(self, force=False):
        return {"available": True, "mode": "sim", "version": "sim-1.0",
                "sim": True, "note": "模拟模式：未连接 Docker，仅演示/测试界面"}

    def available(self):
        return True

    # ---------- 容器 ----------
    def list_containers(self):
        with self._lock:
            items = []
            for name, row in self._containers.items():
                started = float(row.get("started_at") or 0)
                uptime = ""
                if row.get("state") == "running" and started:
                    secs = int(time.time() - started)
                    uptime = "Up %d minutes" % max(1, secs // 60) if secs < 3600 else "Up %d hours" % (secs // 3600)
                elif row.get("state") == "exited":
                    uptime = "Exited (0) %d minutes ago" % max(1, int((time.time() - started) // 60)) if started else "Exited (0)"
                items.append({
                    "name": name,
                    "state": row.get("state", "exited"),
                    "status": uptime or ("Created" if row.get("state") == "created" else "Exited (0)"),
                    "image": row.get("image", ""),
                    "created": row.get("created_at", ""),
                    "labels": {"wxedge.manager": "1", "wxedge.instance": row.get("id", "")},
                })
            return items

    def stats(self):
        result = {}
        now = time.time()
        with self._lock:
            for name, row in self._containers.items():
                if row.get("state") != "running":
                    result[name] = {"cpu_pct": 0.0, "mem_usage_bytes": 0,
                                    "mem_pct": 0.0, "net_io": "0B / 0B", "block_io": "0B / 0B", "pids": "0"}
                    continue
                bucket = int(now // 5)
                seed = int(hashlib.sha256(("%s-%s" % (name, bucket)).encode()).hexdigest()[:8], 16)
                rng = random.Random(seed)
                cpus = float(row.get("cpus") or 1)
                mem_limit = int(row.get("memory_mb") or 512) * 1024 * 1024
                cpu_pct = round(rng.uniform(3.0, 38.0) * min(cpus, 4.0) / 2.0, 1)
                mem_use = int(mem_limit * rng.uniform(0.28, 0.66))
                net = rng.uniform(0.2, 40.0) * 1024 * 1024
                result[name] = {
                    "cpu_pct": cpu_pct,
                    "mem_usage_bytes": mem_use,
                    "mem_limit_bytes": mem_limit,
                    "mem_pct": round(100.0 * mem_use / mem_limit, 1) if mem_limit else 0.0,
                    "net_io": "%.1fMB / %.1fMB" % (net, net * rng.uniform(0.6, 1.4)),
                    "block_io": "%.1fMB / %.1fMB" % (rng.uniform(1, 80), rng.uniform(1, 40)),
                    "pids": str(rng.randint(4, 18)),
                }
        return result

    def create(self, inst, settings):
        storage = os.path.join(inst["data_dir"], "storage")
        runtime_dir = inst.get("runtime_dir") or os.path.join(inst["data_dir"], "containerd")
        os.makedirs(storage, exist_ok=True)
        os.makedirs(runtime_dir, exist_ok=True)
        with self._lock:
            self._containers[inst["container_name"]] = {
                "id": inst.get("id", ""),
                # docker run -d 会立即启动容器，模拟后端保持同样的语义
                "state": "running",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "started_at": time.time(),
                "image": inst.get("image") or settings.get("image"),
                "cpus": inst.get("cpus"),
                "memory_mb": inst.get("memory_mb"),
                "disk_gb": inst.get("disk_gb"),
                "listen_port": inst.get("listen_port"),
                "network_mode": inst.get("network_mode"),
            }
            self._save()
        return "sim-" + hashlib.sha256(inst["container_name"].encode()).hexdigest()[:12]

    def start(self, name):
        with self._lock:
            row = self._containers.get(name)
            if not row:
                raise RuntimeError("容器不存在")
            row["state"] = "running"
            row["started_at"] = time.time()
            self._save()

    def stop(self, name):
        with self._lock:
            row = self._containers.get(name)
            if not row:
                raise RuntimeError("容器不存在")
            row["state"] = "exited"
            row["started_at"] = time.time()
            self._save()

    def restart(self, name):
        self.stop(name)
        time.sleep(0.2)
        self.start(name)

    def remove(self, name):
        with self._lock:
            self._containers.pop(name, None)
            self._save()

    def logs(self, name, tail=200):
        with self._lock:
            row = self._containers.get(name) or {}
        lines = [
            "[模拟后端] 以下为模拟日志，非真实容器输出",
            "time=%s level=info msg=wxedge container starting" % time.strftime("%Y-%m-%dT%H:%M:%S"),
            "time=%s level=info msg=listen_addr=:%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), row.get("listen_port", "?")),
            "time=%s level=info msg=storage=/storage (bind mount)" % time.strftime("%Y-%m-%dT%H:%M:%S"),
            "time=%s level=info msg=containerd_path=/var/lib/containerd" % time.strftime("%Y-%m-%dT%H:%M:%S"),
            "time=%s level=info msg=wxnode found: false (等待激活)" % time.strftime("%Y-%m-%dT%H:%M:%S"),
            "time=%s level=info msg=service started" % time.strftime("%Y-%m-%dT%H:%M:%S"),
            "time=%s level=info msg=heartbeat ok" % time.strftime("%Y-%m-%dT%H:%M:%S"),
        ]
        if row.get("state") == "running":
            lines.append("time=%s level=info msg=bandwidth scheduling ready" % time.strftime("%Y-%m-%dT%H:%M:%S"))
        for i in range(max(0, int(tail) - len(lines))):
            lines.insert(2, "[%04d] heartbeat sequence=%d" % (i, i))
        return "\n".join(lines[-int(tail):]) + "\n"

    # ---------- 镜像 ----------
    def image_exists(self, image):
        return True

    def pull(self, image, on_line=None):
        steps = ["Pulling from wxedge/wxedge", "Waiting for download...", "Verifying Checksum",
                 "Download complete", "Status: Downloaded newer image for " + image]
        for step in steps:
            if on_line:
                on_line("[模拟] " + step)
            time.sleep(0.35)
        return steps

    # ---------- 其他 ----------
    def watchtower_status(self):
        return "absent"

    def ensure_watchtower(self):
        return "absent"

    def stop_watchtower(self):
        return None

    def setup_loop_disk(self, inst):
        return os.path.join(inst["data_dir"], "storage")

    def teardown_loop_disk(self, inst):
        return None

    def remove_macvlan(self, net):
        return None
