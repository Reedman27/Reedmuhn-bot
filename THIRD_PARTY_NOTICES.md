# Third-party notices

Reedmuhn Bot is an AGPL-3.0 project. This file records third-party projects that were used as implementation references or whose compatible code/architecture informed ports in Reedmuhn.

## Red-DiscordBot

- Project: Red-DiscordBot
- License: GNU General Public License v3.0 (GPL-3.0)
- Source: https://github.com/Cog-Creators/Red-DiscordBot
- Local license copy: `third_party/RED-DISCORD-BOT-LICENSE.txt`

Reedmuhn's moderation and scheduled-action design was developed with Red-DiscordBot's mature moderation patterns as a reference. Where Red-DiscordBot source was ported or adapted, the resulting work remains covered by the applicable copyleft terms and this attribution is retained.

Red-DiscordBot is a separate project and is not a dependency of Reedmuhn Bot.

## Sapphire Framework

- Project: `@sapphire/framework`
- License: MIT
- Source: https://github.com/sapphiredev/framework
- Local license copy: `third_party/SAPPHIRE-FRAMEWORK-LICENSE.txt`

Sapphire's modular command/store/listener architecture was used as an architectural reference. The uploaded TypeScript implementation is **not shipped as TypeScript code** in Reedmuhn; applicable framework concepts were translated into Python/discord.py patterns so the bot remains a Python project.

## Important

The licenses above apply to the respective third-party projects and any material that is actually derived from them. Reedmuhn's own code is licensed under AGPL-3.0 as described in `LICENSE`.
