# Python 3 Discord LXD/LXC VPS bot with SSHX management panel
# pip install -U discord.py
# export DISCORD_TOKEN='YOUR_DISCORD_BOT_TOKEN'
# python3 bot.py
#
# Host: LXD initialized (lxd init --minimal), and bot can run `lxc`.
# Discord Developer Portal: enable Message Content Intent.
#
# Commands:
# .create ubuntu 1 1024 5
# .list
# .manage <container-name>

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

MAX_CPU = 4
MAX_RAM_MB = 8192
MAX_DISK_GB = 50
CREATE_TIMEOUT = 600
COMMAND_TIMEOUT = 60

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


def valid_name(name):
    return re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name) is not None


def run_cmd(args, timeout=COMMAND_TIMEOUT):
    if not shutil.which(args[0]):
        raise RuntimeError(f"Command `{args[0]}` is not installed.")
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Command failed").strip()[-1800:])
    return p.stdout.strip()


async def host(*args, timeout=COMMAND_TIMEOUT):
    return await asyncio.to_thread(run_cmd, ["lxc", *map(str, args)], timeout)


async def container_exec(name, command, timeout=120):
    # command is executed inside the selected container, never on the host.
    return await host("exec", name, "--", "bash", "-lc", command, timeout=timeout)


def is_owner(user_id, name):
    return load_owners().get(name) == str(user_id)


def error_embed(exc):
    return discord.Embed(title="❌ Operation failed", description=f"```{str(exc)[:3500]}```",
                         color=discord.Color.red())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | Prefix: {PREFIX}")


@bot.command(name="help")
async def help_cmd(ctx):
    e = discord.Embed(title="LXD VPS Bot", color=discord.Color.blurple())
    e.description = (
        "`.create ubuntu 1 1024 5` — create Ubuntu VPS (CPU, RAM MB, disk GB)\n"
        "`.list` — your containers\n"
        "`.manage <name>` — interactive management panel"
    )
    await ctx.reply(embed=e, mention_author=False)


@bot.command(name="create")
@commands.cooldown(1, 30, commands.BucketType.user)
async def create(ctx, image="ubuntu", cpu: int = 1, ram_mb: int = 1024, disk_gb: int = 5):
    image = image.lower()
    if image not in ("ubuntu", "debian"):
        return await ctx.reply("Images supported: `ubuntu` or `debian`.")
    if not (1 <= cpu <= MAX_CPU and 256 <= ram_mb <= MAX_RAM_MB and 1 <= disk_gb <= MAX_DISK_GB):
        return await ctx.reply(f"Limits: CPU 1–{MAX_CPU}, RAM 256–{MAX_RAM_MB} MB, disk 1–{MAX_DISK_GB} GB.")

    name = f"vps-{ctx.author.id}-{uuid.uuid4().hex[:6]}"
    msg = await ctx.reply(f"⏳ Creating `{name}`…")
    try:
        image_ref = "ubuntu:24.04" if image == "ubuntu" else "images:debian/12"
        await host("launch", image_ref, name, timeout=CREATE_TIMEOUT)
        await host("config", "set", name, "limits.cpu", str(cpu))
        await host("config", "set", name, "limits.memory", f"{ram_mb}MB")
        disk_note = ""
        try:
            await host("config", "device", "set", name, "root", "size", f"{disk_gb}GB")
        except Exception:
            disk_note = "\n⚠️ Disk limit couldn't be applied (host root disk profile may not support it)."
        owners = load_owners()
        owners[name] = str(ctx.author.id)
        save_owners(owners)
        e = discord.Embed(title="✅ VPS CREATED", color=discord.Color.green())
        e.add_field(name="Name", value=f"`{name}`", inline=False)
        e.add_field(name="ID", value=f"`{name[-6:].upper()}`")
        e.add_field(name="Status", value="🟢 Running")
        e.add_field(name="OS", value=image.title())
        e.add_field(name="CPU", value=f"{cpu} Core")
        e.add_field(name="RAM", value=f"{ram_mb} MB")
        e.add_field(name="Storage", value=f"{disk_gb} GB", inline=True)
        e.set_footer(text=f"Use .manage {name}" + disk_note)
        await msg.edit(content=None, embed=e)
    except Exception as exc:
        await msg.edit(content=None, embed=error_embed(exc))


@create.error
async def create_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"Wait {error.retry_after:.0f}s before creating another VPS.")
    elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.reply("Usage: `.create ubuntu 1 1024 5`")
    else:
        await ctx.reply(f"Create error: `{str(error)[:500]}`")


@bot.command(name="list")
async def list_cmd(ctx):
    names = [n for n, uid in load_owners().items() if uid == str(ctx.author.id)]
    if not names:
        return await ctx.reply("No VPS containers found for you.")
    lines = []
    for name in names:
        try:
            state = await host("list", name, "--format", "csv", "-c", "s")
        except Exception:
            state = "unknown"
        lines.append(f"• `{name}` — {state or 'unknown'}")
    await ctx.reply(embed=discord.Embed(title="YOUR VPS", description="\n".join(lines),
                                        color=discord.Color.blurple()))


async def panel_embed(name):
    info = await host("info", name)
    state = "Unknown"
    for line in info.splitlines():
        if line.strip().lower().startswith("status:"):
            state = line.split(":", 1)[1].strip()
            break
    # Pull guest details only when the container is running.
    ip, os_name, uptime = "Not available", "Unknown", "Unavailable"
    cpu_usage, ram_usage, disk_usage = "N/A", "N/A", "N/A"
    if state.lower() == "running":
        try:
            ipout = await container_exec(name, "hostname -I 2>/dev/null || true", timeout=20)
            ip = (ipout.split()[0] if ipout.split() else "Not assigned")
            osout = await container_exec(name, "grep PRETTY_NAME /etc/os-release | cut -d= -f2-", timeout=20)
            os_name = osout.strip().strip('"') or "Linux"
            up = await container_exec(name, "uptime -p 2>/dev/null || uptime", timeout=20)
            uptime = up[:80] or "Running"
            cpu_usage = "Use `top` in SSHX"
            ram_usage = await container_exec(name, "free -m | awk '/Mem:/ {printf \"%s/%s MB\",$3,$2}'", timeout=20)
            disk_usage = await container_exec(name, "df -h / | awk 'NR==2 {print $3 \"/\" $2 \" (\" $5 \")\"}'", timeout=20)
        except Exception:
            pass
    e = discord.Embed(title="🖥️ VPS MANAGEMENT PANEL",
                      description="**Your Cloud Infrastructure Control**",
                      color=discord.Color.from_rgb(88, 101, 242))
    e.add_field(name="▤ SERVER INFORMATION", value=(
        f"**Name:** `{name}`\n**ID:** `{name[-6:].upper()}`\n"
        f"**Status:** {'🟢' if state.lower() == 'running' else '🔴'} {state}\n"
        f"**IP Address:** `{ip}`\n**OS:** {os_name}"
    ), inline=False)
    e.add_field(name="⚙️ RESOURCES", value=(
        f"**CPU:** Configured limit\n**RAM:** Configured limit\n**Storage:** Configured limit"
    ), inline=False)
    e.add_field(name="📊 USAGE", value=(
        f"**CPU Usage:** {cpu_usage}\n**RAM Usage:** {ram_usage}\n"
        f"**Disk Usage:** {disk_usage}\n**Uptime:** {uptime}"
    ), inline=False)
    e.add_field(name="ℹ️", value="Select an action below.", inline=False)
    e.set_footer(text="VPS Management System • SSHX opens a remote shell")
    return e


class ManageView(discord.ui.View):
    def __init__(self, owner_id, name):
        super().__init__(timeout=300)
        self.owner_id, self.name = owner_id, name

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to another user.", ephemeral=True)
            return False
        if not is_owner(self.owner_id, self.name):
            await interaction.response.send_message("Ownership record not found.", ephemeral=True)
            return False
        return True

    async def action(self, interaction, action):
        await interaction.response.defer(ephemeral=True)
        try:
            await host(action, self.name, timeout=120)
            await interaction.followup.send(f"✅ `{self.name}` → **{action}** completed.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def start(self, interaction, button):
        await self.action(interaction, "start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.secondary, row=0)
    async def stop(self, interaction, button):
        await self.action(interaction, "stop")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def restart(self, interaction, button):
        await self.action(interaction, "restart")

    @discord.ui.button(label="SSHX Console", emoji="🖥️", style=discord.ButtonStyle.primary, row=1)
    async def sshx(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Install/run SSHX inside the container, not on the LXD host.
            # The SSHX URL grants shell access to whoever possesses it; share carefully.
            output = await container_exec(
                self.name,
                "command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl); "
                "curl -sSf https://sshx.io/get | sh -s run",
                timeout=240,
            )
            urls = re.findall(r"https?://[^\s\"'<>]+", output)
            link = next((u.rstrip(".,)") for u in urls if "sshx.io" in u), None)
            if link:
                await interaction.followup.send(
                    f"🖥️ **SSHX session for `{self.name}`**\n{link}\n"
                    "⚠️ Anyone with this link may access the shell. Keep it private.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"SSHX command finished, but I couldn't detect its URL. Output:\n```{output[-1500:]}```",
                    ephemeral=True,
                )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="File Manager", emoji="📁", style=discord.ButtonStyle.secondary, row=1)
    async def files(self, interaction, button):
        await interaction.response.send_message(
            "File Manager isn't implemented in this build. Use SSHX and standard Linux file commands.",
            ephemeral=True,
        )

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction, button):
        await interaction.response.defer()
        try:
            await interaction.message.edit(embed=await panel_embed(self.name), view=self)
            await interaction.followup.send("Panel refreshed.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction, button):
        await interaction.response.send_message(
            f"⚠️ Permanently delete `{self.name}`?",
            view=DeleteConfirm(self.owner_id, self.name), ephemeral=True
        )


class DeleteConfirm(discord.ui.View):
    def __init__(self, owner_id, name):
        super().__init__(timeout=60)
        self.owner_id, self.name = owner_id, name

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.owner_id or not is_owner(self.owner_id, self.name):
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await host("delete", self.name, "--force")
            owners = load_owners()
            owners.pop(self.name, None)
            save_owners(owners)
            await interaction.followup.send(f"Deleted `{self.name}`.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        await interaction.response.send_message("Cancelled.", ephemeral=True)


@bot.command(name="manage")
async def manage(ctx, name: str):
    if not valid_name(name) or not is_owner(ctx.author.id, name):
        return await ctx.reply("Container not found or you don't own it.")
    try:
        await ctx.reply(embed=await panel_embed(name), view=ManageView(ctx.author.id, name))
    except Exception as exc:
        await ctx.reply(embed=error_embed(exc))


if not TOKEN:
    raise SystemExit('Set DISCORD_TOKEN first: export DISCORD_TOKEN="your_bot_token"')

bot.run(TOKEN)
