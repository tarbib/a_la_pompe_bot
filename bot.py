"""
Prix Carburants Bot - Suivi des prix des carburants en France par code postal ou GPS.

Chaque utilisateur définit ses préférences de localisation (coordonnées GPS + rayon ou codes postaux)
et son type de carburant, puis /prix renvoie les stations correspondantes triées du moins cher au
plus cher. Les préférences sont sauvegardées dans un fichier JSON.
"""

import os
import re
import json
import time
import logging
import requests
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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

# Annuaire de correspondance id de station -> {"name":..., "brand":...}
BRANDS_URL = "https://raw.githubusercontent.com/Aohzan/hass-prixcarburant/master/custom_components/prix_carburant/stations_name.json"
BRANDS_CACHE_TTL = 24 * 3600  # 24h
_brands_cache = {"data": {}, "fetched_at": 0}

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
    return state.setdefault(user_id, {"codes": [], "carburant": None, "lat": None, "lon": None, "radius": 10})


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


def welcome_keyboard():
    """Clavier natif en bas de l'écran pour l'onboarding."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Envoyer ma position (ponctuel)", request_location=True)]],
        resize_keyboard=True,
        input_field_placeholder="Ou tapez /codes 44000..."
    )


def fuel_keyboard():
    codes = list(FUELS.items())
    rows = []
    for i in range(0, len(codes), 2):
        pair = codes[i:i + 2]
        rows.append(
            [InlineKeyboardButton(label["label"], callback_data=f"fuel_{code}") for code, label in pair]
        )
    return InlineKeyboardMarkup(rows)


def radius_keyboard():
    """Clavier Inline pour choisir le rayon d'action après envoi du GPS."""
    buttons = [
        [
            InlineKeyboardButton("2 km", callback_data="radius_2"),
            InlineKeyboardButton("5 km", callback_data="radius_5"),
        ],
        [
            InlineKeyboardButton("10 km", callback_data="radius_10"),
            InlineKeyboardButton("20 km", callback_data="radius_20"),
        ],
        [
            InlineKeyboardButton("50 km", callback_data="radius_50"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def refresh_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Rafraîchir", callback_data="refresh")]])


def find_available_fuels(user):
    """Interroge l'API sans filtrer par carburant pour voir ce qui est réellement
    disponible dans la zone géographique de l'utilisateur."""
    lat = user.get("lat")
    lon = user.get("lon")
    radius = user.get("radius", 10)
    codes = user.get("codes", [])

    if lat is not None and lon is not None:
        where_clause = f"within_distance(geom, geom'POINT({lon} {lat})', {radius}km)"
    elif codes:
        where_codes = " or ".join(f'cp="{cp}"' for cp in codes)
        where_clause = f"({where_codes})"
    else:
        return set()

    prix_fields = ",".join(f["prix"] for f in FUELS.values())

    params = {
        "where": where_clause,
        "select": f"id,{prix_fields}",
        "limit": 15,
        "timezone": "Europe/Paris",
    }

    try:
        response = requests.get(API_URL, params=params, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.warning(f"API request failed: {e}")
        return None

    if response.status_code != 200:
        return None

    results = response.json().get("results", [])
    if not results:
        return set()

    available = set()
    for r in results:
        for code, f in FUELS.items():
            if r.get(f["prix"]) is not None:
                available.add(code)
    return available


def get_station_brands():
    now = time.time()
    if _brands_cache["data"] and (now - _brands_cache["fetched_at"] < BRANDS_CACHE_TTL):
        return _brands_cache["data"]

    try:
        response = requests.get(BRANDS_URL, timeout=10)
        if response.status_code == 200:
            _brands_cache["data"] = response.json()
            _brands_cache["fetched_at"] = now
            logger.info(f"Station brands directory loaded: {len(_brands_cache['data'])} entries")
        else:
            logger.warning(f"Brands directory fetch returned status {response.status_code}")
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.warning(f"Could not refresh station brands directory: {e}")

    return _brands_cache["data"]


def build_prices_message(user, fuel_code):
    """Interroge l'API et construit le message texte selon la position ou les CP."""
    fuel = FUELS[fuel_code]
    prix_field = fuel["prix"]
    maj_field = fuel["maj"]

    lat = user.get("lat")
    lon = user.get("lon")
    radius = user.get("radius", 10)
    codes = user.get("codes", [])

    if lat is not None and lon is not None:
        where_clause = f"within_distance(geom, geom'POINT({lon} {lat})', {radius}km) and {prix_field} is not null"
        location_str = f"dans un rayon de {radius} km"
    elif codes:
        where_codes = " or ".join(f'cp="{cp}"' for cp in codes)
        where_clause = f"({where_codes}) and {prix_field} is not null"
        location_str = f"pour le(s) code(s) postal(aux) : {', '.join(codes)}"
    else:
        return "⚠️ Aucun emplacement défini. Utilisez le bouton en bas ou la commande /codes."

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
        available = find_available_fuels(user)

        if available is None:
            return f"😕 Aucune station trouvée avec du *{fuel['label']}* {location_str}."

        if not available:
            return (
                f"😕 Aucune station-service référencée {location_str}.\n\n"
                f"💡 Si vous utilisez les codes postaux, n'hésitez pas à essayer une commune "
                f"voisine disposant de zones commerciales."
            )

        autres = ", ".join(FUELS[c]["label"] for c in sorted(available))
        message = (
            f"😕 Aucune station ne déclare de *{fuel['label']}* {location_str} en ce moment.\n\n"
            f"⛽ Carburants disponibles dans ce secteur : {autres}."
        )
        if fuel_code == "sp95":
            message += "\n💡 Le SP95 « classique » est de plus en plus remplacé par le E10 dans les stations."
        return message

    medals = ["🥇", "🥈", "🥉"]
    brands = get_station_brands()
    lines = [f"⛽ *Prix du {fuel['label']}* ⛽\n"]

    for i, r in enumerate(results):
        prix = r.get(prix_field)
        if prix is None:
            continue
        adresse = r.get("adresse") or "Adresse inconnue"
        ville = r.get("ville") or ""
        cp = r.get("cp") or ""
        station_id = str(r.get("id") or "")
        info = brands.get(station_id)
        brand = info.get("brand") if info else None
        
        prix_str = f"{prix:.3f}".replace(".", ",")
        marker = medals[i] if i < len(medals) else "▪️"
        prefix = f"{brand} — " if brand else ""
        lines.append(f"{marker} *{prix_str} €* — {prefix}{adresse}, {cp} {ville}")

    lines.append("\n🕒 Données mises à jour toutes les 10 minutes (source : gouvernement).")
    return "\n".join(lines)


# ── Commandes ──────────────────────────────────────────────────────────────────

WELCOME_MESSAGE = (
    "👋 *Bienvenue sur le bot Prix des Carburants !*\n\n"
    "Ce bot vous aide à trouver les stations-services les moins chères autour de vous. "
    "Pour commencer, configurez votre secteur géographique via l'une de ces deux options :\n\n"
    "📍 *Option 1 : La géolocalisation (Recommandé)*\n"
    "Cliquez sur le bouton « `📍 Envoyer ma position (ponctuel)` » juste en bas de votre écran.\n"
    "_🔒 Vie privée : Il s'agit d'un partage unique (one-shot). Le bot utilise votre position instantanée "
    "uniquement pour interroger l'API nationale, puis coupe le flux. Aucun historique ni suivi en arrière-plan._\n\n"
    "📮 *Option 2 : Par Code Postal*\n"
    "Si vous préférez, configurez vos zones manuellement.\n"
    "⚠️ *Attention :* Entrez les codes postaux **des communes où se trouvent les stations-services** "
    "(zones commerciales, grands axes) et pas forcément celui de votre domicile s'il s'agit d'un quartier purement résidentiel sans station !\n"
    "_Exemple :_ `/codes 44000 44600`\n\n"
    "Une fois la zone configurée :\n"
    "1️⃣ Choisissez votre carburant avec /carburant\n"
    "2️⃣ Consultez les prix les plus bas avec /prix\n\n"
    "🔄 À tout moment, changez vos préférences en tapant /reset."
)


async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown", reply_markup=welcome_keyboard())


async def aide_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown", reply_markup=welcome_keyboard())


async def reset_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    user_id = str(update.effective_user.id)
    if user_id in state:
        state[user_id] = {"codes": [], "carburant": None, "lat": None, "lon": None, "radius": 10}
        save_state(state)
    await update.message.reply_text(
        "🗑️ Vos préférences ont été réinitialisées.\n\n" + WELCOME_MESSAGE,
        parse_mode="Markdown",
        reply_markup=welcome_keyboard()
    )


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Intercepte le partage GPS ponctuel et demande le rayon de recherche."""
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    
    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    
    user["lat"] = lat
    user["lon"] = lon
    user["codes"] = []  # On vide les codes postaux pour prioriser le GPS
    save_state(state)
    
    await update.message.reply_text(
        "📍 *Position reçue avec succès !*\n"
        "_(Le flux de géolocalisation est maintenant fermé)._\n\n"
        "Pour affiner la recherche, choisissez le rayon maximal autour de vous :",
        parse_mode="Markdown",
        reply_markup=radius_keyboard()
    )


async def radius_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enregistre le rayon choisi par l'utilisateur."""
    query = update.callback_query
    await query.answer()
    radius_val = int(query.data.replace("radius_", ""))

    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    user["radius"] = radius_val
    save_state(state)

    msg = f"✅ *Rayon de recherche fixé à {radius_val} km.*\n\n"
    if not user["carburant"]:
        msg += "👉 Étape suivante : Choisissez votre carburant avec /carburant."
    else:
        msg += "👉 Parfait ! Utilisez /prix pour voir les tarifs."

    await query.edit_message_text(msg, parse_mode="Markdown")


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
    
    # On ajoute les nouveaux codes et on nettoie les données GPS obsolètes
    user["codes"] = list(set(user["codes"] + valid))
    user["lat"] = None
    user["lon"] = None
    save_state(state)

    message = f"✅ Codes postaux enregistrés : {', '.join(user['codes'])}"
    if invalid:
        message += f"\n⚠️ Ignorés (invalides) : {', '.join(invalid)}"
        
    if not user["carburant"]:
        message += "\n\n👉 Choisissez maintenant votre carburant avec /carburant."
    else:
        message += "\n\n👉 Utilisez /prix pour voir les tarifs."
        
    await update.message.reply_text(message)


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
    fuel_code = user["carburant"]

    if not user["codes"] and (user["lat"] is None or user["lon"] is None):
        await update.message.reply_text(
            "⚠️ Vous n'avez pas encore défini votre localisation.\n"
            "Utilisez le bouton en bas pour envoyer votre position ou configurez vos codes postaux avec /codes.",
            parse_mode="Markdown",
        )
        return

    if not fuel_code:
        await update.message.reply_text(
            "⚠️ Vous n'avez pas encore choisi de carburant.\nUtilisez /carburant."
        )
        return

    message = build_prices_message(user, fuel_code)
    await update.message.reply_text(message, parse_mode="Markdown", reply_markup=refresh_keyboard())


async def refresh_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    state = load_state()
    user = get_user(state, str(update.effective_user.id))
    fuel_code = user["carburant"]

    if (not user["codes"] and (user["lat"] is None or user["lon"] is None)) or not fuel_code:
        await query.answer("Configurez votre zone géographique et votre carburant d'abord.", show_alert=True)
        return

    message = build_prices_message(user, fuel_code)

    try:
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=refresh_keyboard())
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer("Aucun changement depuis la dernière actualisation.")
        else:
            await query.answer("Erreur lors de la mise à jour des prix.", show_alert=True)


async def fallback_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Si l'utilisateur envoie juste des codes postaux sans la commande /codes."""
    tokens = update.message.text.split()
    valid, invalid = parse_postal_codes(tokens)

    if valid:
        state = load_state()
        user = get_user(state, str(update.effective_user.id))
        user["codes"] = list(set(user["codes"] + valid))
        user["lat"] = None
        user["lon"] = None
        save_state(state)

        message = f"✅ Codes postaux enregistrés : {', '.join(user['codes'])}"
        if invalid:
            message += f"\n⚠️ Ignorés (invalides) : {', '.join(invalid)}"
            
        if not user["carburant"]:
            message += "\n\n👉 Choisissez maintenant votre carburant avec /carburant."
        else:
            message += "\n\n👉 Utilisez /prix pour voir les tarifs."
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
    app.add_handler(CallbackQueryHandler(radius_button, pattern="^radius_"))
    app.add_handler(CallbackQueryHandler(refresh_button, pattern="^refresh$"))

    # Handler pour la géolocalisation
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    # Handler pour le texte brut brut (fallback)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()