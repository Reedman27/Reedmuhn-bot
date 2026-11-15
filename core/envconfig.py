"""Forgiving parsing for optional environment-variable tuning knobs.

A typo in ``.env`` must never stop the bot or the dashboard from starting.
Every helper here follows the same rules:

* unset or blank  -> the default, silently
* unparsable      -> the default, with a logged warning
* out of range    -> clamped to the nearest allowed value, with a logged warning

Defaults live in the code that uses the knob, so ``docker-compose.yml`` can
forward a blank value and get the built-in behaviour.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Mapping

logger = logging.getLogger("envconfig")


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read a whole-number setting, never raising on bad input."""
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("%s=%r is not a whole number - using the default (%s)", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        logger.warning("%s=%s is below the minimum of %s - using %s", name, value, minimum, minimum)
        return minimum
    if maximum is not None and value > maximum:
        logger.warning("%s=%s is above the maximum of %s - using %s", name, value, maximum, maximum)
        return maximum
    return value


def env_choice(
    name: str,
    default: str,
    choices: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read a setting that must be one of ``choices`` (case-insensitive).

    Returns the matching entry from ``choices`` exactly as spelled there.
    """
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    options = tuple(choices)
    lookup = {option.lower(): option for option in options}
    match = lookup.get(str(raw).strip().lower())
    if match is None:
        logger.warning("%s=%r is not one of %s - using the default (%s)", name, raw, ", ".join(options), default)
        return default
    return match
