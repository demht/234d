"""Small, testable helpers for the Telegram collectible finder."""
from dataclasses import dataclass
from html import escape
import re

SLUG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,39}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


@dataclass(frozen=True)
class Hit:
    owner_id: int
    username: str
    display_name: str
    slug: str
    collection: str
    number: int
    model: str = "—"
    pattern: str = "—"
    backdrop: str = "—"
    source: str = "telegram"  # telegram | market
    price: str | None = None


def validate_collection(collection: str) -> str:
    if not SLUG_RE.fullmatch(collection):
        raise ValueError("Slug коллекции: только латинские буквы и цифры, 2–40 символов. Например IceCream")
    return collection


def validate_max_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("Укажи максимальный номер целым числом, например 342255") from exc
    if not 1 <= number <= 20_000_000:
        raise ValueError("Максимальный номер должен быть от 1 до 20 000 000")
    return number


def pick_username(user) -> str | None:
    """Only public, resolvable Telegram usernames; never return IDs as contact links."""
    primary = getattr(user, "username", None)
    if isinstance(primary, str) and USERNAME_RE.fullmatch(primary):
        return primary
    for entry in getattr(user, "usernames", None) or []:
        name = getattr(entry, "username", None)
        if getattr(entry, "active", False) and isinstance(name, str) and USERNAME_RE.fullmatch(name):
            return name
    return None


def gift_attributes(gift) -> dict[str, str]:
    result = {"model": "—", "pattern": "—", "backdrop": "—"}
    for attribute in getattr(gift, "attributes", None) or []:
        kind = type(attribute).__name__.lower()
        name = getattr(attribute, "name", None)
        if not name or not isinstance(name, str):
            continue
        for label in result:
            if kind.endswith(label):
                result[label] = name
    return result


def _format_stars(amount: int, nanos: int = 0) -> str:
    if nanos:
        value = amount + nanos / 1_000_000_000
        return f"{value:,.3f}".rstrip("0").rstrip(".").replace(",", " ") + " ⭐"
    return f"{amount:,}".replace(",", " ") + " ⭐"


def format_resell_amount(values) -> str | None:
    """Format Telegram Stars/TON resale amount objects without depending on Telethon types."""
    rendered: list[str] = []
    for value in values or []:
        kind = type(value).__name__.lower()
        amount = getattr(value, "amount", None)
        if not isinstance(amount, int):
            continue
        if "ton" in kind:
            ton = amount / 1_000_000_000
            text = f"{ton:,.9f}".rstrip("0").rstrip(".").replace(",", " ")
            rendered.append(f"{text} TON")
        else:
            nanos = getattr(value, "nanos", 0)
            rendered.append(_format_stars(amount, nanos if isinstance(nanos, int) else 0))
    return " / ".join(rendered) or None


def describe_hit(hit: Hit, team_name: str = "LAKES TEAM") -> str:
    """HTML-safe branded channel card styled like the LAKES TEAM reference."""
    collection = escape(hit.collection)
    username = escape(hit.username)
    seller_label = "Продавец" if hit.source == "market" else "Владелец"
    heading = "🆕 <b>Новый NFT на Telegram Market!</b>" if hit.source == "market" else "🆕 <b>Новый NFT в Telegram!</b>"

    details = [
        f"⚪ <b>Название:</b> {collection}",
        f"🎨 <b>Модель:</b> {escape(hit.model)}",
        f"🧩 <b>Символ:</b> {escape(hit.pattern)}",
        f"🖼 <b>Фон:</b> {escape(hit.backdrop)}",
        f"🔢 <b>Номер:</b> #{hit.number}",
    ]

    bottom = []
    if hit.price:
        bottom.append(f"💎 <b>Цена:</b> {escape(hit.price)}")
    bottom.append(f"🛒 <b>{'Маркет' if hit.source == 'market' else 'Источник'}:</b> {'Telegram' if hit.source == 'market' else 'Telegram'}")
    bottom.append(f"👤 <b>{seller_label}:</b> @{username}")

    lines = [
        f"💜 <b>ПАРСЕР — {escape(team_name)}</b>",
        heading,
        "",
        f"<blockquote>💎 <b>{collection} #{hit.number}</b></blockquote>",
        "",
        *details,
        "",
        "<blockquote>" + "\n".join(bottom) + "</blockquote>",
    ]
    if hit.source != "market":
        lines.extend(["", "<i>Публичный владелец на момент проверки; подарок может быть не выставлен на продажу.</i>"])
    return "\n".join(lines)

