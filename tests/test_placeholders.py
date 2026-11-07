"""The music embed template conditions must never execute code.

Variable values (track title/author) come from outside sources, so a title
like ``' or __import__('os').system('x') or '`` must stay plain data.
"""
import pytest

from voicelink.placeholders import safe_condition, PlayerPlaceholder


def test_basic_conditions_still_work():
    v = {"volume": "80", "loop": "Off", "title": "Song"}
    assert safe_condition("@@volume@@ > 50", v) is True
    assert safe_condition("@@volume@@ >= 100", v) is False
    assert safe_condition("@@loop@@ == 'Off'", v) is True
    assert safe_condition("@@loop@@ != 'Off' and @@volume@@ > 1", v) is False
    assert safe_condition("not @@title@@ == 'x'", v) is True
    assert safe_condition("'ng' in @@title@@", v) is True
    assert safe_condition("loop == 'Off'", v) is True  # bare names, as before


@pytest.mark.parametrize("payload", [
    "' or __import__('os').system('echo pwned') or '",
    "'; import os; '",
    "x'] or [c for c in ().__class__.__base__.__subclasses__()] or ['",
])
def test_hostile_variable_values_are_just_strings(payload):
    called = []
    assert safe_condition("@@title@@ == 'Song'", {"title": payload}) is False
    assert not called


@pytest.mark.parametrize("expr", [
    "__import__('os').system('x')",
    "().__class__.__base__.__subclasses__()",
    "(lambda: 1)()",
    "[c for c in ()]",
    "open('/etc/passwd')",
    "1 if 1 else 2",
    "'a' + 'b'",
    "x" * 300,
])
def test_code_in_the_template_itself_is_rejected(expr):
    with pytest.raises(Exception):
        safe_condition(expr, {"x": "1"})


class _Stub(PlayerPlaceholder):
    def __init__(self):
        self.regexes = {}


def test_replace_handles_missing_branches_and_bad_conditions():
    r = _Stub()
    v = {"volume": "80"}
    assert r.replace("{{ @@volume@@ > 50 ?? loud // quiet }}", v) == "loud"
    assert r.replace("{{ @@volume@@ > 90 ?? loud // quiet }}", v) == "quiet"
    assert r.replace("{{ @@volume@@ > 90 ?? loud }}", v) == ""
    assert r.replace("{{ no question mark }}", v) == ""          # used to raise IndexError
    assert r.replace("{{ __import__('os') ?? a // b }}", v) == ""
