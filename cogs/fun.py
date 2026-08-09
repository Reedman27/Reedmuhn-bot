import random

import discord
from discord import app_commands
from discord.ext import commands

JOKES = [
    "What do you call cheese that isn't yours? Nacho cheese.",
    "I only know 25 letters of the alphabet. I don't know y.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "I used to hate facial hair, but then it grew on me.",
    "What do you call a fish with no eyes? A fsh.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I would tell you a chemistry joke, but I know I wouldn't get a reaction.",
]

# Kept generic and silly on purpose - none of these target real
# characteristics, just harmless "you're like a bad software update" style
# ribbing, same spirit as the reference bot's /insult.
INSULTS = [
    "You're like a software update: always showing up at the worst time.",
    "You're the human embodiment of a 1-star review.",
    "You're not the sharpest tool - but you're in the toolbox, I guess.",
    "You have the confidence of a printer that's actually out of paper.",
    "You're proof that even loading screens can have personality.",
    "You're the reply-all of people.",
]

COMPLIMENTS = [
    "is the reason the group chat doesn't die.",
    "has main character energy today.",
    "gives off unreasonably good vibes.",
    "is quietly the most reliable person here.",
    "somehow makes everything better just by showing up.",
    "has impeccable taste and everyone knows it.",
]

EIGHT_BALL_ANSWERS = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.",
    "Most likely.", "Signs point to yes.", "Ask again later.", "Cannot predict now.",
    "Don't count on it.", "My reply is no.", "Very doubtful.", "Outlook not so good.",
]

WOULD_YOU_RATHER = [
    ("only be able to whisper for the rest of your life", "only be able to shout for the rest of your life"),
    ("have unlimited free tacos", "have unlimited free pizza"),
    ("always be 10 minutes late", "always be 20 minutes early"),
    ("fight one horse-sized duck", "fight 100 duck-sized horses"),
    ("be able to fly but only 3 feet off the ground", "be invisible but only in complete darkness"),
    ("lose all your saved passwords", "lose all your browser bookmarks"),
]

RPS_OPTIONS = ["rock", "paper", "scissors"]
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- existing ----

    @app_commands.command(name="hug", description="Hug someone")
    @app_commands.describe(user="Who to hug")
    @app_commands.checks.cooldown(3, 10.0)
    async def hug(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.send_message(f"{interaction.user.mention} hugs {user.mention} 🫂")

    @app_commands.command(name="dadjoke", description="Get a random dad joke")
    @app_commands.checks.cooldown(3, 10.0)
    async def dadjoke(self, interaction: discord.Interaction):
        await interaction.response.send_message(random.choice(JOKES))

    @app_commands.command(name="insult", description="Playfully roast someone")
    @app_commands.describe(user="Who to roast")
    @app_commands.checks.cooldown(3, 10.0)
    async def insult(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.send_message(f"{user.mention}, {random.choice(INSULTS)}")

    # ---- new social ----

    @app_commands.command(name="compliment", description="Give someone a compliment")
    @app_commands.describe(user="Who to compliment")
    @app_commands.checks.cooldown(3, 10.0)
    async def compliment(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.send_message(f"{user.mention} {random.choice(COMPLIMENTS)}")

    @app_commands.command(name="pat", description="Pat someone on the head")
    @app_commands.describe(user="Who to pat")
    @app_commands.checks.cooldown(3, 10.0)
    async def pat(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.send_message(f"{interaction.user.mention} pats {user.mention} on the head 🤚")

    @app_commands.command(name="slap", description="Slap someone (playfully)")
    @app_commands.describe(user="Who to slap")
    @app_commands.checks.cooldown(3, 10.0)
    async def slap(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.send_message(f"{interaction.user.mention} slaps {user.mention} 👋")

    @app_commands.command(name="highfive", description="High-five someone")
    @app_commands.describe(user="Who to high-five")
    @app_commands.checks.cooldown(3, 10.0)
    async def highfive(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.send_message(f"{interaction.user.mention} high-fives {user.mention} 🙌")

    @app_commands.command(name="ship", description="Ship two people and get a compatibility score")
    @app_commands.describe(user1="First person", user2="Second person")
    @app_commands.checks.cooldown(3, 10.0)
    async def ship(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
        # Deterministic per-pair so the same two people always get the same
        # score in a given server, instead of a new random number every time.
        seed = interaction.guild.id if interaction.guild else 0
        seed += min(user1.id, user2.id) + max(user1.id, user2.id)
        score = random.Random(seed).randint(0, 100)

        name = user1.display_name[: len(user1.display_name) // 2] + user2.display_name[len(user2.display_name) // 2 :]
        bar_filled = "█" * (score // 10)
        bar_empty = "░" * (10 - score // 10)
        await interaction.response.send_message(
            f"💞 **{name}** — {score}%\n{bar_filled}{bar_empty}"
        )

    # ---- games / randomness ----

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your question")
    @app_commands.checks.cooldown(3, 10.0)
    async def eight_ball(self, interaction: discord.Interaction, question: str):
        await interaction.response.send_message(f"🎱 {random.choice(EIGHT_BALL_ANSWERS)}")

    @app_commands.command(name="coinflip", description="Flip a coin")
    @app_commands.checks.cooldown(3, 10.0)
    async def coinflip(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🪙 {random.choice(['Heads', 'Tails'])}!")

    @app_commands.command(name="roll", description="Roll dice, e.g. 2d6")
    @app_commands.describe(dice="Format: NdM, like 1d20 or 3d6 (defaults to 1d6)")
    @app_commands.checks.cooldown(3, 10.0)
    async def roll(self, interaction: discord.Interaction, dice: str = "1d6"):
        try:
            count_str, sides_str = dice.lower().split("d")
            count, sides = int(count_str), int(sides_str)
        except ValueError:
            await interaction.response.send_message("Format that like `2d6` (2 six-sided dice).", ephemeral=True)
            return

        if not (1 <= count <= 100 and 2 <= sides <= 1000):
            await interaction.response.send_message("Keep it reasonable - up to 100 dice, 2-1000 sides.", ephemeral=True)
            return

        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        shown = ", ".join(map(str, rolls)) if count <= 20 else f"{count} rolls"
        await interaction.response.send_message(f"🎲 {shown} = **{total}**")

    @app_commands.command(name="rps", description="Play rock-paper-scissors against the bot")
    @app_commands.checks.cooldown(3, 10.0)
    @app_commands.choices(choice=[app_commands.Choice(name=o.capitalize(), value=o) for o in RPS_OPTIONS])
    async def rps(self, interaction: discord.Interaction, choice: app_commands.Choice[str]):
        bot_choice = random.choice(RPS_OPTIONS)
        user_choice = choice.value

        if user_choice == bot_choice:
            result = "It's a tie!"
        elif RPS_BEATS[user_choice] == bot_choice:
            result = "You win!"
        else:
            result = "I win!"

        await interaction.response.send_message(f"You: **{user_choice}** | Me: **{bot_choice}** — {result}")

    @app_commands.command(name="choose", description="Let the bot pick between options for you")
    @app_commands.describe(options="Comma-separated list of options")
    @app_commands.checks.cooldown(3, 10.0)
    async def choose(self, interaction: discord.Interaction, options: str):
        choices = [o.strip() for o in options.split(",") if o.strip()]
        if len(choices) < 2:
            await interaction.response.send_message("Give me at least two options, separated by commas.", ephemeral=True)
            return
        await interaction.response.send_message(f"I choose: **{random.choice(choices)}**")

    @app_commands.command(name="wouldyourather", description="Get a random would-you-rather question")
    @app_commands.checks.cooldown(3, 10.0)
    async def would_you_rather(self, interaction: discord.Interaction):
        option_a, option_b = random.choice(WOULD_YOU_RATHER)
        await interaction.response.send_message(f"Would you rather **{option_a}**, or **{option_b}**?")

    # ---- text toys ----

    @app_commands.command(name="mock", description="MoCk TeXt LiKe tHiS")
    @app_commands.describe(text="Text to mock")
    @app_commands.checks.cooldown(3, 10.0)
    async def mock(self, interaction: discord.Interaction, text: str):
        mocked = "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(text))
        await interaction.response.send_message(mocked)

    @app_commands.command(name="reverse", description="Reverse some text")
    @app_commands.describe(text="Text to reverse")
    @app_commands.checks.cooldown(3, 10.0)
    async def reverse(self, interaction: discord.Interaction, text: str):
        await interaction.response.send_message(text[::-1])


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
