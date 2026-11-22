"""webui/help_commands.py is generated from cogs/; it must not drift."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location("gen_help_commands", ROOT / "scripts" / "gen_help_commands.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_help_commands_are_up_to_date():
    gen = _generator()
    current = (ROOT / "webui" / "help_commands.py").read_text(encoding="utf-8")
    assert current == gen.render(ROOT), "run: python scripts/gen_help_commands.py"


def test_the_generator_finds_the_commands():
    found = _generator().collect(ROOT)
    flat = {c[0] for lines in found.values() for c in lines}
    assert {"moderation warn", "automod toggle", "music play", "ping"} <= flat
