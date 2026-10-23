"""Automod detection logic, ported from YAGPDB's automod/triggers.go. The
algorithms here are deliberately kept as plain functions with no discord.py
objects involved, mirroring the approach in utils.py's tempnick_self_allowed
- easy to unit test the actual decision logic directly, separate from
Discord API calls or event wiring.
"""
import re
import unicodedata
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


# Domains that serve GIFs as their whole reason for existing - a link to
# one of these is almost always someone sharing a GIF even when Discord's
# own link preview doesn't embed it as an image. Deliberately just the
# well-known GIF sites, not every site that can host a stray .gif.
GIF_DOMAINS = ("tenor.com", "giphy.com", "gph.is", "media.giphy.com", "media.tenor.com")

_GIF_URL_REGEX = re.compile(r"(?i)\bhttps?://\S+")


def is_gif_url(url: str) -> bool:
    """A URL that's either a direct .gif file or a link to a known
    GIF-hosting site (Tenor, Giphy) - covers both "someone attached a .gif"
    and "someone pasted a Tenor link", which Discord's own GIF picker
    produces as the latter."""
    lowered = url.lower().split("?", 1)[0]
    if lowered.endswith(".gif"):
        return True
    return any(domain in lowered for domain in GIF_DOMAINS)




GIF_LINK_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def gif_identifiers(content: str = "", attachment_filenames: list[str] = (), attachment_content_types: list[str] = ()) -> list[str]:
    """Return normalized identifiers for GIF content in a message.

    Identifiers are exact URLs for pasted GIF links and exact filenames for
    uploaded GIFs.  The caller can use these to implement precise allow/block
    lists without weakening the blanket GIF detector.
    """
    found = []
    for url in GIF_LINK_RE.findall(content or ""):
        if is_gif_url(url):
            found.append(url.rstrip("),.!?\"'"))
    for filename, content_type in zip(attachment_filenames, attachment_content_types or [None] * len(attachment_filenames)):
        if filename and (filename.lower().endswith(".gif") or (content_type or "").lower() == "image/gif"):
            found.append(filename)
    return found


def contains_gif(content: str, attachment_filenames: list[str] = (), attachment_content_types: list[str] = ()) -> bool:
    """True if the message is carrying a GIF by any of the three ways
    Discord actually delivers one: an uploaded .gif attachment (checked by
    both filename and the more reliable content-type, since a renamed file
    extension wouldn't fool the content-type), a pasted link to a
    GIF-hosting site, or a bare .gif URL. `content` is the raw message
    text; attachment_filenames/attachment_content_types come from
    message.attachments (name and content_type respectively).
    """
    for content_type in attachment_content_types:
        if content_type and content_type.lower() == "image/gif":
            return True
    for filename in attachment_filenames:
        if filename and filename.lower().endswith(".gif"):
            return True
    for url in _GIF_URL_REGEX.findall(content):
        if is_gif_url(url):
            return True
    return False


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


# Common leetspeak/lookalike substitutions used to evade a plain banned-word
# filter. Deliberately small and conservative (no OCR-style homoglyphs like
# Cyrillic "а") - just the substitutions people actually type on a regular
# keyboard.
_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
    "7": "t", "8": "b", "$": "s", "@": "a", "+": "t",
    "|": "l", "!": "i",
})


def _normalize_for_fuzzy_match(text: str) -> str:
    """Collapses common banned-word evasion tricks down to a plain
    lowercase run of letters/digits, so a banned word still matches when
    it's spaced out (`b a d`), leetspeak'd (`b4d`), punctuated (`b.a.d!`),
    or stretched out (`baaaad`). Deliberately aggressive - this is only
    ever used behind the opt-in fuzzy "alike words" flag, since collapsing
    spacing/punctuation trades some false positives for catching obvious
    evasion.
    """
    normalized = unicodedata.normalize("NFKD", text.lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.translate(_LEET_MAP)
    # Drop everything that isn't a letter or digit - this is what lets
    # "b a d" and "b.a.d!" collapse onto the same string as "bad".
    normalized = re.sub(r"[^a-z0-9]", "", normalized)
    # Collapse runs of 3+ identical characters down to a single one, so
    # "baaaad" still matches "bad". Only 3+ *in a row* is touched - a
    # genuine double letter like "book" or "class" only ever has 2 in a
    # row and passes through unchanged.
    normalized = re.sub(r"(.)\1{2,}", r"\1", normalized)
    return normalized


def contains_banned_word(text: str, banned_words: list[str], fuzzy: bool = False) -> tuple[str, bool] | None:
    """Whole-word, case-insensitive match against a configured list.
    Returns (matched_word, was_fuzzy_only) or None. Whole-word matching
    (via \\b) avoids "class" tripping a filter on "ass".

    was_fuzzy_only tells the caller whether the match was found by the
    exact check (False) or only by the normalized "alike words" form (True)
    - see _normalize_for_fuzzy_match. The fuzzy path is a plain substring
    match on normalized text (word boundaries don't survive normalization),
    so it's more prone to false positives than the exact check, which is
    both why it's opt-in per-server and why callers may want to treat a
    fuzzy-only hit with more caution (e.g. queue it for review) than an
    exact one.

    When fuzzy=True, also checks a normalized "alike words" form of the
    message, so spaced-out, leetspeak'd, or stretched-out evasions of a
    banned word still match.
    """
    lowered = text.lower()
    fuzzy_text = _normalize_for_fuzzy_match(text) if fuzzy else None
    for raw_word in banned_words:
        word = raw_word.strip().lower()
        if not word:
            continue
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return (word, False)
        if fuzzy_text:
            fuzzy_word = _normalize_for_fuzzy_match(word)
            if fuzzy_word and fuzzy_word in fuzzy_text:
                return (word, True)
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
