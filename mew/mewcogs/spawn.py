import traceback
import discord
import random
import string
import base64
import asyncio
import time

from discord.ext import commands
from mewutils.checks import check_owner
from mewcogs.json_files import *
from mewcogs.pokemon_list import *
from pokemon_utils.utils import evolve
from mewutils.misc import get_pokemon_image, get_file_name, poke_spawn_check, STAFFSERVER
from mewcogs.json_files import make_embed
from mewcogs.fishing import is_key
from mewcogs.pokemon_list import _
from collections import defaultdict


def despawn_embed(e, status):
    e.title = "Despawned!" if status == "despawn" else "Caught!"
    #e.set_image(url=e.image.url)
    return e

class SpawnResult():
    def __init__(self, text: str):
        self.text = text

class PokeGuess():
    def __init__(self):
        self.guessed = False

    def guess_check(self):
        return self.guessed

async def add_spawn(*, bot, user_id, guild_id, pokemon, shiny, inventory) -> SpawnResult:
    """Spawn handler"""     
    pokemon = pokemon.capitalize()

    ivmulti = inventory.get("iv-multiplier", 0)
    # 0%-10% chance from 0-50 iv multis
    boosted = random.randrange(500) < ivmulti
    plevel = random.randint(1, 60)
    pokedata = await bot.commondb.create_poke(bot, user_id, pokemon, shiny=shiny, boosted=boosted, level=plevel)
    ivpercent = round((pokedata.iv_sum / 186) * 100, 2)
    credits = None

    async with bot.db[0].acquire() as pconn:
        items = await pconn.fetchval(
            "SELECT items::json FROM users WHERE u_id = $1", user_id
        )

        if not items:
            items = {}
        #
        user = await bot.mongo_find(
            "users",
            {"user": user_id},
            default={"user": user_id, "progress": {}},
        )
        progress = user["progress"]
        progress["catch-count"] = progress.get("catch-count", 0) + 1
        await bot.mongo_update("users", {"user": user_id}, {"progress": progress})
        berry_chance = max(1, int(random.random() * 350))
        expensive_chance = max(1, int(random.random() * 25))
        if berry_chance in range(1, 8):
            cheaps = [t["item"] for t in SHOP if t["price"] <= 8000 and not is_key(t["item"])]
            expensives = [
                t["item"]
                for t in SHOP
                if t["price"] in range(8000, 20000) and not is_key(t["item"])
            ]
            if berry_chance == 1:
                berry = random.choice(cheaps)
            elif berry_chance == expensive_chance:
                berry = random.choice(expensives)
            else:
                berry = random.choice(list(berryList))
            # items = user_info["items"]
            items[berry] = items.get(berry, 0) + 1
            await pconn.execute(
                f"UPDATE users SET items = $1::json WHERE u_id = $2",
                items,
                user_id,
            )
        else:
            berry_chance = None
        #
        chest_chance = not random.randint(0, 200)
        if chest_chance:
            inventory = await pconn.fetchval(
                "SELECT inventory::json FROM users WHERE u_id = $1", user_id
            )
            chest = "common chest"
            inventory[chest] = inventory.get(chest, 0) + 1
            await pconn.execute(
                "UPDATE users SET inventory = $1::json where u_id = $2",
                inventory,
                user_id
            )
        if bot.premium_server(guild_id):
            credits = random.randint(100, 250)
            await pconn.execute(
                "UPDATE users SET mewcoins = mewcoins + $1 where u_id = $2",
                credits,
                user_id,
            )
    author = f"<@{user_id}>"
    teext = f"Congratulations {author}, you have caught a {pokedata.emoji}{pokemon} ({ivpercent}% iv)!\n"
    if boosted:
        teext += "It was boosted by your IV multiplier!\n"
    if berry_chance:
        teext += f"It also dropped a {berry}!\n"
    if chest_chance:
        teext += f"It also dropped a {chest}!\n"
    if credits:
        teext += f"You also found {credits} credits!\n"
    
    return SpawnResult(teext)

class SpawnView(discord.ui.View):
    def __init__(self, pokemon: str, delspawn: bool, pinspawn: bool, spawn_channel: discord.TextChannel, legendchance: int, ubchance: int, shiny: bool, poke_guess: PokeGuess):
        self.modal = SpawnModal(pokemon, delspawn, pinspawn, spawn_channel, legendchance, ubchance, shiny, self, poke_guess)
        super().__init__(timeout=360)
        self.msg = None
    
    def set_message(self, msg: discord.Message):
        self.msg = msg
    
    async def on_timeout(self):
        if self.msg:
            embed = self.msg.embeds[0]
            embed.title = "Timed out! Better luck next time!"
            await self.msg.edit(embed=embed, view=None)
    
    @discord.ui.button(label="Catch This Pokemon!", style=discord.ButtonStyle.blurple)
    async def click_here(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(self.modal)

class SpawnModal(discord.ui.Modal, title="Catch This Pokemon!"):
    def __init__(self, pokemon: str, delspawn: bool, pinspawn: bool, spawn_channel: discord.TextChannel, legendchance: int, ubchance: int, shiny: bool, view: discord.ui.View, poke_guess: PokeGuess):
        self.pokemon = pokemon
        self.guessed = False
        self.delspawn = delspawn
        self.pinspawn = pinspawn
        self.spawn_channel = spawn_channel
        self.legendchance = legendchance
        self.ubchance = ubchance
        self.shiny = shiny
        self.view = view
        self.poke_guess = poke_guess
        super().__init__()

    name = discord.ui.TextInput(label='Pokemon Name', placeholder="What do you think this pokemon is named?")

    async def on_submit(self, interaction: discord.Interaction):
        self.embedmsg = interaction.message

        await interaction.response.defer()

        pokemon = self.pokemon

        # Check if pokemon name is correct
        if self.guessed or self.poke_guess.guess_check():
            return await interaction.followup.send("Someone's already guessed this pokemon!", ephemeral=True) 

        if interaction.client.botbanned(interaction.user.id):
            return await interaction.followup.send("You are banned from using Mewbot (for now)")

        if not poke_spawn_check(str(self.name), pokemon):
            return await interaction.followup.send("Incorrect name! Try again :(", ephemeral=True) 
        
        # Someone caught the poke, create it
        async with interaction.client.db[0].acquire() as pconn:
            inventory = await pconn.fetchval(
                "SELECT inventory::json from users WHERE u_id = $1",
                interaction.user.id,
            )
            if inventory is None:
                return await interaction.followup.send("You have not started!\nStart with `/start` first!", ephemeral=True)
            
        self.guessed = True
        self.poke_guess.guessed = True

        res = await add_spawn(
            bot=interaction.client,
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            pokemon=pokemon,
            shiny=self.shiny,
            inventory=inventory
        )

        await interaction.followup.send(embed=(make_embed(title="", description=res.text)))
        try:
            if self.delspawn:
                await self.embedmsg.delete()
            else:
                await self.embedmsg.edit(embed=despawn_embed(self.embedmsg.embeds[0], "caught"), view=None)
                if self.pinspawn and self.spawn_channel.permissions_for(interaction.message.guild.me).manage_messages:
                    if any([self.legendchance < 2, self.ubchance < 2]):
                        await self.embedmsg.pin()
        except discord.HTTPException:
            pass
        
        self.view.stop()

        #Dispatches an event that a poke was spawned.
        #on_poke_spawn(self, channel, user)
        interaction.client.dispatch("poke_spawn", self.spawn_channel, interaction.user)


class Spawn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.spawn_cache = defaultdict(
            int
        )  # This doesn't need to be put in Redis, because it's a cache of Guild ID's, which aren't cross-cluster
        self.always_spawn = False

    @check_owner()
    @commands.hybrid_command(name="lop")
    @discord.app_commands.guilds(STAFFSERVER)
    async def lop(self, ctx):
        if self.always_spawn:
            self.always_spawn = False
            await ctx.send("Always spawning disabled.")
        else:
            self.always_spawn = True
            await ctx.send("Always spawning enabled.")


    async def get_type(self, type_id):
        data = await self.bot.db[1].ptypes.find({"types": type_id}).to_list(None)
        data = [x["id"] for x in data]
        data = await self.bot.db[1].forms.find({"pokemon_id": {"$in": data}}).to_list(None)
        data = [x["identifier"].title() for x in data]
        return list(set(data) & set(totalList))

    @commands.Cog.listener()
    async def on_message(self, message):
        await self.bot.wait_until_ready()
        if not message.guild:
            return
        if message.author.bot:
            return
        if self.bot.botbanned(message.author.id):
            return
        if message.guild.id in (264445053596991498, 446425626988249089):
            return
        if time.time() < self.spawn_cache[message.guild.id]:
            return
        if random.random() >= 0.05 and not self.always_spawn:
            return
        if isinstance(message.channel, discord.threads.Thread):
            return
        if isinstance(message.channel, discord.VoiceChannel):
            return
        self.spawn_cache[message.guild.id] = time.time() + 5
        # See if we are allowed to spawn in this channel & get the spawn channel
        try:
            guild = await self.bot.mongo_find("guilds", {"id": message.guild.id})
            redirects, delspawn, pinspawn, disabled_channels, small_images, mention_spawn = (
                guild["redirects"],
                guild["delete_spawns"],
                guild["pin_spawns"],
                guild["disabled_spawn_channels"],
                guild["small_images"],
                guild.get("mention_spawns", False)
            )
        except Exception:
            redirects, delspawn, pinspawn, disabled_channels, small_images, mention_spawn = (
                [],
                False,
                False,
                [],
                False,
                False
            )
        if message.channel.id in disabled_channels:
            return
        if redirects:
            spawn_channel = message.guild.get_channel(random.choice(redirects))
        else:
            spawn_channel = message.channel
        if spawn_channel is None:
            return
        if isinstance(spawn_channel, discord.CategoryChannel):
            if not spawn_channel.text_channels:
                return
            spawn_channel = random.choice(spawn_channel.text_channels)
        if not isinstance(spawn_channel, discord.TextChannel):
            return
        if not spawn_channel.permissions_for(message.guild.me).send_messages:
            return
        if not spawn_channel.permissions_for(message.guild.me).embed_links:
            return
        # Check the "environment" to determine spawn rates
        override_with_ghost = False
        override_with_ice = False
        async with self.bot.db[0].acquire() as pconn:
            inventory = await pconn.fetchval(
                "SELECT inventory::json FROM users WHERE u_id = $1",
                message.author.id,
            )
            threshold = 4000
            if inventory is not None:
                threshold = round(threshold - threshold * (inventory.get("shiny-multiplier", 0) / 100))
            shiny = random.choice([False for i in range(threshold)] + [True])

            honey = await pconn.fetchval(
                "SELECT type FROM honey WHERE channel = $1 LIMIT 1",
                message.channel.id,
            )
            if honey is None:
                honey = 0
            elif honey == "ghost":
                honey = 0
                override_with_ghost = bool(random.randrange(4))
            elif honey == "cheer":
                honey = 0
                override_with_ice = True
            else:
                honey = 50


            legendchance = int(random.random() * (round(4000 - 7600 * honey / 100)))
            ubchance = int(random.random() * (round(3000 - 5700 * honey / 100)))
            pseudochance = int(random.random() * (round(1000 - 1900 * honey / 100)))
            starterchance = int(random.random() * (round(500 - 950 * honey / 100)))

        # Pick which type of pokemon to spawn
        if override_with_ghost:
            pokemon = random.choice(await self.get_type(8))
        elif override_with_ice:
            pokemon = random.choice(await self.get_type(15))
        elif legendchance < 2:
            pokemon = random.choice(LegendList)
        elif ubchance < 2:
            pokemon = random.choice(ubList)
        elif pseudochance < 2:
            pokemon = random.choice(pseudoList)
        elif starterchance < 2:
            pokemon = random.choice(starterList)
        else:
            pokemon = random.choice(pList)
        pokemon = pokemon.lower()

        # Get the data for the pokemon that is about to spawn
        form_info = await self.bot.db[1].forms.find_one({"identifier": pokemon})
        if form_info is None:
            raise ValueError(f'Bad pokemon name "{pokemon}" passed to spawn.py')
        pokemon_info = await self.bot.db[1].pfile.find_one({"id": form_info["pokemon_id"]})
        if not pokemon_info and "alola" in pokemon:
            pokemon_info = await self.bot.db[1].pfile.find_one(
                {"identifier": pokemon.lower().split("-")[0]}
            )
        try:
            pokeurl = await get_file_name(pokemon, self.bot, shiny)
        except Exception:
            return

        # Create & send the pokemon spawn embed
        embed = discord.Embed(
            title=f"A wild Pokémon has Spawned, Say its name to catch it!",
            color=random.choice(self.bot.colors),
        )
        embed.add_field(name="-", value=f"This Pokémons name starts with {pokemon[0]}")
        try:
            if small_images:
                embed.set_thumbnail(url="http://dyleee.github.io/mewbot-images/sprites/" + pokeurl)
            else:
                embed.set_image(url="http://dyleee.github.io/mewbot-images/sprites/" + pokeurl)
        except Exception:
            return

        poke_guess = PokeGuess()

        try:
            view = SpawnView(
                pokemon=pokemon,
                delspawn=delspawn,
                pinspawn=pinspawn,
                spawn_channel=spawn_channel,
                legendchance=legendchance,
                ubchance=ubchance,
                shiny=shiny,
                poke_guess = poke_guess,
            )
            cmsg = await spawn_channel.send(
                embeds=[embed],
                view=view
            )

            view.set_message(cmsg)
        except:
            self.bot.logger.error(traceback.format_exc())
            
        def check(m):
            return (
                m.channel.id == spawn_channel.id
                and poke_spawn_check(m.content.lower().replace(f"<@{self.bot.user.id}>", "").replace(" ", "", 1).replace(" ", "-"), pokemon)
                and not self.bot.botbanned(m.author.id)
                and not poke_guess.guess_check()
            )

        while True:
            try:
                msg = await self.bot.wait_for("message", check=check, timeout=600)
            except asyncio.TimeoutError:
                return
            async with self.bot.db[0].acquire() as pconn:
                inventory = await pconn.fetchval(
                    "SELECT inventory::json from users WHERE u_id = $1",
                    msg.author.id,
                )
                if inventory is None:
                    await spawn_channel.send("You have not started!\nStart with `/start` first!")
                else:
                    break
        
        poke_guess.guessed = True
        
        res = await add_spawn(
            bot=self.bot,
            user_id=msg.author.id,
            guild_id=msg.guild.id,
            pokemon=pokemon,
            shiny=shiny,
            inventory=inventory,
        )

        await spawn_channel.send(embed=(make_embed(title="", description=res.text)))
        try:
            if delspawn:
                await cmsg.delete()
            else:
                await cmsg.edit(embed=despawn_embed(cmsg.embeds[0], "caught"), view=None)
                if pinspawn and spawn_channel.permissions_for(message.guild.me).manage_messages:
                    if any([legendchance < 2, ubchance < 2]):
                        await cmsg.pin()
        except discord.HTTPException:
            pass
        #Dispatches an event that a poke was spawned.
        #on_poke_spawn(self, channel, user)
        self.bot.dispatch("poke_spawn", spawn_channel, msg.author)



async def setup(bot):
    await bot.add_cog(Spawn(bot))
