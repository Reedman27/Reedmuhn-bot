"""Source-level guardrails for deployment secrets and CI supply-chain pins."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_compose_requires_exposed_service_secrets():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # No well-known default passwords: every secret must be set explicitly.
    for secret in ("DISCORD_TOKEN", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"):
        assert f"${{{secret}:?{secret}_required_in_.env}}" in compose
    assert "youshallnotpass" not in compose


def test_lavalink_is_optional():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "LAVALINK_PASSWORD:?" not in compose  # the bot must start without music
    bot_block = compose[compose.index("discord-bot:"):compose.index("  redis:")]
    assert "lavalink" not in bot_block.split("depends_on:")[-1]
    lavalink_block = compose[compose.index("  lavalink:"):compose.index("  webui:")]
    assert 'profiles: ["music"]' in lavalink_block
    entrypoint = (ROOT / "lavalink" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "-z \"$LAVALINK_SERVER_PASSWORD\"" in entrypoint and "exit 1" in entrypoint


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
