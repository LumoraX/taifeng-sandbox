"""隔离策略 —— 与具体后端无关的「这个进程能碰什么」描述。

参照 codex ``sandboxing`` 的权限模型（Apache-2.0）：文件系统分读 / 写两组根目录，出网是一个
独立开关。差异：

- 只保留三类信息（可读范围、可写根、是否出网），不含 codex 的审批档位——审批留在 taifeng
  工具层（ADR 0001 决策 2）；
- 不含平台概念：按租户给哪个策略由上层决定（ADR 0001 决策 4）。

同一份策略交给不同后端（seatbelt / bwrap / 容器）各自翻译，语义保持一致：
**默认拒绝写、默认拒绝出网**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from taifeng_sandbox.errors import SandboxPolicyError

if TYPE_CHECKING:
    from pathlib import Path

ReadScope = Literal["all", "restricted"]


def _normalize_roots(roots: tuple[Path, ...], *, field_name: str) -> tuple[Path, ...]:
    """校验根目录全为绝对路径，并去重保序。"""
    seen: dict[Path, None] = {}
    for root in roots:
        if not root.is_absolute():
            raise SandboxPolicyError(f"{field_name} 必须是绝对路径，得到 {str(root)!r}")
        seen.setdefault(root, None)
    return tuple(seen)


@dataclass(frozen=True)
class SandboxPolicy:
    """一次隔离执行的权限边界。

    Attributes:
        read_scope: ``"all"`` = 整个文件系统可读（``unreadable_roots`` 除外）；
            ``"restricted"`` = 只有系统运行时目录、``readable_roots`` 与 ``writable_roots`` 可读。
        readable_roots: ``read_scope="restricted"`` 时额外放开的只读根目录。
        writable_roots: 可写根目录；可写蕴含可读。
        unreadable_roots: 无论 ``read_scope`` 如何都不可读写的目录（如凭据目录）。
        network: 是否允许出网。默认不允许。
    """

    read_scope: ReadScope = "all"
    readable_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()
    unreadable_roots: tuple[Path, ...] = ()
    network: bool = False

    def __post_init__(self) -> None:
        """构造期校验，避免把不合法策略带到启动进程时才发现。"""
        if self.read_scope not in ("all", "restricted"):
            raise SandboxPolicyError(f"read_scope 取值非法：{self.read_scope!r}")
        for name in ("readable_roots", "writable_roots", "unreadable_roots"):
            object.__setattr__(self, name, _normalize_roots(getattr(self, name), field_name=name))
        self._reject_writable_under_unreadable()

    def _reject_writable_under_unreadable(self) -> None:
        """可写根落在不可读目录之内是自相矛盾的策略，直接拒绝。"""
        for writable in self.writable_roots:
            for blocked in self.unreadable_roots:
                if writable == blocked or blocked in writable.parents:
                    raise SandboxPolicyError(
                        f"writable_roots 中的 {str(writable)!r} 位于 unreadable_roots "
                        f"{str(blocked)!r} 之内"
                    )

    @classmethod
    def read_only(cls, *, unreadable_roots: tuple[Path, ...] = ()) -> SandboxPolicy:
        """全盘只读、不可写、不出网。"""
        return cls(read_scope="all", unreadable_roots=unreadable_roots)

    @classmethod
    def workspace_write(
        cls,
        workspace: Path,
        *,
        extra_writable_roots: tuple[Path, ...] = (),
        unreadable_roots: tuple[Path, ...] = (),
        network: bool = False,
    ) -> SandboxPolicy:
        """全盘可读，只有工作区（及额外指定的目录）可写。"""
        return cls(
            read_scope="all",
            writable_roots=(workspace, *extra_writable_roots),
            unreadable_roots=unreadable_roots,
            network=network,
        )

    @classmethod
    def workspace_only(
        cls,
        workspace: Path,
        *,
        readable_roots: tuple[Path, ...] = (),
        network: bool = False,
    ) -> SandboxPolicy:
        """只能读系统运行时目录与指定目录，只能写工作区。"""
        return cls(
            read_scope="restricted",
            readable_roots=readable_roots,
            writable_roots=(workspace,),
            network=network,
        )


__all__ = ["ReadScope", "SandboxPolicy"]
