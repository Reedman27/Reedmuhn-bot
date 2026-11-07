"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import ast
import operator
import re
import discord

from discord.ext import commands
from typing import TYPE_CHECKING, List, Callable

from .config import Config
from .utils import format_ms

if TYPE_CHECKING:
    from .player import Player
    from .objects import Track
    from .pool import NodePool
    
def ensure_track(func) -> Callable:
    def wrapper(self: PlayerPlaceholder, *args, **kwargs):
        current = self.get_current()
        if not current:
            return "None"
        return func(self, current, *args, **kwargs)
    return wrapper


_MAX_CONDITION_LENGTH = 200
_COMPARE_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


def safe_condition(expression: str, variables: dict) -> bool:
    """Evaluate a ``{{ condition ?? a // b }}`` condition without ``eval``.

    Only literals, ``@@variable@@`` values, comparisons, and/or/not are
    allowed. Variable values (track titles, authors, ...) come from outside
    sources, so they are NEVER spliced into the expression text - they are
    passed in separately as plain values. Anything else raises ValueError.
    """
    if len(expression) > _MAX_CONDITION_LENGTH:
        raise ValueError("condition too long")
    values: dict[str, object] = {}

    def as_value(raw) -> object:
        if callable(raw):
            raw = raw()
        text = "" if raw is None else str(raw)
        return int(text) if text.isdigit() else text

    def to_name(match: re.Match) -> str:
        name = f"__v{len(values)}"
        values[name] = as_value(variables.get(match.group(1), ""))
        return name

    tree = ast.parse(re.sub(r"@@(.*?)@@", to_name, expression.strip()), mode="eval")

    def ev(node: ast.AST):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in values:
                return values[node.id]
            if node.id in variables:  # bare variable name, as the old syntax allowed
                return as_value(variables[node.id])
            raise ValueError(f"unknown name {node.id!r}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not ev(node.operand)
        if isinstance(node, ast.BoolOp):
            results = (ev(v) for v in node.values)
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                compare = _COMPARE_OPS.get(type(op))
                if compare is None:
                    raise ValueError("operator not allowed")
                right = ev(comparator)
                if not compare(left, right):
                    return False
                left = right
            return True
        raise ValueError(f"{type(node).__name__} not allowed")

    return bool(ev(tree))


class PlayerPlaceholder:
    def __init__(self, bot: commands.Bot, player: Player = None) -> None:
        self.bot: commands.Bot = bot
        self.player: Player = player

        self.variables = {
            "channel_name": self.channel_name,
            "track_name": self.track_name,
            "track_url": self.track_url,
            "track_author": self.track_author,
            "track_duration": self.track_duration,
            "track_thumbnail": self.track_thumbnail,
            "track_color": self.track_color,
            "track_requester_id": self.track_requester_id,
            "track_requester_name": self.track_requester_name,
            "track_requester_mention": self.track_requester_mention,
            "track_requester_avatar": self.track_requester_avatar,
            "track_source_name": self.track_source_name,
            "track_source_emoji": self.track_source_emoji,
            "queue_length": self.queue_length,
            "volume": self.volume,
            "dj": self.dj,
            "loop_mode": self.loop_mode,
            "default_embed_color": self.default_embed_color,
            "bot_icon": self.bot_icon,
            "server_invite_link": Config().invite_link,
            "invite_link": f"https://discord.com/oauth2/authorize?client_id={self.bot.user.id}&permissions=2184260928&scope=bot%20applications.commands"
        }

        self.regexes = {
            "@@t_(.*?)@@": self.translation
        }
        
    def get_current(self) -> Track:
        return self.player.current if self.player else None

    def channel_name(self) -> str:
        if not self.player:
            return "None"
        return self.player.channel.name if self.player.channel else "None"
    
    @ensure_track
    def track_name(self, track: Track) -> str:
        return track.title

    @ensure_track
    def track_url(self, track: Track) -> str:
        return track.uri
    
    @ensure_track
    def track_author(self, track: Track) -> str:
        return track.author

    @ensure_track
    def track_duration(self, track: Track) -> str:
        return self.player.get_msg("common.status.live") if track.is_stream else format_ms(track.length)
    
    @ensure_track
    def track_requester_id(self, track: Track) -> str:
        return str(track.requester.id if track.requester else self.bot.user.id)
    
    @ensure_track
    def track_requester_name(self, track: Track) -> str:
        return track.requester.name if track.requester else self.bot.user.display_name
    
    @ensure_track
    def track_requester_mention(self, track: Track) -> str:
        return f"<@{track.requester.id if track.requester else self.bot.user.id}>"
    
    @ensure_track
    def track_requester_avatar(self, track: Track) -> str:
        return track.requester.display_avatar.url if track.requester else self.bot.user.display_avatar.url
    
    @ensure_track
    def track_color(self, track: Track) -> int:
        return int(Config().get_source_config(track.source, "color"), 16)
    
    @ensure_track
    def track_source_name(self, track: Track) -> str:
        return track.source
    
    @ensure_track
    def track_source_emoji(self, track: Track) -> str:
        return track.emoji
    
    def track_thumbnail(self) -> str:
        if not self.player or not self.player.current:
            return "https://i.imgur.com/dIFBwU7.png"
        
        return self.player.current.thumbnail or "https://cdn.discordapp.com/attachments/674788144931012638/823086668445384704/eq-dribbble.gif"

    def queue_length(self) -> str:
        return str(self.player.queue.count) if self.player else "0"
    
    def dj(self) -> str:
        if not self.player:
            return self.bot.user.mention
        
        if dj_id := self.player.settings.get("dj"):
            return f"<@&{dj_id}>"
        
        return self.player.dj.mention

    def volume(self) -> int:
        return self.player.volume if self.player else 0

    def loop_mode(self) -> str:
        return self.player.queue.repeat if self.player else "Off"

    def default_embed_color(self) -> int:
        return Config().embed_color

    def bot_icon(self) -> str:
        return self.bot.user.display_avatar.url if self.player else "https://i.imgur.com/dIFBwU7.png"
    
    def translation(self, text: str) -> str:
        return self.player.get_msg(text)
        
    def replace(self, text: str, variables: dict[str, str]) -> str:
        if not text or text.isspace(): return

        matches: list[str] = re.findall(r"\{\{(.*?)\}\}", text)

        for match in matches:
            # {{ condition ?? if true // if false }}
            condition, _, branches = match.partition("??")
            true_value, _, false_value = branches.partition("//")
            true_value, false_value = true_value.strip(), false_value.strip()

            try:
                result = safe_condition(condition, variables)
                replacement = true_value if result else false_value
            except Exception:
                replacement = ""
            text = text.replace("{{" + match + "}}", replacement)

        for regex_pattern, func in self.regexes.items():
            text = re.sub(regex_pattern, lambda m: func(m.group(1)), text)
        text = re.sub(r'@@(.*?)@@', lambda x: str(variables.get(x.group(1), '')), text)
        return text
    
    @classmethod
    def build_embed(cls, embed_form: dict[str, dict], placeholder: PlayerPlaceholder) -> discord.Embed:
        embed = discord.Embed()
        try:
            rv = {key: func() if callable(func) else func for key, func in placeholder.variables.items()}
            if author := embed_form.get("author"):
                embed.set_author(
                    name = placeholder.replace(author.get("name"), rv),
                    url = placeholder.replace(author.get("url"), rv),
                    icon_url = placeholder.replace(author.get("icon_url"), rv)
                )
            
            if title := embed_form.get("title"):
                embed.title = placeholder.replace(title.get("name"), rv)
                embed.url = placeholder.replace(title.get("url"), rv)

            if fields := embed_form.get("fields", []):
                for f in fields:
                    embed.add_field(name=placeholder.replace(f.get("name"), rv), value=placeholder.replace(f.get("value", ""), rv), inline=f.get("inline", False))

            if footer := embed_form.get("footer"):
                embed.set_footer(
                    text = placeholder.replace(footer.get("text"), rv),
                    icon_url = placeholder.replace(footer.get("icon_url"), rv)
                ) 

            if thumbnail := embed_form.get("thumbnail"):
                embed.set_thumbnail(url = placeholder.replace(thumbnail, rv))
            
            if image := embed_form.get("image"):
                embed.set_image(url = placeholder.replace(image, rv))

            embed.description = placeholder.replace(embed_form.get("description"), rv)
            embed.color = int(placeholder.replace(embed_form.get("color"), rv))

        except:
            pass

        return embed

class BotPlaceholder:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.variables = {
            "guilds": self.guilds_count,
            "users": self.users_count,
            "players": self.players_count,
            "nodes": self.nodes_count
        }
    
    def guilds_count(self) -> int:
        return len(self.bot.guilds)
    
    def users_count(self) -> int:
        return len(self.bot.users)
    
    def players_count(self) -> int:
        count = 0
        for node in NodePool._nodes.values():
            count += len(node._players)

        return count
    
    def nodes_count(self):
        return len(NodePool._nodes)
    
    def replace(self, msg: str) -> str:
        keys: List[str] = re.findall(r'@@(.*?)@@', msg)

        for key in keys:
            value = self.variables.get(key.lower(), None)
            if value:
                msg = msg.replace(f"@@{key}@@", str(value()))

        return msg