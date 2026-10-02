"""经真实容器起 MCP server：内核 ``McpStdioClient`` 经 ``DockerEnvironment`` 的执行器端到端跑通。"""

from __future__ import annotations

import taifeng

from taifeng_sandbox.docker import DockerEnvironment
from tests.conftest import requires_docker
from tests.daemon.test_streaming_process import MCP_SERVER
from tests.docker.conftest import ENV, sandbox_config

pytestmark = requires_docker


class _Recording:
    """包一层执行器，记下启动过的进程，便于事后核对退出码。"""

    def __init__(self, inner: taifeng.CommandExecutor) -> None:
        """包装 ``inner``。"""
        self._inner = inner
        self.started: list[taifeng.CommandProcess] = []

    async def start(self, spec: taifeng.CommandSpec) -> taifeng.CommandProcess:
        """转交 ``inner`` 启动并记下。"""
        proc = await self._inner.start(spec)
        self.started.append(proc)
        return proc


async def test_mcp_stdio_client_through_a_container() -> None:
    """server 脚本经 ``DaemonWorkspace`` 写进容器工作区；握手、列工具、调用都通，关闭后退出码为 0。

    关闭时 server 读到 EOF 自己退出（退出码 0），而不是等满 3 秒被杀。
    """
    config = sandbox_config()
    async with await DockerEnvironment.create(config) as environment:
        await environment.workspace().write_text("server.py", MCP_SERVER)
        executor = _Recording(environment.executor())
        mcp = await taifeng.McpStdioClient.spawn(
            ["python3", "server.py"], env=dict(ENV), executor=executor, cwd=config.workdir
        )
        try:
            assert [tool["name"] for tool in await mcp.list_tools()] == ["ping"]
            result = await mcp.call_tool("ping", {})
            assert result["content"][0]["text"] == "pong"
        finally:
            await mcp.close()
        assert [proc.returncode for proc in executor.started] == [0]
