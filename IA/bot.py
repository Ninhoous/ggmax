"""Discord server blueprint generator using OpenRouter and Components V2.

The bot creates a blueprint first and only changes the guild after the user
clicks Confirmar. It intentionally creates resources in the current guild;
Discord bots cannot create brand-new guilds through the public API.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server-generator")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
IMAGE_PATH = Path(__file__).with_name("1.jpg")

MAX_NAME = 90
MAX_ROLES = 25
MAX_CATEGORIES = 15
MAX_CHANNELS = 60
ALLOWED_CHANNEL_PERMISSIONS = {"view_channel", "send_messages", "read_message_history", "connect", "speak"}


def clean_name(value: Any, fallback: str, *, channel: bool = False) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if channel:
        name = name.lower().replace(" ", "-")
        name = re.sub(r"[^\w-]", "", name, flags=re.UNICODE)
    return (name[:MAX_NAME].strip() or fallback)


def clean_emoji(value: Any, fallback: str) -> str:
    emoji = str(value or "").strip()
    if not emoji or "\n" in emoji or len(emoji) > 8:
        return fallback
    return emoji


def base_name(value: Any, fallback: str, *, channel: bool = False) -> str:
    """Remove an accidental prefix before applying the requested naming style."""
    name = str(value or "").strip()
    if "・" in name:
        name = name.split("・", 1)[-1].strip()
    return clean_name(name, fallback, channel=channel)


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("A resposta da IA não contém JSON válido.")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("O plano retornado não é um objeto JSON.")
    return data


def normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    roles: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for item in raw.get("roles", [])[:MAX_ROLES]:
        if not isinstance(item, dict):
            continue
        raw_name = base_name(item.get("name"), "Membro")
        key = raw_name.casefold()
        if key in seen_roles:
            continue
        seen_roles.add(key)
        perms = [str(p).strip().lower() for p in item.get("permissions", []) if isinstance(p, str)]
        perms = [p for p in perms if p in discord.Permissions.VALID_FLAGS and p != "administrator"]
        color = str(item.get("color", "#5865F2")).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#5865F2"
        emoji = clean_emoji(item.get("emoji"), "👤")
        roles.append({
            "name": f"{emoji} {raw_name}",
            "base_name": raw_name,
            "emoji": emoji,
            "color": color,
            "hoist": bool(item.get("hoist", False)),
            "mentionable": bool(item.get("mentionable", False)),
            "permissions": perms,
        })

    categories: list[dict[str, Any]] = []
    channel_count = 0
    for category in raw.get("categories", [])[:MAX_CATEGORIES]:
        if not isinstance(category, dict):
            continue
        channels: list[dict[str, Any]] = []
        for item in category.get("channels", []):
            if channel_count >= MAX_CHANNELS or not isinstance(item, dict):
                break
            kind = str(item.get("type", "text")).lower()
            if kind not in {"text", "voice"}:
                kind = "text"
            private_to = [base_name(x, "", channel=False) for x in item.get("private_to", []) if str(x).strip()]
            raw_channel_name = base_name(item.get("name"), "geral", channel=True)
            emoji = clean_emoji(item.get("emoji"), "💬")
            channels.append({
                "name": f"{emoji}・{raw_channel_name}"[:MAX_NAME],
                "base_name": raw_channel_name,
                "emoji": emoji,
                "type": kind,
                "topic": str(item.get("topic", ""))[:1024],
                "private_to": private_to[:10],
            })
            channel_count += 1
        if channels:
            categories.append({"name": clean_name(category.get("name"), "Canais"), "channels": channels})

    if not roles:
        roles.append({"name": "Membro", "color": "#5865F2", "hoist": False, "mentionable": False, "permissions": []})
    return {"name": clean_name(raw.get("name"), "Novo servidor"), "description": str(raw.get("description", ""))[:500], "roles": roles, "categories": categories}


async def ask_openrouter(prompt: str) -> dict[str, Any]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY não foi configurada.")
    system = """Você é um arquiteto especialista em comunidades Discord. Gere uma estrutura completa, coerente e prática para o tema pedido. Responda SOMENTE com JSON válido, sem markdown, comentários ou texto fora do JSON.

Esquema obrigatório:
{"name": string, "description": string, "roles": [{"name": string, "emoji": string, "color": "#RRGGBB", "hoist": boolean, "mentionable": boolean, "permissions": [string]}], "categories": [{"name": string, "channels": [{"name": string, "emoji": string, "type": "text"|"voice", "topic": string, "private_to": [string]}]}]}

Regras de nomenclatura:
- Para cada canal, "name" deve conter SOMENTE o nome sem emoji. "emoji" deve conter exatamente um emoji Unicode que combine com o canal. O bot montara o nome final como "emoji・nome-do-canal".
- Para cada cargo, "name" deve conter SOMENTE o nome sem emoji. "emoji" deve conter exatamente um emoji Unicode relacionado ao cargo. O bot montara o nome final como "emoji nome-do-cargo".
- Categorias devem ter apenas texto no campo "name", sem emoji e sem o separador "・".
- Use nomes curtos, claros e em minusculo para canais; nao repita emojis sem necessidade.
- "private_to" deve conter os nomes base dos cargos, sem o emoji e sem o espaco depois dele.

Permissoes:
- Use nomes em ingles snake_case validos no Discord, como view_channel, send_messages, read_message_history, manage_messages, connect e speak.
- Nunca use a permissao administrator, mesmo que o tema peca por ela.
- Crie cargos com hierarquia razoavel e o minimo de privilegios necessario.
- Organize canais em categorias coerentes. Crie canais de informacoes, comunidade, suporte e voz quando fizer sentido para o tema.
- Nao exagere: prefira uma estrutura completa, mas que seja realmente utilizavel."""
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 4000,
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"OpenRouter retornou HTTP {response.status}: {body[:300]}")
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        return normalize_plan(parse_json(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("A resposta da OpenRouter veio em formato inesperado.") from exc


def plan_summary(plan: dict[str, Any]) -> str:
    roles = ", ".join(r["name"] for r in plan["roles"])
    categories = sum(1 for _ in plan["categories"])
    channels = sum(len(c["channels"]) for c in plan["categories"])
    return ("## ATENCAO: o servidor sera limpo\n"
            "Ao confirmar, o bot tentara apagar todos os canais, categorias e cargos apagaveis antes de criar a nova estrutura.\n\n"
            f"## Prévia: {plan['name']}\n{plan['description'] or 'Estrutura gerada pela IA.'}\n\n"
            f"**Cargos ({len(plan['roles'])})**\n{roles}\n\n"
            f"**Categorias:** {categories}  |  **Canais:** {channels}\n\n"
            "Clique em **Confirmar criação** para aplicar no servidor atual.")[:3900]


async def clear_guild(guild: discord.Guild) -> tuple[int, int, list[str]]:
    """Delete resources the bot is allowed to delete.

    @everyone, managed/integration roles, and roles above the bot are not
    deletable through the Discord API and are intentionally preserved.
    """
    deleted_channels = 0
    deleted_roles = 0
    errors: list[str] = []

    # Delete child channels first, then category containers.
    channels = list(guild.channels)
    for channel in [c for c in channels if not isinstance(c, discord.CategoryChannel)]:
        try:
            await channel.delete(reason="Limpeza solicitada pelo gerador de servidor")
            deleted_channels += 1
        except discord.HTTPException as exc:
            errors.append(f"canal {channel.name}: {exc}")
    for category in [c for c in channels if isinstance(c, discord.CategoryChannel)]:
        try:
            await category.delete(reason="Limpeza solicitada pelo gerador de servidor")
            deleted_channels += 1
        except discord.HTTPException as exc:
            errors.append(f"categoria {category.name}: {exc}")

    me = guild.me
    # Fetch both objects directly. The cached Member can omit the bot role,
    # which would incorrectly make every role look higher than the bot.
    fresh_roles = await guild.fetch_roles()
    fresh_me = me
    if me:
        try:
            fresh_me = await guild.fetch_member(me.id)
        except discord.HTTPException as exc:
            errors.append(f"nao foi possivel atualizar a hierarquia do bot: {exc}")
    bot_role_ids = {role.id for role in fresh_me.roles} if fresh_me else set()
    # The automatically created role for a bot can be managed and may not be
    # present in Member.roles. Identify it from RoleTags as a fallback.
    if me:
        self_role = getattr(guild, "self_role", None)
        if self_role:
            bot_role_ids.add(self_role.id)
        for role in fresh_roles:
            tags = role.tags
            bot_id = getattr(tags, "bot_id", None) if tags else None
            is_bot_role = getattr(role, "is_bot_managed", lambda: False)()
            if is_bot_role and bot_id == me.id:
                bot_role_ids.add(role.id)
    bot_roles = [role for role in fresh_roles if role.id in bot_role_ids]
    bot_top_role = max(bot_roles, key=lambda role: role.position, default=None)
    if bot_top_role is None and me:
        errors.append("cargo do bot nao localizado; confirme que o bot possui um cargo proprio no servidor")
    for role in fresh_roles:
        if role.is_default():
            continue
        if role.managed:
            errors.append(f"cargo preservado {role.name}: gerenciado por integracao")
            continue
        # Role comparisons account for Discord's actual hierarchy and avoid
        # relying on position numbers, which can be tied or stale.
        if bot_top_role is None or role >= bot_top_role:
            errors.append(f"cargo preservado {role.name}: esta acima ou no mesmo nivel do bot; mova o cargo do bot para o topo")
            continue
        try:
            await role.delete(reason="Limpeza solicitada pelo gerador de servidor")
            deleted_roles += 1
        except discord.HTTPException as exc:
            errors.append(f"cargo {role.name}: {exc}")
    return deleted_channels, deleted_roles, errors


async def apply_plan(guild: discord.Guild, plan: dict[str, Any]) -> tuple[int, int, int, int, list[str]]:
    deleted_channels, deleted_roles, errors = await clear_guild(guild)
    created_roles: dict[str, discord.Role] = {}
    role_count = 0
    channel_count = 0
    for item in plan["roles"]:
        try:
            permissions = discord.Permissions.none()
            for permission in item["permissions"]:
                if hasattr(permissions, permission):
                    setattr(permissions, permission, True)
            role = await guild.create_role(name=item["name"], permissions=permissions, colour=discord.Colour.from_str(item["color"]), hoist=item["hoist"], mentionable=item["mentionable"], reason="Gerador de servidor via OpenRouter")
            created_roles[item["name"].casefold()] = role
            created_roles[item.get("base_name", item["name"]).casefold()] = role
            role_count += 1
        except discord.HTTPException as exc:
            errors.append(f"cargo {item['name']}: {exc}")

    for category_data in plan["categories"]:
        try:
            category = await guild.create_category(category_data["name"], reason="Gerador de servidor via OpenRouter")
            for channel_data in category_data["channels"]:
                overwrites: dict[discord.Role, discord.PermissionOverwrite] = {}
                private_roles = [created_roles.get(name.casefold()) for name in channel_data["private_to"]]
                private_roles = [role for role in private_roles if role]
                if private_roles:
                    overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
                    for role in private_roles:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
                if channel_data["type"] == "voice":
                    await guild.create_voice_channel(channel_data["name"], category=category, overwrites=overwrites, reason="Gerador de servidor via OpenRouter")
                else:
                    await guild.create_text_channel(channel_data["name"], category=category, topic=channel_data["topic"] or None, overwrites=overwrites, reason="Gerador de servidor via OpenRouter")
                channel_count += 1
        except discord.HTTPException as exc:
            errors.append(f"categoria {category_data['name']}: {exc}")
    return deleted_channels, deleted_roles, role_count, channel_count, errors


async def send_completion_message(guild: discord.Guild, result: str) -> discord.Message:
    """Send the completion card to a random usable text channel."""
    me = guild.me
    # Fetch after cleanup so a deleted channel cannot remain in the local cache.
    fresh_channels = await guild.fetch_channels()
    candidates = [
        channel for channel in guild.text_channels
        if me and channel.id in {item.id for item in fresh_channels}
        and channel.permissions_for(me).send_messages
    ]
    if candidates:
        channel = random.choice(candidates)
    else:
        channel = await guild.create_text_channel(
            "📣・geracao-concluida",
            reason="Canal de resultado do gerador de servidor",
        )

    layout = discord.ui.LayoutView()
    thumbnail = discord.ui.Thumbnail(
        "attachment://1.jpg",
        description="Imagem de conclusao",
    )
    section = discord.ui.Section(
        discord.ui.TextDisplay(content=result[:3900]),
        accessory=thumbnail,
    )
    layout.add_item(discord.ui.Container(section))

    if not IMAGE_PATH.is_file():
        raise FileNotFoundError(f"Imagem nao encontrada: {IMAGE_PATH}")
    return await channel.send(view=layout, file=discord.File(IMAGE_PATH, filename="1.jpg"))


class PlanView(discord.ui.LayoutView):
    def __init__(self, author_id: int, plan: dict[str, Any], guild: discord.Guild):
        super().__init__(timeout=180)
        self.author_id, self.plan, self.guild, self.used = author_id, plan, guild, False
        confirm = discord.ui.Button(label="Confirmar criação", style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm
        cancel.callback = self.cancel
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(content=plan_summary(plan)), discord.ui.ActionRow(confirm, cancel)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Só quem pediu a geração pode usar estes botões.", ephemeral=True)
            return False
        return True

    async def confirm(self, interaction: discord.Interaction) -> None:
        if self.used:
            await interaction.response.send_message("Esta prévia já foi processada.", ephemeral=True)
            return
        self.used = True
        await interaction.response.defer()
        deleted_channels, deleted_roles, roles, channels, errors = await apply_plan(self.guild, self.plan)
        result = (f"## Concluido\nLimpeza: **{deleted_channels} canais/categorias** e **{deleted_roles} cargos** apagados.\n"
                  f"Criacao: **{roles} cargos** e **{channels} canais** em **{self.guild.name}**.")
        if errors:
            result += "\n\n**Itens que falharam:**\n" + "\n".join(f"- {e[:180]}" for e in errors[:8])
        try:
            await send_completion_message(self.guild, result)
        except (discord.HTTPException, FileNotFoundError) as exc:
            log.exception("Falha ao publicar resultado no servidor")
            # The preview channel may have been deleted, so do not try to edit
            # the original ephemeral message here. That causes Unknown Message.
            log.error("Resultado nao publicado: %s", exc)

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.used = True
        self.clear_items()
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(content="## Cancelado\nNenhuma alteração foi feita.")))
        await interaction.response.edit_message(view=self)


class GeneratorBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("Comandos sincronizados")

    async def on_ready(self) -> None:
        log.info("Conectado como %s", self.user)


bot = GeneratorBot()


@bot.tree.command(name="gerar", description="Gera uma estrutura de servidor a partir de uma descrição")
@app_commands.guild_only()
@app_commands.describe(descricao="Descreva o tema, comunidade e organização desejada")
async def gerar(interaction: discord.Interaction, descricao: str) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Você precisa da permissão Gerenciar servidor.", ephemeral=True)
        return
    me = interaction.guild.me
    if not me or not me.guild_permissions.manage_channels or not me.guild_permissions.manage_roles:
        await interaction.response.send_message("Eu preciso das permissões Gerenciar canais e Gerenciar cargos.", ephemeral=True)
        return
    if not descricao.strip() or len(descricao) > 1000:
        await interaction.response.send_message("A descrição deve ter entre 1 e 1000 caracteres.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        plan = await ask_openrouter(descricao)
        await interaction.followup.send(view=PlanView(interaction.user.id, plan, interaction.guild), ephemeral=True)
    except Exception as exc:  # Keep provider/API errors user-friendly.
        log.exception("Falha ao gerar plano")
        await interaction.followup.send(f"Não consegui gerar o plano: `{str(exc)[:500]}`", ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env")
    bot.run(DISCORD_TOKEN)
