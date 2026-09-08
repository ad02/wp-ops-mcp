"""Prints the one-time per-site bootstrap for the REST transport. RUNS NOTHING.

Role is administrator so the API user has unfiltered_html (content with iframes/styles
must not be kses-stripped on REST writes). Run the printed commands over the admin SSH
fallback (with approval) or create the user + application password in wp-admin.
"""
import json
import sys


def build_bootstrap_commands(install: str, username: str = "wpops-mcp",
                             email: str = "wpops-mcp@example.com") -> list[str]:
    return [
        f"wp user create {username} {email} --role=administrator --porcelain",
        f"wp user application-password create {username} wp-ops-mcp --porcelain",
    ]


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: wpops_bootstrap_app_password.py <install> [base_url]")
        return
    install = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else f"https://{install}.example.com"
    print(f"# Run on {install} (WPE SSH gateway or wp-admin), then fill credentials.json:")
    for c in build_bootstrap_commands(install):
        print(f"  {c}")
    print("\n# data/wp_ops_mcp/credentials.json entry:")
    print(json.dumps({install: {"base_url": base_url, "username": "wpops-mcp",
                                "app_password": "<output of command 2>"}}, indent=2))


if __name__ == "__main__":
    main()
