"""LAKES TEAM Telegram NFT gift finder + five-posts-per-minute channel feed.

Sources:
1) Official Telegram resale marketplace via payments.getStarGifts + payments.getResaleStarGifts.
2) Public collectible gift links via payments.getUniqueStarGift.

The personal user session only reads public gift data. The bot account is the UI/channel publisher.
No automatic DMs are sent to discovered users.
"""
import asyncio
import json
import logging
import os
import random
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telethon import Button, TelegramClient, errors, events, types, utils
from telethon.tl.functions.payments import GetUniqueStarGiftRequest

try:
    from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
    MARKET_API_AVAILABLE = True
except ImportError:  # Old Telethon: bot still works with the public-NFT source.
    GetResaleStarGiftsRequest = None
    GetStarGiftsRequest = None
    MARKET_API_AVAILABLE = False

from logic import Hit, describe_hit, format_resell_amount, gift_attributes, pick_username, validate_collection, validate_max_number

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SESSIONS = ROOT / "sessions"
SESSIONS.mkdir(exist_ok=True)
COLLECTIONS_PATH = ROOT / "collections.json"
DATABASE_PATH = ROOT / "seen.sqlite3"

TEAM_NAME = os.getenv("TEAM_NAME", "LAKES TEAM").strip() or "LAKES TEAM"
MAX_PER_COMMAND = 3
MAX_RANDOM_ATTEMPTS = 20
RANDOM_DELAY_SECONDS = 0.8
MARKET_ATTEMPTS = 8
MARKET_PAGE_SIZE = 30
MARKET_CACHE_SECONDS = 10 * 60
MIN_INTERVAL = 1
DEFAULT_AUTOPOST_INTERVAL = 1  # LAKES TEAM: 1 post per second

# Every newly joined non-admin member stays in the configured channel for 24 hours.
CHANNEL_MEMBER_TTL_SECONDS = 24 * 60 * 60
MEMBER_SWEEP_SECONDS = 10

# Optional Stars/payment helpers are kept, but they no longer control channel membership.
ACCESS_DURATION_SECONDS = 24 * 60 * 60
INVITE_LINK_LIFETIME_SECONDS = ACCESS_DURATION_SECONDS
SUBSCRIPTION_SWEEP_SECONDS = 10
PAYMENT_UPDATE_TIMEOUT = 20
PAYMENT_PAYLOAD = "lakes_access_1h_v1"

DEFAULT_COLLECTIONS = {
    "IceCream": 342255,
    "PetSnake": 279106,
    "MousseCake": 230505,
    "ViceCream": 490553,
    "HappyBrownie": 264231,
}

HELP = f"""💜 {TEAM_NAME} NFT Finder

Автопостинг:
/channel @channel — выбрать публичный канал
/channel -100... — выбрать закрытый канал по ID
/bindchannel — для закрытого канала: отправь эту команду прямо В КАНАЛ
/autopost on|off — включить/выключить автопост
/postnow — найти и кинуть один пост прямо сейчас
/source mix|market|telegram — источник автопоста
/interval 1 — интервал в секундах (по умолчанию 1 сек.)
/rate 60 — удобная настройка: до 60 постов в минуту
/status — текущие настройки

Участники канала:
• любого НОВОГО обычного участника бот автоматически удалит через 24 часа
• админов/владельца канала бот не трогает
• заявки на вступление бот НЕ принимает и НЕ отклоняет

Оплата (если используешь):
/price 50 — цена 24 часов в Telegram Stars
/grant USER_ID 1440 — вручную записать доступ
/revoke USER_ID — вручную забрать доступ
/subs — активные оплаты
Пользователь: /buy, /access, /mystatus, /paysupport

Premium-стикер:
1. Пришли боту нужный стикер (можно Premium из @PremiumStickers)
2. Ответь на него командой /setsticker
/sticker on|off — включить/выключить
/stickerevery 1 — отправлять Premium-стикер после каждого NFT-поста
/clearsticker — забыть сохранённый стикер

Ручной поиск:
/random — 1 публичный владелец из обычных NFT-ссылок
/random 3 — до 3 владельцев
/random IceCream 2 — до 2 владельцев Ice Cream
/market — 1 продавец с официального Telegram Market
/list — коллекции обычного поиска
/add IceCream 342255 — добавить/обновить коллекцию
/remove IceCream — удалить коллекцию
/reset — очистить историю найденных владельцев/подарков
/id — твой Telegram ID

Бот никому сам не пишет: кнопка «Написать продавцу» только открывает чат с готовым черновиком."""


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def load_collections() -> dict[str, int]:
    if not COLLECTIONS_PATH.exists():
        save_collections(DEFAULT_COLLECTIONS)
    raw = json.loads(COLLECTIONS_PATH.read_text(encoding="utf-8"))
    return {validate_collection(k): validate_max_number(str(v)) for k, v in raw.items()}


def save_collections(collections: dict[str, int]) -> None:
    temporary = COLLECTIONS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(collections, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(COLLECTIONS_PATH)


def open_database() -> sqlite3.Connection:
    con = sqlite3.connect(DATABASE_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (owner_id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS seen_gifts (slug TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT '',
            last_charge_id TEXT,
            updated_at INTEGER NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS access_invites (
            invite_link TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS channel_members (
            user_id INTEGER PRIMARY KEY,
            joined_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        )"""
    )
    con.commit()
    return con


def grant_subscription(con: sqlite3.Connection, user_id: int, seconds: int, source: str,
                       charge_id: str | None = None) -> int:
    now = int(time.time())
    row = con.execute(
        "SELECT expires_at, active FROM subscriptions WHERE user_id=?", (user_id,)
    ).fetchone()
    current_expiry = int(row[0]) if row and int(row[1]) else 0
    starts_at = max(now, current_expiry)
    expires_at = starts_at + max(1, int(seconds))
    con.execute(
        """INSERT INTO subscriptions(user_id, expires_at, active, source, last_charge_id, updated_at)
           VALUES (?, ?, 1, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             expires_at=excluded.expires_at, active=1, source=excluded.source,
             last_charge_id=excluded.last_charge_id, updated_at=excluded.updated_at""",
        (user_id, expires_at, source, charge_id, now),
    )
    con.commit()
    return expires_at


def subscription_expiry(con: sqlite3.Connection, user_id: int) -> int | None:
    row = con.execute(
        "SELECT expires_at, active FROM subscriptions WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row or not int(row[1]):
        return None
    expires_at = int(row[0])
    if expires_at <= int(time.time()):
        return None
    return expires_at


def format_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes} мин."
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч. {mins} мин." if mins else f"{hours} ч."


def is_seen(con: sqlite3.Connection, owner_id: int) -> bool:
    return con.execute("SELECT 1 FROM seen WHERE owner_id=?", (owner_id,)).fetchone() is not None


def is_gift_seen(con: sqlite3.Connection, slug: str) -> bool:
    return con.execute("SELECT 1 FROM seen_gifts WHERE slug=?", (slug,)).fetchone() is not None


def mark_seen(con: sqlite3.Connection, hit: Hit) -> None:
    con.execute("INSERT OR IGNORE INTO seen(owner_id) VALUES (?)", (hit.owner_id,))
    con.execute("INSERT OR IGNORE INTO seen_gifts(slug) VALUES (?)", (hit.slug,))
    con.commit()


def get_setting(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def setting_bool(con: sqlite3.Connection, key: str, default: bool) -> bool:
    value = get_setting(con, key, "1" if default else "0")
    return value.lower() in {"1", "true", "yes", "on", "да"}


def target_entity(value: str):
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def input_channel_from_saved(con: sqlite3.Connection, value: str):
    """Return a bot-account InputPeerChannel when a private channel hash was saved.

    Telegram channel access_hash values are account-bound. For a private channel, a
    bare -100... ID is not enough after a fresh bot session, so /bindchannel stores
    the hash seen by this bot account.
    """
    value = value.strip()
    if not value.lstrip("-").isdigit():
        return None
    access_hash = get_setting(con, "target_channel_access_hash", "").strip()
    if not access_hash:
        return None
    marked_id = int(value)
    real_id, peer_type = utils.resolve_id(marked_id)
    if peer_type is not types.PeerChannel:
        return None
    return types.InputPeerChannel(channel_id=real_id, access_hash=int(access_hash))


def extract_hit_from_gift(gift, users, *, requested_collection: str = "", requested_number: int = 0,
                          source: str = "telegram") -> Hit | None:
    if gift is None:
        return None
    peer = getattr(gift, "owner_id", None)
    if not isinstance(peer, types.PeerUser):
        return None
    user = next((u for u in (users or []) if getattr(u, "id", None) == peer.user_id), None)
    if user is None or getattr(user, "bot", False) or getattr(user, "deleted", False):
        return None
    username = pick_username(user)
    if not username:
        return None
    slug = getattr(gift, "slug", None)
    if not isinstance(slug, str) or not slug:
        return None
    attrs = gift_attributes(gift)
    display_name = " ".join(
        part for part in [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""] if part
    ).strip() or username
    price = format_resell_amount(getattr(gift, "resell_amount", None)) if source == "market" else None
    return Hit(
        owner_id=peer.user_id,
        username=username,
        display_name=display_name,
        slug=slug,
        collection=getattr(gift, "title", None) or requested_collection or slug.split("-", 1)[0],
        number=getattr(gift, "num", None) or requested_number,
        source=source,
        price=price,
        **attrs,
    )


def parse_random(args: list[str], collections: dict[str, int]) -> tuple[str | None, int]:
    collection = None
    requested = 1
    if len(args) > 2:
        raise ValueError("Формат: /random [Коллекция] [1–3]")
    if args:
        if args[0].isdigit():
            if len(args) > 1:
                raise ValueError("Формат: /random [Коллекция] [1–3]")
            requested = int(args[0])
        else:
            collection = args[0]
            if collection not in collections:
                raise ValueError("Коллекция не добавлена. Посмотри /list и /add")
            if len(args) == 2:
                if not args[1].isdigit():
                    raise ValueError("Укажи число от 1 до 3")
                requested = int(args[1])
    if not 1 <= requested <= MAX_PER_COMMAND:
        raise ValueError("За один запрос можно найти от 1 до 3 владельцев")
    return collection, requested


def hit_buttons(hit: Hit, *, include_done: bool = False):
    open_noun = "продавца" if hit.source == "market" else "владельца"
    write_noun = "продавцу" if hit.source == "market" else "владельцу"
    draft = quote(f"Привет! Увидел ваш подарок {hit.collection} #{hit.number}.")
    buttons = [
        [Button.url("🎁 Открыть подарок", f"https://t.me/nft/{hit.slug}")],
        [Button.url(f"👤 Открыть {open_noun}", f"https://t.me/{hit.username}?profile")],
        [Button.url(f"✍️ Написать {write_noun}", f"https://t.me/{hit.username}?text={draft}")],
    ]
    if include_done:
        # Callback-кнопка есть только у постов в рабочем канале.
        # Удаление разрешено только ADMIN_ID, иначе любой подписчик мог бы стирать посты.
        buttons.append([Button.inline("✅ Написал владельцу", data=b"lakes:handled")])
    return buttons


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    bot_token = os.environ["BOT_TOKEN"]
    admin_id = int(os.environ["ADMIN_ID"])
    collections = load_collections()
    con = open_database()

    # First-run defaults from .env. Later /commands persist in SQLite.
    defaults = {
        "target_channel": os.getenv("TARGET_CHANNEL", "").strip(),
        "autopost_enabled": "1" if env_bool("AUTOPOST_ENABLED", True) else "0",
        "autopost_interval": str(max(MIN_INTERVAL, int(os.getenv("AUTOPOST_INTERVAL", str(DEFAULT_AUTOPOST_INTERVAL))))),
        "source_mode": os.getenv("AUTOPOST_SOURCE", "mix").strip().lower() or "mix",
        "sticker_enabled": "1" if env_bool("STICKER_ENABLED", False) else "0",
        "sticker_every": str(max(1, int(os.getenv("STICKER_EVERY", "1")))),
        "post_count": "0",
        "access_price_stars": str(max(1, int(os.getenv("ACCESS_PRICE_STARS", "50")))),
    }
    for key, value in defaults.items():
        if not con.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
            set_setting(con, key, value)

    # LAKES TEAM migration: set the requested default feed speed to 1 second.
    # Runs once even for databases created by older v2.x builds.
    if not con.execute("SELECT 1 FROM settings WHERE key='v25_one_second_migrated'").fetchone():
        set_setting(con, "autopost_interval", "1")
        set_setting(con, "sticker_every", "1")
        set_setting(con, "v25_one_second_migrated", "1")

    user = TelegramClient(str(SESSIONS / "owner_search"), api_id, api_hash, flood_sleep_threshold=0)
    bot = TelegramClient(str(SESSIONS / "bot"), api_id, api_hash, flood_sleep_threshold=0)
    scan_lock = asyncio.Lock()
    market_cache: dict[str, object] = {"at": 0.0, "items": []}
    mix_next_market = True
    def _bot_api_sync(method: str, params: dict):
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=35) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"Bot API {method}: HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Bot API {method}: network error: {exc}") from exc
        if not data.get("ok"):
            description = data.get("description", "unknown Bot API error")
            raise RuntimeError(f"Bot API {method}: {description}")
        return data.get("result")

    async def bot_api(method: str, **params):
        return await asyncio.to_thread(_bot_api_sync, method, params)

    def access_price_stars() -> int:
        return max(1, int(get_setting(con, "access_price_stars", os.getenv("ACCESS_PRICE_STARS", "50"))))

    async def send_access_invoice(user_id: int) -> None:
        price = access_price_stars()
        await bot_api(
            "sendInvoice",
            chat_id=user_id,
            title=f"{TEAM_NAME} — 24 часа",
            description="24 часа доступа к закрытому каналу. После окончания бот автоматически удалит пользователя, если он не администратор.",
            payload=PAYMENT_PAYLOAD,
            currency="XTR",
            prices=[{"label": "Доступ на 24 часа", "amount": price}],
            start_parameter="lakes_access_24h",
        )

    async def revoke_user_invites(user_id: int) -> None:
        channel = get_setting(con, "target_channel", "").strip()
        rows = con.execute(
            "SELECT invite_link FROM access_invites WHERE user_id=? AND used=0", (user_id,)
        ).fetchall()
        if channel:
            for (link,) in rows:
                try:
                    await bot_api("revokeChatInviteLink", chat_id=target_entity(channel), invite_link=link)
                except Exception:
                    logging.debug("Could not revoke invite %s", link, exc_info=True)
        con.execute("UPDATE access_invites SET used=1 WHERE user_id=? AND used=0", (user_id,))
        con.commit()

    async def create_access_invite(user_id: int) -> str:
        expires_at = subscription_expiry(con, user_id)
        if not expires_at:
            raise ValueError("Активного доступа нет. Сначала оплати 24 часа через /buy.")
        channel = get_setting(con, "target_channel", "").strip()
        if not channel:
            raise ValueError("Канал ещё не настроен владельцем бота.")
        await revoke_user_invites(user_id)
        now = int(time.time())
        link_expires = min(expires_at, now + INVITE_LINK_LIFETIME_SECONDS)
        if link_expires <= now + 5:
            raise ValueError("Доступ уже почти закончился. Обнови его через /buy.")
        result = await bot_api(
            "createChatInviteLink",
            chat_id=target_entity(channel),
            name=f"{TEAM_NAME} {user_id}"[:32],
            expire_date=link_expires,
            member_limit=1,
        )
        link = result["invite_link"]
        con.execute(
            "INSERT OR REPLACE INTO access_invites(invite_link, user_id, expires_at, used) VALUES (?, ?, ?, 0)",
            (link, user_id, link_expires),
        )
        con.commit()
        return link

    async def send_access_link(user_id: int) -> None:
        expires_at = subscription_expiry(con, user_id)
        if not expires_at:
            await bot_api("sendMessage", chat_id=user_id, text="⏳ Доступ не активен. Нажми /buy, чтобы купить 24 часа.")
            return
        link = await create_access_invite(user_id)
        remaining = format_remaining(expires_at - int(time.time()))
        await bot_api(
            "sendMessage",
            chat_id=user_id,
            text=(
                f"✅ Доступ {TEAM_NAME} активен.\n"
                f"Осталось: {remaining}\n\n"
                "Это одноразовая ссылка на один вход. После фактического вступления в канал "
                "запустится отдельный таймер на 24 часа."
            ),
            reply_markup={"inline_keyboard": [[{"text": "🔐 Войти в закрытый канал", "url": link}]]},
        )

    async def is_channel_admin(user_id: int) -> bool:
        channel = get_setting(con, "target_channel", "").strip()
        if not channel:
            return False
        try:
            member = await bot_api("getChatMember", chat_id=target_entity(channel), user_id=user_id)
            return (member or {}).get("status") in {"administrator", "creator"}
        except Exception:
            # If Telegram cannot check the role, keep the old behavior rather than silently leaving access forever.
            logging.debug("Could not check channel role for %s", user_id, exc_info=True)
            return False

    async def remove_subscriber(user_id: int, *, notify: bool = True) -> None:
        channel = get_setting(con, "target_channel", "").strip()
        await revoke_user_invites(user_id)
        protected_admin = await is_channel_admin(user_id) if channel else False
        if channel and not protected_admin:
            try:
                await bot_api("banChatMember", chat_id=target_entity(channel), user_id=user_id, revoke_messages=False)
            except Exception:
                logging.warning("Could not remove expired subscriber %s", user_id, exc_info=True)
            try:
                # Immediately unban: the user is removed, but can buy again and rejoin later.
                await bot_api("unbanChatMember", chat_id=target_entity(channel), user_id=user_id, only_if_banned=True)
            except Exception:
                logging.debug("Could not unban expired subscriber %s", user_id, exc_info=True)
        con.execute(
            "UPDATE subscriptions SET active=0, updated_at=? WHERE user_id=?",
            (int(time.time()), user_id),
        )
        con.commit()
        if notify:
            try:
                text = (
                    "🛡 24 часа доступа закончились, но ты администратор канала — бот тебя не удалял."
                    if protected_admin else
                    "⏳ Твои 24 часа доступа закончились. Чтобы зайти снова, купи доступ ещё раз: /buy"
                )
                await bot_api("sendMessage", chat_id=user_id, text=text)
            except Exception:
                logging.debug("Could not notify expired subscriber %s", user_id, exc_info=True)

    def configured_channel_matches(chat: dict) -> bool:
        configured = get_setting(con, "target_channel", "").strip()
        if not configured:
            return False
        chat_id = str(chat.get("id", ""))
        if configured.lstrip("-").isdigit():
            return chat_id == configured
        username = (chat.get("username") or "").strip()
        return bool(username and configured.lower().lstrip("@") == username.lower().lstrip("@"))

    def remember_channel_join(member: dict) -> None:
        uid = int(member["user"]["id"])
        now = int(time.time())
        expires_at = now + CHANNEL_MEMBER_TTL_SECONDS
        username = (member["user"].get("username") or "")[:64]
        first_name = (member["user"].get("first_name") or "")[:128]
        con.execute(
            """INSERT INTO channel_members(user_id, joined_at, expires_at, active, username, first_name, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 joined_at=excluded.joined_at, expires_at=excluded.expires_at, active=1,
                 username=excluded.username, first_name=excluded.first_name, updated_at=excluded.updated_at""",
            (uid, now, expires_at, username, first_name, now),
        )
        con.commit()
        logging.info("24h timer started: user=%s expires=%s", uid, expires_at)

    def forget_channel_member(user_id: int) -> None:
        con.execute(
            "UPDATE channel_members SET active=0, updated_at=? WHERE user_id=?",
            (int(time.time()), int(user_id)),
        )
        con.commit()

    async def remove_timed_channel_member(user_id: int) -> None:
        channel = get_setting(con, "target_channel", "").strip()
        if not channel:
            return
        if await is_channel_admin(user_id):
            # If a normal member was promoted before the timer ended, protect them.
            forget_channel_member(user_id)
            logging.info("24h timer expired but user is admin; skipped: %s", user_id)
            return
        try:
            member = await bot_api("getChatMember", chat_id=target_entity(channel), user_id=user_id)
            status = (member or {}).get("status")
            if status in {"left", "kicked"}:
                forget_channel_member(user_id)
                return
        except Exception:
            logging.debug("Could not read member status before timed removal: %s", user_id, exc_info=True)
        try:
            await bot_api("banChatMember", chat_id=target_entity(channel), user_id=user_id, revoke_messages=False)
            await bot_api("unbanChatMember", chat_id=target_entity(channel), user_id=user_id, only_if_banned=True)
            logging.info("24h member removed: user=%s", user_id)
        except Exception:
            logging.warning("Could not remove 24h member %s", user_id, exc_info=True)
            return
        forget_channel_member(user_id)

    async def refresh_market_catalog(force: bool = False):
        if not MARKET_API_AVAILABLE:
            return []
        if not force and time.monotonic() - float(market_cache["at"]) < MARKET_CACHE_SECONDS:
            return list(market_cache["items"])
        response = await user(GetStarGiftsRequest(hash=0))
        items = []
        for gift in getattr(response, "gifts", None) or []:
            resale = getattr(gift, "availability_resale", None)
            gift_id = getattr(gift, "id", None)
            if isinstance(gift_id, int) and isinstance(resale, int) and resale > 0:
                items.append((gift_id, getattr(gift, "title", None) or "Telegram Gift", resale))
        market_cache["at"] = time.monotonic()
        market_cache["items"] = items
        logging.info("Telegram Market catalog: %d gift types with resale", len(items))
        return list(items)

    async def find_market_hit() -> Hit | None:
        if not MARKET_API_AVAILABLE:
            return None
        catalog = await refresh_market_catalog()
        if not catalog:
            return None
        candidates = catalog[:]
        random.shuffle(candidates)
        for gift_id, title, _resale_count in candidates[:MARKET_ATTEMPTS]:
            try:
                response = await user(GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=MARKET_PAGE_SIZE))
            except errors.FloodWaitError:
                raise
            except errors.RPCError as exc:
                logging.warning("Market lookup failed for gift_id=%s: %s", gift_id, exc)
                continue
            gifts = list(getattr(response, "gifts", None) or [])
            random.shuffle(gifts)
            for gift in gifts:
                hit = extract_hit_from_gift(gift, getattr(response, "users", None), requested_collection=title, source="market")
                if hit and not is_seen(con, hit.owner_id) and not is_gift_seen(con, hit.slug):
                    return hit
        return None

    def _market_price_key(gift) -> tuple[int, float]:
        """Sort resale gifts by displayed price without changing the normal /market command."""
        stars = None
        ton = None
        for value in getattr(gift, "resell_amount", None) or []:
            amount = getattr(value, "amount", None)
            if not isinstance(amount, int):
                continue
            kind = type(value).__name__.lower()
            if "ton" in kind:
                ton = amount / 1_000_000_000
            else:
                nanos = getattr(value, "nanos", 0)
                stars = amount + (nanos / 1_000_000_000 if isinstance(nanos, int) else 0)
        if stars is not None:
            return (0, float(stars))
        if ton is not None:
            return (1, float(ton))
        return (2, float("inf"))

    async def find_channel_market_hit() -> Hit | None:
        """Channel-only market picker: common gift types, cheaper listings, smaller sellers."""
        if not MARKET_API_AVAILABLE:
            return None
        catalog = await refresh_market_catalog()
        if not catalog:
            return None

        # High resale availability is a useful proxy for common, non-rare gift types.
        common_count = max(12, len(catalog) // 3)
        candidates = sorted(catalog, key=lambda item: item[2], reverse=True)[:common_count]
        random.shuffle(candidates)

        for gift_id, title, _resale_count in candidates[:MARKET_ATTEMPTS]:
            try:
                response = await user(GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=MARKET_PAGE_SIZE))
            except errors.FloodWaitError:
                raise
            except errors.RPCError as exc:
                logging.warning("Channel market lookup failed for gift_id=%s: %s", gift_id, exc)
                continue

            gifts = list(getattr(response, "gifts", None) or [])
            if not gifts:
                continue

            # Prefer owners who only appear once on this market page, then cheaper gifts.
            owner_counts: dict[int, int] = {}
            for gift in gifts:
                peer = getattr(gift, "owner_id", None)
                if isinstance(peer, types.PeerUser):
                    owner_counts[peer.user_id] = owner_counts.get(peer.user_id, 0) + 1

            ranked: list[tuple[int, tuple[int, float], Hit]] = []
            for gift in gifts:
                hit = extract_hit_from_gift(
                    gift,
                    getattr(response, "users", None),
                    requested_collection=title,
                    source="market",
                )
                if not hit or is_seen(con, hit.owner_id) or is_gift_seen(con, hit.slug):
                    continue
                ranked.append((owner_counts.get(hit.owner_id, 99), _market_price_key(gift), hit))

            if ranked:
                ranked.sort(key=lambda row: (0 if row[0] == 1 else 1, row[0], row[1]))
                return ranked[0][2]
        return None

    async def find_public_hit(selected: str | None = None) -> Hit | None:
        if not collections:
            return None
        for attempt in range(MAX_RANDOM_ATTEMPTS):
            name = selected or random.choice(list(collections))
            number = random.randint(1, collections[name])
            try:
                response = await user(GetUniqueStarGiftRequest(slug=f"{name}-{number}"))
            except errors.FloodWaitError:
                raise
            except errors.RPCError as exc:
                msg = str(getattr(exc, "message", "") or exc).upper()
                if "STARGIFT_SLUG_INVALID" not in msg and "STARGIFT_INVALID" not in msg:
                    logging.warning("Gift lookup failed (%s-%s): %s", name, number, exc)
            else:
                hit = extract_hit_from_gift(
                    getattr(response, "gift", None),
                    getattr(response, "users", None),
                    requested_collection=name,
                    requested_number=number,
                    source="telegram",
                )
                if hit and not is_seen(con, hit.owner_id) and not is_gift_seen(con, hit.slug):
                    return hit
            if attempt + 1 < MAX_RANDOM_ATTEMPTS:
                await asyncio.sleep(RANDOM_DELAY_SECONDS)
        return None

    async def find_for_mode(mode: str) -> Hit | None:
        nonlocal mix_next_market
        mode = mode.lower()
        if mode == "market":
            return await find_market_hit()
        if mode in {"telegram", "random"}:
            return await find_public_hit()

        # MIX alternates sources so the feed genuinely contains both kinds.
        first_market = mix_next_market
        mix_next_market = not mix_next_market
        first = find_market_hit if first_market else find_public_hit
        second = find_public_hit if first_market else find_market_hit
        hit = await first()
        return hit or await second()

    async def find_channel_hit(mode: str) -> Hit | None:
        """Selection used only by the channel feed; commands keep their old behavior."""
        mode = mode.lower()
        if mode == "market":
            return await find_channel_market_hit()
        if mode in {"telegram", "random"}:
            return await find_public_hit()

        # MIX: mostly random public owners, with an occasional cheap/common market listing.
        # This keeps the channel closer to the older "ordinary owner" feed.
        if random.random() < 0.80:
            return await find_public_hit() or await find_channel_market_hit()
        return await find_channel_market_hit() or await find_public_hit()

    async def send_saved_sticker(target) -> None:
        if not setting_bool(con, "sticker_enabled", False):
            return
        every = max(1, int(get_setting(con, "sticker_every", "1")))
        count = int(get_setting(con, "post_count", "0"))
        if count % every != 0:
            return
        chat_id = get_setting(con, "sticker_chat_id", "")
        message_id = get_setting(con, "sticker_message_id", "")
        if not chat_id or not message_id:
            return
        try:
            source = await bot.get_messages(int(chat_id), ids=int(message_id))
            if source and source.media:
                await bot.send_file(target, source.media, silent=True)
        except Exception:
            logging.exception("Could not send saved premium sticker")

    async def resolve_publish_target(channel: str):
        saved = input_channel_from_saved(con, channel)
        if saved is not None:
            return saved
        try:
            peer = await bot.get_input_entity(target_entity(channel))
        except ValueError as exc:
            if channel.lstrip("-").isdigit():
                raise ValueError(
                    "Закрытый канал ещё не привязан к bot-сессии. "
                    "Открой канал и отправь туда /bindchannel от имени канала, "
                    "потом в личке боту сделай /postnow."
                ) from exc
            raise
        if isinstance(peer, types.InputPeerChannel):
            set_setting(con, "target_channel_access_hash", str(peer.access_hash))
        return peer

    async def publish_hit(target, hit: Hit, *, silent: bool = True) -> None:
        await bot.send_message(
            target,
            describe_hit(hit, TEAM_NAME),
            parse_mode="html",
            buttons=hit_buttons(hit, include_done=True),
            link_preview=False,
            silent=silent,
        )
        mark_seen(con, hit)
        count = int(get_setting(con, "post_count", "0")) + 1
        set_setting(con, "post_count", str(count))
        await send_saved_sticker(target)

    async def post_one_to_channel() -> tuple[Hit | None, str]:
        channel = get_setting(con, "target_channel", "").strip()
        if not channel:
            return None, "Канал не задан. Используй /channel @username"
        mode = get_setting(con, "source_mode", "mix")
        hit = await find_channel_hit(mode)
        if not hit:
            return None, "Нового подходящего владельца/продавца сейчас не нашёл."
        try:
            publish_target = await resolve_publish_target(channel)
            await publish_hit(publish_target, hit, silent=True)
        except ValueError as exc:
            return None, str(exc)
        except errors.ChatAdminRequiredError:
            return None, "Бот не может писать в канал. Добавь его админом с правом публикации сообщений."
        except errors.ChannelPrivateError:
            return None, "Канал недоступен боту. Проверь /channel и права бота."
        return hit, f"Опубликован @{hit.username}: {hit.collection} #{hit.number}"

    try:
        await user.connect()
        if not await user.is_user_authorized():
            raise RuntimeError("Сначала выполни python auth.py в терминале.")
        me = await user.get_me()
        if getattr(me, "bot", False):
            raise RuntimeError("В auth.py требуется войти в обычный аккаунт Telegram.")
        await bot.start(bot_token=bot_token)

        @bot.on(events.CallbackQuery(data=b"lakes:handled"))
        async def handled_callback(event):
            # В публичном канале кнопку видят все, поэтому удалить пост может только владелец бота.
            if event.sender_id != admin_id:
                await event.answer("Эта кнопка только для LAKES TEAM.", alert=True)
                return
            try:
                await event.answer("✅ Отмечено. Удаляю пост…")
                await bot.delete_messages(event.chat_id, [event.message_id])
            except errors.ChatAdminRequiredError:
                await event.answer("Не хватает права удаления сообщений в канале.", alert=True)
            except errors.MessageDeleteForbiddenError:
                await event.answer("Telegram не разрешил удалить этот пост. Проверь права бота.", alert=True)
            except Exception:
                logging.exception("Could not delete handled channel post")
                try:
                    await event.answer("Не смог удалить пост. Подробности в терминале.", alert=True)
                except Exception:
                    pass

        @bot.on(events.CallbackQuery(data=b"lakes:buy1h"))
        async def buy_access_callback(event):
            try:
                await event.answer("⭐ Отправляю счёт…")
                await send_access_invoice(event.sender_id)
            except Exception:
                logging.exception("Could not send Stars invoice")
                try:
                    await event.answer("Не смог отправить счёт. Попробуй /buy.", alert=True)
                except Exception:
                    pass

        @bot.on(events.CallbackQuery(data=b"lakes:access"))
        async def get_access_callback(event):
            try:
                await event.answer("🔐 Проверяю доступ…")
                await send_access_link(event.sender_id)
            except ValueError as exc:
                await event.answer(str(exc), alert=True)
            except Exception:
                logging.exception("Could not create access link")
                try:
                    await event.answer("Не смог создать ссылку. Напиши /access чуть позже.", alert=True)
                except Exception:
                    pass

        @bot.on(events.NewMessage(incoming=True, pattern=r"^/bindchannel(?:@\w+)?$"))
        async def bind_channel_handler(event):
            # This command is intentionally sent inside the private broadcast channel.
            # Receiving the channel update gives this bot account its own access_hash.
            if not event.is_channel or event.is_private:
                return
            try:
                peer = await event.get_input_chat()
                if not isinstance(peer, types.InputPeerChannel):
                    return
                set_setting(con, "target_channel", str(event.chat_id))
                set_setting(con, "target_channel_access_hash", str(peer.access_hash))
                logging.info("Bound private channel %s (channel_id=%s)", event.chat_id, peer.channel_id)
                try:
                    await event.delete()
                except Exception:
                    pass
                try:
                    await bot.send_message(
                        admin_id,
                        f"✅ Закрытый канал привязан: <code>{event.chat_id}</code>\n"
                        "Теперь можешь сделать /postnow.",
                        parse_mode="html",
                    )
                except Exception:
                    logging.info("Private-channel bind succeeded; could not send DM confirmation")
            except Exception:
                logging.exception("Could not bind private channel")

        @bot.on(events.NewMessage(incoming=True))
        async def handler(event):
            if not event.is_private or not event.raw_text.startswith("/"):
                return
            parts = event.raw_text.split()
            command = parts[0].split("@", 1)[0].lower()
            args = parts[1:]

            if command == "/id":
                await event.respond(f"Твой Telegram ID: {event.sender_id}")
                return

            # Public paid-access commands. Admin commands below remain owner-only.
            if event.sender_id != admin_id:
                try:
                    if command in {"/start", "/buy"}:
                        expiry = subscription_expiry(con, event.sender_id)
                        status = (
                            f"✅ Доступ активен ещё {format_remaining(expiry - int(time.time()))}."
                            if expiry else "🔒 Доступ сейчас не активен."
                        )
                        await event.respond(
                            f"💜 <b>{TEAM_NAME}</b>\n\n"
                            f"{status}\n"
                            f"24 часа доступа — <b>{access_price_stars()} ⭐</b>.\n\n"
                            "После оплаты бот даст новую персональную одноразовую ссылку. "
                            "Через 24 часа доступ автоматически закончится.",
                            parse_mode="html",
                            buttons=[
                                [Button.inline(f"⭐ Купить 24 часа — {access_price_stars()} Stars", data=b"lakes:buy1h")],
                                [Button.inline("🔐 Получить ссылку", data=b"lakes:access")],
                            ],
                        )
                    elif command == "/access":
                        await send_access_link(event.sender_id)
                    elif command == "/mystatus":
                        expiry = subscription_expiry(con, event.sender_id)
                        if expiry:
                            await event.respond(f"✅ Доступ активен ещё {format_remaining(expiry - int(time.time()))}.")
                        else:
                            await event.respond("🔒 Активного доступа нет. Купить 24 часа: /buy")
                    elif command == "/paysupport":
                        support = os.getenv("PAY_SUPPORT_CONTACT", "").strip()
                        text = "По вопросам оплаты напиши владельцу сервиса."
                        if support:
                            text += f"\nКонтакт: {support}"
                        await event.respond(text)
                    else:
                        await event.respond("Доступные команды: /buy, /access, /mystatus, /paysupport")
                except ValueError as exc:
                    await event.respond(f"Ошибка: {exc}")
                except Exception:
                    logging.exception("Paid-access user command failed")
                    await event.respond("Неожиданная ошибка. Попробуй ещё раз чуть позже.")
                return

            try:
                if command in {"/start", "/help"}:
                    await event.respond(HELP)

                elif command == "/buy":
                    await send_access_invoice(event.sender_id)
                    await event.respond("⭐ Тестовый счёт отправлен выше.")

                elif command == "/access":
                    await send_access_link(event.sender_id)

                elif command == "/mystatus":
                    expiry = subscription_expiry(con, event.sender_id)
                    if expiry:
                        await event.respond(f"✅ Доступ активен ещё {format_remaining(expiry - int(time.time()))}.")
                    else:
                        await event.respond("🔒 Активного доступа нет. Купить 24 часа: /buy")

                elif command == "/paysupport":
                    support = os.getenv("PAY_SUPPORT_CONTACT", "").strip()
                    await event.respond(
                        "По вопросам оплаты напиши владельцу сервиса." + (f"\nКонтакт: {support}" if support else "")
                    )

                elif command == "/status":
                    market = "✅" if MARKET_API_AVAILABLE else "❌ обнови Telethon"
                    sticker = "✅" if setting_bool(con, "sticker_enabled", False) else "❌"
                    await event.respond(
                        f"💜 <b>{TEAM_NAME}</b>\n"
                        f"Канал: <code>{get_setting(con, 'target_channel', '') or 'не задан'}</code>\n"
                        f"Автопост: {'✅' if setting_bool(con, 'autopost_enabled', True) else '❌'}\n"
                        f"Скорость: ~{60 / max(MIN_INTERVAL, int(get_setting(con, 'autopost_interval', str(DEFAULT_AUTOPOST_INTERVAL)))):.1f} постов/мин\n"
                        f"Интервал: {get_setting(con, 'autopost_interval', str(DEFAULT_AUTOPOST_INTERVAL))} сек.\n"
                        f"Источник: <b>{get_setting(con, 'source_mode', 'mix')}</b>\n"
                        f"Telegram Market API: {market}\n"
                        f"Premium-стикер: {sticker}, каждые {get_setting(con, 'sticker_every', '1')} постов\n"
                        f"Опубликовано этой базой: {get_setting(con, 'post_count', '0')}\n"
                        f"Цена доступа: {access_price_stars()} ⭐ / 24 часа\n"
                        f"24ч таймеров участников: {con.execute('SELECT COUNT(*) FROM channel_members WHERE active=1 AND expires_at>?', (int(time.time()),)).fetchone()[0]}\n"
                        f"Заявки на вступление: бот не трогает",
                        parse_mode="html",
                    )

                elif command == "/channel":
                    if len(args) != 1:
                        raise ValueError("Пример: /channel @lakes_market или /channel -100...")
                    channel = args[0].strip()
                    if not channel.startswith("@") and not channel.lstrip("-").isdigit():
                        raise ValueError("Укажи @username канала или числовой ID вида -100...")

                    set_setting(con, "target_channel", channel)
                    set_setting(con, "target_channel_access_hash", "")

                    if channel.lstrip("-").isdigit():
                        try:
                            peer = await bot.get_input_entity(int(channel))
                        except ValueError:
                            await event.respond(
                                f"✅ ID закрытого канала сохранён: <code>{channel}</code>\n\n"
                                "Теперь <b>прямо в самом канале</b> отправь:\n"
                                "<code>/bindchannel</code>\n\n"
                                "Бот удалит эту команду и привяжет канал. После этого в личке: /postnow",
                                parse_mode="html",
                            )
                            return
                        else:
                            if isinstance(peer, types.InputPeerChannel):
                                set_setting(con, "target_channel_access_hash", str(peer.access_hash))
                    else:
                        peer = await bot.get_input_entity(channel)
                        if isinstance(peer, types.InputPeerChannel):
                            set_setting(con, "target_channel_access_hash", str(peer.access_hash))

                    await event.respond(f"✅ Канал сохранён и привязан: {channel}\nТеперь сделай /postnow для теста.")

                elif command == "/autopost":
                    if len(args) != 1 or args[0].lower() not in {"on", "off"}:
                        raise ValueError("Пример: /autopost on")
                    set_setting(con, "autopost_enabled", "1" if args[0].lower() == "on" else "0")
                    await event.respond("✅ Настройка автопоста сохранена.")

                elif command == "/interval":
                    if len(args) != 1 or not args[0].isdigit():
                        raise ValueError("Пример: /interval 1")
                    seconds = int(args[0])
                    if not MIN_INTERVAL <= seconds <= 86400:
                        raise ValueError(f"Интервал: от {MIN_INTERVAL} до 86400 секунд")
                    set_setting(con, "autopost_interval", str(seconds))
                    await event.respond(
                        f"✅ Интервал: {seconds} сек. (~{60 / seconds:.1f} постов/мин)"
                    )

                elif command == "/rate":
                    if len(args) != 1 or not args[0].isdigit():
                        raise ValueError("Пример: /rate 60")
                    rate = int(args[0])
                    if not 1 <= rate <= 60:
                        raise ValueError("Скорость: от 1 до 60 постов в минуту")
                    seconds = max(MIN_INTERVAL, round(60 / rate))
                    set_setting(con, "autopost_interval", str(seconds))
                    await event.respond(
                        f"✅ Скорость: примерно {60 / seconds:.1f} постов/мин (каждые {seconds} сек.)"
                    )

                elif command == "/source":
                    if len(args) != 1 or args[0].lower() not in {"mix", "market", "telegram"}:
                        raise ValueError("Пример: /source mix (варианты: mix, market, telegram)")
                    set_setting(con, "source_mode", args[0].lower())
                    await event.respond(f"✅ Источник: {args[0].lower()}")

                elif command == "/setsticker":
                    if not event.reply_to_msg_id:
                        raise ValueError("Ответь /setsticker на сообщение со стикером.")
                    msg = await event.get_reply_message()
                    if not msg or not msg.document:
                        raise ValueError("В сообщении, на которое ты ответил, нет стикера/документа.")
                    is_sticker = any(type(a).__name__ == "DocumentAttributeSticker" for a in (msg.document.attributes or []))
                    if not is_sticker:
                        raise ValueError("Это не Telegram-стикер.")
                    set_setting(con, "sticker_chat_id", str(event.chat_id))
                    set_setting(con, "sticker_message_id", str(msg.id))
                    set_setting(con, "sticker_enabled", "1")
                    await event.respond("✅ Стикер сохранён. Буду добавлять его в ленту по настройке /stickerevery.")

                elif command == "/clearsticker":
                    set_setting(con, "sticker_chat_id", "")
                    set_setting(con, "sticker_message_id", "")
                    set_setting(con, "sticker_enabled", "0")
                    await event.respond("Стикер очищен.")

                elif command == "/sticker":
                    if len(args) != 1 or args[0].lower() not in {"on", "off"}:
                        raise ValueError("Пример: /sticker on")
                    set_setting(con, "sticker_enabled", "1" if args[0].lower() == "on" else "0")
                    await event.respond("✅ Настройка стикера сохранена.")

                elif command == "/stickerevery":
                    if len(args) != 1 or not args[0].isdigit():
                        raise ValueError("Пример: /stickerevery 1")
                    every = int(args[0])
                    if not 1 <= every <= 100:
                        raise ValueError("Укажи число от 1 до 100")
                    set_setting(con, "sticker_every", str(every))
                    await event.respond(f"✅ Стикер будет после каждого {every}-го NFT-поста.")

                elif command == "/price":
                    if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 1:
                        raise ValueError("Пример: /price 50")
                    set_setting(con, "access_price_stars", str(int(args[0])))
                    await event.respond(f"✅ Цена 24 часов: {int(args[0])} ⭐")

                elif command == "/grant":
                    if not args or not args[0].lstrip("-").isdigit():
                        raise ValueError("Пример: /grant 123456789 1440")
                    user_id = int(args[0])
                    minutes = 1440
                    if len(args) >= 2:
                        if not args[1].isdigit() or not 1 <= int(args[1]) <= 43200:
                            raise ValueError("Минуты: от 1 до 43200")
                        minutes = int(args[1])
                    expiry = grant_subscription(con, user_id, minutes * 60, "manual")
                    try:
                        await send_access_link(user_id)
                        delivery = " Ссылка отправлена пользователю."
                    except Exception:
                        logging.warning("Manual grant succeeded but link could not be delivered", exc_info=True)
                        delivery = " Доступ записан; пользователь должен написать /access боту."
                    await event.respond(
                        f"✅ Доступ выдан: <code>{user_id}</code> на {minutes} мин.{delivery}",
                        parse_mode="html",
                    )

                elif command == "/revoke":
                    if len(args) != 1 or not args[0].lstrip("-").isdigit():
                        raise ValueError("Пример: /revoke 123456789")
                    user_id = int(args[0])
                    await remove_subscriber(user_id, notify=True)
                    await event.respond(f"✅ Доступ <code>{user_id}</code> отключён.", parse_mode="html")

                elif command == "/subs":
                    now = int(time.time())
                    rows = con.execute(
                        "SELECT user_id, expires_at, source FROM subscriptions "
                        "WHERE active=1 AND expires_at>? ORDER BY expires_at LIMIT 30",
                        (now,),
                    ).fetchall()
                    if not rows:
                        await event.respond("Активных подписок сейчас нет.")
                    else:
                        lines = [
                            f"• <code>{uid}</code> — {format_remaining(exp - now)} ({source})"
                            for uid, exp, source in rows
                        ]
                        await event.respond("🔐 <b>Активные подписки</b>\n" + "\n".join(lines), parse_mode="html")

                elif command == "/postnow":
                    if scan_lock.locked():
                        await event.respond("Поиск уже идёт. Подожди немного.")
                        return
                    progress = await event.respond("🔎 Ищу свежего владельца/продавца для канала…")
                    async with scan_lock:
                        try:
                            hit, status = await post_one_to_channel()
                        except errors.FloodWaitError as exc:
                            await progress.edit(f"Telegram попросил паузу {exc.seconds} сек. Автопост продолжится позже.")
                        else:
                            await progress.edit(("✅ " if hit else "⚠️ ") + status)

                elif command == "/market":
                    if scan_lock.locked():
                        await event.respond("Поиск уже идёт. Подожди немного.")
                        return
                    if not MARKET_API_AVAILABLE:
                        await event.respond("В этой версии Telethon нет resale API. Выполни: pip install -U telethon")
                        return
                    progress = await event.respond("🛒 Ищу публичного продавца на Telegram Market…")
                    async with scan_lock:
                        hit = await find_market_hit()
                    if not hit:
                        await progress.edit("Сейчас не нашёл нового продавца с публичным @username. Повтори позже.")
                    else:
                        await progress.delete()
                        await bot.send_message(event.chat_id, describe_hit(hit, TEAM_NAME), parse_mode="html", buttons=hit_buttons(hit), link_preview=False)
                        mark_seen(con, hit)

                elif command == "/list":
                    lines = [f"• {name}: 1–{limit:,}".replace(",", " ") for name, limit in collections.items()]
                    await event.respond("Коллекции:\n" + ("\n".join(lines) if lines else "Нет. Добавь через /add"))

                elif command == "/add":
                    if len(args) != 2:
                        raise ValueError("Пример: /add IceCream 342255")
                    name = validate_collection(args[0])
                    limit = validate_max_number(args[1])
                    collections[name] = limit
                    save_collections(collections)
                    await event.respond(f"Добавлено: {name}, номера 1–{limit}.")

                elif command == "/remove":
                    if len(args) != 1 or args[0] not in collections:
                        raise ValueError("Пример: /remove IceCream; название должно быть в /list")
                    collections.pop(args[0])
                    save_collections(collections)
                    await event.respond("Коллекция удалена из поиска.")

                elif command == "/reset":
                    con.execute("DELETE FROM seen")
                    con.execute("DELETE FROM seen_gifts")
                    con.commit()
                    await event.respond("История найденных владельцев и NFT очищена.")

                elif command in {"/random", "/next"}:
                    if not collections:
                        await event.respond("Список коллекций пуст. Используй /add")
                        return
                    selected, count = parse_random(args, collections)
                    if scan_lock.locked():
                        await event.respond("Поиск уже идёт. Подожди немного.")
                        return
                    progress = await event.respond("🔎 Ищу публичных владельцев по NFT-ссылкам…")
                    found = 0
                    async with scan_lock:
                        for _ in range(count):
                            hit = await find_public_hit(selected)
                            if not hit:
                                break
                            await bot.send_message(event.chat_id, describe_hit(hit, TEAM_NAME), parse_mode="html", buttons=hit_buttons(hit), link_preview=False)
                            mark_seen(con, hit)
                            found += 1
                    await progress.edit(f"Готово. Новых владельцев: {found}.")

                else:
                    await event.respond("Неизвестная команда. /help")

            except ValueError as exc:
                await event.respond(f"Ошибка: {exc}")
            except errors.FloodWaitError as exc:
                await event.respond(f"Telegram запросил паузу: {exc.seconds} сек.")
            except Exception:
                logging.exception("Unexpected error in command %s", command)
                await event.respond("Неожиданная ошибка. Подробности в терминале.")

        async def payment_updates_loop():
            offset = int(get_setting(con, "botapi_update_offset", "0") or 0)
            while True:
                try:
                    updates = await bot_api(
                        "getUpdates",
                        offset=offset,
                        timeout=PAYMENT_UPDATE_TIMEOUT,
                        allowed_updates=["pre_checkout_query", "message", "chat_member"],
                    )
                    for update in updates or []:
                        offset = int(update["update_id"]) + 1
                        set_setting(con, "botapi_update_offset", str(offset))

                        pre = update.get("pre_checkout_query")
                        if pre:
                            ok = (
                                pre.get("currency") == "XTR"
                                and int(pre.get("total_amount", 0)) == access_price_stars()
                                and pre.get("invoice_payload") == PAYMENT_PAYLOAD
                            )
                            kwargs = {"pre_checkout_query_id": pre["id"], "ok": ok}
                            if not ok:
                                kwargs["error_message"] = "Этот счёт устарел. Открой /buy и создай новый."
                            await bot_api("answerPreCheckoutQuery", **kwargs)
                            continue

                        member_update = update.get("chat_member")
                        if member_update:
                            chat = member_update.get("chat") or {}
                            if not configured_channel_matches(chat):
                                continue
                            old_member = member_update.get("old_chat_member") or {}
                            new_member = member_update.get("new_chat_member") or {}
                            old_status = old_member.get("status")
                            new_status = new_member.get("status")
                            user = new_member.get("user") or old_member.get("user") or {}
                            uid = int(user.get("id", 0) or 0)
                            if not uid:
                                continue

                            # A real new/re-join starts a fresh 24h timer. Admins/owner are never tracked.
                            joined = old_status in {"left", "kicked"} and new_status in {"member", "restricted"}
                            if joined:
                                remember_channel_join(new_member)
                                continue

                            # If the person leaves or is removed before 24h, stop tracking that membership.
                            if new_status in {"left", "kicked"}:
                                forget_channel_member(uid)
                                continue

                            # Promotion to admin/creator protects the account from the timer.
                            if new_status in {"administrator", "creator"}:
                                forget_channel_member(uid)
                                continue

                        message = update.get("message") or {}
                        payment = message.get("successful_payment")
                        if not payment:
                            continue
                        if payment.get("invoice_payload") != PAYMENT_PAYLOAD or payment.get("currency") != "XTR":
                            continue
                        charge_id = payment.get("telegram_payment_charge_id")
                        if not charge_id:
                            continue
                        if con.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)).fetchone():
                            continue
                        uid = int(message["from"]["id"])
                        amount = int(payment.get("total_amount", 0))
                        con.execute(
                            "INSERT INTO payments(charge_id, user_id, amount, currency, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (charge_id, uid, amount, "XTR", PAYMENT_PAYLOAD, int(time.time())),
                        )
                        con.commit()
                        expiry = grant_subscription(con, uid, ACCESS_DURATION_SECONDS, "stars", charge_id)
                        logging.info("Stars payment: user=%s amount=%s expiry=%s", uid, amount, expiry)
                        try:
                            await send_access_link(uid)
                        except Exception:
                            logging.exception("Payment recorded, but access link delivery failed for %s", uid)
                            await bot_api(
                                "sendMessage", chat_id=uid,
                                text="✅ Оплата получена, 24 часа доступа записаны. Не удалось создать ссылку автоматически — напиши /access через минуту.",
                            )
                        try:
                            await bot_api(
                                "sendMessage", chat_id=admin_id,
                                text=f"⭐ Оплата: {uid} — {amount} Stars. Доступ +24 часа.",
                            )
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("Payment/update loop failed")
                    await asyncio.sleep(3)

        async def channel_member_expiry_loop():
            await asyncio.sleep(3)
            while True:
                try:
                    now = int(time.time())
                    rows = con.execute(
                        "SELECT user_id FROM channel_members WHERE active=1 AND expires_at<=?", (now,)
                    ).fetchall()
                    for (user_id,) in rows:
                        await remove_timed_channel_member(int(user_id))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("24h channel-member loop failed")
                await asyncio.sleep(MEMBER_SWEEP_SECONDS)

        async def autopost_loop():
            await asyncio.sleep(5)
            while True:
                started = time.monotonic()
                interval = max(MIN_INTERVAL, int(get_setting(con, "autopost_interval", str(DEFAULT_AUTOPOST_INTERVAL))))
                if setting_bool(con, "autopost_enabled", True) and get_setting(con, "target_channel", "").strip():
                    if not scan_lock.locked():
                        try:
                            async with scan_lock:
                                hit, status = await post_one_to_channel()
                            if hit:
                                logging.info("Autopost: %s", status)
                            else:
                                logging.info("Autopost skipped: %s", status)
                        except errors.FloodWaitError as exc:
                            logging.warning("Autopost flood wait: %s sec", exc.seconds)
                            interval = max(interval, exc.seconds + 5)
                        except Exception:
                            logging.exception("Autopost iteration failed")
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(1.0, interval - elapsed))

        tasks = [
            asyncio.create_task(autopost_loop(), name="lakes-autopost"),
            asyncio.create_task(payment_updates_loop(), name="lakes-payments"),
            asyncio.create_task(channel_member_expiry_loop(), name="lakes-24h-members"),
        ]
        print(
            f"LAKES TEAM bot запущен; admin={admin_id}; user MTProto={me.id}; "
            f"market_api={MARKET_API_AVAILABLE}; new_members=24h; join_requests=ignored"
        )
        try:
            await bot.run_until_disconnected()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await bot.disconnect()
        await user.disconnect()
        con.close()


if __name__ == "__main__":
    asyncio.run(main())
