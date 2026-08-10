"""Small helpers shared across cogs."""
import ast
import operator
import re


def manager_or_permission(permission: str):
    """Allow a Discord administrator, a configured Bot Manager role, or the
    command's normal Discord permission. The role is configured per guild in
    the WebUI. This intentionally does not affect AutoMod exemptions.
    """
    from discord import app_commands

    @app_commands.check
    async def predicate(interaction):
        member = interaction.user
        if interaction.guild is None or member is None:
            return False
        if getattr(member.guild_permissions, "administrator", False):
            return True
        if getattr(member.guild_permissions, permission, False):
            return True
        configured = set(interaction.client.db.list_bot_manager_roles(interaction.guild.id))
        return bool(configured.intersection(getattr(member, "_roles", []))) or any(
            role.id in configured for role in getattr(member, "roles", [])
        )

    return predicate

def can_moderate(actor, target) -> bool:
    """Whether `actor` (a discord.Member) is allowed to take a moderation
    action against `target` (a discord.Member), based on role hierarchy -
    mirroring the check Discord's own client does natively.

    This matters because it's easy to assume the bot's own permission
    checks are enough, but they're not: Discord's REST API only checks the
    BOT's role against the target, not the CALLER's. Without this, a
    moderator with a low role (but the ban_members permission, or a
    configured Bot Manager role) could kick/ban/mute someone with an equal
    or higher role than themselves - including another admin - which
    Discord's own UI would block but the bot's permission decorator alone
    would not.
    """
    if actor.id == target.id:
        return False
    if actor.guild.owner_id == actor.id:
        return True
    return actor.top_role > target.top_role


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """Parses strings like '10m', '2h', '3d' into a number of seconds.
    Raises ValueError if the string doesn't match that shape."""
    match = re.fullmatch(r"(\d+)([smhdw])", text.strip().lower())
    if not match:
        raise ValueError(f"invalid duration: {text!r}")
    amount, unit = match.groups()
    return int(amount) * _UNITS[unit]


def format_duration(seconds: int) -> str:
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def tempnick_self_allowed(mode: str, member_role_ids: set, configured_role_ids: set) -> bool:
    """Whether a member is allowed to use /tempnick on themselves, per the
    server's configured rule. Pulled out as a plain function (no discord.py
    objects involved) so it's trivial to unit test the actual logic
    directly, separate from Discord permission checks or API calls.

    mode: "everyone" (default - everyone can), "allowlist" (only members
    with a configured role can), or "denylist" (everyone except members
    with a configured role can).
    """
    if mode == "allowlist":
        return bool(member_role_ids & configured_role_ids)
    if mode == "denylist":
        return not (member_role_ids & configured_role_ids)
    return True  # "everyone"


# ---- safe arithmetic evaluation ----
# Deliberately NOT using eval() on user input - that would let anyone run
# arbitrary Python on your server. Instead we parse to an AST and only
# permit a fixed whitelist of numeric operations, so `/calc __import__('os')`
# or similar is a parse error, not a security incident.

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_MAX_EXPRESSION_LENGTH = 200  # chars - generous for any real calculator use
_MAX_AST_NODES = 200
_MAX_POWER_EXPONENT = 1000  # fast first-line reject for an absurd single exponent
_MAX_RESULT_BITS = 4096  # ~1233 decimal digits - hard ceiling on any intermediate value


class CalcError(ValueError):
    pass


def _int_bit_length(value) -> int:
    return value.bit_length() if isinstance(value, int) else 0


def safe_eval(expression: str) -> float:
    """Evaluates a basic arithmetic expression (+ - * / // % ** and
    parentheses) and returns the numeric result. Raises CalcError on
    anything outside that whitelist - no names, no calls, no attribute
    access, nothing but numbers and operators reaches this far.

    Bounded on computational cost, not just syntax: a per-node exponent cap
    alone isn't enough - (2**999)**999 passes a same-node check (999 <=
    1000) while still costing real CPU/RAM, because the check never looked
    at how big the LEFT operand already was. So before every Pow or Mult
    actually runs, this checks the operation's PROJECTED result size against
    a hard bit-length ceiling. That closes the nesting bypass regardless of
    how deep the expression is, because every node's own current operands
    are checked at the moment it's evaluated - a huge left-hand value from
    prior nesting gets caught here even though the immediate exponent looks
    small. Expression length and total AST node count are capped up front
    too, so an attacker can't route around this with a wall of text either.
    """
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise CalcError(f"expression too long (max {_MAX_EXPRESSION_LENGTH} characters)")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"couldn't parse that: {exc.msg}") from exc

    if len(list(ast.walk(tree))) > _MAX_AST_NODES:
        raise CalcError("expression too complex")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalcError("only numbers are allowed")
            return node.value
        if isinstance(node, ast.BinOp):
            op_func = _ALLOWED_BINOPS.get(type(node.op))
            if op_func is None:
                raise CalcError(f"operator {type(node.op).__name__} isn't allowed")
            left, right = _eval(node.left), _eval(node.right)

            if isinstance(node.op, ast.Pow):
                if abs(right) > _MAX_POWER_EXPONENT:
                    raise CalcError("exponent too large")
                if isinstance(left, int) and isinstance(right, int) and right > 0:
                    if _int_bit_length(left) * right > _MAX_RESULT_BITS:
                        raise CalcError("result would be too large")
            elif isinstance(node.op, ast.Mult):
                if isinstance(left, int) and isinstance(right, int):
                    if _int_bit_length(left) + _int_bit_length(right) > _MAX_RESULT_BITS:
                        raise CalcError("result would be too large")

            try:
                result = op_func(left, right)
            except ZeroDivisionError as exc:
                raise CalcError("division by zero") from exc
            except OverflowError as exc:
                raise CalcError("result too large") from exc

            if _int_bit_length(result) > _MAX_RESULT_BITS:
                # Belt-and-suspenders for any operation not explicitly
                # pre-checked above.
                raise CalcError("result too large")
            return result
        if isinstance(node, ast.UnaryOp):
            op_func = _ALLOWED_UNARYOPS.get(type(node.op))
            if op_func is None:
                raise CalcError(f"operator {type(node.op).__name__} isn't allowed")
            return op_func(_eval(node.operand))
        raise CalcError(f"'{type(node).__name__}' isn't allowed in expressions")

    return _eval(tree)
