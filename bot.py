from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cloudinary
import cloudinary.uploader
from flask import Flask
from github import Github, GithubException, UnknownObjectException
from slugify import slugify
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


SECRET_VALUES: set[str] = set()
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOGS_DIR = BASE_DIR / "logs"
PRODUCTS_PATH = OUTPUT_DIR / "products.json"
CSV_PATH = OUTPUT_DIR / "meta_catalog.csv"

REQUIRED_CONFIG_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)
GITHUB_CONFIG_KEYS = (
    "GITHUB_TOKEN",
    "GITHUB_REPO_OWNER",
    "GITHUB_REPO_NAME",
    "GITHUB_BRANCH",
)

DRAFT_STAGE_PHOTOS = "photos"
DRAFT_STAGE_CONFIRMATION = "confirmation"
DELETE_STAGE_QUERY = "query"
DELETE_STAGE_SELECT = "select"
DELETE_STAGE_CONFIRM = "confirm"
PRICE_STAGE_QUERY = "query"
PRICE_STAGE_SELECT = "select"
PRICE_STAGE_PRICE = "price"
PRICE_STAGE_CONFIRM = "confirm"
STOCK_PAGE_SIZE = 20
flask_app = Flask(__name__)
telegram_thread: threading.Thread | None = None
telegram_thread_lock = threading.Lock()

CSV_COLUMNS = [
    "id",
    "title",
    "description",
    "availability",
    "condition",
    "price",
    "brand",
    "image_link",
    "additional_image_link",
]

FIELD_ALIASES = {
    "titre": "title",
    "title": "title",
    "nom": "title",
    "produit": "title",
    "description": "description",
    "desc": "description",
    "prix": "price",
    "price": "price",
    "marque": "brand",
    "brand": "brand",
    "lien": "link",
    "link": "link",
    "url": "link",
    "disponibilite": "availability",
    "disponibilité": "availability",
    "availability": "availability",
    "etat": "condition",
    "état": "condition",
    "condition": "condition",
    "categorie": "google_product_category",
    "catégorie": "google_product_category",
    "category": "google_product_category",
}

BRANDS = ("Dell", "HP", "Lenovo", "Microsoft", "Apple", "Asus", "Acer")
IGNORED_ANNOUNCEMENT_LINES = {
    "nouvel arrivage",
    "stock disponible",
}

CURRENCY_ALIASES = {
    "fcfa": "XOF",
    "cfa": "XOF",
    "xof": "XOF",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "€": "EUR",
    "usd": "USD",
    "$": "USD",
}


def redact_secrets(value: Any) -> str:
    text = str(value)
    for secret in SECRET_VALUES:
        if secret:
            text = text.replace(secret, "[SECRET]")
    return text


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_secrets(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    arg if isinstance(arg, (int, float)) else redact_secrets(arg)
                    for arg in record.args
                )
        return True


def register_config_secrets(config: dict[str, Any]) -> None:
    for key in (*REQUIRED_CONFIG_KEYS, *GITHUB_CONFIG_KEYS, "PUBLIC_CATALOG_URL"):
        value = config.get(key)
        if value:
            SECRET_VALUES.add(str(value))


def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    redacting_filter = SecretRedactingFilter()
    handlers = [
        logging.FileHandler(LOGS_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
    for handler in handlers:
        handler.addFilter(redacting_filter)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def load_config() -> dict[str, Any]:
    config_keys = (
        *REQUIRED_CONFIG_KEYS,
        *GITHUB_CONFIG_KEYS,
        "PUBLIC_CATALOG_URL",
        "CLOUDINARY_FOLDER",
    )
    config = {key: os.environ.get(key, "").strip() for key in config_keys}
    missing = [key for key in REQUIRED_CONFIG_KEYS if not config.get(key)]
    if missing:
        raise ValueError(f"Variables d'environnement manquantes: {', '.join(missing)}")

    return config


def configure_cloudinary(config: dict[str, Any]) -> None:
    cloudinary.config(
        cloud_name=config["CLOUDINARY_CLOUD_NAME"],
        api_key=config["CLOUDINARY_API_KEY"],
        api_secret=config["CLOUDINARY_API_SECRET"],
        secure=True,
    )


def ensure_runtime_dirs() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def load_products() -> list[dict[str, Any]]:
    if not PRODUCTS_PATH.exists():
        return []
    with PRODUCTS_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_products(products: list[dict[str, Any]]) -> None:
    with PRODUCTS_PATH.open("w", encoding="utf-8") as file:
        json.dump(products, file, ensure_ascii=False, indent=2)


def normalize_price(value: str, default_currency: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""

    price_match = re.search(
        r"(?P<amount>\d[\d\s.,]*)\s*(?P<currency>fcfa|cfa|xof|eur|euros?|€|usd|\$)?",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not price_match:
        return cleaned

    amount = price_match.group("amount").replace(" ", "").replace(",", ".")
    currency_raw = (price_match.group("currency") or default_currency).lower()
    currency = CURRENCY_ALIASES.get(currency_raw, default_currency.upper())
    return f"{amount} {currency}"


def extract_price_from_text(text: str, default_currency: str) -> str:
    price_line = next(
        (
            line
            for line in text.splitlines()
            if re.search(r"\b(prix|price)\b|fcfa|cfa|xof|eur|€|usd|\$", line, re.I)
        ),
        "",
    )
    return normalize_price(price_line, default_currency) if price_line else ""


def clean_announcement_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^[\s▪▫•●\-–—]+", "", cleaned)
    cleaned = cleaned.replace("\ufe0f", "")
    cleaned = cleaned.strip("*_`~ \t")
    return re.sub(r"\s+", " ", cleaned).strip()


def normalized_lookup_text(value: str) -> str:
    return slugify(value, separator=" ")


def is_ignored_announcement_line(line: str) -> bool:
    return normalized_lookup_text(line) in IGNORED_ANNOUNCEMENT_LINES


def extract_announcement_lines(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        line = clean_announcement_line(raw_line)
        if line and not is_ignored_announcement_line(line):
            lines.append(line)
    return lines


def find_brand(text: str) -> str:
    for brand in BRANDS:
        if re.search(rf"\b{re.escape(brand)}\b", text, flags=re.IGNORECASE):
            return "HP" if brand.lower() == "hp" else brand
    return ""


def normalize_ram(value: str) -> str:
    match = re.search(r"(\d+)\s*(?:g|gb|go)\b", value, flags=re.IGNORECASE)
    if not match:
        return value.strip()
    return f"{int(match.group(1))}GB"


def normalize_storage(value: str) -> str:
    size_match = re.search(r"(\d+)\s*(?:g|gb|go|t|tb|to)\b", value, flags=re.IGNORECASE)
    disk_match = re.search(r"\b(ssd|hdd|nvme|emmc)\b", value, flags=re.IGNORECASE)
    if not size_match:
        return value.strip()

    size = int(size_match.group(1))
    unit = size_match.group(0).lower()
    normalized_unit = "TB" if any(token in unit for token in ("t", "tb", "to")) else "GB"
    disk_type = disk_match.group(1).upper() if disk_match else ""
    capacity = f"{size}{normalized_unit}"
    return f"{disk_type} {capacity}".strip()


def normalize_numeric_price(value: str) -> str:
    match = re.search(r"(\d[\d\s.,]*)", value)
    if not match:
        return ""
    digits = re.sub(r"\D", "", match.group(1))
    return str(int(digits)) if digits else ""


def line_value(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else line.strip()


def lowercase_first(value: str) -> str:
    if not value:
        return value
    return value[0].lower() + value[1:]


def normalize_processor_description(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\bcore\b", "Core", text, flags=re.IGNORECASE)
    text = re.sub(r"\bgénération\b", "génération", text, flags=re.IGNORECASE)
    text = re.sub(r"\bgeneration\b", "génération", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_screen_description(value: str) -> str:
    text = re.sub(r"^é?e?cran\s*:?\s*", "", value.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^screen\s*:?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\btactile\b", "tactile", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return f"écran {text}" if text else ""


def normalize_keyboard_description(value: str) -> str:
    text = re.sub(r"^clavier\s*:?\s*", "", value.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^keyboard\s*:?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\br[ée]tro(?:eclair[ée]|éclairé)?\b", "rétroéclairé", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return f"clavier {text}" if text else ""


def normalize_security_description(value: str) -> str:
    lookup = normalized_lookup_text(value)
    has_fingerprint = "empreinte" in lookup
    has_face_id = "face id" in lookup or "reconnaissance faciale" in lookup

    if has_fingerprint and has_face_id:
        return "lecteur d'empreintes digitales et reconnaissance faciale"
    if has_fingerprint:
        return "lecteur d'empreintes digitales"
    if has_face_id:
        return "reconnaissance faciale"

    text = re.sub(r"^s[ée]curit[ée]\s*:?\s*", "", value.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^avec option\s+", "", text, flags=re.IGNORECASE)
    return lowercase_first(re.sub(r"\s+", " ", text).strip())


def normalize_autonomy_description(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    return lowercase_first(text)


def generate_meta_description(product_data: dict[str, Any]) -> str:
    parts = []

    if product_data.get("processor"):
        parts.append(normalize_processor_description(str(product_data["processor"])))
    if product_data.get("ram"):
        parts.append(f"RAM {product_data['ram']}")
    if product_data.get("storage"):
        parts.append(str(product_data["storage"]))
    if product_data.get("screen"):
        parts.append(normalize_screen_description(str(product_data["screen"])))
    if product_data.get("keyboard"):
        parts.append(normalize_keyboard_description(str(product_data["keyboard"])))
    if product_data.get("security"):
        parts.append(normalize_security_description(str(product_data["security"])))
    if product_data.get("autonomy"):
        parts.append(normalize_autonomy_description(str(product_data["autonomy"])))

    cleaned_parts = [part for part in parts if part]
    if not cleaned_parts:
        return ""
    return f"{', '.join(cleaned_parts)}."


def parse_whatsapp_supplier_announcement(text: str) -> dict[str, Any]:
    lines = extract_announcement_lines(text)
    title = lines[0] if lines else ""
    searchable_text = "\n".join(lines)
    screen = ""

    data: dict[str, Any] = {
        "title": title,
        "brand": find_brand(title) or find_brand(searchable_text),
        "processor": "",
        "ram": "",
        "storage": "",
        "screen": "",
        "keyboard": "",
        "security": "",
        "autonomy": "",
        "price": "",
        "touchscreen": False,
    }

    for line in lines:
        lookup = normalized_lookup_text(line)
        value = line_value(line)

        if re.search(r"\b(intel|amd|core\s*i[3579]|ryzen|celeron|pentium|m[1234])\b", line, re.I):
            data["processor"] = value
        elif lookup.startswith("ram") or re.search(r"\b\d+\s*(?:g|gb|go)\b", line, re.I) and "ram" in lookup:
            data["ram"] = normalize_ram(value)
        elif "stockage" in lookup or re.search(r"\b(ssd|hdd|nvme|emmc)\b", line, re.I):
            data["storage"] = normalize_storage(value)
        elif any(token in lookup for token in ("ecran", "screen", "pouces", "full hd")):
            screen = value
            data["screen"] = screen
        elif "clavier" in lookup or "keyboard" in lookup:
            data["keyboard"] = value
        elif "securite" in lookup or "security" in lookup or "empreinte" in lookup or "face id" in lookup:
            data["security"] = value
        elif "autonomie" in lookup or "battery" in lookup:
            data["autonomy"] = value
        elif "prix" in lookup or re.search(r"\b\d[\d\s.,]*(?:frs?|fcfa|cfa|xof)\b", line, re.I):
            data["price"] = normalize_numeric_price(value)

    tactile_text = f"{title} {screen}"
    data["touchscreen"] = bool(re.search(r"\b(tactile|touch|touchscreen)\b", tactile_text, re.I))
    return data


def parse_announcement(text: str, config: dict[str, Any]) -> dict[str, Any]:
    catalog_config = config.get("catalog", {})
    default_currency = catalog_config.get("default_currency", "XOF")
    supplier_data = parse_whatsapp_supplier_announcement(text)
    defaults = {
        "title": "",
        "description": "",
        "availability": catalog_config.get("default_availability", "in stock"),
        "condition": catalog_config.get("default_condition", "new"),
        "price": "",
        "link": catalog_config.get("default_product_link", ""),
        "brand": catalog_config.get("default_brand", ""),
        "google_product_category": catalog_config.get("default_google_product_category", ""),
        "fb_product_category": catalog_config.get("default_fb_product_category", ""),
        "processor": "",
        "ram": "",
        "storage": "",
        "screen": "",
        "keyboard": "",
        "security": "",
        "autonomy": "",
        "touchscreen": False,
    }

    data = defaults.copy()
    data.update({key: value for key, value in supplier_data.items() if value not in ("", None)})
    free_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            normalized_key = FIELD_ALIASES.get(key.strip().lower())
            if normalized_key:
                if normalized_key == "price":
                    data[normalized_key] = supplier_data.get("price") or normalize_price(value, default_currency)
                else:
                    data[normalized_key] = value.strip()
                continue
        free_lines.append(line)

    clean_free_lines = extract_announcement_lines("\n".join(free_lines))

    if not data["title"] and clean_free_lines:
        data["title"] = clean_free_lines[0]

    if not data["description"]:
        description_lines = clean_free_lines[1:] if data["title"] and clean_free_lines else clean_free_lines
        data["description"] = "\n".join(description_lines).strip()

    if not data["price"]:
        data["price"] = supplier_data.get("price") or extract_price_from_text(text, default_currency)

    if data["title"] and not data["description"]:
        data["description"] = data["title"]

    meta_description = generate_meta_description(data)
    if meta_description:
        data["description"] = meta_description

    return data


def validation_errors(product_data: dict[str, Any]) -> list[str]:
    errors = []
    if not str(product_data.get("title", "")).strip():
        errors.append("titre manquant")
    if not re.sub(r"\D", "", str(product_data.get("price", ""))):
        errors.append("prix manquant")
    if not str(product_data.get("ram", "")).strip():
        errors.append("RAM absente")
    if not str(product_data.get("storage", "")).strip():
        errors.append("stockage absent")
    return errors


def validation_message(errors: list[str]) -> str:
    return (
        "Annonce incomplète. Corrige puis renvoie l'annonce fournisseur.\n"
        "Champs à compléter : "
        + ", ".join(errors)
    )


def remove_known_specs_from_model(title: str, brand: str) -> str:
    model = title
    if brand:
        model = re.sub(rf"\b{re.escape(brand)}\b", "", model, flags=re.IGNORECASE)
    model = re.sub(r"\b(tactile|touch|touchscreen)\b", "", model, flags=re.IGNORECASE)
    model = re.sub(r"\b\d+\s*(?:g|gb|go|t|tb|to)\b", "", model, flags=re.IGNORECASE)
    model = re.sub(r"\b(ssd|hdd|nvme|emmc)\b", "", model, flags=re.IGNORECASE)
    model = re.sub(r"\s+", " ", model)
    return model.strip()


def processor_id_part(processor: str) -> str:
    lookup = normalized_lookup_text(processor)
    generation_match = re.search(r"\b(i[3579])\b.*?\b(\d{1,2})(?:e|eme|th)?\b", lookup)
    if generation_match:
        return f"{generation_match.group(1)}-{generation_match.group(2)}"

    model_match = re.search(r"\b(i[3579])[- ]?(\d{2})\d{2,4}\b", lookup)
    if model_match:
        return f"{model_match.group(1)}-{model_match.group(2)}"

    return slugify(processor)


def ram_id_part(ram: str) -> str:
    match = re.search(r"(\d+)\s*(?:g|gb|go)\b", ram, flags=re.IGNORECASE)
    if not match:
        return slugify(ram)
    return f"{int(match.group(1))}gb"


def storage_id_part(storage: str) -> str:
    match = re.search(r"(\d+)\s*(gb|go|tb|to)\b", storage, flags=re.IGNORECASE)
    if not match:
        return slugify(storage)
    unit = "tb" if match.group(2).lower() in {"tb", "to"} else "gb"
    return f"{int(match.group(1))}{unit}"


def build_product_id_base(product_data: dict[str, Any]) -> str:
    title = str(product_data.get("title") or "")
    brand = str(product_data.get("brand") or find_brand(title))
    model = remove_known_specs_from_model(title, brand)

    parts = [
        brand,
        model,
        processor_id_part(str(product_data.get("processor") or "")),
        ram_id_part(str(product_data.get("ram") or "")),
        storage_id_part(str(product_data.get("storage") or "")),
    ]
    if product_data.get("touchscreen"):
        parts.append("touch")

    slug_parts = [slugify(part) for part in parts if slugify(part)]
    return "-".join(slug_parts) or "produit"


def build_unique_product_id(product_data: dict[str, Any], existing_products: list[dict[str, Any]]) -> str:
    base_id = build_product_id_base(product_data)
    existing_ids = {str(product.get("id")) for product in existing_products}
    if base_id not in existing_ids:
        return base_id

    suffix = 2
    while f"{base_id}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base_id}-{suffix}"


def draft_for(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    return context.user_data.get("draft_product")


def format_product_summary(product_data: dict[str, Any], images_count: int) -> str:
    touchscreen = "Oui" if product_data.get("touchscreen") else "Non"
    return (
        "Récapitulatif du produit:\n"
        f"Titre: {product_data.get('title') or 'Non détecté'}\n"
        f"Marque: {product_data.get('brand') or 'Non renseignée'}\n"
        f"Processeur: {product_data.get('processor') or 'Non détecté'}\n"
        f"RAM: {product_data.get('ram') or 'Non détectée'}\n"
        f"Stockage: {product_data.get('storage') or 'Non détecté'}\n"
        f"Écran: {product_data.get('screen') or 'Non détecté'}\n"
        f"Clavier: {product_data.get('keyboard') or 'Non détecté'}\n"
        f"Sécurité: {product_data.get('security') or 'Non détectée'}\n"
        f"Autonomie: {product_data.get('autonomy') or 'Non détectée'}\n"
        f"Tactile: {touchscreen}\n"
        f"Prix: {product_data.get('price') or 'Non détecté'}\n"
        f"Description: {product_data.get('description') or 'Non détectée'}\n"
        f"Photos: {images_count}\n\n"
        "Réponds OUI pour publier ou NON pour annuler."
    )


def build_product(product_data: dict[str, Any], image_urls: list[str], product_id: str) -> dict[str, Any]:
    return {
        "id": product_id,
        **product_data,
        "availability": "in stock",
        "condition": "new",
        "image_link": image_urls[0],
        "additional_image_link": ",".join(image_urls[1:]),
        "images_count": len(image_urls),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def save_product(product: dict[str, Any], products: list[dict[str, Any]] | None = None) -> None:
    if products is None:
        products = load_products()
    products.append(product)
    save_products(products)


def format_meta_price(price: Any) -> str:
    digits = re.sub(r"\D", "", str(price or ""))
    return f"{int(digits)} XOF" if digits else ""


def meta_csv_row(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": product.get("id", ""),
        "title": product.get("title", ""),
        "description": product.get("description", ""),
        "availability": "in stock",
        "condition": "new",
        "price": format_meta_price(product.get("price")),
        "brand": product.get("brand", ""),
        "image_link": product.get("image_link", ""),
        "additional_image_link": product.get("additional_image_link", ""),
    }


def append_meta_csv_row(product: dict[str, Any]) -> None:
    file_exists = CSV_PATH.exists() and CSV_PATH.stat().st_size > 0
    encoding = "utf-8" if file_exists else "utf-8-sig"
    with CSV_PATH.open("a", encoding=encoding, newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(meta_csv_row(product))


def write_meta_csv_from_products(products: list[dict[str, Any]]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for product in products:
            writer.writerow(meta_csv_row(product))


def load_meta_csv_rows() -> list[dict[str, str]]:
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        return []

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def write_meta_csv_rows(rows: list[dict[str, Any]]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})


def count_csv_products() -> int:
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        return 0

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return sum(1 for _ in csv.DictReader(csv_file))


def public_catalog_url(config: dict[str, Any]) -> str:
    configured_url = str(config.get("PUBLIC_CATALOG_URL", "")).strip()
    if configured_url:
        return configured_url

    owner = str(config.get("GITHUB_REPO_OWNER", "")).strip()
    repo_name = str(config.get("GITHUB_REPO_NAME", "")).strip()
    return f"https://{owner}.github.io/{repo_name}/meta_catalog.csv"


def github_config(config: dict[str, Any]) -> dict[str, str]:
    missing = [key for key in GITHUB_CONFIG_KEYS if not str(config.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"Configuration GitHub incomplète: {', '.join(missing)}")

    return {
        "token": str(config["GITHUB_TOKEN"]),
        "owner": str(config["GITHUB_REPO_OWNER"]),
        "repo_name": str(config["GITHUB_REPO_NAME"]),
        "branch": str(config["GITHUB_BRANCH"]),
        "public_url": public_catalog_url(config),
    }


def publish_catalog_to_github(config: dict[str, Any]) -> str:
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        raise RuntimeError("Catalogue introuvable.")

    github_settings = github_config(config)
    csv_content = CSV_PATH.read_text(encoding="utf-8-sig")
    repo_full_name = f"{github_settings['owner']}/{github_settings['repo_name']}"
    github_client = Github(github_settings["token"])

    try:
        repo = github_client.get_repo(repo_full_name)
        try:
            existing_file = repo.get_contents("meta_catalog.csv", ref=github_settings["branch"])
        except UnknownObjectException:
            repo.create_file(
                "meta_catalog.csv",
                "Update Meta catalog CSV",
                csv_content,
                branch=github_settings["branch"],
            )
        else:
            repo.update_file(
                "meta_catalog.csv",
                "Update Meta catalog CSV",
                csv_content,
                existing_file.sha,
                branch=github_settings["branch"],
            )
    except GithubException as error:
        logging.error("Erreur GitHub: %s", redact_secrets(error))
        message = error.data.get("message", str(error)) if isinstance(error.data, dict) else str(error)
        raise RuntimeError(f"GitHub API: {error.status} {message}") from error
    finally:
        github_client.close()

    return github_settings["public_url"]


async def send_github_publish_success(message: Any, config: dict[str, Any], label: str) -> bool:
    try:
        url = publish_catalog_to_github(config)
    except RuntimeError as error:
        await message.reply_text(f"❌ Publication échouée : {redact_secrets(error)}")
        return False

    await message.reply_text(f"{label}\n🔗 {url}")
    return True


def product_lookup_by_id() -> dict[str, dict[str, Any]]:
    return {str(product.get("id")): product for product in load_products()}


def extract_ram_from_description(description: str) -> str:
    match = re.search(r"\bRAM\s+(\d+\s*GB)\b", description, flags=re.IGNORECASE)
    return match.group(1).replace(" ", "").upper() if match else ""


def extract_storage_from_description(description: str) -> str:
    match = re.search(r"\b((?:SSD|HDD|NVME|EMMC)\s+\d+\s*(?:GB|TB))\b", description, flags=re.IGNORECASE)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1).upper()).replace(" GB", "GB").replace(" TB", "TB")


def product_details_for_csv_row(row: dict[str, Any], products_by_id: dict[str, dict[str, Any]]) -> dict[str, str]:
    product = products_by_id.get(str(row.get("id")), {})
    description = str(row.get("description", ""))
    return {
        "ram": str(product.get("ram") or extract_ram_from_description(description) or "Non renseignée"),
        "storage": str(product.get("storage") or extract_storage_from_description(description) or "Non renseigné"),
    }


def delete_row_matches(row: dict[str, Any], search_text: str) -> bool:
    haystack = " ".join(
        str(row.get(field, ""))
        for field in ("title", "id", "brand", "description")
    )
    return search_text in haystack.casefold()


def format_delete_candidate(index: int, row: dict[str, Any], products_by_id: dict[str, dict[str, Any]]) -> str:
    details = product_details_for_csv_row(row, products_by_id)
    title = row.get("title") or row.get("id") or "Sans titre"
    price = row.get("price") or "Non renseigné"
    storage = re.sub(r"^(?:SSD|HDD|NVME|EMMC)\s+", "", details["storage"], flags=re.IGNORECASE)
    return f"{index}. {title} — {details['ram']} — {storage} — {price}"


def format_price_candidate(index: int, row: dict[str, Any], products_by_id: dict[str, dict[str, Any]]) -> str:
    return format_delete_candidate(index, row, products_by_id)


def remove_product_from_local_store(product_id: str) -> None:
    products = load_products()
    remaining_products = [product for product in products if str(product.get("id")) != product_id]
    if len(remaining_products) != len(products):
        save_products(remaining_products)


def update_product_price_local_store(product_id: str, price: str) -> None:
    products = load_products()
    changed = False
    numeric_price = re.sub(r"\D", "", price)
    for product in products:
        if str(product.get("id")) == product_id:
            product["price"] = numeric_price or price
            changed = True
            break
    if changed:
        save_products(products)


def stock_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = []
    page_buttons = []
    if page > 0:
        page_buttons.append(InlineKeyboardButton("Précédent", callback_data=f"stock_page:{page - 1}"))
    if page < total_pages - 1:
        page_buttons.append(InlineKeyboardButton("Suivant", callback_data=f"stock_page:{page + 1}"))
    if page_buttons:
        buttons.append(page_buttons)
    buttons.append([InlineKeyboardButton("Télécharger CSV", callback_data="stock_download_csv")])
    return InlineKeyboardMarkup(buttons)


def product_matches_filter(product: dict[str, Any], search_text: str) -> bool:
    if not search_text:
        return True

    haystack = " ".join(
        str(product.get(field, ""))
        for field in ("title", "id", "brand", "description")
    )
    return search_text in haystack.casefold()


def stock_message(products: list[dict[str, Any]], page: int, search_text: str = "") -> tuple[str, InlineKeyboardMarkup]:
    total_results = len(products)
    total_pages = max(1, (total_results + STOCK_PAGE_SIZE - 1) // STOCK_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_products = products[page * STOCK_PAGE_SIZE:(page + 1) * STOCK_PAGE_SIZE]

    lines = [
        "📦 Catalogue",
        f"Total produits : {total_results}",
    ]
    if search_text:
        lines.append(f"Filtre : {search_text}")
    if total_pages > 1:
        lines.append(f"Page : {page + 1}/{total_pages}")
    lines.append("")

    for index, product in enumerate(page_products, start=page * STOCK_PAGE_SIZE + 1):
        lines.extend(
            [
                f"{index}. {product.get('title') or product.get('id') or 'Sans titre'}",
                f"Prix : {format_meta_price(product.get('price')) or 'Non renseigné'}",
                f"Marque : {product.get('brand') or 'Non renseignée'}",
                f"RAM : {product.get('ram') or 'Non renseignée'}",
                f"Stockage : {product.get('storage') or 'Non renseigné'}",
                "",
            ]
        )

    return "\n".join(lines).strip(), stock_keyboard(page, total_pages)


async def send_catalog_csv(message: Any) -> None:
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        logging.warning("Export demandé mais CSV introuvable: %s", CSV_PATH)
        await message.reply_text("Catalogue introuvable.")
        return

    total_products = count_csv_products()
    with CSV_PATH.open("rb") as csv_file:
        await message.reply_document(
            document=csv_file,
            filename=CSV_PATH.name,
            caption=f"Total produits : {total_products}",
        )


async def download_telegram_photos(
    photo_files: list[dict[str, str]],
    product_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> list[str]:
    image_paths = []
    for index, photo_file in enumerate(photo_files, start=1):
        telegram_file = await context.bot.get_file(photo_file["file_id"])
        unique_id = photo_file.get("file_unique_id") or index
        local_path = DOWNLOADS_DIR / f"{product_id}_{index}_{unique_id}.jpg"
        await telegram_file.download_to_drive(custom_path=local_path)
        image_paths.append(str(local_path))
    return image_paths


def upload_images(image_paths: list[str], config: dict[str, Any], product_id: str) -> list[str]:
    image_urls = []
    for index, image_path in enumerate(image_paths, start=1):
        public_id = f"catalogue/{product_id}/{index}"
        try:
            upload_result = cloudinary.uploader.upload(
                image_path,
                public_id=public_id,
                resource_type="image",
                overwrite=False,
            )
        except Exception as error:
            logging.error("Erreur Cloudinary public_id=%s: %s", public_id, redact_secrets(error))
            raise RuntimeError("Problème Cloudinary.") from error

        secure_url = upload_result.get("secure_url")
        if not secure_url:
            logging.error("Cloudinary n'a pas retourné de secure_url pour public_id=%s", public_id)
            raise RuntimeError("Problème Cloudinary.")
        image_urls.append(secure_url)
    return image_urls


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Bienvenue.\n\n"
        "Workflow rapide:\n"
        "1. /ajouter\n"
        "2. Envoie une ou plusieurs photos du produit\n"
        "3. Colle l'annonce fournisseur\n"
        "4. Réponds OUI pour publier ou NON pour annuler\n\n"
        "Utilise /aide pour voir toutes les commandes."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commandes disponibles:\n"
        "/start - aide rapide\n"
        "/ajouter - commencer un nouveau produit\n"
        "/description - ajouter l'annonce fournisseur en commande texte\n"
        "/export - envoyer le CSV Meta Commerce Manager\n"
        "/publier - publier le CSV sur GitHub\n"
        "/stock - consulter le catalogue\n"
        "/supprimer - supprimer un produit du CSV avec confirmation\n"
        "/modifierprix - modifier uniquement le prix d'un produit\n"
        "/annuler - annuler l'ajout en cours\n"
        "/aide - afficher cette aide\n\n"
        "Format recommandé:\n"
        "Titre: Sac cuir\n"
        "Prix: 15000 FCFA\n"
        "Description: Sac neuf, couleur noire\n"
        "Marque: Ma boutique"
    )


async def ajouter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["draft_product"] = {
        "stage": DRAFT_STAGE_PHOTOS,
        "photo_files": [],
        "announcement_text": "",
        "parsed_product": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await update.message.reply_text("Envoie les photos du produit.")


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft_product", None)
    context.user_data.pop("delete_flow", None)
    context.user_data.pop("price_flow", None)
    await update.message.reply_text("Ajout annulé.")


async def supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft_product", None)
    context.user_data.pop("price_flow", None)
    context.user_data["delete_flow"] = {
        "stage": DELETE_STAGE_QUERY,
        "matches": [],
        "selected_id": "",
    }
    await update.message.reply_text("Envoie l’ID du produit ou une partie du nom.")


async def modifierprix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft_product", None)
    context.user_data.pop("delete_flow", None)
    context.user_data["price_flow"] = {
        "stage": PRICE_STAGE_QUERY,
        "matches": [],
        "selected_id": "",
        "new_price": "",
    }
    await update.message.reply_text("Envoie l’ID ou le nom du produit.")


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = draft_for(context)
    if draft is None:
        await update.message.reply_text("Utilise /ajouter avant d'envoyer un produit.")
        return

    if draft.get("stage") == DRAFT_STAGE_CONFIRMATION:
        await update.message.reply_text("Réponds OUI pour publier ou NON pour annuler.")
        return

    photo = update.message.photo[-1]
    draft["photo_files"].append(
        {
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
        }
    )

    await update.message.reply_text("Photos reçues. Envoie maintenant l’annonce fournisseur.")


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = draft_for(context)
    if draft is None:
        await update.message.reply_text("Utilise /ajouter avant d'envoyer un produit.")
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Annonce vide. Envoie l'annonce fournisseur.")
        return

    if draft.get("stage") == DRAFT_STAGE_CONFIRMATION:
        answer = text.casefold()
        if answer == "non":
            context.user_data.pop("draft_product", None)
            await update.message.reply_text("Publication annulée.")
            return

        if answer == "oui":
            if not draft.get("parsed_product") or not draft.get("photo_files"):
                context.user_data.pop("draft_product", None)
                await update.message.reply_text("Le brouillon est incomplet. Recommence avec /ajouter.")
                return

            config = context.application.bot_data["config"]
            try:
                products = load_products()
                product_id = build_unique_product_id(draft["parsed_product"], products)
                image_paths = await download_telegram_photos(draft["photo_files"], product_id, context)
                image_urls = upload_images(image_paths, config, product_id)
                product = build_product(draft["parsed_product"], image_urls, product_id)
                save_product(product, products)
                append_meta_csv_row(product)
            except TelegramError as error:
                logging.error("Erreur Telegram pendant la publication: %s", redact_secrets(error))
                await update.message.reply_text("Erreur Telegram pendant le téléchargement des photos. Réessaie ou utilise /annuler.")
                return
            except RuntimeError as error:
                logging.error("Erreur publication: %s", redact_secrets(error))
                await update.message.reply_text("Problème Cloudinary pendant l'upload. Réessaie ou utilise /annuler.")
                return
            except OSError as error:
                logging.error("Erreur fichier pendant la publication: %s", redact_secrets(error))
                await update.message.reply_text("Erreur fichier pendant la sauvegarde du catalogue. Réessaie ou utilise /annuler.")
                return

            context.user_data.pop("draft_product", None)
            await update.message.reply_text(
                f"Produit publié: {product['title'] or product['id']}\n"
                f"ID: {product['id']}\n"
                "Utilise /export pour générer le CSV."
            )
            await send_github_publish_success(
                update.message,
                config,
                "✅ Catalogue publié sur GitHub",
            )
            return

        await update.message.reply_text("Réponds OUI pour publier ou NON pour annuler.")
        return

    if not draft.get("photo_files"):
        await update.message.reply_text("Envoie d'abord les photos du produit.")
        return

    config = context.application.bot_data["config"]
    product_data = parse_announcement(text, config)
    errors = validation_errors(product_data)
    if errors:
        logging.warning("Annonce refusée: %s", ", ".join(errors))
        await update.message.reply_text(validation_message(errors))
        return

    draft["announcement_text"] = text
    draft["parsed_product"] = product_data
    draft["stage"] = DRAFT_STAGE_CONFIRMATION
    await update.message.reply_text(format_product_summary(product_data, len(draft["photo_files"])))


async def handle_delete_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("delete_flow")
    if not flow:
        return False

    text = (update.message.text or "").strip()
    if not text:
        return True

    stage = flow.get("stage")
    rows = load_meta_csv_rows()
    if not rows:
        context.user_data.pop("delete_flow", None)
        await update.message.reply_text("Catalogue introuvable.")
        return True

    products_by_id = product_lookup_by_id()

    if stage == DELETE_STAGE_QUERY:
        search_text = text.casefold()
        matches = [row for row in rows if delete_row_matches(row, search_text)]
        if not matches:
            context.user_data.pop("delete_flow", None)
            await update.message.reply_text("Aucun produit trouvé.")
            return True

        if len(matches) == 1:
            selected = matches[0]
            flow["stage"] = DELETE_STAGE_CONFIRM
            flow["selected_id"] = selected.get("id", "")
            await update.message.reply_text(
                f"{format_delete_candidate(1, selected, products_by_id)}\n\n"
                "Réponds OUI pour confirmer ou NON pour annuler."
            )
            return True

        flow["stage"] = DELETE_STAGE_SELECT
        flow["matches"] = matches
        result_lines = [
            format_delete_candidate(index, row, products_by_id)
            for index, row in enumerate(matches, start=1)
        ]
        await update.message.reply_text(
            "\n".join(result_lines)
            + "\n\nRéponds avec le numéro du produit à supprimer."
        )
        return True

    if stage == DELETE_STAGE_SELECT:
        if not text.isdigit():
            await update.message.reply_text("Réponds avec le numéro du produit à supprimer.")
            return True

        matches = flow.get("matches", [])
        selected_index = int(text)
        if selected_index < 1 or selected_index > len(matches):
            await update.message.reply_text("Numéro invalide. Réponds avec le numéro du produit à supprimer.")
            return True

        selected = matches[selected_index - 1]
        flow["stage"] = DELETE_STAGE_CONFIRM
        flow["selected_id"] = selected.get("id", "")
        await update.message.reply_text(
            f"{format_delete_candidate(selected_index, selected, products_by_id)}\n\n"
            "Réponds OUI pour confirmer ou NON pour annuler."
        )
        return True

    if stage == DELETE_STAGE_CONFIRM:
        answer = text.casefold()
        if answer == "non":
            context.user_data.pop("delete_flow", None)
            await update.message.reply_text("Suppression annulée.")
            return True

        if answer != "oui":
            await update.message.reply_text("Réponds OUI pour confirmer ou NON pour annuler.")
            return True

        selected_id = str(flow.get("selected_id", ""))
        remaining_rows = [row for row in rows if str(row.get("id", "")) != selected_id]
        if len(remaining_rows) == len(rows):
            context.user_data.pop("delete_flow", None)
            await update.message.reply_text("Produit introuvable.")
            return True

        write_meta_csv_rows(remaining_rows)
        remove_product_from_local_store(selected_id)
        context.user_data.pop("delete_flow", None)
        await send_catalog_csv(update.message)
        await update.message.reply_text(f"Produits restants : {len(remaining_rows)}")
        await send_github_publish_success(
            update.message,
            context.application.bot_data["config"],
            "✅ Catalogue publié sur GitHub",
        )
        return True

    context.user_data.pop("delete_flow", None)
    await update.message.reply_text("Suppression annulée.")
    return True


async def handle_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("price_flow")
    if not flow:
        return False

    text = (update.message.text or "").strip()
    if not text:
        return True

    stage = flow.get("stage")
    rows = load_meta_csv_rows()
    if not rows:
        context.user_data.pop("price_flow", None)
        await update.message.reply_text("Catalogue introuvable.")
        return True

    products_by_id = product_lookup_by_id()

    if stage == PRICE_STAGE_QUERY:
        search_text = text.casefold()
        matches = [row for row in rows if delete_row_matches(row, search_text)]
        if not matches:
            context.user_data.pop("price_flow", None)
            await update.message.reply_text("Aucun produit trouvé.")
            return True

        if len(matches) == 1:
            selected = matches[0]
            flow["stage"] = PRICE_STAGE_PRICE
            flow["selected_id"] = selected.get("id", "")
            await update.message.reply_text(
                f"{format_price_candidate(1, selected, products_by_id)}\n\n"
                "Nouveau prix ?"
            )
            return True

        flow["stage"] = PRICE_STAGE_SELECT
        flow["matches"] = matches
        result_lines = [
            format_price_candidate(index, row, products_by_id)
            for index, row in enumerate(matches, start=1)
        ]
        await update.message.reply_text(
            "\n".join(result_lines)
            + "\n\nRéponds avec le numéro du produit à modifier."
        )
        return True

    if stage == PRICE_STAGE_SELECT:
        if not text.isdigit():
            await update.message.reply_text("Réponds avec le numéro du produit à modifier.")
            return True

        matches = flow.get("matches", [])
        selected_index = int(text)
        if selected_index < 1 or selected_index > len(matches):
            await update.message.reply_text("Numéro invalide. Réponds avec le numéro du produit à modifier.")
            return True

        selected = matches[selected_index - 1]
        flow["stage"] = PRICE_STAGE_PRICE
        flow["selected_id"] = selected.get("id", "")
        await update.message.reply_text(
            f"{format_price_candidate(selected_index, selected, products_by_id)}\n\n"
            "Nouveau prix ?"
        )
        return True

    if stage == PRICE_STAGE_PRICE:
        new_price = format_meta_price(text)
        if not new_price:
            await update.message.reply_text("Prix invalide. Envoie un montant, par exemple 185000.")
            return True

        flow["stage"] = PRICE_STAGE_CONFIRM
        flow["new_price"] = new_price
        await update.message.reply_text(
            f"Nouveau prix : {new_price}\n"
            "Confirmer le nouveau prix ? OUI/NON"
        )
        return True

    if stage == PRICE_STAGE_CONFIRM:
        answer = text.casefold()
        if answer == "non":
            context.user_data.pop("price_flow", None)
            await update.message.reply_text("Modification annulée.")
            return True

        if answer != "oui":
            await update.message.reply_text("Confirmer le nouveau prix ? OUI/NON")
            return True

        selected_id = str(flow.get("selected_id", ""))
        new_price = str(flow.get("new_price", ""))
        updated = False
        for row in rows:
            if str(row.get("id", "")) == selected_id:
                row["price"] = new_price
                updated = True
                break

        if not updated:
            context.user_data.pop("price_flow", None)
            await update.message.reply_text("Produit introuvable.")
            return True

        write_meta_csv_rows(rows)
        update_product_price_local_store(selected_id, new_price)
        context.user_data.pop("price_flow", None)
        await update.message.reply_text(f"Prix mis à jour : {new_price}")
        await send_github_publish_success(
            update.message,
            context.application.bot_data["config"],
            "✅ Catalogue publié sur GitHub",
        )
        return True

    context.user_data.pop("price_flow", None)
    await update.message.reply_text("Modification annulée.")
    return True


async def collect_product_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("delete_flow"):
        if update.message.photo:
            await update.message.reply_text("Envoie l’ID du produit ou une partie du nom.")
            return
        if await handle_delete_text(update, context):
            return

    if context.user_data.get("price_flow"):
        if update.message.photo:
            await update.message.reply_text("Envoie l’ID ou le nom du produit.")
            return
        if await handle_price_text(update, context):
            return

    if update.message.photo:
        await receive_photo(update, context)
        return
    await receive_text(update, context)


async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = draft_for(context)
    if draft is None:
        await update.message.reply_text("Aucun produit en cours. Utilise /ajouter pour commencer.")
        return

    command_text = update.message.text or ""
    extra_description = command_text.partition(" ")[2].strip()
    if not extra_description:
        await update.message.reply_text("Colle l'annonce fournisseur directement dans le chat.")
        return

    if not draft.get("photo_files"):
        await update.message.reply_text("Envoie d'abord les photos du produit.")
        return

    config = context.application.bot_data["config"]
    product_data = parse_announcement(extra_description, config)
    errors = validation_errors(product_data)
    if errors:
        logging.warning("Annonce refusée via /description: %s", ", ".join(errors))
        await update.message.reply_text(validation_message(errors))
        return

    draft["announcement_text"] = extra_description
    draft["parsed_product"] = product_data
    draft["stage"] = DRAFT_STAGE_CONFIRMATION
    await update.message.reply_text(format_product_summary(product_data, len(draft["photo_files"])))


async def export_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_catalog_csv(update.message)


async def publier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_github_publish_success(
        update.message,
        context.application.bot_data["config"],
        "✅ Catalogue publié",
    )


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = load_products()
    if not products:
        await update.message.reply_text("Le catalogue est vide.")
        return

    raw_filter = " ".join(context.args).strip()
    search_text = raw_filter.casefold()
    filtered_products = [product for product in products if product_matches_filter(product, search_text)]
    context.user_data["stock_filter"] = raw_filter

    if not filtered_products:
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Télécharger CSV", callback_data="stock_download_csv")]]
        )
        await update.message.reply_text("Aucun produit trouvé.", reply_markup=reply_markup)
        return

    message_text, reply_markup = stock_message(filtered_products, page=0, search_text=raw_filter)
    await update.message.reply_text(message_text, reply_markup=reply_markup)


async def stock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "stock_download_csv":
        await send_catalog_csv(query.message)
        return

    if not query.data.startswith("stock_page:"):
        return

    products = load_products()
    if not products:
        await query.edit_message_text("Le catalogue est vide.")
        return

    raw_filter = str(context.user_data.get("stock_filter", ""))
    search_text = raw_filter.casefold()
    filtered_products = [product for product in products if product_matches_filter(product, search_text)]
    if not filtered_products:
        await query.edit_message_text("Aucun produit trouvé.")
        return

    page = int(query.data.split(":", 1)[1])
    message_text, reply_markup = stock_message(filtered_products, page=page, search_text=raw_filter)
    await query.edit_message_text(message_text, reply_markup=reply_markup)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error:
        logging.error("Erreur non gérée %s: %s", type(error).__name__, redact_secrets(error))
    else:
        logging.error("Erreur non gérée sans exception fournie.")

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Une erreur est survenue. Les détails ont été enregistrés dans logs/bot.log."
            )
        except TelegramError as reply_error:
            logging.error("Impossible d'envoyer le message d'erreur Telegram: %s", redact_secrets(reply_error))


def build_telegram_application(config: dict[str, Any]) -> Application:
    try:
        application = Application.builder().token(config["TELEGRAM_BOT_TOKEN"]).build()
    except InvalidToken as error:
        logging.critical("Token Telegram invalide: %s", redact_secrets(error))
        raise SystemExit(1) from error

    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("aide", help_command))
    application.add_handler(CommandHandler("ajouter", ajouter))
    application.add_handler(CommandHandler("description", description))
    application.add_handler(CommandHandler("export", export_catalog))
    application.add_handler(CommandHandler("publier", publier))
    application.add_handler(CommandHandler("stock", stock))
    application.add_handler(CommandHandler("supprimer", supprimer))
    application.add_handler(CommandHandler("modifierprix", modifierprix))
    application.add_handler(CommandHandler("annuler", annuler))
    application.add_handler(CallbackQueryHandler(stock_callback, pattern=r"^stock_(page:\d+|download_csv)$"))
    application.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), collect_product_message))
    application.add_error_handler(error_handler)
    return application


def run_telegram_polling(config: dict[str, Any]) -> None:
    application = build_telegram_application(config)
    logging.info("Bot Telegram démarré.")
    try:
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)
    except InvalidToken as error:
        logging.critical("Token Telegram invalide pendant le démarrage: %s", redact_secrets(error))
    except TelegramError as error:
        logging.critical("Erreur Telegram pendant le polling: %s", redact_secrets(error))


def start_telegram_thread(config: dict[str, Any]) -> None:
    global telegram_thread
    with telegram_thread_lock:
        if telegram_thread and telegram_thread.is_alive():
            return

        telegram_thread = threading.Thread(
            target=run_telegram_polling,
            args=(config,),
            name="telegram-polling",
            daemon=True,
        )
        telegram_thread.start()


@flask_app.get("/")
def index() -> dict[str, str]:
    return {"service": "telegram-meta-catalog-bot", "status": "ok"}


@flask_app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "telegram_thread_alive": bool(telegram_thread and telegram_thread.is_alive()),
    }


def main() -> None:
    ensure_runtime_dirs()
    setup_logging()
    try:
        config = load_config()
    except ValueError as error:
        logging.critical("Configuration invalide: %s", error)
        raise SystemExit(1) from error

    register_config_secrets(config)
    configure_cloudinary(config)
    start_telegram_thread(config)

    port = int(os.environ.get("PORT", "10000"))
    logging.info("Serveur Flask démarré sur le port %s", port)
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    main()
