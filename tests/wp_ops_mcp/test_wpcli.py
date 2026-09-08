"""Tests for the wp_ops_mcp SSH + WP-CLI transport."""
import pytest

from core.errors import APIError
from skills.wpengine.ssh import SSHResult
from wp_ops_mcp.transport.wpcli import WPCliTransport, SSHProbe


class FakeSSH:
    """Duck-typed stand-in for WPESSHClient."""
    def __init__(self, *, result: SSHResult | None = None, raise_exc: Exception | None = None):
        self.install_name = "fakeinstall"
        self._result = result
        self._raise = raise_exc
        self.calls: list[str] = []

    async def exec(self, command: str, timeout: int = 30) -> SSHResult:
        self.calls.append(command)
        if self._raise:
            raise self._raise
        return self._result

    async def wp_cli(self, command: str, timeout: int = 30) -> SSHResult:
        self.calls.append(f"wp {command}")
        if self._raise:
            raise self._raise
        return self._result


class TestProbe:
    async def test_reachable_and_wp_cli_ok(self):
        ssh = FakeSSH(result=SSHResult(stdout="WP-CLI 2.10.0", stderr="", exit_code=0))
        t = WPCliTransport("fakeinstall", ssh_client=ssh)
        probe = await t.probe()
        assert isinstance(probe, SSHProbe)
        assert probe.reachable is True
        assert probe.wp_cli is True

    async def test_reachable_but_wp_cli_missing(self):
        ssh = FakeSSH(result=SSHResult(stdout="", stderr="wp: command not found", exit_code=127))
        t = WPCliTransport("fakeinstall", ssh_client=ssh)
        probe = await t.probe()
        assert probe.reachable is True
        assert probe.wp_cli is False

    async def test_unreachable_on_connection_error(self):
        ssh = FakeSSH(raise_exc=APIError("ssh", None, "SSH auth failed"))
        t = WPCliTransport("fakeinstall", ssh_client=ssh)
        probe = await t.probe()
        assert probe.reachable is False
        assert probe.wp_cli is False
        assert "ssh" in probe.detail.lower() or "auth" in probe.detail.lower()


class TestRun:
    async def test_run_delegates_to_wp_cli(self):
        ssh = FakeSSH(result=SSHResult(stdout="ok", stderr="", exit_code=0))
        t = WPCliTransport("fakeinstall", ssh_client=ssh)
        res = await t.run("option get blogname")
        assert res.stdout == "ok"
        assert "wp option get blogname" in ssh.calls

    async def test_run_raw_executes_command_verbatim(self):
        # run_raw runs a full shell command in ONE session (no cd/wp prefix),
        # used by discovery to batch many WP-CLI calls into one connection.
        ssh = FakeSSH(result=SSHResult(stdout="combined", stderr="", exit_code=0))
        t = WPCliTransport("fakeinstall", ssh_client=ssh)
        res = await t.run_raw("echo a; echo b")
        assert res.stdout == "combined"
        assert "echo a; echo b" in ssh.calls
