"""``DaemonCommandExecutor``：经守护进程执行命令。"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

import pytest
from taifeng import (
    CancellationToken,
    CommandExecutor,
    CommandProcess,
    CommandSpec,
    ScriptDescriptor,
    ScriptInvocation,
)

from taifeng_sandbox import SandboxProtocolError, shell_script_executor
from taifeng_sandbox.daemon import (
    DaemonClient,
    DaemonCommandExecutor,
    RemoteProcess,
    StdioTransport,
    protocol,
)
from tests.daemon.conftest import daemon_argv

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}


def _shell(command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> CommandSpec:
    """构造 shell 模式的命令。"""
    return CommandSpec(command=command, shell=True, cwd=cwd, env=dict(env or ENV))


async def _run(executor: CommandExecutor, spec: CommandSpec) -> tuple[int, str, str]:
    """启动并等待结束。"""
    proc = await executor.start(spec)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


async def test_satisfies_kernel_protocols(client: DaemonClient) -> None:
    """执行器与进程句柄都满足 taifeng 协议。"""
    executor = DaemonCommandExecutor(client)
    assert isinstance(executor, CommandExecutor)
    proc = await executor.start(_shell("true"))
    assert isinstance(proc, CommandProcess)
    assert await proc.wait() == 0


async def test_output_and_exit_code(client: DaemonClient) -> None:
    """两路输出分开取回，退出码如实返回。"""
    code, out, err = await _run(
        DaemonCommandExecutor(client), _shell("echo out; echo err 1>&2; exit 3")
    )
    assert (code, out.strip(), err.strip()) == (3, "out", "err")


async def test_default_cwd_is_root(client: DaemonClient, root: Path) -> None:
    """未指定工作目录时用守护进程根目录。"""
    code, out, _ = await _run(DaemonCommandExecutor(client), _shell("pwd -P"))
    assert (code, out.strip()) == (0, str(root))


async def test_cwd_may_be_outside_root(client: DaemonClient, tmp_path: Path) -> None:
    """进程工作目录不受根目录约束：进程的边界是执行环境，不是守护进程根目录。"""
    code, out, _ = await _run(
        DaemonCommandExecutor(client), _shell("pwd -P", cwd=str(tmp_path.resolve()))
    )
    assert (code, out.strip()) == (0, str(tmp_path.resolve()))


async def test_relative_cwd_resolves_against_root(client: DaemonClient, root: Path) -> None:
    """相对工作目录相对根目录解释。"""
    (root / "sub").mkdir()
    code, out, _ = await _run(DaemonCommandExecutor(client), _shell("pwd -P", cwd="sub"))
    assert (code, out.strip()) == (0, str(root / "sub"))


async def test_missing_cwd_is_spawn_error(client: DaemonClient, root: Path) -> None:
    """工作目录不存在按启动失败处理（``OSError``）。"""
    with pytest.raises(OSError, match="no-such-dir"):
        await DaemonCommandExecutor(client).start(_shell("pwd", cwd=str(root / "no-such-dir")))


async def test_environment_is_exactly_what_was_given(
    client: DaemonClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程环境就是请求给的那份，既不继承宿主也不继承守护进程的环境。"""
    monkeypatch.setenv("HOST_ONLY_SECRET", "leak-me")
    spec = _shell('echo "[${HOST_ONLY_SECRET}][${GIVEN}]"', env={**ENV, "GIVEN": "ok"})
    code, out, _ = await _run(DaemonCommandExecutor(client), spec)
    assert (code, out.strip()) == (0, "[][ok]")


async def test_missing_binary_is_file_not_found(client: DaemonClient) -> None:
    """可执行文件不存在还原成 ``FileNotFoundError``。"""
    spec = CommandSpec(command="/no/such/binary --flag", shell=False, cwd=None, env=dict(ENV))
    with pytest.raises(FileNotFoundError):
        await DaemonCommandExecutor(client).start(spec)


async def test_failed_start_leaves_no_handlers(client: DaemonClient) -> None:
    """启动失败后不残留通知登记。"""
    spec = CommandSpec(command="/no/such/binary", shell=False, cwd=None, env=dict(ENV))
    with pytest.raises(FileNotFoundError):
        await DaemonCommandExecutor(client).start(spec)
    assert client._handlers == {}  # noqa: SLF001 —— 白盒核对清理


async def test_kill_terminates_process_group(client: DaemonClient, root: Path) -> None:
    """强杀连同 shell 派生的子进程一起终止。"""
    marker = root / "late.txt"
    proc = await DaemonCommandExecutor(client).start(
        _shell(f"(sleep 2; echo late > {marker}) & wait")
    )
    await asyncio.sleep(0.3)
    proc.kill()
    assert await asyncio.wait_for(proc.wait(), timeout=5) != 0
    await asyncio.sleep(2.2)
    assert not marker.exists()


async def test_kill_after_exit_is_noop(client: DaemonClient) -> None:
    """进程结束后再强杀不报错，也不改变退出码。"""
    proc = await DaemonCommandExecutor(client).start(_shell("exit 5"))
    assert await proc.wait() == 5
    proc.kill()
    assert proc.returncode == 5


async def test_concurrent_processes_do_not_mix_output(client: DaemonClient) -> None:
    """并发进程的输出互不串线。"""
    executor = DaemonCommandExecutor(client)
    results = await asyncio.gather(
        *(_run(executor, _shell(f"for i in 1 2 3; do echo job-{n}; done")) for n in range(8))
    )
    for index, (code, out, _) in enumerate(results):
        assert code == 0
        assert out.split() == [f"job-{index}"] * 3


async def test_large_output_arrives_intact(client: DaemonClient) -> None:
    """跨多个输出块的大输出完整到达。"""
    code, out, _ = await _run(
        DaemonCommandExecutor(client), _shell("head -c 300000 /dev/zero | tr '\\0' 'x'")
    )
    assert code == 0
    assert len(out) == 300000
    assert set(out) == {"x"}


async def test_output_beyond_buffer_limit_is_dropped(client: DaemonClient) -> None:
    """超过宿主侧缓冲上限的输出被丢弃并计数，不会无限占内存。"""
    executor = DaemonCommandExecutor(client, max_buffer_bytes=1000)
    proc = await executor.start(_shell("head -c 50000 /dev/zero | tr '\\0' 'x'"))
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert len(stdout) == 1000
    assert isinstance(proc, RemoteProcess)
    assert proc.dropped_bytes == 49000


async def test_closing_connection_kills_running_processes(root: Path) -> None:
    """连接关闭时守护进程杀掉名下进程，进程句柄按被杀收尾。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    client = await DaemonClient.connect(transport)
    marker = root / "survivor.txt"
    proc = await DaemonCommandExecutor(client).start(_shell(f"sleep 2; echo alive > {marker}"))
    await asyncio.sleep(0.3)
    await client.close()
    assert await asyncio.wait_for(proc.wait(), timeout=5) != 0
    await asyncio.sleep(2.2)
    assert not marker.exists()


async def test_script_executor_over_daemon(client: DaemonClient, root: Path) -> None:
    """脚本执行器与守护进程执行器组合：参数、输出、超时都生效。"""
    script = root / "skill" / "greet.sh"
    script.parent.mkdir()
    script.write_text('echo "hello $1"\n')
    descriptor = ScriptDescriptor(
        skill_id="demo",
        name="greet",
        path=script,
        language="shell",
        args_schema={"type": "object", "properties": {"who": {}}},
    )
    executor = shell_script_executor(DaemonCommandExecutor(client))
    result = await executor.execute(
        ScriptInvocation(descriptor=descriptor, args={"who": "world"}, cancel=CancellationToken())
    )
    assert result.ok, result.stderr
    assert result.stdout.strip() == "hello world"

    slow = root / "skill" / "slow.sh"
    slow.write_text("exec sleep 30\n")
    slow_descriptor = ScriptDescriptor(
        skill_id="demo", name="slow", path=slow, language="shell", timeout_seconds=0.5
    )
    timed_out = await executor.execute(
        ScriptInvocation(descriptor=slow_descriptor, args={}, cancel=CancellationToken())
    )
    assert timed_out.is_timeout
    assert timed_out.killed


async def _until(condition: Callable[[], bool]) -> None:
    """让出事件循环直到条件成立，最多等 5 秒。"""
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 —— 轮询白盒状态，没有可等的事件
            await asyncio.sleep(0)


class _RecordingRequests:
    """替换 ``client.request``：记下发出的请求，以及每个请求落定后的结果。"""

    def __init__(self, client: DaemonClient) -> None:
        """包装 ``client`` 原来的 ``request``。"""
        self._request = client.request
        self.sent: list[str] = []
        self.done: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = []

    async def __call__(
        self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """转交原来的 ``request`` 并记录。"""
        self.sent.append(method)
        result = await self._request(method, params, **kwargs)
        self.done.append((method, params, result))
        return result


async def _cancel_once_sent(
    executor: DaemonCommandExecutor, spec: CommandSpec, sent: list[str]
) -> None:
    """启动请求一发出就取消 ``start()``，再等后台善后结束。"""
    start = asyncio.ensure_future(executor.start(spec))
    await _until(lambda: bool(sent))
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    cleanups = list(executor._cleanups)  # noqa: SLF001 —— 白盒等后台善后
    assert len(cleanups) == 1
    await asyncio.wait_for(asyncio.gather(*cleanups), 10)


async def test_cancelled_start_kills_the_started_process(
    client: DaemonClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动请求已发出时 ``start()`` 被取消：等守护进程回了启动结果，再补发强杀，真的杀到进程。

    进程是要标准输入、一直等着读的流式进程——不杀它会挂到连接关闭。强杀若紧跟着取消发出，会赶在
    守护进程登记进程之前扑空（``killed`` 为假）。
    """
    recorder = _RecordingRequests(client)
    monkeypatch.setattr(client, "request", recorder)
    executor = DaemonCommandExecutor(client)
    spec = CommandSpec(
        command=f"{sys.executable} -c 'import sys; sys.stdin.read()'",
        shell=False,
        cwd=None,
        env={},
        stdin=True,
    )
    await _cancel_once_sent(executor, spec, recorder.sent)
    assert [method for method, _, _ in recorder.done] == [
        protocol.METHOD_PROCESS_START,
        protocol.METHOD_PROCESS_KILL,
    ]
    (_, start_params, _), (_, kill_params, killed) = recorder.done
    assert start_params is not None and kill_params is not None
    assert kill_params["processId"] == start_params["processId"]
    assert killed == {"killed": True}
    assert client._handlers == {}  # noqa: SLF001 —— 撤销了通知登记


class _HeldStart:
    """守护进程连接的替身：启动请求挂到放行为止，强杀请求一律按连接已断失败。"""

    closed = False

    def __init__(self) -> None:
        """记下收到的请求。"""
        self.methods: list[str] = []
        self.release = asyncio.Event()

    def on_notification(self, method: str, process_id: str, handler: object) -> None:
        """不分发通知。"""

    def remove_handlers(self, process_id: str) -> None:
        """没有登记可撤。"""

    def on_disconnect(self, listener: object) -> Callable[[], None]:
        """不会断开。"""
        return lambda: None

    async def request(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        """启动请求等放行后成功；其他请求失败。"""
        self.methods.append(method)
        if method == protocol.METHOD_PROCESS_START:
            await self.release.wait()
            return {"pid": 1}
        raise SandboxProtocolError("守护进程连接已断开")


async def test_cancelled_start_kills_only_after_the_start_settles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """善后先等启动请求落定才发强杀；强杀失败被取走并记 debug，不变成没人取的任务异常。"""
    fake = _HeldStart()
    executor = DaemonCommandExecutor(fake)  # type: ignore[arg-type]
    start = asyncio.ensure_future(executor.start(_shell("true")))
    await _until(lambda: bool(fake.methods))
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    cleanups = list(executor._cleanups)  # noqa: SLF001
    await asyncio.sleep(0)
    assert fake.methods == [protocol.METHOD_PROCESS_START]
    with caplog.at_level(logging.DEBUG, logger="taifeng_sandbox.daemon.executor"):
        fake.release.set()
        await asyncio.wait_for(asyncio.gather(*cleanups), 5)
    assert fake.methods == [protocol.METHOD_PROCESS_START, protocol.METHOD_PROCESS_KILL]
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    assert "强杀放弃启动的进程" in caplog.text
    assert executor._cleanups == set()  # noqa: SLF001


async def test_failed_start_sends_no_kill(
    client: DaemonClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守护进程明确回了启动失败：进程没起来，不补发强杀。"""
    recorder = _RecordingRequests(client)
    monkeypatch.setattr(client, "request", recorder)
    executor = DaemonCommandExecutor(client)
    spec = CommandSpec(command="/no/such/binary", shell=False, cwd=None, env=dict(ENV))
    with pytest.raises(FileNotFoundError):
        await executor.start(spec)
    assert recorder.sent == [protocol.METHOD_PROCESS_START]
    assert executor._cleanups == set()  # noqa: SLF001
