"""主机资源探测（跨平台，仅标准库）。"""
import ctypes
import os
import platform
import shutil
import socket
import string
import sys
import threading
import time

_LOCK = threading.Lock()
_LAST_SAMPLE = {"ts": 0.0, "idle": 0.0, "total": 0.0}
_WIN_PREV = {"idle": 0, "total": 0}

_FS_OK = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "hfs", "apfs", "overlay",
    "ecryptfs", "bcachefs", "cifs", "nfs", "nfs4",
}


# ---------- CPU ----------
def _linux_cpu_snapshot():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            parts = fh.readline().split()
    except OSError:
        return None
    if len(parts) < 5:
        return None
    nums = [int(x) for x in parts[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return {"idle": float(idle), "total": float(sum(nums))}


def _win_cpu_snapshot():
    import ctypes.wintypes as wt

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None

    def val(ft):
        return (ft.hi << 32) | ft.lo
    return {"idle": float(val(idle)), "total": float(val(kernel) + val(user))}


def _cpu_snapshot():
    if sys.platform.startswith("linux"):
        return _linux_cpu_snapshot()
    if sys.platform == "win32":
        return _win_cpu_snapshot()
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        return None
    return {"idle": 0.0, "total": 0.0, "load1": load, "count": os.cpu_count() or 1}


def cpu_usage():
    snap = _cpu_snapshot()
    if not snap:
        return 0.0
    if "load1" in snap:
        return round(min(100.0, snap["load1"] / max(1, snap["count"]) * 100.0), 1)
    with _LOCK:
        prev = dict(_LAST_SAMPLE)
        now = time.time()
        if prev["ts"] and now - prev["ts"] >= 0.15:
            dt = snap["total"] - prev["total"]
            if dt > 0:
                pct = 100.0 * (1.0 - (snap["idle"] - prev["idle"]) / dt)
                _LAST_SAMPLE.update({"ts": now, "idle": snap["idle"], "total": snap["total"]})
                return round(max(0.0, min(100.0, pct)), 1)
        _LAST_SAMPLE.update({"ts": now, "idle": snap["idle"], "total": snap["total"]})
    time.sleep(0.12)
    snap2 = _cpu_snapshot()
    if not snap2:
        return 0.0
    with _LOCK:
        dt = snap2["total"] - _LAST_SAMPLE["total"]
        if dt > 0:
            pct = 100.0 * (1.0 - (snap2["idle"] - _LAST_SAMPLE["idle"]) / dt)
            _LAST_SAMPLE.update({"ts": time.time(), "idle": snap2["idle"], "total": snap2["total"]})
            return round(max(0.0, min(100.0, pct)), 1)
    return 0.0


def cpu_model():
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    cpu = platform.processor() or platform.machine()
    return cpu or "Unknown CPU"


# ---------- 内存 ----------
def _windows_mem():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
        _pack_ = 1
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    return {"total_bytes": int(stat.ullTotalPhys), "available_bytes": int(stat.ullAvailPhys)}


def memory_info():
    raw = None
    if sys.platform.startswith("linux"):
        try:
            vals = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    try:
                        vals[key.strip()] = int(rest.strip().split()[0]) * 1024
                    except (IndexError, ValueError):
                        continue
            total = vals.get("MemTotal", 0)
            avail = vals.get("MemAvailable")
            if avail is None:
                avail = vals.get("MemFree", 0) + vals.get("Buffers", 0) + vals.get("Cached", 0)
            raw = {"total_bytes": total, "available_bytes": avail}
        except OSError:
            raw = None
    elif sys.platform == "win32":
        raw = _windows_mem()
    if not raw:
        total = 0
        try:
            if hasattr(os, "sysconf"):
                total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            total = 0
        raw = {"total_bytes": total, "available_bytes": total}
    total = int(raw.get("total_bytes") or 0)
    avail = int(raw.get("available_bytes") or 0)
    avail = max(0, min(avail, total))
    used = max(0, total - avail)
    return {
        "total_bytes": total,
        "available_bytes": avail,
        "used_bytes": used,
        "used_pct": round(100.0 * used / total, 1) if total else 0.0,
    }


# ---------- 磁盘 ----------
def disks():
    out = []
    if sys.platform.startswith("linux"):
        seen = set()
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            device = parts[0]
            mount = parts[1].replace("\\040", " ")
            fstype = parts[2]
            if fstype not in _FS_OK:
                continue
            if device in seen:
                continue
            seen.add(device)
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total <= 0:
                continue
            out.append({
                "mount": mount,
                "device": device,
                "fstype": fstype,
                "total_bytes": int(total),
                "free_bytes": int(free),
                "used_bytes": int(total - free),
            })
    elif sys.platform == "win32":
        try:
            roots = list(os.listdrives())
        except AttributeError:
            roots = [d + ":\\" for d in string.ascii_uppercase if os.path.exists(d + ":\\")]
        for root in roots:
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            out.append({
                "mount": root,
                "device": root,
                "fstype": "ntfs",
                "total_bytes": int(usage.total),
                "free_bytes": int(usage.free),
                "used_bytes": int(usage.used),
            })
    else:
        try:
            usage = shutil.disk_usage("/")
            out.append({"mount": "/", "device": "/", "fstype": "apfs",
                        "total_bytes": usage.total, "free_bytes": usage.free,
                        "used_bytes": usage.used})
        except OSError:
            pass
    out.sort(key=lambda d: (0 if d["mount"] == "/" else 1, d["mount"]))
    return out


def disk_for_path(path):
    path = os.path.abspath(path)
    best = None
    if sys.platform.startswith("linux"):
        for item in disks():
            mount = item["mount"]
            if path == mount or path.startswith(mount.rstrip("/") + "/"):
                if best is None or len(mount) > len(best["mount"]):
                    best = dict(item)
    if best is None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return {
            "mount": os.path.splitdrive(path)[0] or "/",
            "device": os.path.splitdrive(path)[0] or "/",
            "fstype": "",
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
        }
    return best


# ---------- 系统信息 ----------
COMMON_STORAGE_DIRS = ["/mnt", "/media", "/data", "/srv", "/home", "/opt", "/var/lib"]


def _fstype_map():
    return {item["mount"]: item.get("fstype", "") for item in disks()}


def storage_candidates(data_dir=None):
    """自动探测可用存储目录（Linux 挂载点 + 常见数据目录；Windows 盘符）。"""
    fsmap = _fstype_map()
    result = []
    seen = set()

    skip_prefixes = ("/usr", "/boot", "/dev", "/proc", "/sys", "/run", "/snap", "/var/lib/docker", "/var/lib/containerd")

    def add(path, source):
        try:
            path = os.path.abspath(path)
        except (TypeError, ValueError):
            return
        if path in seen or not os.path.isdir(path):
            return
        if sys.platform.startswith("linux") and any(path == prefix or path.startswith(prefix + "/") for prefix in skip_prefixes):
            return
        seen.add(path)
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return
        fstype = fsmap.get(path, "")
        if not fstype:
            for mount, value in fsmap.items():
                if path == mount or path.startswith(mount.rstrip("/") + "/"):
                    fstype = value
        writable = os.access(path, os.W_OK)
        result.append({
            "path": path,
            "source": source,
            "fstype": fstype,
            "free_bytes": int(usage.free),
            "total_bytes": int(usage.total),
            "writable": bool(writable),
            "recommended": bool(writable and usage.free > 10 * (1024 ** 3) and path not in ("/", "C:\\")),
        })

    if sys.platform.startswith("linux"):
        for item in disks():
            add(item["mount"], "mount")
        for path in COMMON_STORAGE_DIRS:
            add(path, "common")
    elif sys.platform == "win32":
        for item in disks():
            add(item["mount"], "mount")
    if data_dir:
        add(data_dir, "settings")
        parent = os.path.dirname(os.path.abspath(data_dir))
        if parent:
            add(parent, "settings")
    result.sort(key=lambda item: (0 if item["recommended"] else 1, 0 if item["writable"] else 1,
                                  -item["free_bytes"], item["path"]))
    return result


def os_pretty_name():
    if sys.platform.startswith("linux"):
        try:
            with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    return platform.system() + " " + platform.release()


def uptime_seconds():
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as fh:
                return int(float(fh.read().split()[0]))
        except (OSError, IndexError, ValueError):
            return 0
    if sys.platform == "win32":
        try:
            return int(ctypes.windll.kernel32.GetTickCount64() / 1000)
        except Exception:
            return 0
    return 0


def host_info(data_dir):
    disks_info = disks()
    data_disk = disk_for_path(data_dir)
    return {
        "os": {
            "pretty": os_pretty_name(),
            "system": platform.system(),
            "release": platform.release(),
            "kernel": platform.version(),
            "arch": platform.machine(),
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
        },
        "cpu": {
            "count": os.cpu_count() or 1,
            "model": cpu_model(),
            "usage_pct": cpu_usage(),
        },
        "mem": memory_info(),
        "disks": disks_info,
        "data_disk": data_disk,
        "uptime_seconds": uptime_seconds(),
        "time": time.time(),
    }
