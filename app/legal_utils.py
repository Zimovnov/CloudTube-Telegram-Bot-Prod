from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import PUBLIC_OFFER_URL, PUBLIC_PD_CONSENT_URL, PUBLIC_PRIVACY_URL
from app.i18n import t


_LEGAL_DOCS = (
    ("offer", "legal_offer_button", PUBLIC_OFFER_URL),
    ("privacy", "legal_privacy_button", PUBLIC_PRIVACY_URL),
    ("consent", "legal_consent_button", PUBLIC_PD_CONSENT_URL),
)


def get_public_legal_url(kind):
    text = str(kind or "").strip().lower()
    for doc_kind, _, url in _LEGAL_DOCS:
        if doc_kind == text:
            return url
    return ""


def has_public_legal_urls(*kinds):
    if not kinds:
        return any(url for _, _, url in _LEGAL_DOCS)
    return any(get_public_legal_url(kind) for kind in kinds)


def get_public_legal_links(lang, kinds=None):
    selected = {str(kind).strip().lower() for kind in (kinds or ()) if str(kind).strip()}
    items = []
    for doc_kind, label_key, url in _LEGAL_DOCS:
        if selected and doc_kind not in selected:
            continue
        if not url:
            continue
        items.append((doc_kind, t(label_key, lang), url))
    return items


def build_public_legal_markup(lang, kinds=None, row_width=2):
    buttons = [
        InlineKeyboardButton(label, url=url)
        for _, label, url in get_public_legal_links(lang, kinds=kinds)
    ]
    if not buttons:
        return None
    width = max(1, int(row_width))
    rows = [buttons[idx : idx + width] for idx in range(0, len(buttons), width)]
    return InlineKeyboardMarkup(rows)


def extend_markup_with_legal(markup, lang, kinds=None, row_width=2):
    legal_markup = build_public_legal_markup(lang, kinds=kinds, row_width=row_width)
    if legal_markup is None:
        return markup
    rows = []
    if markup is not None and getattr(markup, "inline_keyboard", None):
        rows.extend(markup.inline_keyboard)
    rows.extend(legal_markup.inline_keyboard)
    return InlineKeyboardMarkup(rows)
