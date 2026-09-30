"""文档不能悄悄过期：站内链接要指得到东西，接口参考要覆盖公开接口与线协议。"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

import taifeng_sandbox
from taifeng_sandbox import daemon, docker, local
from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.docker import DockerSandboxConfig
from taifeng_sandbox.policy import SandboxPolicy

REPO = Path(__file__).resolve().parents[1]

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _documents() -> list[Path]:
    """仓库里的全部 Markdown 文档。"""
    return [REPO / "README.md", REPO / "AGENTS.md", *sorted((REPO / "docs").rglob("*.md"))]


def _anchors(path: Path) -> set[str]:
    """一份文档里各级标题对应的锚点（与代码托管平台的生成规则一致）。"""
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    for heading in _HEADING.findall(text):
        plain = re.sub(r"[`*]", "", heading).strip().lower()
        anchors.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", plain)))
    return anchors


def _broken_links(path: Path) -> list[str]:
    """一份文档里指不到东西的站内链接。"""
    broken: list[str] = []
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists():
            broken.append(f"{target}（文件不存在）")
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            broken.append(f"{target}（没有这个标题）")
    return broken


def _missing(names: set[str], document: str) -> list[str]:
    """哪些名字没有作为完整的词出现在文档里。"""
    return sorted(
        name
        for name in names
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", document)
    )


def _read(relative: str) -> str:
    """读仓库里的一份文档。"""
    return (REPO / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", _documents(), ids=lambda path: str(path.relative_to(REPO)))
def test_document_links_resolve(path: Path) -> None:
    """文档里的相对链接都指向存在的文件与标题。"""
    assert _broken_links(path) == []


def test_reference_covers_public_names() -> None:
    """接口参考覆盖各模块公开导出的全部名字。"""
    names: set[str] = set()
    for module in (taifeng_sandbox, local, docker, daemon):
        names |= {name for name in module.__all__ if not name.startswith("__")}
    assert {"SandboxPolicy", "DockerEnvironment", "DaemonClient"} <= names
    document = _read("docs/architecture/reference.md") + _read("docs/architecture/protocol.md")
    assert _missing(names, document) == []


def test_reference_covers_config_and_policy_fields() -> None:
    """接口参考覆盖容器配置与隔离策略的全部字段。"""
    fields = {field.name for field in dataclasses.fields(DockerSandboxConfig)}
    fields |= {field.name for field in dataclasses.fields(SandboxPolicy)}
    assert {"pids_limit", "docker_env", "unreadable_roots"} <= fields
    assert _missing(fields, _read("docs/architecture/reference.md")) == []


def test_protocol_document_covers_methods_and_error_codes() -> None:
    """线协议文档覆盖全部方法、通知与错误码。"""
    document = _read("docs/architecture/protocol.md")
    methods = {
        value
        for name, value in vars(protocol).items()
        if name.startswith(("METHOD_", "NOTIFY_")) and isinstance(value, str)
    }
    codes = {
        str(value)
        for name, value in vars(protocol).items()
        if name.startswith("ERROR_") and isinstance(value, int)
    }
    assert "process/start" in methods
    assert "-32020" in codes
    assert [method for method in sorted(methods) if f"`{method}`" not in document] == []
    assert [code for code in sorted(codes) if f"`{code}`" not in document] == []
