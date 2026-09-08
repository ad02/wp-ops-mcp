"""SSH + WP-CLI transport via the WP Engine SSH gateway.

Thin wrapper over the repo's existing skills.wpengine.ssh.WPESSHClient (reused as-is)
so the MCP layer has a stable, injectable interface.

IMPORTANT operational constraints of the WPE SSH gateway (keep ops short-lived):
- Sessions time out at ~10 minutes; never hold one session open for a whole batch.
- 5 concurrent SSH connections per install user.
- No root; files outside ~/sites/<install>/ vanish at session end.
Prefer REST for bulk; use this for what REST can't reach (options, debug, et-cache purge).
"""
from __future__ import annotations

from dataclasses import dataclass

# The SSH/wp-cli transport is WP-Ops-internal (it needs the WP Engine SSH gateway). This
# repo ships REST-only, so the dependency is optional: importing this module without it
# is fine, and only USING the transport raises a clear error.
try:
    from core.errors import APIError                       # type: ignore
    from skills.wpengine.ssh import SSHResult, WPESSHClient  # type: ignore
    SSH_AVAILABLE = True
except ImportError:                                        # pragma: no cover
    SSH_AVAILABLE = False

    class APIError(RuntimeError):
        pass

    class SSHResult:                                       # minimal stand-in
        def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
            self.stdout, self.stderr, self.exit_status = stdout, stderr, exit_status

    class WPESSHClient:                                    # fails loudly, never silently
        def __init__(self, *a, **kw):
            raise APIError(
                "the wp-cli/SSH transport is not available in this distribution - "
                "use WPOPS_TRANSPORT=rest (HTTPS + application password)")


@dataclass
class SSHProbe:
    """Result of an SSH + WP-CLI reachability check."""
    reachable: bool
    wp_cli: bool
    detail: str


class WPCliTransport:
    def __init__(self, install: str, key_path: str | None = None, ssh_client=None):
        # ssh_client is injectable for tests; defaults to the real WPESSHClient.
        self.install = install
        self._ssh = ssh_client or WPESSHClient(install, key_path=key_path)

    async def probe(self) -> SSHProbe:
        """Connect and check WP-CLI availability."""
        try:
            res = await self._ssh.exec("wp --version")
        except APIError as e:
            return SSHProbe(reachable=False, wp_cli=False, detail=str(e))
        except Exception as e:  # network/paramiko surprises
            return SSHProbe(reachable=False, wp_cli=False, detail=f"ssh error: {e}")
        return SSHProbe(reachable=True, wp_cli=res.success, detail="ok" if res.success
                        else (res.stderr.strip() or "wp-cli not available"))

    async def run(self, wp_command: str, timeout: int = 30) -> SSHResult:
        """Run a WP-CLI command (e.g. 'option get blogname') on the install."""
        return await self._ssh.wp_cli(wp_command, timeout=timeout)

    async def run_raw(self, command: str, timeout: int = 90) -> SSHResult:
        """Run a full shell command in ONE SSH session (no cd/wp prefix).

        Used to batch many WP-CLI calls into a single connection — WPE throttles
        rapid reconnects, so discovery must not open a connection per command.
        """
        return await self._ssh.exec(command, timeout=timeout)
