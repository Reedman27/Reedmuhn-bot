"""Source-level regression checks for the music slash-command authorization fix.

These tests intentionally use the Python AST so the security policy can be
checked in the WebUI-only CI environment without importing discord.py or
connecting to Discord/Lavalink.
"""
from __future__ import annotations

import ast
from pathlib import Path


MUSIC_SOURCE = Path(__file__).resolve().parents[2] / "cogs" / "music.py"
MUTATING_PLAYER_CONTROLS = {
    "pause",
    "resume",
    "skip",
    "back",
    "seek",
    "forward",
    "rewind",
    "replay",
    "loop",
    "shuffle",
    "remove",
    "clear",
    "volume",
    "leave",
    "autoplay",
    "filter",
    "effect_clear",
}


def _music_methods() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(MUSIC_SOURCE.read_text(encoding="utf-8"))
    music_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Music"
    )
    return {
        node.name: node
        for node in music_class.body
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _authorization_calls(node: ast.AST) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "self"
        and child.func.attr == "_authorize_player_control"
    ]


def test_every_mutating_player_control_uses_shared_authorization():
    methods = _music_methods()
    missing = sorted(name for name in MUTATING_PLAYER_CONTROLS if not _authorization_calls(methods[name]))
    assert not missing, f"Music slash commands missing DJ/vote authorization: {missing}"


def test_authorization_helper_is_an_instance_method():
    helper = _music_methods()["_authorize_player_control"]
    assert not any(
        isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
        for decorator in helper.decorator_list
    ), "helper uses self and must not be decorated staticmethod"


def test_requester_skip_exception_cannot_skip_to_arbitrary_queue_position():
    skip_calls = _authorization_calls(_music_methods()["skip"])
    assert len(skip_calls) == 1
    keywords = {item.arg: item.value for item in skip_calls[0].keywords}
    for key in ("allow_vote", "allow_requester"):
        value = keywords[key]
        assert isinstance(value, ast.Compare), f"{key} must depend on the queue index"
        assert isinstance(value.left, ast.Name) and value.left.id == "index"
        assert isinstance(value.ops[0], ast.LtE)
        assert isinstance(value.comparators[0], ast.Constant) and value.comparators[0].value == 0


def test_shuffle_preserves_the_controller_vote_path():
    shuffle_calls = _authorization_calls(_music_methods()["shuffle"])
    assert len(shuffle_calls) == 1
    keywords = {item.arg: item.value for item in shuffle_calls[0].keywords}
    assert isinstance(keywords.get("allow_vote"), ast.Constant)
    assert keywords["allow_vote"].value is True
