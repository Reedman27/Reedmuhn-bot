"""Automod detection logic, ported from YAGPDB's automod/triggers.go. The
algorithms here are deliberately kept as plain functions with no discord.py
objects involved, mirroring the approach in utils.py's tempnick_self_allowed
- easy to unit test the actual decision logic directly, separate from
Discord API calls or event wiring.
"""
import re
import time
from collections import deque
from dataclasses import dataclass, field

# Matches discord.gg/xxx, discordapp.com/invite/xxx, discord.com/invite/xxx -
# same pattern YAGPDB uses (common/invites.go's DiscordInviteSource.Regex).
INVITE_REGEX = re.compile(
    r"(?i)(?:discord\.gg|discordapp\.com/+invite|discord\.com/+invite)/+([a-zA-Z0-9-]+)"
)


def find_invite_codes(text: str) -> list[str]:
    """Every discord.gg/xxx style invite code found in the text."""
    return INVITE_REGEX.findall(text)


def caps_violation(text: str, min_len: int, percent_threshold: int) -> bool:
    """Port of YAGPDB's AllCapsTrigger. Counts case-changeable characters
    (letters that have a distinct upper/lower form - so punctuation and
    numbers don't count toward the percentage either way), triggers only if
    BOTH the raw count of capitals meets min_len AND the percentage of
    capitals among case-changeable characters meets the threshold. The
    dual condition is what stops a 3-character "OK!" from tripping a
    100%-caps rule meant for actual shouting.
    """
    total_capitalizable = 0
    num_caps = 0
    for ch in text:
        if ch.isupper():
            num_caps += 1
            total_capitalizable += 1
        elif ch.lower() != ch.upper():
            total_capitalizable += 1

    if total_capitalizable < 1 or num_caps < min_len:
        return False
    percentage = (num_caps * 100) // total_capitalizable
    return percentage >= percent_threshold


def contains_banned_word(text: str, banned_words: list[str]) -> str | None:
    """Whole-word, case-insensitive match against a configured list.
    Returns the matched word, or None. Whole-word matching (via \\b) avoids
    "class" tripping a filter on "ass"."""
    lowered = text.lower()
    for raw_word in banned_words:
        word = raw_word.strip().lower()
        if not word:
            continue
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return None


def sliding_window_count(timestamps: list, now: float, window_seconds: int) -> int:
    """How many of the given timestamps fall within window_seconds of now.
    This is the core of YAGPDB's SlowmodeTrigger - a plain sliding-window
    rate check, used here for "X messages in Y seconds" spam detection."""
    cutoff = now - window_seconds
    return sum(1 for t in timestamps if t >= cutoff)


def count_consecutive_duplicates(history: list, now: float, window_seconds: int, current_content: str) -> int:
    """Port of YAGPDB's SpamTrigger. `history` is a list of (timestamp,
    normalized_content) for a user's past messages, oldest first. Walks
    backward from the most recent message and counts how many are
    consecutively identical to current_content within the time window -
    stopping at the first non-match, not counting matches scattered
    throughout history. That's deliberate: it catches someone spamming the
    same line repeatedly, not someone who happens to repeat a phrase twice
    with other messages in between.
    """
    target = current_content.strip().lower()
    count = 1  # the current message itself counts toward the threshold
    cutoff = now - window_seconds if window_seconds > 0 else None

    for ts, content in reversed(history):
        if cutoff is not None and ts < cutoff:
            break
        if content == target:
            count += 1
        else:
            break

    return count


@dataclass
class UserMessageTracker:
    """Per (guild, channel, user) rolling history, kept in memory only -
    same approach YAGPDB takes (reading its live message cache rather than
    hitting the database for this high-frequency check). Not persisted:
    losing this on a bot restart just means spam detection has a cold start,
    which is an acceptable tradeoff for something this ephemeral.
    """
    timestamps: deque = field(default_factory=lambda: deque(maxlen=200))
    contents: deque = field(default_factory=lambda: deque(maxlen=200))  # (timestamp, normalized_content) pairs

    def record(self, now: float, content: str) -> None:
        self.timestamps.append(now)
        self.contents.append((now, content.strip().lower()))
