"""线协议：常量一致性、握手与错误处理。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from taifeng_sandbox import SandboxProtocolError, SandboxRemoteError, SandboxUnavailableError
from taifeng_sandbox.daemon import DaemonClient, StdioTransport, protocol, server
from tests.daemon.conftest import daemon_argv

if TYPE_CHECKING:
    from pathlib import Path


def test_server_constants_match_protocol_module() -> None:
    """守护进程单文件里自带的常量必须与协议模块逐项一致。"""
    names = [
        name
        for name in protocol.__all__
        if name.startswith(("ERROR_", "MAX_")) or name == "PROTOCOL_VERSION"
    ]
    assert names
    for name in names:
        assert getattr(server, name) == getattr(protocol, name), name


def test_server_handles_every_declared_method() -> None:
    """协议模块声明的每个方法，守护进程源码里都有对应的方法名。"""
    source = server.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    declared = {getattr(protocol, name) for name in protocol.__all__ if name.startswith("METHOD_")}
    assert {"process/write", "process/closeStdin"} <= declared
    for name in protocol.__all__:
        if name.startswith(("METHOD_", "NOTIFY_")):
            assert f'"{getattr(protocol, name)}"' in text, name


async def test_handshake_reports_server_info(client: DaemonClient, root: Path) -> None:
    """握手后能拿到守护进程的版本与根目录。"""
    info = client.server_info
    assert info["protocolVersion"] == protocol.PROTOCOL_VERSION
    assert info["root"] == str(root)
    assert info["serverName"] == "taifeng-sandbox-daemon"


async def test_unknown_method_is_remote_error(client: DaemonClient) -> None:
    """未知方法返回协议错误码。"""
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("nope/nothing")
    assert caught.value.code == protocol.ERROR_METHOD_NOT_FOUND


async def test_invalid_params_is_remote_error(client: DaemonClient) -> None:
    """参数缺失返回参数错误。"""
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request(protocol.METHOD_PROCESS_START, {"processId": "x"})
    assert caught.value.code == protocol.ERROR_INVALID_PARAMS


async def _raw_exchange(transport: StdioTransport, payload: dict[str, object]) -> dict[str, object]:
    """绕过客户端直接收发一条消息。"""
    await transport.send((json.dumps(payload) + "\n").encode())
    line = await transport.receive()
    assert line is not None
    reply = json.loads(line)
    assert isinstance(reply, dict)
    return reply


async def test_request_before_handshake_rejected(root: Path) -> None:
    """握手之前只接受 initialize。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    try:
        reply = await _raw_exchange(
            transport,
            {"jsonrpc": "2.0", "id": 1, "method": "fs/readDirectory", "params": {"path": "."}},
        )
        assert reply["error"]["code"] == protocol.ERROR_NOT_INITIALIZED  # type: ignore[index]
    finally:
        await transport.close()


async def test_unsupported_version_rejected(root: Path) -> None:
    """协议版本不匹配时握手失败。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    try:
        reply = await _raw_exchange(
            transport,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 999}},
        )
        assert reply["error"]["code"] == protocol.ERROR_UNSUPPORTED_VERSION  # type: ignore[index]
    finally:
        await transport.close()


async def test_malformed_line_is_skipped(root: Path) -> None:
    """畸形消息被跳过，连接保持可用。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    try:
        await transport.send(b"this is not json\n")
        reply = await _raw_exchange(
            transport,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"protocolVersion": protocol.PROTOCOL_VERSION},
            },
        )
        assert reply["id"] == 7
        assert "result" in reply
    finally:
        await transport.close()


async def test_daemon_exit_fails_pending_and_later_requests(tmp_path: Path) -> None:
    """守护进程起不来：握手失败，错误里带上它的诊断输出。"""
    missing = tmp_path / "missing-root"
    transport = await StdioTransport.spawn(daemon_argv(missing))
    with pytest.raises(SandboxProtocolError) as caught:
        await DaemonClient.connect(transport)
    assert "根目录不存在" in str(caught.value)


async def test_missing_launcher_is_unavailable(tmp_path: Path) -> None:
    """启动命令本身不存在：按后端不可用处理。"""
    with pytest.raises(SandboxUnavailableError):
        await StdioTransport.spawn([str(tmp_path / "no-such-binary")])


async def test_request_after_close_fails(client: DaemonClient) -> None:
    """关闭后的请求立即失败，不会挂住。"""
    await client.close()
    with pytest.raises(SandboxProtocolError):
        await client.request(protocol.METHOD_FS_READ_DIRECTORY, {"path": "."})
    assert client.closed
