"""memfd 参数传递的 Linux ABI 兼容性与真实内核封口测试。"""

from __future__ import annotations

import errno
import os
import sys

import pytest

from taifeng_sandbox.local.executor import _sealed_memfd

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="memfd 只在 Linux 上可用")

_SEAL_NAMES = ("F_ADD_SEALS", "F_SEAL_SEAL", "F_SEAL_SHRINK", "F_SEAL_GROW", "F_SEAL_WRITE")


@pytest.mark.parametrize("missing", [(), *((name,) for name in _SEAL_NAMES), _SEAL_NAMES])
def test_kernel_seals_with_missing_python_constants(
    monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...]
) -> None:
    """Python 未导出任一或全部封口符号时，匿名文件仍具备全部内核封口。"""
    import fcntl

    for name in missing:
        monkeypatch.delattr(fcntl, name, raising=False)
    data = b"--setenv\0TOKEN\0" + b"x" * 131072 + b"\0"
    fd = _sealed_memfd(data)
    try:
        assert not os.get_inheritable(fd)
        assert os.read(fd, len(data) + 1) == data
        # Linux UAPI 的 F_GET_SEALS=1034；不依赖本用例刻意删除的 Python 符号。
        assert fcntl.fcntl(fd, 1034) == 0x000F
        with pytest.raises(OSError, match="Operation not permitted") as write_error:
            os.pwrite(fd, b"changed", 0)
        assert write_error.value.errno == errno.EPERM
        for size in (len(data) - 1, len(data) + 1):
            with pytest.raises(OSError) as truncate_error:
                os.ftruncate(fd, size)
            assert truncate_error.value.errno == errno.EPERM
        # F_SEAL_SEAL 阻止再次修改封口集合，即使申请的集合为空。
        with pytest.raises(OSError) as seal_error:
            fcntl.fcntl(fd, 1033, 0)
        assert seal_error.value.errno == errno.EPERM
    finally:
        os.close(fd)


def test_kernel_sealing_failure_propagates_and_closes_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    """内核拒绝封口时必须关闭匿名文件并原样抛错，不能返回未封口的参数。"""
    import fcntl

    opened: list[int] = []

    def reject_sealing(fd: int, command: int, seals: int) -> int:
        """模拟内核不支持或拒绝封口。"""
        opened.append(fd)
        raise OSError(errno.EPERM, "kernel rejected seals")

    monkeypatch.setattr(fcntl, "fcntl", reject_sealing)
    with pytest.raises(OSError, match="kernel rejected seals") as seal_error:
        _sealed_memfd(b"secret")
    assert seal_error.value.errno == errno.EPERM
    assert len(opened) == 1
    with pytest.raises(OSError) as closed_error:
        os.fstat(opened[0])
    assert closed_error.value.errno == errno.EBADF
