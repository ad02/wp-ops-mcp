from scripts.wpops_bootstrap_app_password import build_bootstrap_commands


def test_bootstrap_commands_shape():
    cmds = build_bootstrap_commands("examplestg")
    assert cmds[0].startswith("wp user create wpops-mcp ")
    assert "--role=administrator" in cmds[0] and "--porcelain" in cmds[0]
    assert cmds[1] == "wp user application-password create wpops-mcp wp-ops-mcp --porcelain"
    assert all("\n" not in c and '"' not in c and "'" not in c for c in cmds)
