import asyncio
import json
import datetime
import os

import hikari
import lightbulb
import miru
from receiptgen import database, ticketsystem, utils
from aiohttp import web

token = os.getenv("BOT_KEY")

bot = lightbulb.BotApp(
    token=token,
    intents=hikari.Intents.ALL_UNPRIVILEGED | hikari.Intents.GUILD_MEMBERS | hikari.Intents.MESSAGE_CONTENT,
    default_enabled_guilds=[1255986026669674616],
    prefix="!"
)

bot.d.miru = miru.Client(bot, ignore_unknown_interactions=True)
routes = web.RouteTableDef()


@bot.listen(hikari.StartedEvent)
async def on_start(event: hikari.StartedEvent):
    await bot.update_presence(
        status=hikari.Status.ONLINE,
        activity=hikari.Activity(
            name="AmethyX Receipts",
            type=hikari.ActivityType.WATCHING,
        ),
    )

    bot.d.miru.start_view(ticketsystem.MainTicketView())
    bot.d.miru.start_view(ticketsystem.TicketChannelView())

    # Start web server
    await runner.setup()
    webserver = web.TCPSite(runner, host='0.0.0.0', port=5000)
    await webserver.start()

    # Start background loops
    asyncio.create_task(remove_expired_tickets_loop())
    asyncio.create_task(remove_access_roles_loop())


@bot.listen(lightbulb.CommandErrorEvent)
async def on_error(event: lightbulb.CommandErrorEvent) -> None:
    exception = event.exception.__cause__ or event.exception

    if isinstance(exception, lightbulb.CommandIsOnCooldown):
        await event.context.respond(
            f"Command is on cooldown. Retry in `{exception.retry_after:.2f}` seconds.",
            flags=hikari.MessageFlag.EPHEMERAL
        )
        return

    raise exception


config = utils.get_config()


async def access_notif(state, user_id, guild_data):
    member = await bot.rest.fetch_member(
        guild=guild_data.get("guild_id"),
        user=user_id
    )
    notification_channel = guild_data.get("notification_channel")

    emulator_tutorial = "[Emulator Tutorial]()"
    generator_tutorial = "[Email Gen Tutorial]()"
    paper_generator_tutorial = "[Paper Receipt Tutorial]()"

    if not notification_channel:
        return

    if state == "added":
        embed = hikari.Embed(
            title="Email Gen Access Added",
            description=f"Thank you for choosing AmethyX {member.mention}"
                        f"\nYou can now use the receipt generator by typing\n /menu or /generator",
            color=config["color"]
        ).add_field("Tutorial", f"We recommend you watch {generator_tutorial}") \
         .add_field("Vouch", f"Please vouch for us in <#{config['vouch_channel']}>")

    elif state == "emulator":
        embed = hikari.Embed(
            title="Emulator Access Added",
            description=f"Thank you for choosing AmethyX {member.mention}"
                        f"\nYou can use the emulators at https://amethyx.net/account",
            color=config["color"]
        ).add_field("Tutorial", paper_generator_tutorial) \
         .add_field("Vouch", f"Please vouch in <#{config['vouch_channel']}>")

    elif state == "paper_receipts":
        embed = hikari.Embed(
            title="Paper Receipts Access Added",
            description=f"Thank you for choosing AmethyX {member.mention}"
                        f"\nYou can use the emulators at https://amethyx.net/account",
            color=config["color"]
        ).add_field("Tutorial", paper_generator_tutorial) \
         .add_field("Vouch", f"Please vouch in <#{config['vouch_channel']}>")

    elif state == "full_package":
        embed = hikari.Embed(
            title="Full Access Added",
            description=f"Thank you for choosing AmethyX {member.mention}"
                        f"\nEmulators and paper receipts: https://amethyx.net/account"
                        f"\nEmail receipts: /menu or /generate",
            color=config["color"]
        ).add_field("Tutorial", f"{paper_generator_tutorial}, {generator_tutorial}, {emulator_tutorial}") \
         .add_field("Vouch", f"Please vouch in <#{config['vouch_channel']}>")

    elif state == "removed":
        embed = hikari.Embed(
            title="Subscription Expired",
            description=f"{member.mention}, your subscription has ended.",
            color="#ff244c",
            timestamp=datetime.datetime.now().astimezone()
        )

    else:
        return

    await bot.rest.create_message(embed=embed, channel=notification_channel, user_mentions=True)
    mention = await bot.rest.create_message(content=member.mention, channel=notification_channel, user_mentions=True)
    await asyncio.sleep(1.5)
    await mention.delete()


@routes.post("/add-access-role")
async def add_access_role(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.Response(text="Invalid JSON", status=400)

    guild_db = database.GuildAPI(guild_id=data["guild_id"])
    guild_data = await guild_db.get_guild()

    if data.get("type") in ["emulator", "full_package", "paper_receipts"]:
        asyncio.create_task(access_notif(data["type"], int(data["user_id"]), guild_data))

        asyncio.create_task(bot.rest.add_role_to_member(
            guild=int(data["guild_id"]),
            user=int(data["user_id"]),
            role=guild_data["access_role"]
        ))
        return web.Response(text="success")

    if guild_data.get("access_role"):
        asyncio.create_task(bot.rest.add_role_to_member(
            guild=int(data["guild_id"]),
            user=int(data["user_id"]),
            role=guild_data["access_role"]
        ))

    asyncio.create_task(access_notif("added", int(data["user_id"]), guild_data))

    return web.Response(text="success")


app = web.Application()
app.add_routes(routes)
runner = web.AppRunner(app)


@bot.listen()
async def cleanup_webserver(_: hikari.StoppingEvent):
    await runner.cleanup()


@bot.listen(hikari.MessageCreateEvent)
async def honeypot(event: hikari.MessageCreateEvent):
    if event.is_bot:
        return

    if event.message.channel_id == config["honeypot_channel"]:
        await event.message.delete()
        try:
            await bot.rest.ban_user(
                guild=event.message.guild_id,
                user=event.author,
                delete_message_seconds=300,
                reason="fell into a honeypot"
            )
        except hikari.ForbiddenError:
            pass


@bot.listen(hikari.GuildChannelDeleteEvent)
async def ticket_delete(event: hikari.GuildChannelDeleteEvent):
    await database.Ticket.delete_ticket(event.channel_id)


# -----------------------------
# REPLACEMENT BACKGROUND TASKS
# -----------------------------

async def remove_expired_tickets_loop():
    await bot.wait_until_started()
    while True:
        non_deleted = await database.Ticket.get_non_deleted()
        if non_deleted:
            for channel_id in non_deleted:
                await database.Ticket.delete_ticket(channel_id)
                await bot.rest.delete_channel(int(channel_id))
        await asyncio.sleep(3600)  # every hour


async def remove_access_roles_loop():
    await bot.wait_until_started()
    while True:
        views = [ticketsystem.MainTicketView(), ticketsystem.TicketChannelView()]
        for view in views:
            bot.d.miru.start_view(view)

        guild_db = database.GuildAPI()
        for guild_member in await guild_db.members_without_access():

            member_guild = guild_member.get("guild")
            guild_db = database.GuildAPI(guild_id=member_guild)
            guild_data = await guild_db.get_guild()

            if guild_data.get("access_role"):
                await bot.rest.remove_role_from_member(
                    guild=guild_data["guild_id"],
                    role=guild_data["access_role"],
                    user=guild_member.get("member")
                )

                await access_notif(
                    state="removed",
                    user_id=guild_member.get("member"),
                    guild_data=guild_data
                )

        await asyncio.sleep(60)  # every minute


@bot.listen(hikari.GuildChannelDeleteEvent)
async def ticket_removed(event: hikari.GuildChannelDeleteEvent):
    await database.Ticket.delete_ticket(event.channel_id)


bot.load_extensions_from("./cogs_rent/")
bot.load_extensions_from("./cogs_dev/")
bot.load_extensions_from("./cogs_shared/")
bot.run()
