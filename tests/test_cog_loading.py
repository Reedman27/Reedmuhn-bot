"""Cog discovery, the DISABLED_COGS switch, and registry/boot-report consistency."""
from pathlib import Path

import framework

ROOT = Path(__file__).resolve().parents[1]


def test_every_cog_file_is_discovered(monkeypatch):
    monkeypatch.chdir(ROOT)
    found = framework.discover_extensions()
    on_disk = {
        f"cogs.{p.parent.name}.{p.stem}"
        for p in (ROOT / "cogs").glob("*/*.py")
        if p.name != "__init__.py" and "async def setup(" in p.read_text()
    }
    assert set(found) == on_disk and len(found) >= 30


def test_every_discovered_cog_is_in_the_feature_registry(monkeypatch):
    monkeypatch.chdir(ROOT)
    import bot
    registered = {f.extension for f in bot.FEATURES.all()}
    assert set(framework.discover_extensions()) == registered


def test_disabled_cogs_matches_file_or_dotted_path():
    disabled = framework.parse_disabled(" music, FUN.polls ,")
    assert framework.is_disabled("cogs.music.music", disabled)
    assert framework.is_disabled("cogs.fun.polls", disabled)
    assert not framework.is_disabled("cogs.fun.fun", disabled)


def test_helper_files_without_setup_are_skipped(tmp_path, monkeypatch):
    (tmp_path / "grp").mkdir()
    (tmp_path / "grp" / "real.py").write_text("async def setup(bot):\n    pass\n")
    (tmp_path / "grp" / "helper.py").write_text("X = 1\n")
    (tmp_path / "grp" / "_private.py").write_text("async def setup(bot):\n    pass\n")
    monkeypatch.chdir(tmp_path)
    assert framework.discover_extensions(tmp_path) == ["cogs.grp.real"]


def test_a_broken_cog_does_not_stop_the_rest(tmp_path, monkeypatch):
    import asyncio, os
    os.environ["DB_PATH"] = str(tmp_path / "bot.db")
    monkeypatch.setenv("LAVALINK_PASSWORD", "test-only-password")
    monkeypatch.chdir(ROOT)
    import bot

    async def run():
        b = bot.MyBot()
        async with b:
            real_load = b.load_extension

            async def flaky(name, **kw):
                if name.endswith("cogs.utility.ping"):
                    raise RuntimeError("boom")
                return await real_load(name, **kw)

            b.load_extension = flaky
            report = await b._load_all_cogs()
            b.boot_report = report
            return report, b.build_boot_embed()

    report, embed = asyncio.run(run())
    assert [r.file for r in report.failed] == ["ping.py"] and len(report.loaded) >= 30
    assert embed.title.endswith("with errors") and "boom" in "".join(f.value for f in embed.fields)


def test_music_is_off_not_failed_without_a_lavalink_password_at_boot(tmp_path, monkeypatch):
    import asyncio, os
    os.environ["DB_PATH"] = str(tmp_path / "bot.db")
    monkeypatch.delenv("LAVALINK_PASSWORD", raising=False)
    monkeypatch.chdir(ROOT)
    import bot

    async def run():
        b = bot.MyBot()
        async with b:
            return await b._load_all_cogs()

    report = asyncio.run(run())
    music = [r for r in report.results if r.file == "music.py"][0]
    assert music.skipped and not music.ok and "LAVALINK_PASSWORD" in music.reason
    assert len(report.loaded) >= 30  # the rest of the bot is unaffected


def test_music_is_off_not_failed_without_a_lavalink_password():
    assert framework.missing_requirement("cogs.music.music", {}) == "LAVALINK_PASSWORD"
    assert framework.missing_requirement("cogs.music.music", {"LAVALINK_PASSWORD": "  "}) == "LAVALINK_PASSWORD"
    assert framework.missing_requirement("cogs.music.music", {"LAVALINK_PASSWORD": "x"}) == ""
    assert framework.missing_requirement("cogs.fun.fun", {}) == ""
