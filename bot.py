"""
Prix Carburants Bot - Suivi des prix des carburants en France par code postal.

Chaque utilisateur définit ses codes postaux et son type de carburant,
puis /prix renvoie les stations correspondantes triées du moins cher au
plus cher. Les préférences sont sauvegardées dans un fichier JSON.
"""

import os
import re
import json
import logging
import requests
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest

# ── Configuration ─────────────────────────────────────────────────────────────

STATE_FILE = Path("/app/data/state.json")

API_URL = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/prix-des-carburants-en-france-flux-instantane-v2/records"

# Nom du carburant -> (libellé affiché, champ prix, champ date de maj)
FUELS = {
    "gazole": {"label": "Gazole", "prix": "gazole_prix", "maj": "gazole_maj"},
    "sp95": {"label": "SP95", "prix": "sp95_prix", "maj": "sp95_maj"},
    "sp98": {"label": "SP98", "prix": "sp98_prix", "maj": "sp98_maj"},
    "e10": {"label": "E10", "prix": "e10_prix", "maj": "e10_maj"},
    "e85": {"label": "E85 (Superéthanol)", "prix": "e85_prix", "maj": "e85_maj"},
    "gplc": {"label": "GPLc", "prix": "gplc_prix", "maj": "gplc_maj"},
}

CP_REGEX = re.compile(r"^\d{5}$")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Data helpers ───────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    """Read a JSON file, returning `default` if it doesn't exist or is broken."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Could not read {path}: {e}")
        return default


def save_json(path: Path, data):
    """Write data to a JSON file using a temp file to avoid corruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(path)
    except IOError as e:
        logger.error(f"Could not save {path}: {e}")
        tmp.unlink(missing_ok=True)


def load_state():
    return load_json(STATE_FILE, {})


def save_state(data):
    save_json(STATE_FILE, data)


def get_user(state: dict, user_id: str) -> dict:
    """Return (and create if needed) a user's entry in state."""
    return state.setdefault(user_id, {"codes": [], "carburant": None})


# ── Helpers métier ─────────────────────────────────────────────────────────────

def parse_postal_codes(raw_tokens):
    """Sépare/valide une liste de tokens en codes postaux à 5 chiffres."""
    valid, invalid = [], []
    for token in raw_tokens:
        for piece in token.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if CP_REGEX.match(piece):
                if piece not in valid:
                    valid.append(piece)
            else:
                invalid.append(piece)
    return valid, invalid


def fuel_keyboard():
    codes = list(FUELS.items())
    rows = []
    for i in range(0, len(codes), 2):
        pair = codes[i:i + 2]
        rows.append(
            [InlineKeyboardButton(label["label"], callback_data=f"fuel_{code}") for code, label in pair]
        )
    return InlineKeyboardMarkup(rows)


def refresh_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Rafraîchir", callback_data="refresh")]])


def build_prices_message(codes, fuel_code):
    """Interroge l'API et construit le message texte trié du moins cher au plus cher."""
    fuel = FUELS[fuel_code]
    prix_field = fuel["prix"]
    maj_field = fuel["maj"]

    where_codes = " or ".join(f'cp="{cp}"' for cp in codes)
    where_clause = f"({where_codes}) and {prix_field} is not null"

    params = {
        "where": where_clause,
        "select": f"id,cp,adresse,ville,{prix_field},{maj_field}",
        "order_by": f"{prix_field} ASC",
        "limit": 100,
        "timezone": "Europe/Paris",
    }

    try:
        response = requests.get(API_URL, params=params, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.warning(f"API request failed: {e}")
        return "❌ Impossible de contacter l'API du gouvernement. Réessayez plus tard."

    if response.status_code != 200:
        return "❌ Échec de récupération des données depuis l'API."

    results = response.json().get("results", [])

    if not results:
        codes_str = ", ".join(codes)
        return (
            f"😕 Aucune station trouvée avec du *{fuel['label']}* "
            f"pour le(s) code(s) postal(aux) : {codes_str}."
        )

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"⛽ *Prix du {fuel['label']}* ⛽\n"]

    for i, r in enumerate(results):
        prix = r.get(prix_field)
        if prix is None:
            continue
        adresse = r.get("adresse") or "Adresse inconnue"
        ville = r.get("ville") or ""
        cp = r.get("cp") or ""
        prix_str = f"{prix:.3f}".replace(".", ",")
        marker = medals[i] if i < len(medals) else "▪️"
        lines.append(f"{marker} *{prix_str} €* — {adresse}, {cp} {ville}")

    lines.append("\n🕒 Données mises à jour toutes les 10 minutes (source : gouvernement).")
    return "\n".join(lines)


# ── Commandes ──────────────────────────────────────────────────────────────────

WELCOME_MESSAGE = (
    "👋 *Bienvenue sur le bot Prix des Carburants !*\n\n"
    "Ce bot vous permet de suivre les prix des carburants en France, "
    "par code postal, grâce aux données officielles du gouvernement.\n\n"
    "*Pour commencer :*\n"
    "1️⃣ Définissez vos codes postaux avec /codes\n"
    "   _Exemple :_ `/codes 44000 44600`\n"
    "2️⃣ Choisissez votre carburant avec /carburant\n"
    "3️⃣ Consultez les prix avec /prix\n\n"
    "🔄 Pour réinitialiser vos codes postaux : /reset\n"
    "❓ Pour revoir ces instructions : /aide"
)


async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown")


async def aide_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown")


async def set_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            "⚠️ Merci d'indiquer un ou plusieurs codes postaux.\n"
            "_Exemple :_ `/codes 44000 44600`",
            parse_mode="Markdown",
        )
        return

    valid, invalid = parse_postal_codes(ctx.args)
    if not valid:
        await update.message.reply_text("❌ Aucun code postal valide détecté (5 chiffres attendus).")
        return

    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    user["codes"] = user["codes"] + [c for c in valid if c not in user["codes"]]
    save_state(state)

    message = f"✅ Codes postaux enregistrés : {', '.join(user['codes'])}"
    if invalid:
        message += f"\n⚠️ Ignorés (invalides) : {', '.join(invalid)}"
    await update.message.reply_text(message)


async def reset_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    user["codes"] = []
    save_state(state)
    await update.message.reply_text("🗑️ Vos codes postaux ont été réinitialisés.")


async def choose_carburant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⛽ Choisissez votre type de carburant :",
        reply_markup=fuel_keyboard(),
    )


async def fuel_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    fuel_code = query.data.replace("fuel_", "")

    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    user["carburant"] = fuel_code
    save_state(state)

    label = FUELS[fuel_code]["label"]
    await query.edit_message_text(
        f"✅ Carburant sélectionné : *{label}*\n\nUtilisez /prix pour voir les tarifs.",
        parse_mode="Markdown",
    )


async def prix_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    codes = user["codes"]
    fuel_code = user["carburant"]

    if not codes:
        await update.message.reply_text(
            "⚠️ Vous n'avez pas encore défini de code postal.\n"
            "Utilisez /codes, par exemple : `/codes 44000 44600`",
            parse_mode="Markdown",
        )
        return

    if not fuel_code:
        await update.message.reply_text(
            "⚠️ Vous n'avez pas encore choisi de carburant.\nUtilisez /carburant."
        )
        return

    message = build_prices_message(codes, fuel_code)
    await update.message.reply_text(message, parse_mode="Markdown", reply_markup=refresh_keyboard())


async def refresh_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    codes = user["codes"]
    fuel_code = user["carburant"]

    if not codes or not fuel_code:
        await query.answer("Configurez vos codes postaux et votre carburant d'abord.", show_alert=True)
        return

    message = build_prices_message(codes, fuel_code)

    try:
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=refresh_keyboard())
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer("Aucun changement depuis la dernière actualisation.")
        else:
            raise


async def fallback_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Si l'utilisateur envoie juste des codes postaux sans la commande /codes."""
    tokens = update.message.text.split()
    valid, invalid = parse_postal_codes(tokens)

    if valid:
        state = load_state()
        user = get_user(state, str(update.effective_user.id))
        user["codes"] = user["codes"] + [c for c in valid if c not in user["codes"]]
        save_state(state)

        message = f"✅ Codes postaux enregistrés : {', '.join(user['codes'])}"
        if invalid:
            message += f"\n⚠️ Ignorés (invalides) : {', '.join(invalid)}"
        await update.message.reply_text(message)
    else:
        await update.message.reply_text(
            "Je n'ai pas compris. Tapez /aide pour voir comment utiliser le bot."
        )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN environment variable not set.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("aide", aide_command))
    app.add_handler(CommandHandler("codes", set_codes))
    app.add_handler(CommandHandler("reset", reset_codes))
    app.add_handler(CommandHandler("carburant", choose_carburant))
    app.add_handler(CommandHandler("prix", prix_command))

    app.add_handler(CallbackQueryHandler(fuel_button, pattern="^fuel_"))
    app.add_handler(CallbackQueryHandler(refresh_button, pattern="^refresh$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
