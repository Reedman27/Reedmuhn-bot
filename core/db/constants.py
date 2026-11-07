"""Shared database constants."""

# Permissions a temp-voice channel's creator can be given ON THEIR OWN CHANNEL.
# This list is the hard ceiling: the dashboard can only pick from it, and the
# bot re-applies the same mask when it builds the overwrite, so nothing outside
# it (Administrator, Manage Server, Manage Roles server-wide, ...) can ever be
# handed out, even from a hand-edited database.
TEMP_VOICE_OWNER_PERMS = {
    "manage_channels": (1 << 4, "Rename, resize and edit the channel"),
    "manage_roles": (1 << 28, "Manage permissions on the channel"),
    "create_instant_invite": (1 << 0, "Create invites to the channel"),
    "mute_members": (1 << 22, "Mute people in the channel"),
    "deafen_members": (1 << 23, "Deafen people in the channel"),
    "move_members": (1 << 24, "Move people out of the channel"),
    "priority_speaker": (1 << 8, "Priority speaker"),
}
TEMP_VOICE_OWNER_MASK = sum(bit for bit, _label in TEMP_VOICE_OWNER_PERMS.values())
TEMP_VOICE_DEFAULT_OWNER_PERMS = (1 << 4) | (1 << 22) | (1 << 23) | (1 << 24)
TEMP_VOICE_MAX_CHANNELS_CEILING = 100
