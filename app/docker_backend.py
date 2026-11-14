"""真实 Docker 后端：通过 docker CLI 管理网心云容器。"""
import json
import os
import shutil
import subprocess
import sys
import time

LABEL_MANAGER = "wxedge.manager"
LABEL_INSTANCE = "wxedge.instance"
CONTAINER_PREFIX = "wxedge-"


class DockerError(RuntimeError):
    pass


def docker_executable():
    return shutil.which("docker")


def _run(args, timeout=90, check=False):
    exe = docker_executable()
    if not exe:
        raise DockerError("未找到 docker 命令，请先安装 Docker（apt install docker.io）")
    try:
        proc = subprocess.run(
            [exe] + list(args),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise DockerError("docker %s 执行超时" % " ".join(str(a) for a in args[:2]))
    except OSError as exc:
        raise DockerError("无法执行 docker: %s" % exc)
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise DockerError(msg.splitlines()[-1] if msg else ("docker 退出码 %s" % proc.returncode))
    return proc


def _sh(args, timeout=120, check=True):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerError("命令执行失败: %s (%s)" % (" ".join(args[:2]), exc))
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise DockerError(msg or ("命令退出码 %s" % proc.returncode))
    return proc


def build_run_args(inst, settings):
    """生成与官方容器魔方一致的 docker run 参数，并附加资源限制。"""
    name = inst["container_name"]
    settings = settings or {}
    args = [
        "run", "-d", "--name", name,
        "--restart", inst.get("restart_policy") or settings.get("restart_policy") or "always",
        "--label", LABEL_MANAGER + "=1",
        "--label", "%s=%s" % (LABEL_INSTANCE, inst.get("id", "")),
        "--label", "com.centurylinklabs.watchtower.enable=%s"
        % ("true" if settings.get("auto_update") else "false"),
    ]
    net = inst.get("network_mode") or "host"
    if net == "macvlan":
        args += ["--net", inst["macvlan_network"], "--ip", inst["macvlan_ip"]]
    else:
        args += ["--net", "host"]
        args += ["-e", "LISTEN_ADDR=:%d" % int(inst.get("listen_port") or 18888)]
    args += ["--privileged", "--tmpfs", "/run", "--tmpfs", "/tmp"]
    try:
        cpus = float(inst.get("cpus") or 1)
    except (TypeError, ValueError):
        cpus = 1.0
    args += ["--cpus", ("%g" % cpus)]
    if inst.get("cpuset"):
        args += ["--cpuset-cpus", str(inst["cpuset"]).strip()]
    mem = int(inst.get("memory_mb") or 512)
    swap = int(inst.get("memory_swap_mb") or mem)
    if swap < mem:
        swap = mem
    args += ["--memory", "%dm" % mem, "--memory-swap", "%dm" % swap]
    storage = os.path.join(inst["data_dir"], "storage")
    runtime_dir = inst.get("runtime_dir") or os.path.join(inst["data_dir"], "containerd")
    args += ["-v", "%s:/storage:rw" % storage]
    args += ["-v", "%s:/var/lib/containerd" % runtime_dir]
    for key, value in (inst.get("env") or {}).items():
        args += ["-e", "%s=%s" % (key, value)]
    args += [inst.get("image") or settings.get("image")]
    return args


def _parse_percent(text):
    try:
        return round(float(str(text).strip().rstrip("%")), 2)
    except (TypeError, ValueError):
        return 0.0


_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2,
          "gb": 1000 ** 3, "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4}


def parse_bytes(text):
    text = str(text or "").strip()
    if not text or text in ("0B", "0"):
        return 0
    try:
        if "/" in text:
            text = text.split("/")[0].strip()
        if text.startswith("--"):
            return 0
        units = sorted(_UNITS, key=len, reverse=True)
        low = text.lower()
        for unit in units:
            if low.endswith(unit):
                number = text[: len(text) - len(unit)].strip().replace(" ", "")
                return int(float(number) * _UNITS[unit])
        return int(float(text))
    except (TypeError, ValueError):
        return 0


class DockerBackend:
    name = "docker"

    def __init__(self, settings_getter):
        self._settings_getter = settings_getter
        self._info_cache = (0.0, None)

    def settings(self):
        return self._settings_getter()

    # ---------- 连接 ----------
    def info(self, force=False):
        now = time.time()
        cached_at, cached = self._info_cache
        if not force and cached is not None and now - cached_at < 20:
            return cached
        exe = docker_executable()
        if not exe:
            info = {"available": False, "mode": "docker",
                    "error": "未找到 docker 命令，请先安装 Docker Engine"}
        else:
            try:
                cli = _run(["version", "--format", "{{.Client.Version}}"], timeout=12)
                srv = _run(["version", "--format", "{{.Server.Version}}"], timeout=12)
                if srv.returncode != 0:
                    err = (srv.stderr or srv.stdout or "").strip().splitlines()
                    info = {"available": False, "mode": "docker",
                            "error": "Docker 守护进程不可用：" + (err[0][:180] if err else "未知错误")}
                else:
                    cg = "unknown"
                    try:
                        cg = _run(["info", "--format", "{{.CgroupVersion}}"], timeout=12).stdout.strip() or "unknown"
                    except DockerError:
                        pass
                    info = {"available": True, "mode": "docker",
                            "client": cli.stdout.strip(), "version": srv.stdout.strip(),
                            "cgroup": cg, "sim": False}
            except DockerError as exc:
                info = {"available": False, "mode": "docker", "error": str(exc)}
        self._info_cache = (now, info)
        return info

    def available(self):
        return bool(self.info().get("available"))

    # ---------- 容器 ----------
    def list_containers(self):
        out = _run(["ps", "-a", "--filter", "label=%s=1" % LABEL_MANAGER,
                    "--format", "{{json .}}"], timeout=25).stdout
        items = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            labels = {}
            for part in (row.get("Labels") or "").split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    labels[key] = value
            items.append({
                "name": (row.get("Names") or "").split(",")[0],
                "state": (row.get("State") or "").lower(),
                "status": row.get("Status") or "",
                "image": row.get("Image") or "",
                "created": row.get("CreatedAt") or "",
                "labels": labels,
            })
        return items

    def stats(self):
        try:
            out = _run(["stats", "--no-stream", "--format", "{{json .}}"], timeout=30).stdout
        except DockerError:
            return {}
        result = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            result[name] = {
                "cpu_pct": _parse_percent(row.get("CPUPerc")),
                "mem_usage_bytes": parse_bytes(row.get("MemUsage")),
                "mem_limit_bytes": 0,
                "mem_pct": _parse_percent(row.get("MemPerc")),
                "net_io": row.get("NetIO") or "",
                "block_io": row.get("BlockIO") or "",
                "pids": row.get("PIDs") or "",
            }
        return result

    def create(self, inst, settings):
        storage = os.path.join(inst["data_dir"], "storage")
        runtime_dir = inst.get("runtime_dir") or os.path.join(inst["data_dir"], "containerd")
        os.makedirs(storage, exist_ok=True)
        os.makedirs(runtime_dir, exist_ok=True)
        if inst.get("disk_mode") == "loop":
            self.setup_loop_disk(inst)
        if inst.get("network_mode") == "macvlan":
            self.ensure_macvlan(inst)
        proc = _run(build_run_args(inst, settings), timeout=300, check=True)
        return proc.stdout.strip()

    def start(self, name):
        _run(["start", name], timeout=60, check=True)

    def stop(self, name):
        _run(["stop", "-t", "20", name], timeout=90, check=True)

    def restart(self, name):
        _run(["restart", "-t", "20", name], timeout=120, check=True)

    def remove(self, name):
        _run(["rm", "-f", name], timeout=90)

    def logs(self, name, tail=200):
        proc = _run(["logs", "-t", "--tail", str(int(tail)), name], timeout=30)
        return (proc.stdout or "") + (proc.stderr or "")

    # ---------- 镜像 ----------
    def image_exists(self, image):
        try:
            return _run(["image", "inspect", image], timeout=25).returncode == 0
        except DockerError:
            return False

    def pull(self, image, on_line=None):
        exe = docker_executable()
        if not exe:
            raise DockerError("未找到 docker 命令")
        proc = subprocess.Popen(
            [exe, "pull", image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        lines = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
        code = proc.wait()
        if code != 0:
            raise DockerError("拉取镜像失败：" + " | ".join(lines[-3:]))
        return lines

    # ---------- 独立磁盘镜像已迁移到 storage.py 统一管理 ----------

    # ---------- macvlan ----------
    def ensure_macvlan(self, inst):
        net = inst.get("macvlan_network") or ("wxedge-net-" + inst["id"])
        if _run(["network", "inspect", net], timeout=20).returncode != 0:
            _run(["network", "create", "-d", "macvlan",
                  "--subnet", inst["macvlan_subnet"],
                  "--gateway", inst["macvlan_gateway"],
                  "-o", "parent=" + inst["macvlan_parent"], net], timeout=40, check=True)
        return net

    def remove_macvlan(self, net):
        if net:
            _run(["network", "rm", net], timeout=30)

    # ---------- 自动更新（watchtower） ----------
    WATCHTOWER = "wxedge-manager-watchtower"

    def watchtower_status(self):
        try:
            proc = _run(["inspect", "--format", "{{.State.Status}}", self.WATCHTOWER], timeout=15)
            if proc.returncode == 0:
                return proc.stdout.strip() or "unknown"
        except DockerError:
            pass
        return "absent"

    def ensure_watchtower(self):
        status = self.watchtower_status()
        if status == "absent":
            _run(["run", "-d", "--name", self.WATCHTOWER, "--restart", "unless-stopped",
                  "-v", "/var/run/docker.sock:/var/run/docker.sock",
                  "--label", LABEL_MANAGER + "=1",
                  "containrrr/watchtower:latest",
                  "--cleanup", "--label-enable", "--interval", "3600"],
                 timeout=300, check=True)
        elif status != "running":
            _run(["start", self.WATCHTOWER], timeout=40, check=True)
        return self.watchtower_status()

    def stop_watchtower(self):
        _run(["rm", "-f", self.WATCHTOWER], timeout=60)
