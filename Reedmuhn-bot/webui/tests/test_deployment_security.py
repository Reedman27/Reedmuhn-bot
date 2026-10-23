"""Source-level guardrails for deployment secrets and CI supply-chain pins."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_compose_requires_exposed_service_secrets():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # The bot token and dashboard password must be set. Lavalink is internal-only
    # (expose, not ports), so it keeps a working default instead of blocking boot.
    for secret in ("DISCORD_TOKEN", "WEBUI_PASSWORD"):
        assert f"${{{secret}:?{secret}_required_in_.env}}" in compose
    assert "LAVALINK_PASSWORD:-youshallnotpass" in compose


def test_lavalink_is_not_published_to_the_host():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose[compose.index("mybot-lavalink"):compose.index("  webui:")]
    assert "expose:" in block
    assert "ports:" not in block


def test_github_actions_are_sha_pinned_with_minimal_contents_permission():
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    pin = re.compile(r"uses:\s+[^\s]+@([0-9a-f]{40})(?:\s|$)")
    for workflow_path in workflows:
        source = workflow_path.read_text(encoding="utf-8")
        uses_lines = [line for line in source.splitlines() if re.match(r"\s*uses:\s", line)]
        assert uses_lines, f"No actions found in {workflow_path.name}"
        for line in uses_lines:
            assert pin.search(line), f"Action is not pinned to a full commit SHA: {workflow_path.name}: {line}"
        assert re.search(r"(?m)^permissions:\s*\n\s+contents:\s+read\s*$", source), (
            f"Missing least-privilege workflow permissions: {workflow_path.name}"
        )
