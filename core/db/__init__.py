"""SQLite storage, kept as one small class rather than an ORM since the schema
is intentionally small and direct - an ORM would add dependency weight without
much benefit for this self-hosted bot.

``Db`` is assembled from one mixin per feature area (see the sibling modules);
``from core.db import Db`` works exactly as before.
"""
from .schema import SchemaMixin
from .scheduling import SchedulingMixin
from .moderation import ModerationMixin
from .automod import AutomodMixin
from .community import CommunityMixin
from .engagement import EngagementMixin
from .tickets import TicketsMixin
from .tempvoice import TempvoiceMixin
from .integrations import IntegrationsMixin
from .logconfig import LoggingMixin
from .search import SearchMixin
from .snapshots import SnapshotsMixin
from .dashboard import DashboardMixin
from .constants import (
    TEMP_VOICE_DEFAULT_OWNER_PERMS,
    TEMP_VOICE_MAX_CHANNELS_CEILING,
    TEMP_VOICE_OWNER_MASK,
    TEMP_VOICE_OWNER_PERMS,
)


class Db(
    SchemaMixin,
    SchedulingMixin,
    ModerationMixin,
    AutomodMixin,
    CommunityMixin,
    EngagementMixin,
    TicketsMixin,
    TempvoiceMixin,
    IntegrationsMixin,
    LoggingMixin,
    SearchMixin,
    SnapshotsMixin,
    DashboardMixin,
):
    """The bot's and dashboard's shared SQLite database."""


__all__ = [
    "Db",
    "TEMP_VOICE_DEFAULT_OWNER_PERMS",
    "TEMP_VOICE_MAX_CHANNELS_CEILING",
    "TEMP_VOICE_OWNER_MASK",
    "TEMP_VOICE_OWNER_PERMS",
]
