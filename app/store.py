"""JSON 状态持久化。"""
import copy
import json
import os
import threading

DEFAULT_IMAGE = "images-cluster.xycloud.com/wxedge/wxedge:latest"


def default_settings(data_dir):
    return {
        "image": DEFAULT_IMAGE,
        "storage_root": os.path.join(data_dir, "instances"),
        "port_base": 18888,
        "backend_mode": "auto",          # auto | docker | sim
        "auto_heal": True,
        "auto_update": False,
        "health_interval": 15,
        "restart_policy": "always",      # always | unless-stopped | no
        "disk_mode": "dir",              # dir | loop
        "default_quota_action": "none",  # none | stop（目录模式超限动作）
        "name_prefix": "节点",
        "auth_token": "",
        "poll_seconds": 4,
        "panel_bind_host": "0.0.0.0",
        "panel_bind_port": 8080,
    }


class Store:
    """线程安全的内存状态 + 原子落盘 JSON。"""

    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self.path = os.path.join(self.data_dir, "state.json")
        self.lock = threading.RLock()
        os.makedirs(self.data_dir, exist_ok=True)
        self.data = self._load()

    # ---------- 基础 ----------
    def _default(self):
        return {
            "version": 1,
            "settings": default_settings(self.data_dir),
            "instances": [],
            "activity": [],
            "meta": {"next_index": 1},
        }

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return self._default()
        if not isinstance(data, dict):
            return self._default()
        base = self._default()
        base["settings"].update(data.get("settings") or {})
        base["instances"] = data.get("instances") or []
        base["activity"] = data.get("activity") or []
        base["meta"].update(data.get("meta") or {})
        return base

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self):
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def update(self, fn):
        with self.lock:
            result = fn(self.data)
            self._save_locked()
            return result

    # ---------- 便捷方法 ----------
    def settings(self):
        with self.lock:
            return copy.deepcopy(self.data["settings"])

    def set_settings(self, patch):
        def fn(data):
            data["settings"].update(patch)
            return copy.deepcopy(data["settings"])
        return self.update(fn)

    def instances(self):
        with self.lock:
            return copy.deepcopy(self.data["instances"])

    def get_instance(self, iid):
        with self.lock:
            for item in self.data["instances"]:
                if item.get("id") == iid:
                    return copy.deepcopy(item)
        return None

    def put_instance(self, inst):
        def fn(data):
            for idx, item in enumerate(data["instances"]):
                if item.get("id") == inst.get("id"):
                    data["instances"][idx] = inst
                    return True
            data["instances"].append(inst)
            return True
        return self.update(fn)

    def remove_instance(self, iid):
        def fn(data):
            before = len(data["instances"])
            data["instances"] = [x for x in data["instances"] if x.get("id") != iid]
            return before != len(data["instances"])
        return self.update(fn)

    def next_instance_number(self):
        def fn(data):
            idx = int(data["meta"].get("next_index", 1))
            data["meta"]["next_index"] = idx + 1
            return idx
        return self.update(fn)

    def add_activity(self, level, message, instance_id=None, instance_name=None):
        entry = {
            "ts": __import__("time").time(),
            "level": level,
            "message": message,
            "instance_id": instance_id,
            "instance_name": instance_name,
        }

        def fn(data):
            data["activity"].insert(0, entry)
            del data["activity"][120:]
            return entry
        return self.update(fn)
