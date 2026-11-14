"""核心业务逻辑：实例生命周期、自动配置、自检与健康自愈。"""
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import socket
import sys
import threading
import time

import hostinfo
import storage as storage_util
from docker_backend import DockerError, build_run_args
from sim_backend import SimBackend
from store import Store

VERSION = "1.3.1"
NAME_MAX = 32
RESTART_POLICIES = ("always", "unless-stopped", "no")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _as_int(value, field, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError("%s 必须是整数" % field)
    if number < minimum or number > maximum:
        raise ApiError("%s 需在 %s ~ %s 之间" % (field, minimum, maximum))
    return number


def _as_float(value, field, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError("%s 必须是数字" % field)
    if number < minimum or number > maximum:
        raise ApiError("%s 需在 %s ~ %s 之间" % (field, minimum, maximum))
    return round(number, 2)


def port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def human_bytes(num):
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return ("%.1f %s" % (num, unit)) if unit != "B" else ("%d B" % num)
        num /= 1024.0
    return "0 B"


class Manager:
    def __init__(self, data_dir, sim_override=False, host="0.0.0.0", port=8080):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.host = host
        self.port = int(port)
        self.store = Store(self.data_dir)
        self.sim_override = bool(sim_override)
        settings = self.store.settings()
        try:
            os.makedirs(settings["storage_root"], exist_ok=True)
        except OSError:
            pass
        self.sim = SimBackend(os.path.join(self.data_dir, "sim-runtime.json"))
        self.docker = __import__("docker_backend").DockerBackend(self.store.settings)
        self._runtime = {}
        self._runtime_at = 0.0
        self._stats_cache = {}
        self._stats_at = 0.0
        self._running_since = {}
        self._reachable = {}
        self._last_heal = {}
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._loop_attempts = {}
        self._quota_warned = {}
        self._last_health_run = 0.0
        self._stop_event = threading.Event()
        self._health_thread = threading.Thread(target=self._health_loop, name="wxedge-health", daemon=True)
        self._health_thread.start()

    # ------------------------------------------------------------ 后端
    def backend_state(self):
        settings = self.store.settings()
        mode = settings.get("backend_mode", "auto")
        if self.sim_override:
            return {"mode": "sim", "backend": self.sim, "info": self.sim.info(),
                    "warning": "启动参数 --sim：模拟模式，不会操作真实容器"}
        if mode == "sim":
            return {"mode": "sim", "backend": self.sim, "info": self.sim.info(),
                    "warning": "已手动切换为模拟模式，不会操作真实容器"}
        if mode == "docker":
            info = self.docker.info()
            return {"mode": "docker", "backend": self.docker, "info": info,
                    "warning": "" if info.get("available") else info.get("error", "")}
        info = self.docker.info()
        if info.get("available"):
            return {"mode": "docker", "backend": self.docker, "info": info, "warning": ""}
        return {"mode": "sim", "backend": self.sim, "info": self.sim.info(), "fallback": True,
                "warning": "未检测到可用的 Docker，已自动切换到模拟模式（不会操作真实容器）"}

    # ------------------------------------------------------------ 运行时
    def _probe(self, port, network_mode):
        if network_mode != "host":
            return None
        port = int(port or 0)
        if not port:
            return None
        now = time.time()
        cached = self._reachable.get(port)
        if cached and now - cached[0] < 5:
            return cached[1]
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                ok = True
        except OSError:
            ok = False
        self._reachable[port] = (now, ok)
        return ok

    def refresh_runtime(self, force=False):
        if not force and time.time() - self._runtime_at < 3:
            return self._runtime
        bstate = self.backend_state()
        backend = bstate["backend"]
        containers = {}
        stats = {}
        error = ""
        try:
            for row in backend.list_containers():
                containers[row["name"]] = row
            if containers:
                if bstate["mode"] == "docker" and self._stats_cache and time.time() - self._stats_at < 6:
                    stats = self._stats_cache
                else:
                    stats = backend.stats()
                    self._stats_cache = stats
                    self._stats_at = time.time()
        except Exception as exc:  # 后端异常也要让界面可用
            error = str(exc)
        runtime = {}
        now = time.time()
        for inst in self.store.instances():
            name = inst["container_name"]
            row = containers.get(name)
            state = (row or {}).get("state", "missing")
            if state == "running":
                self._running_since.setdefault(name, now)
            else:
                self._running_since.pop(name, None)
            started = self._running_since.get(name)
            entry = {
                "exists": row is not None,
                "state": state,
                "status": (row or {}).get("status", "未创建"),
                "image": (row or {}).get("image", ""),
                "started_at": started,
                "uptime_seconds": int(now - started) if started else 0,
                # 模拟模式没有真实监听端口，不做过期探测，避免误报「端口未响应」
                "reachable": (True if bstate["mode"] != "docker" else
                              self._probe(inst.get("listen_port"), inst.get("network_mode", "host")))
                if state == "running" else None,
            }
            row_stats = stats.get(name) or {}
            entry.update({
                "cpu_pct": row_stats.get("cpu_pct", 0.0),
                "mem_usage_bytes": row_stats.get("mem_usage_bytes", 0),
                "mem_pct": row_stats.get("mem_pct", 0.0),
                "net_io": row_stats.get("net_io", ""),
                "block_io": row_stats.get("block_io", ""),
                "pids": row_stats.get("pids", ""),
            })
            entry.update(self._storage_snapshot(inst))
            runtime[name] = entry
        self._runtime = runtime
        self._runtime_at = time.time()
        self._runtime_error = error
        return runtime

    def _public_instance(self, inst, runtime):
        out = dict(inst)
        # 兼容旧版本状态文件：缺失的存储字段按默认值补齐
        out.setdefault("disk_mode", "dir")
        out.setdefault("quota_action", "none")
        out["runtime"] = runtime.get(inst["container_name"]) or {"exists": False, "state": "missing"}
        return out

    # ------------------------------------------------------------ 概览
    def _public_settings(self, settings):
        out = dict(settings)
        token = out.get("auth_token") or ""
        out["auth_token"] = "***" if token else ""
        out["auth_enabled"] = bool(token)
        return out

    def _totals(self, instances, settings):
        total = {"count": len(instances), "running": 0, "stopped": 0, "abnormal": 0,
                 "cpus": 0.0, "memory_mb": 0, "disk_gb": 0,
                 "auto_heal": 0, "desired_running": 0}
        for inst in instances:
            rt = inst.get("runtime") or {}
            state = rt.get("state", "missing")
            total["running"] += 1 if state in ("running", "restarting") else 0
            total["stopped"] += 1 if state in ("exited", "created", "missing", "paused", "dead") else 0
            total["abnormal"] += 1 if inst.get("desired_state") == "running" and state not in ("running", "restarting") else 0
            total["cpus"] += float(inst.get("cpus") or 0)
            total["memory_mb"] += int(inst.get("memory_mb") or 0)
            total["disk_gb"] += int(inst.get("disk_gb") or 0)
            total["auto_heal"] += 1 if settings.get("auto_heal") and inst.get("enabled") else 0
            total["desired_running"] += 1 if inst.get("desired_state") == "running" else 0
        total["cpus"] = round(total["cpus"], 2)
        return total

    def overview(self):
        settings = self.store.settings()
        bstate = self.backend_state()
        runtime = self.refresh_runtime(force=True)
        instances = [self._public_instance(i, runtime) for i in self.store.instances()]
        host = hostinfo.host_info(self.data_dir)
        info = bstate["info"] or {}
        return {
            "version": VERSION,
            "time": time.time(),
            "backend": {
                "mode": bstate["mode"],
                "available": bool(info.get("available", True)),
                "version": info.get("version", ""),
                "cgroup": info.get("cgroup", ""),
                "sim": bstate["mode"] == "sim",
                "warning": bstate.get("warning", ""),
                "error": getattr(self, "_runtime_error", "") or "",
            },
            "panel": {"bind_host": self.host, "bind_port": self.port},
            "settings": self._public_settings(settings),
            "host": host,
            "instances": instances,
            "totals": self._totals(instances, settings),
            "activity": self.store.snapshot().get("activity", [])[:30],
        }

    # ------------------------------------------------------------ 校验
    def _require(self, iid):
        inst = self.store.get_instance(iid)
        if not inst:
            raise ApiError("实例不存在：%s" % iid, 404)
        return inst

    def _normalize_env(self, value):
        if value is None:
            return {}
        if isinstance(value, list):
            value = {str(item.get("key", "")).strip(): str(item.get("value", ""))
                     for item in value if isinstance(item, dict)}
        if not isinstance(value, dict):
            raise ApiError("环境变量格式不正确")
        env = {}
        for key, val in value.items():
            key = str(key).strip()
            if not key:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ApiError("环境变量名不合法：%s" % key)
            env[key] = str(val)
        if len(env) > 20:
            raise ApiError("环境变量最多 20 个")
        return env

    def _instance_payload(self, payload, existing=None, pending_id=""):
        existing = existing or {}
        settings = self.store.settings()
        name = str(payload.get("name", existing.get("name", ""))).strip()
        if not name:
            raise ApiError("请填写实例名称")
        if len(name) > NAME_MAX:
            raise ApiError("实例名称最长 %d 个字符" % NAME_MAX)
        others = [i for i in self.store.instances() if i.get("id") != existing.get("id")]
        if any(i.get("name") == name for i in others):
            raise ApiError("实例名称已存在：%s" % name)
        cpus = _as_float(payload.get("cpus", existing.get("cpus", 1)), "CPU 核心数", 0.1, 256)
        memory_mb = _as_int(payload.get("memory_mb", existing.get("memory_mb", 1024)), "内存(MB)", 128, 1048576)
        disk_gb = _as_int(payload.get("disk_gb", existing.get("disk_gb", 50)), "硬盘(GB)", 1, 65536)
        disk_mode = str(payload.get("disk_mode", existing.get("disk_mode", settings.get("disk_mode", "dir"))))
        if disk_mode not in ("dir", "loop"):
            raise ApiError("磁盘模式不合法")
        quota_action = str(payload.get("quota_action", existing.get("quota_action", settings.get("default_quota_action", "none"))))
        if quota_action not in ("none", "stop"):
            raise ApiError("存储超限动作不合法")
        network_mode = str(payload.get("network_mode", existing.get("network_mode", "host")))
        if network_mode not in ("host", "macvlan"):
            raise ApiError("网络模式不合法")
        default_dir = os.path.join(settings["storage_root"], pending_id or existing.get("id") or "instance")
        data_dir = str(payload.get("data_dir", existing.get("data_dir", "")) or "").strip() or default_dir
        if not os.path.isabs(data_dir):
            raise ApiError("存储目录必须是绝对路径")
        data_dir = os.path.normpath(data_dir)
        if len(data_dir) < 4 or data_dir in ("/", "\\"):
            raise ApiError("存储目录不合法")
        runtime_dir = str(payload.get("runtime_dir", existing.get("runtime_dir", "")) or "").strip()
        if runtime_dir:
            if not os.path.isabs(runtime_dir):
                raise ApiError("容器安装目录必须是绝对路径")
            runtime_dir = os.path.normpath(runtime_dir)
        default_port = existing.get("listen_port") or self.suggest_port()
        listen_port = _as_int(payload.get("listen_port", default_port), "监听端口", 1024, 65535)
        if network_mode == "host":
            if listen_port == self.port:
                raise ApiError("监听端口不能与 WebUI 端口相同")
            for other in others:
                if other.get("network_mode", "host") == "host" and int(other.get("listen_port") or 0) == listen_port:
                    raise ApiError("端口 %d 已被实例「%s」占用" % (listen_port, other.get("name")))
        for other in others:
            other_dir = os.path.abspath(other.get("data_dir") or "")
            mine = os.path.abspath(data_dir)
            if mine == other_dir:
                raise ApiError("存储目录与其他实例重复")
            if mine.startswith(other_dir.rstrip(os.sep) + os.sep) or other_dir.startswith(mine.rstrip(os.sep) + os.sep):
                raise ApiError("存储目录不能与其他实例相互嵌套")
        macvlan = {}
        if network_mode == "macvlan":
            macvlan = {
                "macvlan_parent": str(payload.get("macvlan_parent", existing.get("macvlan_parent", ""))).strip(),
                "macvlan_subnet": str(payload.get("macvlan_subnet", existing.get("macvlan_subnet", ""))).strip(),
                "macvlan_gateway": str(payload.get("macvlan_gateway", existing.get("macvlan_gateway", ""))).strip(),
                "macvlan_ip": str(payload.get("macvlan_ip", existing.get("macvlan_ip", ""))).strip(),
                "macvlan_network": str(payload.get("macvlan_network", existing.get("macvlan_network", ""))).strip()
                or ("wxedge-net-" + (pending_id or existing.get("id") or "instance")),
            }
            for field in ("macvlan_parent", "macvlan_subnet", "macvlan_gateway", "macvlan_ip"):
                if not macvlan[field]:
                    raise ApiError("macvlan 模式需要填写 %s" % field)
            try:
                subnet = ipaddress.ip_network(macvlan["macvlan_subnet"], strict=False)
                gateway = ipaddress.ip_address(macvlan["macvlan_gateway"])
                ip = ipaddress.ip_address(macvlan["macvlan_ip"])
            except ValueError as exc:
                raise ApiError("macvlan 地址格式不正确：%s" % exc)
            if gateway not in subnet or ip not in subnet:
                raise ApiError("macvlan 网关/IP 必须属于子网 %s" % subnet)
            if ip == gateway:
                raise ApiError("macvlan 容器 IP 不能与网关相同")
        image = str(payload.get("image", existing.get("image", "")) or "").strip() or settings["image"]
        restart_policy = str(payload.get("restart_policy", existing.get("restart_policy", settings.get("restart_policy", "always"))))
        if restart_policy not in RESTART_POLICIES:
            raise ApiError("重启策略不合法")
        cpuset = str(payload.get("cpuset", existing.get("cpuset", "")) or "").strip()
        if cpuset and not re.match(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$", cpuset):
            raise ApiError("CPU 绑定格式示例：0-1,3")
        notes = str(payload.get("notes", existing.get("notes", "")))[:200]
        cfg = {
            "name": name,
            "cpus": cpus,
            "cpuset": cpuset,
            "memory_mb": memory_mb,
            "memory_swap_mb": _as_int(payload.get("memory_swap_mb", existing.get("memory_swap_mb", memory_mb)),
                                     "内存交换(MB)", 128, 2097152),
            "disk_gb": disk_gb,
            "disk_mode": disk_mode,
            "quota_action": quota_action,
            "network_mode": network_mode,
            "listen_port": listen_port,
            "data_dir": data_dir,
            "runtime_dir": runtime_dir,
            "image": image,
            "restart_policy": restart_policy,
            "env": self._normalize_env(payload.get("env", existing.get("env") or {})),
            "notes": notes,
            "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
            "desired_state": "running" if payload.get("desired_state", existing.get("desired_state", "running")) == "running" else "stopped",
        }
        cfg.update(macvlan)
        return cfg

    # ------------------------------------------------------------ 实例 CRUD
    def _write_marker(self, inst):
        marker = os.path.join(inst["data_dir"], ".wxedge-manager.json")
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"managed_by": "wxedge-manager", "id": inst.get("id"),
                       "name": inst.get("name"), "created_at": time.time()},
                      fh, ensure_ascii=False, indent=2)

    def create_instance(self, payload):
        settings = self.store.settings()
        number = self.store.next_instance_number()
        iid = "wx-%02d" % number
        cfg = self._instance_payload(payload, pending_id=iid)
        cfg.update({
            "id": iid,
            "container_name": "wxedge-%02d" % number,
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        data_dir = cfg["data_dir"]
        dir_existed = os.path.isdir(data_dir)
        try:
            os.makedirs(os.path.join(data_dir, "storage"), exist_ok=True)
            runtime_dir = cfg["runtime_dir"] or os.path.join(data_dir, "containerd")
            os.makedirs(runtime_dir, exist_ok=True)
            self._write_marker(cfg)
            self._prepare_storage(cfg)
            backend = self.backend_state()["backend"]
            backend.create(cfg, settings)
        except Exception as exc:
            try:
                self.backend_state()["backend"].remove(cfg["container_name"])
            except Exception:
                pass
            if not dir_existed:
                shutil.rmtree(data_dir, ignore_errors=True)
            else:
                try:
                    os.remove(os.path.join(data_dir, ".wxedge-manager.json"))
                except OSError:
                    pass
            raise ApiError("创建实例失败：%s" % exc, 500)
        self.store.put_instance(cfg)
        self.store.add_activity("success", "创建实例 %s（%s 核 / %d MB / %d GB）"
                                % (cfg["name"], ("%g" % cfg["cpus"]), cfg["memory_mb"], cfg["disk_gb"]),
                                iid, cfg["name"])
        self.refresh_runtime(force=True)
        return self._public_instance(cfg, self._runtime)

    def update_instance(self, iid, payload):
        existing = self._require(iid)
        settings = self.store.settings()
        merged = dict(existing)
        merged.update(payload or {})
        cfg = self._instance_payload(merged, existing=existing, pending_id=iid)
        cfg.update({
            "id": iid,
            "container_name": existing["container_name"],
            "created_at": existing.get("created_at", time.time()),
            "updated_at": time.time(),
        })
        backend = self.backend_state()["backend"]
        name = cfg["container_name"]
        try:
            os.makedirs(os.path.join(cfg["data_dir"], "storage"), exist_ok=True)
            runtime_dir = cfg["runtime_dir"] or os.path.join(cfg["data_dir"], "containerd")
            os.makedirs(runtime_dir, exist_ok=True)
            self._write_marker(cfg)
            old_mode = existing.get("disk_mode") or "dir"
            new_mode = cfg.get("disk_mode") or "dir"
            if old_mode == "loop" and new_mode != "loop":
                storage_util.unmount(existing)
            self._prepare_storage(cfg)
            try:
                backend.stop(name)
            except Exception:
                pass
            try:
                backend.remove(name)
            except Exception:
                pass
            backend.create(cfg, settings)
        except Exception as exc:
            try:
                backend.remove(name)
            except Exception:
                pass
            try:
                backend.create(existing, settings)
            except Exception:
                pass
            raise ApiError("更新实例失败，已尝试恢复原配置：%s" % exc, 500)
        self.store.put_instance(cfg)
        self.store.add_activity("info", "更新实例 %s 配置并重建容器" % cfg["name"], iid, cfg["name"])
        self.refresh_runtime(force=True)
        return self._public_instance(cfg, self._runtime)

    def delete_instance(self, iid, purge=False):
        inst = self._require(iid)
        try:
            self.backend_state()["backend"].remove(inst["container_name"])
        except Exception:
            pass
        try:
            storage_util.unmount(inst)
        except Exception:
            pass
        if inst.get("network_mode") == "macvlan":
            try:
                self.backend_state()["backend"].remove_macvlan(inst.get("macvlan_network"))
            except Exception:
                pass
        if purge:
            self._purge_data(inst)
        self.store.remove_instance(iid)
        self.store.add_activity("warn" if purge else "info",
                                "删除实例 %s%s" % (inst["name"], "（含数据）" if purge else "（保留数据目录）"),
                                iid, inst["name"])
        self.refresh_runtime(force=True)
        return {"deleted": iid, "purged": bool(purge)}

    def _purge_data(self, inst):
        data_dir = os.path.abspath(inst.get("data_dir") or "")
        settings = self.store.settings()
        storage_root = os.path.abspath(settings.get("storage_root") or "")
        marker = os.path.join(data_dir, ".wxedge-manager.json")
        if data_dir in ("/", "\\") or len(data_dir) < 5:
            raise ApiError("安全校验失败：拒绝删除根目录", 409)
        if not os.path.isfile(marker):
            raise ApiError("安全校验失败：数据目录缺少管理器标记，拒绝删除", 409)
        if storage_root and data_dir == storage_root:
            raise ApiError("安全校验失败：拒绝删除实例存储根目录", 409)
        try:
            storage_util.unmount(inst)
        except Exception:
            pass
        try:
            shutil.rmtree(data_dir)
        except OSError as exc:
            raise ApiError("删除数据目录失败：%s" % exc, 500)

    def action(self, iid, action):
        inst = self._require(iid)
        backend = self.backend_state()["backend"]
        name = inst["container_name"]
        settings = self.store.settings()
        try:
            if action in ("start", "restart", "recreate") and inst.get("disk_mode") == "loop":
                self._prepare_storage(inst)
            if action == "start":
                try:
                    backend.start(name)
                except Exception:
                    backend.create(inst, settings)
                inst["desired_state"] = "running"
                inst["enabled"] = True
                message = "启动实例 %s" % inst["name"]
            elif action == "stop":
                backend.stop(name)
                inst["desired_state"] = "stopped"
                message = "停止实例 %s" % inst["name"]
            elif action == "restart":
                try:
                    backend.restart(name)
                except Exception:
                    backend.create(inst, settings)
                inst["desired_state"] = "running"
                inst["enabled"] = True
                message = "重启实例 %s" % inst["name"]
            elif action == "recreate":
                try:
                    backend.remove(name)
                except Exception:
                    pass
                backend.create(inst, settings)
                inst["desired_state"] = "running"
                inst["enabled"] = True
                message = "重建实例 %s（数据保留）" % inst["name"]
            elif action == "enable":
                inst["enabled"] = True
                message = "开启自动修复 %s" % inst["name"]
            elif action == "disable":
                inst["enabled"] = False
                message = "关闭自动修复 %s" % inst["name"]
            else:
                raise ApiError("不支持的操作：%s" % action)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError("操作失败：%s" % exc, 500)
        inst["updated_at"] = time.time()
        self.store.put_instance(inst)
        self.store.add_activity("info", message, iid, inst["name"])
        self.refresh_runtime(force=True)
        return self._public_instance(inst, self._runtime)

    def instance_logs(self, iid, tail=200):
        inst = self._require(iid)
        try:
            tail = max(10, min(2000, int(tail)))
        except (TypeError, ValueError):
            tail = 200
        try:
            text = self.backend_state()["backend"].logs(inst["container_name"], tail)
        except Exception as exc:
            raise ApiError("获取日志失败：%s" % exc, 500)
        return {"id": iid, "name": inst["name"], "text": text, "tail": tail}

    def instance_detail(self, iid):
        inst = self._require(iid)
        runtime = self.refresh_runtime(force=True)
        out = self._public_instance(inst, runtime)
        out["storage"] = self._dir_usage(inst.get("data_dir"))
        out["settings_hint"] = {
            "image": self.store.settings().get("image"),
            "auto_heal": self.store.settings().get("auto_heal"),
        }
        return out

    def storage_paths(self):
        """自动检测可用存储目录，并给出推荐项。"""
        settings = self.store.settings()
        current = os.path.abspath(settings.get("storage_root") or self.data_dir)
        paths = hostinfo.storage_candidates(current)
        if not any(item["path"] == current for item in paths) and os.path.isdir(current):
            usage = hostinfo.disk_for_path(current) or {}
            paths.insert(0, {
                "path": current, "source": "settings", "fstype": "",
                "free_bytes": usage.get("free_bytes", 0),
                "total_bytes": usage.get("total_bytes", 0),
                "writable": os.access(current, os.W_OK), "recommended": True,
            })
        recommended = next((item for item in paths if item["recommended"]), paths[0] if paths else None)
        return {
            "platform": sys.platform,
            "current": current,
            "paths": paths,
            "recommended": recommended,
        }

    def _prepare_storage(self, inst):
        """按磁盘模式准备存储：loop 卷创建/扩容/挂载。"""
        if (inst.get("disk_mode") or "dir") != "loop":
            return
        try:
            storage_util.ensure(inst)
        except storage_util.StorageError as exc:
            raise ApiError("准备独立磁盘失败：%s" % exc, 500)

    def _storage_snapshot(self, inst):
        mode = inst.get("disk_mode") or "dir"
        limit_bytes = int(inst.get("disk_gb") or 0) * (1024 ** 3)
        mounted = True
        used = 0
        if mode == "loop":
            info = storage_util.usage(inst)
            mounted = bool(info.get("mounted"))
            used = int(info.get("used_bytes") or 0)
        else:
            try:
                used = int(self._dir_usage(inst.get("data_dir")).get("bytes") or 0)
            except Exception:
                used = 0
        pct = round(100.0 * used / limit_bytes, 1) if limit_bytes else 0.0
        return {
            "disk_mode": mode,
            "disk_used_bytes": used,
            "disk_limit_bytes": limit_bytes,
            "disk_usage_pct": min(pct, 999.9),
            "disk_mounted": mounted,
            "quota_action": inst.get("quota_action") or "none",
        }

    def _dir_usage(self, path):
        if not path or not os.path.isdir(path):
            return {"bytes": 0, "files": 0, "capped": False}
        now = time.time()
        cache = getattr(self, "_usage_cache", None)
        if cache is None:
            cache = self._usage_cache = {}
        hit = cache.get(path)
        if hit and now - hit[0] < 20:
            return hit[1]
        total, files, capped = 0, 0, False
        try:
            for root, _dirs, names in os.walk(path):
                for fname in names:
                    if files >= 5000:
                        capped = True
                        break
                    try:
                        total += os.path.getsize(os.path.join(root, fname))
                        files += 1
                    except OSError:
                        continue
                if capped:
                    break
        except OSError:
            pass
        result = {"bytes": total, "files": files, "capped": capped}
        cache[path] = (now, result)
        return result

    # ------------------------------------------------------------ 自动配置
    def _capacity(self):
        host = hostinfo.host_info(self.data_dir)
        cpu_total = max(1, int(host["cpu"]["count"]))
        mem_total = int(host["mem"]["total_bytes"] // (1024 * 1024))
        free_gb = 0
        if host.get("data_disk"):
            free_gb = int(host["data_disk"].get("free_bytes", 0) // (1024 ** 3))
        return host, cpu_total, mem_total, free_gb

    def suggest_port(self, start=None):
        settings = self.store.settings()
        used = {int(i.get("listen_port") or 0) for i in self.store.instances()
                if i.get("network_mode", "host") == "host"}
        port = int(start or settings.get("port_base") or 18888)
        for _ in range(300):
            if 1024 <= port <= 65535 and port not in used and port != self.port and port_free(port):
                return port
            port += 1
            if port > 65535:
                port = 1024
        raise ApiError("端口区间已用尽，请调整端口起始值")

    def plan_batch(self, count, strategy="balanced", overrides=None):
        overrides = overrides or {}
        count = _as_int(count, "实例数量", 1, 65535)
        if strategy not in ("balanced", "performance", "eco"):
            strategy = "balanced"
        settings = self.store.settings()
        host, cpu_total, mem_total, free_gb = self._capacity()
        reserve_cpu = max(1.0, cpu_total * 0.15)
        reserve_mem = max(1024, mem_total * 0.15)
        usable_cpu = max(0.5, cpu_total - reserve_cpu)
        usable_mem = max(256, mem_total - reserve_mem)
        usable_disk = max(0, free_gb - 20)
        mult = {"balanced": (1.0, 1.0, 1.0), "performance": (1.5, 1.5, 1.4), "eco": (0.6, 0.6, 1.0)}[strategy]
        base_cpu = usable_cpu / count * mult[0]
        base_mem = usable_mem / count * mult[1]
        base_disk = usable_disk / count * mult[2] if usable_disk else 50
        if overrides.get("cpus") not in (None, ""):
            base_cpu = float(overrides["cpus"])
            if base_cpu <= 0:
                raise ApiError("CPU 核心数需大于 0")
        if overrides.get("memory_mb") not in (None, ""):
            base_mem = float(overrides["memory_mb"])
            if base_mem < 128:
                raise ApiError("内存至少 128 MB")
        if overrides.get("disk_gb") not in (None, ""):
            base_disk = float(overrides["disk_gb"])
            if base_disk < 1:
                raise ApiError("硬盘至少 1 GB")

        cpus_override = overrides.get("cpus")
        memory_override = overrides.get("memory_mb")
        disk_override = overrides.get("disk_gb")
        if "storage_root" in overrides and overrides["storage_root"]:
            storage_root = os.path.abspath(str(overrides["storage_root"]))
        else:
            storage_root = settings["storage_root"]
        image = str(overrides.get("image") or settings.get("image") or "")
        prefix = str(overrides.get("name_prefix") or settings.get("name_prefix") or "节点").strip()[:16] or "节点"
        port_start = int(overrides.get("port_base") or self.suggest_port())
        port_end = int(overrides.get("port_end") or 65535)
        if port_start < 1024 or port_start > 65535 or port_end < 1024 or port_end > 65535:
            raise ApiError("端口范围需在 1024 ~ 65535 之间")
        if port_end < port_start:
            raise ApiError("端口结束需大于或等于端口起始")
        if count > port_end - port_start + 1:
            raise ApiError("端口范围不足：%d 个实例，只有 %d 个端口" % (count, port_end - port_start + 1))
        used_ports = {int(i.get("listen_port") or 0) for i in self.store.instances()}
        existing_names = {i.get("name") for i in self.store.instances()}
        existing_count = len(self.store.instances())
        items = []
        warnings = []
        disk_mode = overrides.get("disk_mode") or settings.get("disk_mode", "dir")
        if disk_mode not in ("dir", "loop"):
            disk_mode = "dir"
        for index in range(count):
            cpus = max(0.1, round(min(base_cpu, 256, max(0.1, cpu_total * 2)) * 4) / 4) if cpus_override in (None, "") else round(float(cpus_override), 2)
            memory_mb = int(max(128, min(base_mem, 1048576, mem_total * 2)) // 256 * 256) if memory_override in (None, "") else int(float(memory_override))
            memory_mb = max(128, memory_mb)
            disk_gb = int(max(1, min(base_disk, 65536))) if disk_override in (None, "") else int(float(disk_override))
            number = existing_count + index + 1
            name = ("%s%02d" % (prefix, number)) if not re.search(r"[0-9]+$", prefix) else ("%s-%02d" % (prefix, number))
            if name in existing_names:
                suffix = 1
                base_name = name
                while name in existing_names:
                    suffix += 1
                    name = "%s-%d" % (base_name, suffix)
            port = None
            for candidate in range(port_start, port_end + 1):
                if candidate not in used_ports and candidate != self.port and port_free(candidate):
                    port = candidate
                    break
            if port is None:
                raise ApiError("端口范围 %d ~ %d 内没有足够的可用端口" % (port_start, port_end))
            used_ports.add(port)
            data_dir = os.path.join(storage_root, "wx-%02d" % number)
            items.append({
                "name": name,
                "cpus": cpus,
                "memory_mb": memory_mb,
                "disk_gb": disk_gb,
                "listen_port": port,
                "data_dir": data_dir,
                "image": image,
                "disk_mode": disk_mode,
                "network_mode": "host",
            })
        sum_cpu = sum(i["cpus"] for i in items)
        sum_mem = sum(i["memory_mb"] for i in items)
        sum_disk = sum(i["disk_gb"] for i in items)
        if sum_cpu > cpu_total * 1.2:
            warnings.append("CPU 总分配 %.2f 核 超过物理核心数 %d，可能争抢资源" % (sum_cpu, cpu_total))
        if sum_mem > mem_total * 0.95:
            warnings.append("内存总分配 %s 接近/超过主机内存 %s" % (human_bytes(sum_mem * 1024 * 1024), human_bytes(mem_total * 1024 * 1024)))
        if sum_disk > usable_disk:
            warnings.append("硬盘总分配 %d GB 超过数据盘可用预留空间 %d GB" % (sum_disk, usable_disk))
        if free_gb and free_gb < 50 * count:
            warnings.append("数据盘剩余 %d GB，单实例官方建议 ≥50 GB 存储" % free_gb)
        if disk_mode == "loop" and not (os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0):
            warnings.append("独立磁盘镜像需要 Linux root 权限，否则会创建失败")
        return {
            "count": count,
            "strategy": strategy,
            "items": items,
            "warnings": warnings,
            "host": {"cpu_count": cpu_total, "memory_mb": mem_total, "free_gb": free_gb},
            "settings": self._public_settings(settings),
        }

    def batch_create(self, count, strategy="balanced", overrides=None):
        plan = self.plan_batch(count, strategy, overrides or {})
        created = []
        errors = []
        for item in plan["items"]:
            try:
                created.append(self.create_instance(item))
            except ApiError as exc:
                errors.append({"name": item.get("name"), "error": exc.message})
            except Exception as exc:
                errors.append({"name": item.get("name"), "error": str(exc)})
        return {"plan": plan, "created": created, "errors": errors}

    def preview(self, payload):
        settings = self.store.settings()
        cfg = self._instance_payload(payload or {}, pending_id="wx-preview")
        cfg["container_name"] = "wxedge-preview"
        cfg["id"] = "wx-preview"
        args = build_run_args(cfg, settings)
        warnings = []
        host, cpu_total, mem_total, free_gb = self._capacity()
        if cfg["cpus"] > cpu_total:
            warnings.append("CPU 分配超过物理核心数（%d 核），容器会争抢资源" % cpu_total)
        if cfg["memory_mb"] > mem_total:
            warnings.append("内存分配超过主机物理内存")
        if free_gb and cfg["disk_gb"] > free_gb - 10:
            warnings.append("硬盘分配可能超过数据盘可用空间（剩余 %d GB）" % free_gb)
        if cfg["disk_mode"] == "loop" and not (
                os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0):
            warnings.append("独立磁盘镜像需要 Linux root 权限运行管理器")
        return {
            "args": args,
            "command": "docker " + " ".join(shlex.quote(a) for a in args),
            "warnings": warnings,
            "suggest_port": self.suggest_port(),
            "default_dir": os.path.join(settings["storage_root"], "wx-xx"),
        }

    # ------------------------------------------------------------ 设置
    def update_settings(self, patch):
        settings = self.store.settings()
        clean = {}
        patch = patch or {}
        for key in ("image", "storage_root", "port_base", "backend_mode", "auto_heal",
                    "auto_update", "health_interval", "restart_policy", "disk_mode",
                    "default_quota_action", "name_prefix", "auth_token", "poll_seconds",
                    "panel_bind_host", "panel_bind_port"):
            if key not in patch:
                continue
            value = patch[key]
            if key == "image":
                value = str(value).strip()
                if not value:
                    raise ApiError("镜像地址不能为空")
            elif key == "storage_root":
                value = str(value).strip()
                if not value or not os.path.isabs(value):
                    raise ApiError("存储根目录必须是绝对路径")
                value = os.path.normpath(value)
            elif key == "port_base":
                value = _as_int(value, "端口起始", 1024, 65000)
            elif key == "backend_mode":
                if value not in ("auto", "docker", "sim"):
                    raise ApiError("后端模式不合法")
            elif key in ("auto_heal", "auto_update"):
                value = bool(value)
            elif key == "health_interval":
                value = _as_int(value, "健康检测间隔", 5, 3600)
            elif key == "restart_policy":
                if value not in RESTART_POLICIES:
                    raise ApiError("重启策略不合法")
            elif key == "disk_mode":
                if value not in ("dir", "loop"):
                    raise ApiError("磁盘模式不合法")
            elif key == "default_quota_action":
                if value not in ("none", "stop"):
                    raise ApiError("默认存储超限动作不合法")
            elif key == "panel_bind_host":
                value = str(value).strip()
                if not value or len(value) > 64 or " " in value:
                    raise ApiError("面板监听地址不合法")
            elif key == "panel_bind_port":
                value = _as_int(value, "面板端口", 1024, 65535)
            elif key == "name_prefix":
                value = str(value).strip()[:16]
                if not value:
                    raise ApiError("名称前缀不能为空")
            elif key == "auth_token":
                if value == "***":
                    continue
                value = str(value).strip()
                if value and len(value) < 6:
                    raise ApiError("访问令牌至少 6 位")
            elif key == "poll_seconds":
                value = _as_int(value, "刷新间隔", 2, 60)
            clean[key] = value
        if "storage_root" in clean:
            try:
                os.makedirs(clean["storage_root"], exist_ok=True)
            except OSError as exc:
                raise ApiError("无法创建存储根目录：%s" % exc)
        updated = self.store.set_settings(clean)
        self.docker._info_cache = (0.0, None)
        self.store.add_activity("info", "更新系统设置")
        if "panel_bind_host" in clean or "panel_bind_port" in clean:
            self.store.add_activity("info", "面板监听配置已更新，重启面板后生效")
        if "auto_update" in clean and self.backend_state()["mode"] == "docker":
            try:
                if clean["auto_update"]:
                    status = self.docker.ensure_watchtower()
                    self.store.add_activity("info", "已启用镜像自动更新（watchtower：%s）" % status)
                else:
                    self.docker.stop_watchtower()
                    self.store.add_activity("info", "已关闭镜像自动更新")
            except Exception as exc:
                self.store.add_activity("warn", "自动更新配置失败：%s" % exc)
        return self._public_settings(updated)

    # ------------------------------------------------------------ 自检
    def self_check(self):
        settings = self.store.settings()
        checks = []

        def add(name, level, detail, hint=""):
            checks.append({"name": name, "level": level, "detail": detail, "hint": hint})

        add("管理器版本", "ok", "v%s（Python %s）" % (VERSION, platform.python_version()))
        exe = shutil.which("docker")
        add("Docker CLI", "ok" if exe else "warn", exe or "未找到 docker 命令",
            "Ubuntu/Debian：sudo apt install docker.io")
        info = self.docker.info(force=True)
        if info.get("available"):
            add("Docker 守护进程", "ok", "服务端版本 %s，cgroup v%s"
                % (info.get("version", "?"), info.get("cgroup", "?")))
            try:
                exists = self.docker.image_exists(settings["image"])
                add("网心云镜像", "ok" if exists else "warn",
                    "已存在：%s" % settings["image"] if exists else "本地不存在，创建实例时需拉取（约数百 MB）",
                    "可在「自检」页点击「拉取镜像」")
            except Exception as exc:
                add("网心云镜像", "warn", "检查失败：%s" % exc)
            try:
                wt = self.docker.watchtower_status()
                add("自动更新组件", "ok" if wt in ("running", "absent") else "warn",
                    "watchtower：%s" % wt, "开启镜像自动更新后由管理器创建")
            except Exception as exc:
                add("自动更新组件", "warn", "检查失败：%s" % exc)
        else:
            level = "warn" if settings.get("backend_mode") in ("auto", "sim") else "fail"
            add("Docker 守护进程", level, info.get("error", "不可用"),
                "sudo systemctl enable --now docker；或把后端模式设为 auto 使用模拟模式")
        storage_root = settings.get("storage_root") or self.data_dir
        try:
            os.makedirs(storage_root, exist_ok=True)
            probe = os.path.join(storage_root, ".write-test-%d" % os.getpid())
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            add("实例存储目录", "ok", "可写：%s" % storage_root)
        except OSError as exc:
            add("实例存储目录", "fail", "不可写：%s（%s）" % (storage_root, exc),
                "检查目录权限，或修改「实例存储根目录」")
        data_disk = hostinfo.disk_for_path(storage_root)
        if data_disk:
            add("数据盘空间", "ok" if data_disk["free_bytes"] > 10 * (1024 ** 3) else "warn",
                "%s 剩余 %s / 共 %s" % (data_disk.get("mount"), human_bytes(data_disk["free_bytes"]),
                                        human_bytes(data_disk["total_bytes"])),
                "单实例官方建议 ≥50 GB 可用空间")
        is_root = (os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0)
        add("管理器运行权限", "ok" if is_root else "warn",
            "root（可创建 privileged 容器与 loop 磁盘）" if is_root else "非 root，可能无法创建 privileged 容器",
            "sudo systemctl start wxedge-manager（systemd 默认以 root 运行）")
        loop_ok = storage_util.loop_supported()
        root_ok = storage_util.is_root()
        if loop_ok and root_ok:
            add("独立磁盘镜像支持", "ok", "loop 设备可用且管理器为 root，可创建 ext4 硬配额卷")
        elif loop_ok:
            add("独立磁盘镜像支持", "warn", "支持 loop 设备，但管理器非 root，无法创建独立磁盘")
        else:
            add("独立磁盘镜像支持", "warn", "未发现 /dev/loop-control，独立磁盘镜像不可用",
                "Ubuntu 一般默认支持；容器内需 --privileged 才可使用")
        add("面板监听", "ok", "http://%s:%d（可在「系统设置」中修改端口并重启生效）" % (self.host, self.port))
        add("访问令牌", "ok" if settings.get("auth_token") else "warn",
            "已设置" if settings.get("auth_token") else "未设置（任何能访问端口的人都能操作实例）",
            "公网/LAN 访问前建议在设置页设置访问令牌")
        add("主机资源", "ok", "%d 核 / %s 内存 / 数据盘剩余 %s"
            % (hostinfo.host_info(self.data_dir)["cpu"]["count"],
               human_bytes(hostinfo.memory_info()["total_bytes"]),
               human_bytes((data_disk or {}).get("free_bytes", 0))))
        return {"checks": checks, "at": time.time(), "version": VERSION,
                "backend": self.backend_state()["mode"]}

    # ------------------------------------------------------------ 任务
    def start_pull_job(self):
        settings = self.store.settings()
        bstate = self.backend_state()
        job_id = "pull-%d" % int(time.time() * 1000)
        job = {"id": job_id, "kind": "pull", "status": "running",
               "image": settings["image"], "lines": [], "error": "",
               "started_at": time.time(), "finished_at": None}
        with self._jobs_lock:
            self._jobs[job_id] = job
            if len(self._jobs) > 10:
                for key in sorted(self._jobs, key=lambda k: self._jobs[k]["started_at"])[:-10]:
                    self._jobs.pop(key, None)

        def run():
            try:
                def on_line(line):
                    job["lines"].append(str(line)[:300])
                    del job["lines"][:-400]
                bstate["backend"].pull(settings["image"], on_line)
                job["status"] = "done"
            except Exception as exc:
                job["status"] = "error"
                job["error"] = str(exc)
            finally:
                job["finished_at"] = time.time()

        threading.Thread(target=run, name="wxedge-pull", daemon=True).start()
        return {"id": job_id}

    def get_job(self, job_id):
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ApiError("任务不存在：%s" % job_id, 404)
            return dict(job)

    # ------------------------------------------------------------ 健康自愈
    def _health_tick(self):
        settings = self.store.settings()
        bstate = self.backend_state()
        backend = bstate["backend"]
        runtime = self.refresh_runtime(force=True)

        # 1) 确保 loop 存储卷挂载（与 Docker 是否可用无关，模拟模式同样生效）
        for inst in self.store.instances():
            if inst.get("disk_mode") != "loop" or inst.get("desired_state") != "running":
                continue
            try:
                storage_util.ensure(inst)
            except storage_util.StorageError as exc:
                key = inst["id"]
                if time.time() - self._loop_attempts.get(key, 0) > 300:
                    self._loop_attempts[key] = time.time()
                    self.store.add_activity("error", "独立磁盘挂载失败 %s：%s"
                                            % (inst["name"], exc), key, inst["name"])

        # 2) 目录模式存储超限策略：stop = 自动停止实例
        for inst in self.store.instances():
            if (inst.get("quota_action") or "none") != "stop":
                continue
            rt = runtime.get(inst["container_name"]) or {}
            limit = int(rt.get("disk_limit_bytes") or 0)
            used = int(rt.get("disk_used_bytes") or 0)
            if not limit or used <= limit or rt.get("state") != "running":
                continue
            try:
                backend.stop(inst["container_name"])
            except Exception:
                pass
            inst["desired_state"] = "stopped"
            inst["updated_at"] = time.time()
            self.store.put_instance(inst)
            self.store.add_activity("warn", "存储超限（%s / %s），已停止实例 %s"
                                    % (human_bytes(used), human_bytes(limit), inst["name"]),
                                    inst["id"], inst["name"])
            self.refresh_runtime(force=True)

        # 3) Docker 自动修复
        if bstate["mode"] != "docker" or not bstate["info"].get("available"):
            return
        if not settings.get("auto_heal"):
            return
        for inst in self.store.instances():
            if not inst.get("enabled") or inst.get("desired_state") != "running":
                continue
            rt = runtime.get(inst["container_name"]) or {}
            if rt.get("state") in ("running", "restarting", "created"):
                continue
            now = time.time()
            if now - self._last_heal.get(inst["id"], 0) < 60:
                continue
            self._last_heal[inst["id"]] = now
            try:
                if not rt.get("exists"):
                    backend.create(inst, settings)
                    message = "自动修复：容器丢失，已按原配置重建"
                else:
                    backend.start(inst["container_name"])
                    message = "自动修复：容器未运行，已启动"
                self.store.add_activity("warn", message + "：%s" % inst["name"], inst["id"], inst["name"])
                self.refresh_runtime(force=True)
            except Exception as exc:
                self.store.add_activity("error", "自动修复失败 %s：%s" % (inst["name"], exc),
                                        inst["id"], inst["name"])

    def _health_loop(self):
        while not self._stop_event.wait(3):
            try:
                settings = self.store.settings()
                interval = max(5, int(settings.get("health_interval") or 15))
                now = time.time()
                if now - self._last_health_run < interval:
                    continue
                self._last_health_run = now
                self._health_tick()
            except Exception:
                continue

    def shutdown(self):
        self._stop_event.set()
