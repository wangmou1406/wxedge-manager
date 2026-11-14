"""独立存储卷（loop ext4）与存储用量工具。

- loop 模式：为每个实例创建独立的 ext4 镜像文件并挂载，提供内核级硬性容量上限。
- dir 模式：普通目录挂载，由管理器统计用量，可按「超限停止」策略进行约束。
"""
import os
import subprocess
import sys

IMAGE_NAME = "storage.img"
MOUNT_NAME = "storage"


class StorageError(RuntimeError):
    pass


def loop_supported():
    return sys.platform.startswith("linux") and os.path.exists("/dev/loop-control")


def is_root():
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def _sh(args, check=True, timeout=600):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StorageError("命令执行失败: %s (%s)" % (" ".join(args[:2]), exc))
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        raise StorageError(message.splitlines()[-1] if message else ("命令退出码 %s" % proc.returncode))
    return proc


def image_path(inst):
    return os.path.join(inst["data_dir"], IMAGE_NAME)


def mount_path(inst):
    return os.path.join(inst["data_dir"], MOUNT_NAME)


def ensure(inst):
    """确保 loop 磁盘就绪（按 disk_gb 创建/扩容并挂载），返回挂载点。"""
    if (inst.get("disk_mode") or "dir") != "loop":
        return None
    if not sys.platform.startswith("linux"):
        raise StorageError("独立磁盘镜像仅支持 Linux")
    if not loop_supported():
        raise StorageError("未检测到 /dev/loop-control，当前环境不支持 loop 设备")
    if not is_root():
        raise StorageError("独立磁盘镜像需要 root 运行管理器（使用 systemd 服务或 sudo 启动）")
    data_dir = inst["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    image = image_path(inst)
    mount = mount_path(inst)
    size_gb = int(inst.get("disk_gb") or 0)
    os.makedirs(mount, exist_ok=True)
    if not os.path.exists(image):
        if size_gb <= 0:
            raise StorageError("磁盘大小必须大于 0 GB")
        _sh(["fallocate", "-l", "%dG" % size_gb, image])
        _sh(["mkfs.ext4", "-q", "-F", image])
    else:
        current_gb = max(1, os.path.getsize(image) // (1024 ** 3))
        if size_gb and current_gb < size_gb:
            if os.path.ismount(mount):
                _sh(["umount", mount])
            _sh(["truncate", "-s", "%dG" % size_gb, image])
            _sh(["e2fsck", "-f", "-y", image], check=False)
            _sh(["resize2fs", image], check=False)
    if not os.path.ismount(mount):
        _sh(["mount", "-o", "loop", image, mount])
    return mount


def unmount(inst):
    mount = mount_path(inst)
    if os.path.ismount(mount):
        _sh(["umount", "-l", mount], check=False)


def purge(inst):
    unmount(inst)
    try:
        os.remove(image_path(inst))
    except OSError:
        pass


def usage(inst):
    """读取 loop 卷实际占用；未挂载时返回 mounted=False。"""
    mount = mount_path(inst)
    if not os.path.ismount(mount):
        return {"mounted": False, "used_bytes": 0, "total_bytes": 0}
    try:
        stat = os.statvfs(mount)
    except OSError as exc:
        return {"mounted": True, "used_bytes": 0, "total_bytes": 0, "error": str(exc)}
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bfree * stat.f_frsize
    return {"mounted": True, "used_bytes": int(total - free), "total_bytes": int(total)}
