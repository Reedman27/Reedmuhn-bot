"""Small Python-native extension registry.

This is intentionally lightweight: discord.py remains the runtime framework,
while this registry provides a stable place for feature metadata and future
plugin discovery. It translates the useful Store/modular-extension idea from
Sapphire into normal Python instead of shipping TypeScript runtime code.
"""
from dataclasses import dataclass
from typing import Iterable


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

    def all(self) -> tuple[Feature, ...]:
        return tuple(self._features.values())
