"""测试公共夹具。"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

# 本机隔离后端只能在对应平台真跑
requires_macos = pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt 只在 macOS 上可用")


def bwrap_usable() -> bool:
    """本机是否是 Linux、装了 bubblewrap、且允许创建用户命名空间。"""
    if not sys.platform.startswith("linux"):
        return False
    bwrap = next(
        (path for path in ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")
         if shutil.which(path) is not None),
        None,
    )
    if bwrap is None:
        return False
    probe = subprocess.run(  # noqa: S603 —— 固定参数的探测命令
        [bwrap, "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "/bin/true"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    return probe.returncode == 0


requires_bwrap = pytest.mark.skipif(
    not bwrap_usable(), reason="需要 Linux + bubblewrap + 可用的用户命名空间"
)


def docker_available() -> bool:
    """本机是否有可用的 Docker 守护进程。"""
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run(  # noqa: S603 —— 固定参数的探测命令
        [docker, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    return probe.returncode == 0


requires_docker = pytest.mark.skipif(not docker_available(), reason="没有可用的 Docker 守护进程")
