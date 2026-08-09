"""Generates a welcome card image (like the screenshot-style cards common
on other bots): dark background, member avatar in a circle, "WELCOME
<name>" text, and a member-count line. Pure image generation - no Discord
API calls in here - so it's testable on its own with a fake avatar.
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS_DIR = Path(__file__).parent / "assets"
CARD_SIZE = (900, 300)
AVATAR_SIZE = 180
BG_COLOR = (24, 24, 37)
ACCENT_COLOR = (138, 173, 244)
TEXT_COLOR = (202, 211, 245)
MUTED_COLOR = (150, 158, 190)

_font_bold_cache = {}
_font_regular_cache = {}


def _font_bold(size: int) -> ImageFont.FreeTypeFont:
    if size not in _font_bold_cache:
        _font_bold_cache[size] = ImageFont.truetype(str(ASSETS_DIR / "DejaVuSans-Bold.ttf"), size)
    return _font_bold_cache[size]


def _font_regular(size: int) -> ImageFont.FreeTypeFont:
    if size not in _font_regular_cache:
        _font_regular_cache[size] = ImageFont.truetype(str(ASSETS_DIR / "DejaVuSans.ttf"), size)
    return _font_regular_cache[size]


def _make_circular_avatar(avatar_bytes: bytes, size: int) -> Image.Image:
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    avatar = ImageOps.fit(avatar, (size, size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)

    circular = Image.new("RGBA", (size, size))
    circular.paste(avatar, (0, 0), mask)
    return circular


def render_welcome_card(
    avatar_bytes: bytes,
    member_name: str,
    server_name: str,
    member_count: int,
) -> bytes:
    """Returns PNG bytes for a welcome card. avatar_bytes should be the raw
    bytes of the member's avatar image (any format Pillow can open - PNG,
    JPEG, WEBP all work since Discord serves avatars in those formats)."""
    card = Image.new("RGB", CARD_SIZE, BG_COLOR)
    draw = ImageDraw.Draw(card)

    # subtle accent bar down the left edge
    draw.rectangle([(0, 0), (6, CARD_SIZE[1])], fill=ACCENT_COLOR)

    # avatar, circular, right-aligned with a ring around it
    avatar_x = CARD_SIZE[0] - AVATAR_SIZE - 60
    avatar_y = (CARD_SIZE[1] - AVATAR_SIZE) // 2
    ring_pad = 6
    draw.ellipse(
        [
            (avatar_x - ring_pad, avatar_y - ring_pad),
            (avatar_x + AVATAR_SIZE + ring_pad, avatar_y + AVATAR_SIZE + ring_pad),
        ],
        fill=ACCENT_COLOR,
    )
    circular_avatar = _make_circular_avatar(avatar_bytes, AVATAR_SIZE)
    card.paste(circular_avatar, (avatar_x, avatar_y), circular_avatar)

    # text block, left-aligned
    text_x = 60
    draw.text((text_x, 90), "WELCOME", font=_font_bold(40), fill=TEXT_COLOR)

    # member name - shrink the font if it's long enough to risk overlapping the avatar
    name_font_size = 36
    max_name_width = avatar_x - text_x - 40
    name_font = _font_bold(name_font_size)
    while draw.textlength(member_name, font=name_font) > max_name_width and name_font_size > 18:
        name_font_size -= 2
        name_font = _font_bold(name_font_size)
    draw.text((text_x, 145), member_name, font=name_font, fill=ACCENT_COLOR)

    draw.text((text_x, 200), f"to {server_name}", font=_font_regular(22), fill=MUTED_COLOR)
    draw.text((text_x, 235), f"You make us #{member_count}", font=_font_regular(18), fill=MUTED_COLOR)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()
