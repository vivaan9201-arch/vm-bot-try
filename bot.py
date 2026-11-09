# LXD Discord VPS bot (Python 3)
# Install: python3 -m pip install -U discord.py
# Set token: export DISCORD_TOKEN="your_bot_token"
# Run: python3 bot.py
#
# Host requirements:
#   - LXD installed and initialized
#   - Bot process can run `lxc` commands (usually via the lxd group)
#   - Enable Message Content Intent in the Discord Developer Portal
#
# Commands:
#   .create ubuntu 1 1024 5   -> Ubuntu container, 1 CPU, 1024 MB RAM, 5 GB disk
#   .manage <container-name>  -> interactive panel
#   .list                    -> list containers owned by you
#   .help                     -> commands

import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "."
DATA_FILE = Path(__file__).with_name("lxc_owners.json")

# Resource limits keep accidental oversized container requests in check.
MAX_CPU = 4
MAX_RAM_MB = 8192
MAX_DISK_GB = 50
CREATE_TIMEOUT = 600
COMMAND_TIMEOUT = 45

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


def load_owners():
    try:
        return json.loads(DATA_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_owners(data):
    DATA_FILE.write_text(json.dumps(data, indent=2))


def safe_name(value):
    value = value.lower()
    return re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", value) is not None


def run_lxc(*args, timeout=COMMAND_TIMEOUT):
    """Run an LXC command without a shell; return stdout."""
    if not shutil.which("lxc"):
        raise RuntimeError("`lxc` CLI is missing. Install and initialize LXD first.")
    proc = subprocess.run(
        ["lxc", *map(str, args)],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "Unknown LXD error").strip()
        raise RuntimeError(detail[-1500:])
    return proc.stdout.strip()


def owned_by(user_id, name):
    return load_owners().get(name) == str(user_id)


async def lxc_call(*args, timeout=COMMAND_TIMEOUT):
    return await asyncio.to_thread(run_lxc, *args, timeout=timeout)


def error_embed(message):
    return discord.Embed(title="❌ LXD Error", description=str(message)[:3500],
                         color=discord.Color.red())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | Prefix: {PREFIX}")


@bot.command(name="help")
async def help_command(ctx):
    e = discord.Embed(title="LXD VPS Bot", color=discord.Color.blurple())
    e.description = (
        "`.create ubuntu <cpu> <ram_mb> <disk_gb>`\n"
        "Example: `.create ubuntu 1 1024 5`\n\n"
        "`.manage <name>` — interactive controls\n"
        "`.list` — your containers\n"
        "`.help` — this message"
    )
    await ctx.reply(embed=e, mention_author=False)


@bot.command(name="create")
@commands.cooldown(1, 30, commands.BucketType.user)
async def create(ctx, image="ubuntu", cpu: int = 1, ram_mb: int = 1024, disk_gb: int = 5):
    if image.lower() not in {"ubuntu", "debian"}:
        return await ctx.reply("Supported images: `ubuntu`, `debian`.")
    if not (1 <= cpu <= MAX_CPU and 256 <= ram_mb <= MAX_RAM_MB and 1 <= disk_gb <= MAX_DISK_GB):
        return await ctx.reply(
            f"Limits: CPU 1–{MAX_CPU}, RAM 256–{MAX_RAM_MB} MB, disk 1–{MAX_DISK_GB} GB."
        )

    name = f"vps-{ctx.author.id}-{uuid.uuid4().hex[:6]}"
    msg = await ctx.reply(f"⏳ Creating `{name}`…")
    try:
        # Images use the official Ubuntu/Debian LXD remotes.
        image_alias = "24.04" if image.lower() == "ubuntu" else "12"
        await lxc_call("launch", f"images:{image_alias}", name, timeout=CREATE_TIMEOUT)
        await lxc_call("config", "set", name, "limits.cpu", str(cpu))
        await lxc_call("config", "set", name, "limits.memory", f"{ram_mb}MB")
        # Root disk limit works when the default profile's root disk supports size.
        try:
            await lxc_call("config", "device", "set", name, "root", "size", f"{disk_gb}GB")
        except RuntimeError:
            # Keep the container but tell the user if the host profile cannot set disk size.
            pass

        owners = load_owners()
        owners[name] = str(ctx.author.id)
        save_owners(owners)
        e = discord.Embed(title="✅ VPS Created", color=discord.Color.green())
        e.add_field(name="Name", value=f"`{name}`", inline=False)
        e.add_field(name="Image", value=image.title())
        e.add_field(name="CPU", value=f"{cpu} core(s)")
        e.add_field(name="RAM", value=f"{ram_mb} MB")
        e.add_field(name="Disk requested", value=f"{disk_gb} GB")
        e.set_footer(text=f"Manage it with .manage {name}")
        await msg.edit(content=None, embed=e)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        await msg.edit(content=None, embed=error_embed(exc))


@create.error
async def create_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"Try again in {error.retry_after:.0f}s.")
    elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.reply("Usage: `.create ubuntu 1 1024 5`")
    else:
        await ctx.reply(f"Command error: `{str(error)[:500]}`")


@bot.command(name="list")
async def list_vps(ctx):
    owners = load_owners()
    names = [n for n, owner in owners.items() if owner == str(ctx.author.id)]
    if not names:
        return await ctx.reply("You don't own any bot-created containers yet.")
    try:
        rows = []
        for name in names:
            state = await lxc_call("list", name, "--format", "csv", "-c", "s")
            rows.append(f"• `{name}` — {state or 'unknown'}")
        await ctx.reply(embed=discord.Embed(title="Your VPS containers",
                                           description="\n".join(rows), color=discord.Color.blurple()))
    except Exception as exc:
        await ctx.reply(embed=error_embed(exc))


class ManageView(discord.ui.View):
    def __init__(self, owner_id, name):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.name = name

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to another user.", ephemeral=True)
            return False
        if not owned_by(self.owner_id, self.name):
            await interaction.response.send_message("Ownership record not found.", ephemeral=True)
            return False
        return True

    async def do_action(self, interaction, action):
        await interaction.response.defer(ephemeral=True)
        try:
            if action == "status":
                output = await lxc_call("info", self.name)
            else:
                await lxc_call(action, self.name)
                output = f"`{self.name}` → **{action}** completed."
            await interaction.followup.send(f"```\\n{output[:1800]}\\n```", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, row=0)
    async def start_btn(self, interaction, button):
        await self.do_action(interaction, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction, button):
        await self.do_action(interaction, "stop")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, row=0)
    async def restart_btn(self, interaction, button):
        await self.do_action(interaction, "restart")

    @discord.ui.button(label="Info / Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def info_btn(self, interaction, button):
        await self.do_action(interaction, "status")

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, row=1)
    async def delete_btn(self, interaction, button):
        await interaction.response.send_message(
            f"Really delete `{self.name}`? This permanently removes its container.",
            view=ConfirmDeleteView(self.owner_id, self.name), ephemeral=True
        )


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, owner_id, name):
        super().__init__(timeout=60)
        self.owner_id, self.name = owner_id, name

    @discord.ui.button(label="Confirm delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.owner_id or not owned_by(self.owner_id, self.name):
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await lxc_call("delete", self.name, "--force")
            owners = load_owners()
            owners.pop(self.name, None)
            save_owners(owners)
            await interaction.followup.send(f"Deleted `{self.name}`.", ephemeral=True)
            self.stop()
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


@bot.command(name="manage")
async def manage(ctx, name: str):
    if not safe_name(name) or not owned_by(ctx.author.id, name):
        return await ctx.reply("Container not found or you don't own it.")
    try:
        info = await lxc_call("info", name)
        e = discord.Embed(title="VPS MANAGEMENT PANEL", description=f"```\\n{info[:3000]}\\n```",
                          color=discord.Color.blurple())
        e.set_footer(text=f"Container: {name}")
        await ctx.reply(embed=e, view=ManageView(ctx.author.id, name))
    except Exception as exc:
        await ctx.reply(embed=error_embed(exc))


if not TOKEN:
    raise SystemExit('Set DISCORD_TOKEN first: export DISCORD_TOKEN="your_bot_token"')
bot.run(TOKEN)
