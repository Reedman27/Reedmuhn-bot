"""Cog discovery, boot reporting and the feature registry.

Cogs live in feature folders under ``cogs/``::

    cogs/
      moderation/     warnings, automod, anti-nuke, logging...
      community/      welcome, birthdays, counting, reaction roles...
      utility/        custom commands, reminders, tickets, /ping...
      fun/            fun commands, polls, XP/economy/giveaways
      music/          music player
      notifications/  YouTube alerts
      dashboard/      cogs that exist only to serve the web dashboard

Dropping a ``.py`` file with an ``async def setup(bot)`` into any of those
folders is all it takes to add a cog - there is no list to edit. Adding a new
folder works the same way (it just needs an ``__init__.py``).

To switch a cog off without deleting it, name it in the ``DISABLED_COGS``
environment variable (comma separated, e.g. ``DISABLED_COGS=music,fun``) -
match either the file name or the dotted path.

``FeatureStore`` is the human-readable registry (description + category) that
sits beside discovery. A cog missing from it still loads; the boot report just
flags it so the registry doesn't silently drift.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

COGS_PACKAGE = "cogs"

# Cogs that stay off (not "failed") until an environment variable is set.
# Music needs a Lavalink server, which is optional.
REQUIRES_ENV = {"cogs.music.music": "LAVALINK_PASSWORD"}


def missing_requirement(extension: str, environ) -> str:
    """Name of the env var this cog needs but doesn't have, else ''."""
    name = REQUIRES_ENV.get(extension, "")
    return name if name and not str(environ.get(name, "")).strip() else ""


@dataclass(frozen=True)
class Feature:
    name: str
    extension: str
    description: str
    category: str


class FeatureStore:
    def __init__(self, features: Iterable[Feature] = ()):
        self._features: dict[str, Feature] = {}
        for feature in features:
            self.register(feature)

    def register(self, feature: Feature) -> None:
        self._features[feature.name] = feature

    def get(self, name: str) -> Feature | None:
        return self._features.get(name)

    def by_extension(self, extension: str) -> Feature | None:
        return next((f for f in self._features.values() if f.extension == extension), None)

    def all(self) -> tuple[Feature, ...]:
        return tuple(self._features.values())


def _defines_setup(path: Path) -> bool:
    """True if the file has a top-level ``setup`` function (without importing it)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        # A file with a syntax error should still be *attempted* so the boot
        # report shows the real error instead of hiding the file.
        return True
    return any(
        isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "setup"
        for node in tree.body
    )


def discover_extensions(root: Path | str = COGS_PACKAGE) -> list[str]:
    """Dotted module paths of every cog under ``root``, folder by folder.

    Folders and files are sorted so boot order is stable. Files starting with
    ``_`` and files with no ``setup`` function (helpers) are skipped.
    """
    root = Path(root)
    found: list[str] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        for file in sorted(folder.glob("*.py")):
            if file.name.startswith("_") or not _defines_setup(file):
                continue
            found.append(f"{COGS_PACKAGE}.{folder.name}.{file.stem}")
    return found


def parse_disabled(raw: str | None) -> set[str]:
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


def is_disabled(extension: str, disabled: set[str]) -> bool:
    parts = extension.split(".")
    return bool(disabled & {extension.lower(), parts[-1].lower(), ".".join(parts[1:]).lower()})


@dataclass
class CogResult:
    extension: str
    ok: bool = False
    skipped: bool = False
    reason: str = ""  # why it was skipped, when it wasn't DISABLED_COGS
    error: str = ""
    seconds: float = 0.0

    @property
    def folder(self) -> str:
        parts = self.extension.split(".")
        return parts[1] if len(parts) > 2 else "(root)"

    @property
    def file(self) -> str:
        return self.extension.split(".")[-1] + ".py"


@dataclass
class BootReport:
    results: list[CogResult] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    @property
    def loaded(self) -> list[CogResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[CogResult]:
        return [r for r in self.results if not r.ok and not r.skipped]

    @property
    def skipped(self) -> list[CogResult]:
        return [r for r in self.results if r.skipped]

    def by_folder(self) -> dict[str, list[CogResult]]:
        grouped: dict[str, list[CogResult]] = {}
        for result in self.results:
            grouped.setdefault(result.folder, []).append(result)
        return grouped

    def summary_line(self) -> str:
        line = f"{len(self.loaded)} loaded"
        if self.failed:
            line += f", {len(self.failed)} FAILED"
        if self.skipped:
            line += f", {len(self.skipped)} disabled"
        return line
