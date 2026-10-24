from __future__ import annotations

import csv
import base64
import hashlib
import io
import json
import math
import re
import secrets
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytesseract
import requests
import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError
from streamlit.errors import StreamlitSecretNotFoundError


PRODUCT_TABLE = "Productos"
MOVEMENT_TABLE = "Movimientos"
USER_TABLE = "Usuarios"
PERU_TIMEZONE = ZoneInfo("America/Lima")
PASSWORD_ITERATIONS = 600_000
DEFAULT_PRODUCTS = (
    ("barquimieles-360", "Barquimieles de 360 unidades"),
    ("barquimieles-12", "Barquimieles de 12 unidades"),
    ("coronitas", "Coronitas"),
)
MOVEMENT_LOCK = threading.Lock()
AIRTABLE_REQUEST_LOCK = threading.Lock()
LAST_AIRTABLE_REQUEST = 0.0
MIN_AIRTABLE_REQUEST_INTERVAL = 0.22
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


class AppError(Exception):
    """A user-safe operational error."""


class AirtableError(AppError):
    """An Airtable request failed."""


def normalize_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", value).lower()
        if unicodedata.category(character) != "Mn"
    )


def parse_product(text: str) -> str | None:
    normalized = normalize_text(text)
    if re.search(r"\bcoronitas?\b", normalized):
        return "coronitas"
    if re.search(r"\bbarquiroll\b", normalized):
        return "barquimieles-360"
    if re.search(r"barquimiel(?:es)?", normalized):
        if re.search(r"\b360\b", normalized):
            return "barquimieles-360"
        if re.search(r"\b12\b", normalized):
            return "barquimieles-12"
    return None


def parse_lot(text: str) -> str | None:
    pattern = re.compile(
        r"\b(?:lote|lot)\s*(?:(?:n(?:ro|umero|o)?\.?\s*(?:[°º#?]|(?=[:=]))|[°º#?])\s*)?[:=#-]?\s*([A-Z0-9][A-Z0-9./-]{0,29})\b",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    return match.group(1).strip(" .-/").upper() if match else None


def parse_quantity(text: str) -> int | None:
    normalized = normalize_text(text)
    normalized = re.sub(
        r"barquimiel(?:es)?\s*(?:de\s*)?(?:360|12)\s*unidades?",
        " ",
        normalized,
    )
    normalized = re.sub(
        r"\bcontenido\s*[:=-]?\s*\d{1,6}\s*(?:und(?:s)?|unidades?)\b",
        " ",
        normalized,
    )
    patterns = (
        r"\b(?:cantidad|cant\.?|qty)\s*[:=-]?\s*(\d{1,6})\b",
        r"\b(?:x|×)\s*(\d{1,6})\b",
        r"\b(\d{1,6})\s*(?:cajas?|paquetes?|bolsas?|piezas?|unidades?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return int(match.group(1))
    return None


def normalize_lot_lines(lines: Any) -> list[dict[str, Any]]:
    if hasattr(lines, "to_dict"):
        lines = lines.to_dict(orient="records")
    if not isinstance(lines, (list, tuple)):
        raise AppError("No se pudieron leer las filas de lote.")

    normalized: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            raise AppError("Hay una fila de lote inválida.")
        lot_value = line.get("Lote")
        quantity_value = line.get("Cantidad")
        lot_empty = lot_value is None or (
            isinstance(lot_value, float) and math.isnan(lot_value)
        ) or not str(lot_value).strip()
        quantity_empty = quantity_value is None or (
            isinstance(quantity_value, float) and math.isnan(quantity_value)
        )
        if lot_empty and quantity_empty:
            continue
        if isinstance(quantity_value, bool) or quantity_empty:
            quantity = None
        else:
            try:
                number = float(quantity_value)
            except (TypeError, ValueError, OverflowError):
                raise AppError("Cada lote debe tener una cantidad entera mayor que cero.")
            if not math.isfinite(number) or not number.is_integer():
                raise AppError("La cantidad por lote debe ser un número entero.")
            quantity = int(number)
        normalized.append(
            {
                "Lote": "" if lot_empty else str(lot_value).strip(),
                "Cantidad": quantity,
            }
        )
    if len(normalized) > 10:
        raise AppError("Registra como máximo 10 lotes por operación.")
    return normalized


def monday_of(value: date) -> date:
    return value - timedelta(days=value.weekday())


def peru_now() -> datetime:
    return datetime.now(PERU_TIMEZONE)


def movement_timestamp(movement_date: date) -> str:
    local_time = peru_now().time().replace(tzinfo=None)
    return datetime.combine(movement_date, local_time, tzinfo=PERU_TIMEZONE).isoformat(
        timespec="seconds"
    )


def format_time(value: Any) -> str:
    if not isinstance(value, str) or "T" not in value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            PERU_TIMEZONE
        ).strftime("%H:%M:%S")
    except ValueError:
        return ""


def format_money(value: float | int | None, currency: str) -> str:
    if value is None:
        return "Sin precio"
    return f"{currency} {value:,.2f}"


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise AppError(
            "El usuario debe tener entre 3 y 32 caracteres: letras, números, punto, guion o guion bajo."
        )
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise AppError("La contraseña debe tener al menos 8 caracteres.")
    if len(password) > 256:
        raise AppError("La contraseña no puede superar 256 caracteres.")


def is_placeholder(value: str) -> bool:
    return any(
        marker in value.lower()
        for marker in ("reemplaza", "replace", "usa_aqui", "your_", "tu_")
    )


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded_hash: Any) -> bool:
    if not isinstance(password, str) or not isinstance(encoded_hash, str):
        return False
    try:
        algorithm, iteration_text, salt_text, digest_text = encoded_hash.split("$")
        iterations = int(iteration_text)
        if (
            algorithm != "pbkdf2_sha256"
            or iterations < PASSWORD_ITERATIONS
            or iterations > 2_000_000
        ):
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual_digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return secrets.compare_digest(actual_digest, expected_digest)


def validate_legacy_backup(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise AppError("El archivo no es un respaldo reconocido de la aplicación anterior.")
    prices = payload.get("prices")
    movements = payload.get("movements")
    if not isinstance(prices, dict) or not isinstance(movements, list):
        raise AppError("El respaldo no contiene precios y movimientos válidos.")

    product_ids = {product_id for product_id, _ in DEFAULT_PRODUCTS}
    for product_id in product_ids:
        price = prices.get(product_id)
        if price is not None and (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price < 0
        ):
            raise AppError("El respaldo contiene un precio inválido.")

    stock = {product_id: 0 for product_id in product_ids}
    normalized_movements = []
    seen_movement_ids: set[str] = set()
    for movement in movements:
        if not isinstance(movement, dict):
            raise AppError("El respaldo contiene un movimiento inválido.")
        movement_id = movement.get("id")
        movement_type = movement.get("type")
        product_id = movement.get("productId")
        quantity = movement.get("quantity")
        movement_date = movement.get("date")
        unit_price = movement.get("unitPrice")
        note = movement.get("note", "")
        if not isinstance(movement_date, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", movement_date
        ):
            raise AppError("El respaldo contiene una fecha inválida.")
        try:
            parsed_date = date.fromisoformat(movement_date)
        except (TypeError, ValueError) as error:
            raise AppError("El respaldo contiene una fecha inválida.") from error
        if parsed_date.isoformat() != movement_date:
            raise AppError("El respaldo contiene una fecha inválida.")
        if (
            not isinstance(movement_id, str)
            or not movement_id
            or movement_id in seen_movement_ids
            or movement_type not in ("in", "out")
            or not isinstance(product_id, str)
            or product_id not in product_ids
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
            or not isinstance(note, str)
            or (
                unit_price is not None
                and (
                    isinstance(unit_price, bool)
                    or not isinstance(unit_price, (int, float))
                    or not math.isfinite(unit_price)
                    or unit_price < 0
                )
            )
        ):
            raise AppError("El respaldo contiene un movimiento con datos inválidos.")
        seen_movement_ids.add(movement_id)
        stock[product_id] += quantity if movement_type == "in" else -quantity
        if stock[product_id] < 0:
            raise AppError("El respaldo incluye salidas que dejan existencias negativas.")
        normalized_movements.append(
            {
                "ID": movement_id,
                "Tipo": "Entrada" if movement_type == "in" else "Salida",
                "Producto ID": product_id,
                "Producto": dict(DEFAULT_PRODUCTS)[product_id],
                "Cantidad": quantity,
                "Fecha": parsed_date.isoformat(),
                "Precio unitario": unit_price,
                "Nota": note,
                "Registrado por": "Migración de respaldo local",
            }
        )
    return {
        "prices": {product_id: prices.get(product_id) for product_id in product_ids},
        "movements": normalized_movements,
    }


class AirtableClient:
    def __init__(self, token: str, base_id: str):
        if not token or not base_id:
            raise AppError("Falta configurar el token o el identificador de la base de Airtable.")
        self.base_id = base_id
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def _request(
        self,
        method: str,
        table: str,
        *,
        record_id: str | None = None,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = requests.utils.quote(table, safe="")
        if record_id:
            path = f"{path}/{requests.utils.quote(record_id, safe='')}"
        url = f"https://api.airtable.com/v0/{self.base_id}/{path}"
        global LAST_AIRTABLE_REQUEST
        with AIRTABLE_REQUEST_LOCK:
            now = time.monotonic()
            wait = MIN_AIRTABLE_REQUEST_INTERVAL - (now - LAST_AIRTABLE_REQUEST)
            if wait > 0:
                time.sleep(wait)
            LAST_AIRTABLE_REQUEST = time.monotonic()
        try:
            response = self.session.request(
                method, url, params=params, json=payload, timeout=20
            )
        except requests.RequestException as error:
            raise AirtableError(
                "No se pudo conectar con Airtable. Revisa la conexión y vuelve a intentar."
            ) from error
        if not response.ok:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                detail = ""
            message = detail or f"respuesta HTTP {response.status_code}"
            raise AirtableError(f"Airtable rechazó la operación: {message}")
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise AirtableError("Airtable devolvió una respuesta que no se pudo leer.") from error

    def list_records(self, table: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        params: dict[str, Any] = {"pageSize": 100}
        while True:
            result = self._request("GET", table, params=params)
            page = result.get("records")
            if not isinstance(page, list):
                raise AirtableError(f"La tabla {table} no devolvió una lista de registros.")
            records.extend(page)
            offset = result.get("offset")
            if not offset:
                return records
            params["offset"] = offset

    def create_record(self, table: str, fields: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", table, payload={"fields": fields})
        record = result.get("id")
        if not isinstance(record, str):
            raise AirtableError(f"Airtable no confirmó la creación en {table}.")
        return result

    def create_records(self, table: str, records: list[dict[str, Any]]) -> None:
        for start in range(0, len(records), 10):
            self._request(
                "POST",
                table,
                payload={"records": [{"fields": item} for item in records[start : start + 10]]},
            )

    def update_record(
        self, table: str, record_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "PATCH", table, record_id=record_id, payload={"fields": fields}
        )


def record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise AirtableError("Airtable devolvió un registro mal formado.")
    return fields


def stock_by_product(
    products: list[dict[str, Any]], movements: list[dict[str, Any]]
) -> dict[str, int]:
    stock: dict[str, int] = {}
    for product in products:
        fields = record_fields(product)
        product_id = fields.get("ID")
        name = fields.get("Nombre")
        price = fields.get("Precio")
        active = fields.get("Activo", False)
        if (
            not isinstance(product_id, str)
            or not product_id
            or product_id in stock
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(active, bool)
            or (
                price is not None
                and (
                    isinstance(price, bool)
                    or not isinstance(price, (int, float))
                    or not math.isfinite(price)
                    or price < 0
                )
            )
        ):
            raise AirtableError("Hay un producto inválido o duplicado en Airtable.")
        stock[product_id] = 0
    movement_ids: set[str] = set()
    for record in movements:
        fields = record_fields(record)
        product_id = str(fields.get("Producto ID", ""))
        movement_id = fields.get("ID")
        movement_type = fields.get("Tipo")
        quantity = fields.get("Cantidad")
        price = fields.get("Precio unitario")
        if (
            not isinstance(movement_id, str)
            or not movement_id
            or movement_id in movement_ids
            or product_id not in stock
            or movement_type not in ("Entrada", "Salida")
            or format_date(fields.get("Fecha")) is None
            or isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(quantity)
            or int(quantity) != quantity
            or quantity < 1
            or (
                price is not None
                and (
                    isinstance(price, bool)
                    or not isinstance(price, (int, float))
                    or not math.isfinite(price)
                    or price < 0
                )
            )
        ):
            raise AirtableError("Hay un movimiento con datos inválidos en Airtable.")
        movement_ids.add(movement_id)
        stock[product_id] += int(quantity) if movement_type == "Entrada" else -int(quantity)
    if any(quantity < 0 for quantity in stock.values()):
        raise AirtableError(
            "El historial de Airtable produce existencias negativas. Revisa los movimientos antes de continuar."
        )
    return stock


def stock_by_lot(movements: list[dict[str, Any]], product_id: str) -> dict[str, int]:
    stock: dict[str, int] = {}
    for record in movements:
        fields = record_fields(record)
        if str(fields.get("Producto ID", "")) != product_id:
            continue
        lot_value = fields.get("Lote")
        lot = str(lot_value).strip().upper() if lot_value else "SIN LOTE"
        quantity = int(fields["Cantidad"])
        stock[lot] = stock.get(lot, 0) + (
            quantity if fields.get("Tipo") == "Entrada" else -quantity
        )
    negative_lots = [lot for lot, quantity in stock.items() if quantity < 0]
    if negative_lots:
        raise AirtableError(
            "El historial de Airtable produce existencias negativas para uno o más lotes. Revisa las salidas antes de continuar."
        )
    return {lot: quantity for lot, quantity in stock.items() if quantity > 0}


def get_products(client: AirtableClient) -> list[dict[str, Any]]:
    return client.list_records(PRODUCT_TABLE)


def get_movements(client: AirtableClient) -> list[dict[str, Any]]:
    return client.list_records(MOVEMENT_TABLE)


def bootstrap_first_admin(
    client: AirtableClient,
    users: list[dict[str, Any]],
    username: str,
    password: str,
) -> list[dict[str, Any]]:
    if has_admin_account(users):
        return users
    normalized = validate_username(username)
    if any(
        normalize_username(str(record_fields(record).get("Usuario", ""))) == normalized
        for record in users
    ):
        raise AppError("El usuario administrador inicial ya existe; configura otro usuario o inicia sesión.")
    password_hash = hash_password(password)
    client.create_record(
        USER_TABLE,
        {
            "Usuario": normalized,
            "Hash contraseña": password_hash,
            "Rol": "admin",
            "Activo": True,
        },
    )
    return client.list_records(USER_TABLE)


def reset_admin_password(
    client: AirtableClient,
    users: list[dict[str, Any]],
    username: str,
    password: str,
) -> list[dict[str, Any]]:
    normalized = validate_username(username)
    matches = [
        record
        for record in users
        if normalize_username(str(record_fields(record).get("Usuario", "")))
        == normalized
        and record_fields(record).get("Rol") == "admin"
    ]
    if len(matches) != 1:
        raise AppError(
            "No se encontró una única cuenta administradora activa con el usuario configurado para recuperación."
        )
    user = matches[0]
    if record_fields(user).get("Activo") is not True:
        raise AppError(
            "La cuenta administradora está desactivada. Reactívala en Airtable antes de restablecer su contraseña."
        )
    if not verify_password(password, record_fields(user).get("Hash contraseña")):
        client.update_record(
            USER_TABLE,
            str(user.get("id", "")),
            {"Hash contraseña": hash_password(password)},
        )
        return client.list_records(USER_TABLE)
    return users


def has_admin_account(users: list[dict[str, Any]]) -> bool:
    return any(
        fields.get("Rol") == "admin"
        and isinstance(fields.get("Usuario"), str)
        and bool(fields["Usuario"].strip())
        and isinstance(fields.get("Hash contraseña"), str)
        and bool(fields["Hash contraseña"])
        for fields in (record_fields(record) for record in users)
    )


def find_user(username: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    matches = [
        record
        for record in records
        if normalize_username(str(record_fields(record).get("Usuario", ""))) == normalized
    ]
    return matches[0] if len(matches) == 1 else None


def authenticate_user(
    username: str, password: str, records: list[dict[str, Any]]
) -> dict[str, Any] | None:
    user = find_user(username, records)
    if user is None:
        return None
    fields = record_fields(user)
    if (
        fields.get("Activo") is not True
        or fields.get("Rol") not in ("admin", "trabajador")
        or not verify_password(password, fields.get("Hash contraseña"))
    ):
        return None
    return user


def format_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def ensure_default_products(client: AirtableClient) -> list[dict[str, Any]]:
    products = get_products(client)
    if products:
        return products
    client.create_records(
        PRODUCT_TABLE,
        [
            {"ID": product_id, "Nombre": name, "Precio": None, "Activo": True}
            for product_id, name in DEFAULT_PRODUCTS
        ],
    )
    return get_products(client)


def add_movement(
    client: AirtableClient,
    *,
    movement_type: str,
    product_id: str,
    quantity: int,
    movement_date: date,
    note: str,
    username: str,
    lot: str = "SIN LOTE",
) -> None:
    add_movement_batch(
        client,
        movement_type=movement_type,
        product_id=product_id,
        lot_lines=[{"Lote": lot, "Cantidad": quantity}],
        movement_date=movement_date,
        note=note,
        username=username,
    )


def add_movement_batch(
    client: AirtableClient,
    *,
    movement_type: str,
    product_id: str,
    lot_lines: list[dict[str, Any]],
    movement_date: date,
    note: str,
    username: str,
) -> None:
    if movement_type not in ("Entrada", "Salida"):
        raise AppError("Selecciona un tipo de movimiento válido.")
    if not isinstance(lot_lines, list) or not lot_lines or len(lot_lines) > 10:
        raise AppError("Registra entre 1 y 10 lotes por operación.")
    if len(note) > 160:
        raise AppError("La nota no puede superar 160 caracteres.")
    if not isinstance(movement_date, date):
        raise AppError("La fecha del movimiento no es válida.")
    normalized_lines: list[dict[str, Any]] = []
    total_quantity = 0
    for line in lot_lines:
        if not isinstance(line, dict):
            raise AppError("Hay una fila de lote inválida.")
        lot = line.get("Lote")
        quantity = line.get("Cantidad")
        if not isinstance(lot, str) or not lot.strip() or len(lot.strip()) > 40:
            raise AppError("Cada fila debe tener un lote de entre 1 y 40 caracteres.")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
        ):
            raise AppError("Cada lote debe tener una cantidad entera mayor que cero.")
        normalized_lines.append({"Lote": lot.strip().upper(), "Cantidad": quantity})
        total_quantity += quantity
    if len({line["Lote"] for line in normalized_lines}) != len(normalized_lines):
        raise AppError("No repitas el mismo lote; combina sus cantidades en una sola fila.")

    with MOVEMENT_LOCK:
        products = get_products(client)
        movements = get_movements(client)
        stock = stock_by_product(products, movements)
        product = next(
            (
                record
                for record in products
                if record_fields(record).get("ID") == product_id
                and record_fields(record).get("Activo") is True
            ),
            None,
        )
        if product is None:
            raise AppError("Ese producto no existe o está desactivado.")
        if product_id not in stock:
            raise AppError("No se pudo calcular la existencia actual del producto.")
        if movement_type == "Salida" and total_quantity > stock[product_id]:
            raise AppError(
                f"Existencia insuficiente: quedan {stock[product_id]} presentación(es) de {record_fields(product).get('Nombre', product_id)}."
            )
        if movement_type == "Salida":
            available_by_lot = stock_by_lot(movements, product_id)
            for line in normalized_lines:
                available = available_by_lot.get(line["Lote"], 0)
                if line["Cantidad"] > available:
                    raise AppError(
                        f"El lote {line['Lote']} tiene {available} caja(s) disponible(s); no se puede registrar una salida de {line['Cantidad']}."
                    )
        fields = record_fields(product)
        price = fields.get("Precio")
        if price is not None and (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price < 0
        ):
            raise AppError("El precio configurado en Airtable no es válido.")
        group_id = str(uuid.uuid4())
        timestamp = movement_timestamp(movement_date)
        client.create_records(
            MOVEMENT_TABLE,
            [
                {
                    "ID": str(uuid.uuid4()),
                    "Grupo ID": group_id,
                    "Lote": line["Lote"],
                    "Tipo": movement_type,
                    "Producto ID": product_id,
                    "Producto": str(fields.get("Nombre", product_id)),
                    "Cantidad": line["Cantidad"],
                    "Fecha": timestamp,
                    "Precio unitario": price,
                    "Nota": note.strip(),
                    "Registrado por": username,
                }
                for line in normalized_lines
            ],
        )


def read_settings() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        airtable = dict(st.secrets["airtable"])
        app_settings = dict(st.secrets.get("app", {}))
    except (KeyError, TypeError, StreamlitSecretNotFoundError) as error:
        raise AppError(
            "Falta configuración. Sigue la guía de README.md para Airtable, el inicio de sesión y los secretos de Streamlit."
        ) from error
    required_airtable = ("token", "base_id")
    if any(
        not str(airtable.get(key, "")).strip()
        or is_placeholder(str(airtable.get(key, "")))
        for key in required_airtable
    ):
        raise AppError("Configura airtable.token y airtable.base_id en los secretos de Streamlit.")
    bootstrap_username = str(app_settings.get("bootstrap_admin_username", "")).strip()
    bootstrap_password = str(app_settings.get("bootstrap_admin_password", ""))
    reset_admin = app_settings.get("reset_admin_password", False)
    if not isinstance(reset_admin, bool):
        raise AppError("app.reset_admin_password debe ser true o false.")
    if bool(bootstrap_username) != bool(bootstrap_password):
        raise AppError(
            "Configura juntos bootstrap_admin_username y bootstrap_admin_password, o elimina ambos después de crear el primer administrador."
        )
    if reset_admin and not bootstrap_username:
        raise AppError(
            "Para restablecer la cuenta, configura también bootstrap_admin_username y bootstrap_admin_password."
        )
    if bootstrap_username:
        validate_username(bootstrap_username)
        if is_placeholder(bootstrap_password):
            raise AppError("Reemplaza la contraseña temporal de ejemplo por una propia.")
        validate_password(bootstrap_password)
    return airtable, app_settings


def show_login(users: list[dict[str, Any]]) -> None:
    st.title("Registro de barquillos")
    st.info("Inicia sesión con el usuario y la contraseña que te asignó el administrador.")
    locked_until = float(st.session_state.get("login_locked_until", 0))
    locked = time.time() < locked_until
    if locked:
        st.error("Demasiados intentos fallidos. Espera un minuto y vuelve a intentar.")
    with st.form("login_form", clear_on_submit=True):
        username = st.text_input("Usuario", max_chars=32)
        password = st.text_input("Contraseña", type="password", max_chars=256)
        submitted = st.form_submit_button("Iniciar sesión", type="primary", disabled=locked)
    if submitted:
        user = authenticate_user(username, password, users)
        if user is None:
            failures = int(st.session_state.get("login_failures", 0)) + 1
            st.session_state["login_failures"] = failures
            if failures >= 5:
                st.session_state["login_failures"] = 0
                st.session_state["login_locked_until"] = time.time() + 60
            st.error("Usuario o contraseña incorrectos, o la cuenta está desactivada.")
        else:
            fields = record_fields(user)
            st.session_state["authenticated_username"] = fields["Usuario"]
            st.session_state["authenticated_password_hash"] = fields["Hash contraseña"]
            st.session_state["login_failures"] = 0
            st.session_state.pop("login_locked_until", None)
            st.rerun()
    st.stop()


def currency_code(app_settings: dict[str, Any]) -> str:
    value = str(app_settings.get("currency", "MXN")).upper()
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise AppError("app.currency debe ser un código de moneda ISO de tres letras, por ejemplo MXN.")
    return value


def render_stock(
    products: list[dict[str, Any]],
    movements: list[dict[str, Any]],
    role: str,
    currency: str,
) -> dict[str, int]:
    active_products = [
        product
        for product in products
        if record_fields(product).get("Activo") is True
    ]
    stock = stock_by_product(products, movements)
    st.subheader("Existencias actuales")
    st.caption("El conteo suma presentaciones; no convierte tamaños de empaque.")
    if not active_products:
        st.info("No hay productos activos. Pide al administrador que configure el catálogo.")
        return stock
    columns = st.columns(min(3, len(active_products)))
    for index, record in enumerate(active_products):
        fields = record_fields(record)
        product_id = str(fields.get("ID", ""))
        quantity = stock.get(product_id, 0)
        with columns[index % len(columns)]:
            st.metric(str(fields.get("Nombre", product_id)), f"{quantity} presentaciones")
            lot_stock = stock_by_lot(movements, product_id)
            if lot_stock:
                with st.expander("Existencias por lote", expanded=False):
                    st.dataframe(
                        [
                            {"Lote": lot, "Cajas disponibles": count}
                            for lot, count in sorted(lot_stock.items())
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                st.caption("Sin existencias registradas por lote.")
            if role == "admin":
                price = fields.get("Precio")
                st.caption(f"Precio: {format_money(price, currency)}")
                if price is not None:
                    st.caption(f"Valor actual: {format_money(quantity * price, currency)}")
    if role == "admin":
        configured = [
            record_fields(record).get("Precio")
            for record in active_products
            if record_fields(record).get("Precio") is not None
        ]
        total_value = sum(
            stock.get(str(record_fields(record).get("ID", "")), 0)
            * record_fields(record).get("Precio", 0)
            for record in active_products
            if record_fields(record).get("Precio") is not None
        )
        st.metric("Valor de existencias con precio configurado", format_money(total_value, currency))
        if len(configured) < len(active_products):
            st.warning("Algunos productos no tienen precio; no se incluyen en el valor total.")
    return stock


def render_photo_capture(products: list[dict[str, Any]]) -> None:
    with st.expander("Proponer producto y cantidad desde una foto"):
        st.caption(
            "La foto se procesa en Streamlit y no se guarda en Airtable. El archivo se conserva solo mientras está en la sesión de captura."
        )
        detected_text = st.session_state.get("ocr_text")
        if detected_text is not None:
            product_id = st.session_state.get("ocr_product_id")
            quantity = st.session_state.get("ocr_quantity")
            lot = st.session_state.get("ocr_lot")
            st.text_area("Texto reconocido (revísalo)", detected_text, height=100, disabled=True)
            st.button(
                "Elegir otra foto",
                on_click=clear_photo_proposal,
                key="clear_photo_proposal",
            )
            suggestions = []
            if product_id:
                product = next(
                    (
                        record
                        for record in products
                        if record_fields(record).get("ID") == product_id
                    ),
                    None,
                )
                if product:
                    suggestions.append(str(record_fields(product).get("Nombre", product_id)))
            if quantity:
                suggestions.append(f"cantidad {quantity}")
            if lot:
                suggestions.append(f"lote {lot}")
            if suggestions:
                st.info("Propuesta del OCR: " + " · ".join(suggestions) + ". Confirma los campos en el formulario antes de guardar.")
            else:
                st.warning(
                    "No se pudo reconocer producto ni cantidad. Completa ambos en el formulario de registro que aparece abajo."
                )
        else:
            uploaded = st.file_uploader(
                "Selecciona una foto clara del producto y la cantidad",
                type=("png", "jpg", "jpeg", "webp"),
                key="movement_photo",
            )
            if st.button("Leer foto", disabled=uploaded is None):
                if uploaded is not None:
                    image_bytes = uploaded.getvalue()
                    if len(image_bytes) > 8 * 1024 * 1024:
                        st.error("La foto debe pesar menos de 8 MB.")
                    else:
                        try:
                            image = Image.open(io.BytesIO(image_bytes))
                            image = ImageOps.exif_transpose(image).convert("RGB")
                            detected_text = pytesseract.image_to_string(image, lang="spa")
                            st.session_state["ocr_text"] = detected_text
                            st.session_state["ocr_product_id"] = parse_product(detected_text)
                            st.session_state["ocr_quantity"] = parse_quantity(detected_text)
                            st.session_state["ocr_lot"] = parse_lot(detected_text)
                            st.rerun()
                        except (
                            UnidentifiedImageError,
                            OSError,
                            Image.DecompressionBombError,
                        ) as error:
                            st.error(f"No se pudo leer la imagen: {error}")
                        except (
                            pytesseract.TesseractError,
                            pytesseract.TesseractNotFoundError,
                        ) as error:
                            st.error(
                                "No está disponible el OCR en español en el servidor. Revisa packages.txt y la instalación de Tesseract."
                            )
                            st.caption(str(error))


def clear_photo_proposal() -> None:
    st.session_state.pop("ocr_text", None)
    st.session_state.pop("ocr_product_id", None)
    st.session_state.pop("ocr_quantity", None)
    st.session_state.pop("ocr_lot", None)


def render_movement_form(
    client: AirtableClient,
    products: list[dict[str, Any]],
    movements: list[dict[str, Any]],
    username: str,
) -> None:
    active_products = [
        record
        for record in products
        if record_fields(record).get("Activo") is True
    ]
    if not active_products:
        st.info("No hay productos activos para registrar.")
        return
    render_photo_capture(active_products)
    by_id = {
        str(record_fields(record).get("ID", "")): record for record in active_products
    }
    proposed_product = st.session_state.get("ocr_product_id")
    product_ids = list(by_id)
    product_index = product_ids.index(proposed_product) if proposed_product in product_ids else 0
    proposed_quantity = st.session_state.get("ocr_quantity")
    default_quantity = proposed_quantity if isinstance(proposed_quantity, int) and proposed_quantity > 0 else 1
    proposed_lot = st.session_state.get("ocr_lot")

    st.subheader("Registrar entrada o salida")
    st.caption(
        f"Hora actual de Perú: {peru_now():%d/%m/%Y %H:%M:%S}. "
        "La fecha y hora se guardan al confirmar el movimiento."
    )
    movement_type = st.selectbox(
        "Movimiento", ("Entrada", "Salida"), key="movement_type"
    )
    product_id = st.selectbox(
        "Producto",
        product_ids,
        index=product_index,
        format_func=lambda value: str(record_fields(by_id[value]).get("Nombre", value)),
        key="movement_product_id",
    )
    available_lots = stock_by_lot(movements, product_id)
    if movement_type == "Salida" and not available_lots:
        st.warning("No hay cajas disponibles de este producto en ningún lote.")
        return
    if movement_type == "Salida":
        default_lot = (
            proposed_lot.strip().upper()
            if isinstance(proposed_lot, str)
            and proposed_lot.strip().upper() in available_lots
            else next(iter(sorted(available_lots)))
        )
        lot_options = sorted(available_lots)
        default_lot_lines = [
            {"Lote": default_lot, "Cantidad": min(default_quantity, available_lots[default_lot])}
        ]
    else:
        lot_options = []
        default_lot_lines = [
            {
                "Lote": proposed_lot if isinstance(proposed_lot, str) else "",
                "Cantidad": default_quantity,
            }
        ]
    editor_key = f"movement_lot_lines_{movement_type}_{product_id}"
    with st.form("movement_form", clear_on_submit=True):
        lot_lines = st.data_editor(
            default_lot_lines,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key=editor_key,
            column_config={
                "Lote": (
                    st.column_config.SelectboxColumn(
                        "Lote de fabricación",
                        help="Selecciona el lote del que salen las cajas.",
                        options=lot_options,
                        required=True,
                    )
                    if movement_type == "Salida"
                    else st.column_config.TextColumn(
                        "Lote de fabricación",
                        help="Código de lote leído de la etiqueta o capturado manualmente.",
                        required=True,
                        max_chars=40,
                    )
                ),
                "Cantidad": st.column_config.NumberColumn(
                    "Cantidad de cajas",
                    min_value=1,
                    step=1,
                    format="%d",
                    required=True,
                ),
            },
        )
        movement_date = st.date_input(
            "Fecha del movimiento (hora de Perú al guardar)",
            value=peru_now().date(),
        )
        note = st.text_input("Nota opcional", max_chars=160)
        if movement_type == "Salida":
            st.caption(
                "Elige exactamente de qué lotes salen las cajas. "
                "La existencia de cada lote se valida al guardar."
            )
        else:
            st.caption("Puedes añadir varias filas de lote; se guardarán juntas como una operación.")
        confirmed = st.checkbox("Confirmo que revisé el tipo, producto, lotes, cantidades y fecha.")
        submitted = st.form_submit_button("Guardar movimiento", type="primary")
    if submitted:
        if not confirmed:
            st.error("Marca la confirmación antes de guardar.")
        else:
            try:
                normalized_lines = normalize_lot_lines(lot_lines)
                total_quantity = sum(
                    int(line["Cantidad"])
                    for line in normalized_lines
                    if isinstance(line.get("Cantidad"), (int, float))
                    and not isinstance(line.get("Cantidad"), bool)
                )
                add_movement_batch(
                    client,
                    movement_type=movement_type,
                    product_id=product_id,
                    lot_lines=normalized_lines,
                    movement_date=movement_date,
                    note=note,
                    username=username,
                )
                st.session_state.pop("ocr_product_id", None)
                st.session_state.pop("ocr_quantity", None)
                st.session_state.pop("ocr_lot", None)
                st.session_state.pop("ocr_text", None)
                st.success("Movimiento guardado en Airtable.")
                st.rerun()
            except AppError as error:
                st.error(str(error))


def movement_rows(
    movements: list[dict[str, Any]], start: date, end: date
) -> list[tuple[date, dict[str, Any], dict[str, Any]]]:
    rows = []
    for record in movements:
        fields = record_fields(record)
        movement_date = format_date(fields.get("Fecha"))
        if movement_date is None:
            raise AirtableError("Hay un movimiento con fecha inválida en Airtable.")
        if start <= movement_date <= end:
            rows.append((movement_date, record, fields))
    return sorted(rows, key=lambda item: (item[0], item[2].get("ID", "")), reverse=True)


def render_report(
    movements: list[dict[str, Any]], products: list[dict[str, Any]], currency: str
) -> None:
    st.subheader("Reporte semanal")
    selected_date = st.date_input("Elige una fecha dentro de la semana", value=peru_now().date(), key="report_date")
    start = monday_of(selected_date)
    end = start + timedelta(days=6)
    rows = movement_rows(movements, start, end)
    entries = [fields for _, _, fields in rows if fields.get("Tipo") == "Entrada"]
    exits = [fields for _, _, fields in rows if fields.get("Tipo") == "Salida"]

    def total_value(items: list[dict[str, Any]]) -> tuple[float, int]:
        total = 0.0
        without_price = 0
        for fields in items:
            price = fields.get("Precio unitario")
            quantity = fields.get("Cantidad", 0)
            if price is None:
                without_price += 1
            elif isinstance(price, (int, float)) and isinstance(quantity, (int, float)):
                total += price * quantity
            else:
                raise AirtableError("Hay un precio o cantidad inválidos en Airtable.")
        return total, without_price

    entry_value, entry_without_price = total_value(entries)
    exit_value, exit_without_price = total_value(exits)
    st.caption(f"Semana del {start:%d/%m/%Y} al {end:%d/%m/%Y}")
    columns = st.columns(4)
    columns[0].metric("Entradas", sum(fields.get("Cantidad", 0) for fields in entries))
    columns[1].metric("Salidas", sum(fields.get("Cantidad", 0) for fields in exits))
    columns[2].metric("Valor entradas", format_money(entry_value, currency))
    columns[3].metric("Valor salidas", format_money(exit_value, currency))
    if entry_without_price + exit_without_price:
        st.warning(f"{entry_without_price + exit_without_price} movimiento(s) no tienen precio guardado.")
    if not rows:
        st.info("No hay movimientos registrados para esta semana.")
        return

    names = {
        str(record_fields(record).get("ID", "")): str(record_fields(record).get("Nombre", ""))
        for record in products
    }
    display_rows = []
    export_rows = []
    for movement_date, _, fields in rows:
        price = fields.get("Precio unitario")
        quantity = fields.get("Cantidad")
        value = price * quantity if isinstance(price, (int, float)) else None
        product_id = str(fields.get("Producto ID", ""))
        name = str(fields.get("Producto") or names.get(product_id, product_id))
        display_rows.append(
            {
                "Fecha": movement_date.strftime("%d/%m/%Y"),
                "Hora (Perú)": format_time(fields.get("Fecha")),
                "Lote": fields.get("Lote", ""),
                "Grupo de operación": fields.get("Grupo ID", ""),
                "Movimiento": fields.get("Tipo", ""),
                "Producto": name,
                "Cantidad": quantity,
                "Precio al registrar": format_money(price, currency),
                "Valor": format_money(value, currency),
                "Nota": fields.get("Nota", ""),
                "Registrado por": fields.get("Registrado por", ""),
            }
        )
        export_rows.append(
            {
                "Fecha": movement_date.isoformat(),
                "Hora (Perú)": format_time(fields.get("Fecha")),
                "Lote": fields.get("Lote", ""),
                "Grupo de operación": fields.get("Grupo ID", ""),
                "Movimiento": fields.get("Tipo", ""),
                "Producto": name,
                "Cantidad": quantity,
                f"Precio unitario {currency}": "" if price is None else price,
                f"Valor {currency}": "" if value is None else value,
                "Nota": fields.get("Nota", ""),
                "Registrado por": fields.get("Registrado por", ""),
            }
        )
    st.dataframe(display_rows, use_container_width=True, hide_index=True)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(export_rows[0]))
    writer.writeheader()
    writer.writerows(export_rows)
    st.download_button(
        "Descargar reporte semanal CSV",
        data="\ufeff" + output.getvalue(),
        file_name=f"reporte-semanal-{start.isoformat()}.csv",
        mime="text/csv",
    )


def render_product_admin(client: AirtableClient, products: list[dict[str, Any]], currency: str) -> None:
    st.subheader("Productos, precios y disponibilidad")
    for record in products:
        fields = record_fields(record)
        product_id = str(fields.get("ID", ""))
        with st.form(f"product_{record['id']}"):
            name = st.text_input("Nombre", value=str(fields.get("Nombre", "")), max_chars=100)
            has_price = st.checkbox(
                "Precio configurado",
                value=fields.get("Precio") is not None,
                key=f"has_price_{record['id']}",
            )
            price = st.number_input(
                f"Precio por presentación ({currency})",
                min_value=0.0,
                step=1.0,
                value=float(fields.get("Precio") or 0),
                format="%.2f",
                key=f"price_{record['id']}",
            )
            active = st.checkbox("Producto activo", value=fields.get("Activo") is True, key=f"active_{record['id']}")
            saved = st.form_submit_button(f"Guardar {product_id}")
        if saved:
            if not name.strip():
                st.error("El nombre del producto no puede quedar vacío.")
            else:
                try:
                    client.update_record(
                        PRODUCT_TABLE,
                        record["id"],
                            {
                                "Nombre": name.strip(),
                                "Precio": price if has_price else None,
                                "Activo": active,
                            },
                    )
                    st.success(f"Producto «{name.strip()}» actualizado.")
                    st.rerun()
                except AppError as error:
                    st.error(str(error))
    st.markdown("#### Añadir producto")
    with st.form("new_product_form"):
        new_name = st.text_input("Nombre del nuevo producto", max_chars=100)
        new_price = st.number_input(
            f"Precio por presentación ({currency})",
            min_value=0.0,
            step=1.0,
            value=0.0,
            format="%.2f",
            key="new_price",
        )
        has_price = st.checkbox("Configurar este precio ahora", value=False)
        add_product = st.form_submit_button("Añadir producto")
    if add_product:
        normalized_name = new_name.strip()
        existing_names = {
            str(record_fields(record).get("Nombre", "")).strip().casefold()
            for record in products
        }
        if not normalized_name:
            st.error("Escribe un nombre para el producto.")
        elif normalized_name.casefold() in existing_names:
            st.error("Ya existe un producto con ese nombre.")
        else:
            try:
                client.create_record(
                    PRODUCT_TABLE,
                    {
                        "ID": str(uuid.uuid4()),
                        "Nombre": normalized_name,
                        "Precio": new_price if has_price else None,
                        "Activo": True,
                    },
                )
                st.success(f"Producto «{normalized_name}» añadido.")
                st.rerun()
            except AppError as error:
                st.error(str(error))
    st.caption("Desactivar un producto lo oculta a trabajadores, pero conserva sus movimientos históricos.")


def render_user_admin(client: AirtableClient, users: list[dict[str, Any]]) -> None:
    st.subheader("Acceso de trabajadores")
    legacy_count = sum(
        1
        for record in users
        if not record_fields(record).get("Usuario")
        or not record_fields(record).get("Hash contraseña")
    )
    if legacy_count:
        st.warning(
            f"Hay {legacy_count} registro(s) de acceso antiguos sin usuario y contraseña. No pueden iniciar sesión; vuelve a crear sus cuentas aquí con usuarios y contraseñas individuales."
        )
    st.caption("Crea una contraseña temporal y comunícala al trabajador de forma privada.")
    with st.form("new_worker_form", clear_on_submit=True):
        username = st.text_input("Usuario", max_chars=32).strip().lower()
        password = st.text_input("Contraseña inicial (mínimo 8 caracteres)", type="password", max_chars=256)
        add_worker = st.form_submit_button("Autorizar trabajador")
    if add_worker:
        try:
            username = validate_username(username)
            validate_password(password)
            if find_user(username, users) is not None:
                raise AppError("Ese usuario ya está registrado.")
            if any(
                normalize_username(str(record_fields(record).get("Usuario", ""))) == username
                for record in users
            ):
                raise AppError("Ese usuario ya está registrado.")
            client.create_record(
                USER_TABLE,
                {
                    "Usuario": username,
                    "Hash contraseña": hash_password(password),
                    "Rol": "trabajador",
                    "Activo": True,
                },
            )
            st.success("Trabajador autorizado. Entrégale su usuario y contraseña de forma privada.")
            st.rerun()
        except AppError as error:
            st.error(str(error))

    for record in users:
        fields = record_fields(record)
        username = fields.get("Usuario")
        role = fields.get("Rol")
        if (
            not isinstance(username, str)
            or not username.strip()
            or not isinstance(fields.get("Hash contraseña"), str)
        ):
            continue
        if role == "admin":
            st.caption(f"Administrador: {username}")
        elif role == "trabajador":
            active = fields.get("Activo") is True
            with st.expander(f"{username} · {'Activo' if active else 'Desactivado'}"):
                with st.form(f"edit_user_{record['id']}", clear_on_submit=True):
                    new_password = st.text_input(
                        "Nueva contraseña (déjala vacía para conservarla)",
                        type="password",
                        max_chars=256,
                        key=f"reset_password_{record['id']}",
                    )
                    new_active = st.checkbox(
                        "Cuenta activa",
                        value=active,
                        key=f"active_user_{record['id']}",
                    )
                    update_user = st.form_submit_button("Guardar cambios")
                if update_user:
                    try:
                        updates: dict[str, Any] = {"Activo": new_active}
                        if new_password:
                            validate_password(new_password)
                            updates["Hash contraseña"] = hash_password(new_password)
                        client.update_record(USER_TABLE, record["id"], updates)
                        st.success("Acceso actualizado.")
                        st.rerun()
                    except AppError as error:
                        st.error(str(error))


def render_password_change(
    client: AirtableClient, user_record: dict[str, Any], username: str
) -> None:
    with st.expander("Cambiar mi contraseña"):
        with st.form("change_own_password", clear_on_submit=True):
            current_password = st.text_input("Contraseña actual", type="password", max_chars=256)
            new_password = st.text_input("Nueva contraseña (mínimo 8 caracteres)", type="password", max_chars=256)
            confirm_password = st.text_input("Confirmar nueva contraseña", type="password", max_chars=256)
            submitted = st.form_submit_button("Actualizar contraseña")
        if submitted:
            fields = record_fields(user_record)
            if not verify_password(current_password, fields.get("Hash contraseña")):
                st.error("La contraseña actual no es correcta.")
            elif new_password != confirm_password:
                st.error("La nueva contraseña y su confirmación no coinciden.")
            else:
                try:
                    validate_password(new_password)
                    password_hash = hash_password(new_password)
                    client.update_record(
                        USER_TABLE,
                        user_record["id"],
                        {"Hash contraseña": password_hash},
                    )
                    st.session_state["authenticated_password_hash"] = password_hash
                    st.success("Tu contraseña fue actualizada.")
                except AppError as error:
                    st.error(str(error))


def import_legacy_backup(
    client: AirtableClient,
    payload: Any,
    products: list[dict[str, Any]],
    movements: list[dict[str, Any]],
) -> tuple[int, int]:
    validated = validate_legacy_backup(payload)
    existing_products = {
        str(record_fields(record).get("ID", "")) for record in products
    }
    existing_movement_ids = {
        str(record_fields(record).get("ID", "")) for record in movements
    }
    product_records = []
    for product_id, name in DEFAULT_PRODUCTS:
        if product_id not in existing_products:
            product_records.append(
                {
                    "ID": product_id,
                    "Nombre": name,
                    "Precio": validated["prices"].get(product_id),
                    "Activo": True,
                }
            )
    new_movements = [
        movement
        for movement in validated["movements"]
        if movement["ID"] not in existing_movement_ids
    ]
    if product_records:
        client.create_records(PRODUCT_TABLE, product_records)
    if new_movements:
        client.create_records(MOVEMENT_TABLE, new_movements)
    return len(product_records), len(new_movements)


def render_admin_data(
    client: AirtableClient,
    products: list[dict[str, Any]],
    movements: list[dict[str, Any]],
    users: list[dict[str, Any]],
    base_id: str,
) -> None:
    st.subheader("Datos y respaldos")
    st.write(f"Base de Airtable conectada: `{base_id}`")
    st.write(
        f"Productos: {len(products)} · Movimientos: {len(movements)} · Usuarios: {len(users)}"
    )
    backup = {
        "exported_at": peru_now().isoformat(timespec="seconds"),
        "base_id": base_id,
        "products": [record_fields(record) for record in products],
        "movements": [record_fields(record) for record in movements],
        "users": [
            {
                "Usuario": record_fields(record).get("Usuario"),
                "Rol": record_fields(record).get("Rol"),
                "Activo": record_fields(record).get("Activo") is True,
            }
            for record in users
        ],
    }
    st.download_button(
        "Descargar respaldo JSON de Airtable",
        data=json.dumps(backup, ensure_ascii=False, indent=2),
        file_name=f"respaldo-airtable-{peru_now().date().isoformat()}.json",
        mime="application/json",
    )
    st.markdown("#### Importar respaldo de la app local anterior")
    st.caption(
        "Solo acepta el JSON descargado desde la versión anterior. Los movimientos ya presentes no se duplican; los precios actuales de productos existentes se conservan."
    )
    uploaded = st.file_uploader(
        "Selecciona respaldo-inventario-*.json",
        type=("json",),
        key="legacy_backup",
    )
    confirm_import = st.checkbox(
        "Confirmo que deseo agregar al Airtable los movimientos del respaldo.",
        key="confirm_legacy_import",
    )
    if st.button("Importar respaldo local", disabled=uploaded is None or not confirm_import):
        try:
            payload = json.loads(uploaded.getvalue().decode("utf-8-sig"))
            created_products, created_movements = import_legacy_backup(
                client, payload, products, movements
            )
            st.success(
                f"Migración lista: {created_products} producto(s) añadidos y {created_movements} movimiento(s) importados. Los registros repetidos se omitieron."
            )
            st.rerun()
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            st.error(f"No se pudo leer el archivo JSON: {error}")
        except AppError as error:
            st.error(str(error))


def run_app() -> None:
    st.set_page_config(page_title="Registro de barquillos", page_icon="🍪", layout="wide")
    st.title("Registro de barquillos")
    st.caption("Inventario compartido · Controlado por roles · Datos en Airtable")
    try:
        airtable_settings, app_settings = read_settings()
        currency = currency_code(app_settings)
    except AppError as error:
        st.error(str(error))
        st.stop()

    client = AirtableClient(
        str(airtable_settings["token"]), str(airtable_settings["base_id"])
    )
    try:
        users = client.list_records(USER_TABLE)
        bootstrap_username = str(app_settings.get("bootstrap_admin_username", "")).strip()
        bootstrap_password = str(app_settings.get("bootstrap_admin_password", ""))
        if app_settings.get("reset_admin_password") and has_admin_account(users):
            users = reset_admin_password(
                client, users, bootstrap_username, bootstrap_password
            )
        elif not has_admin_account(users):
            if not bootstrap_username or not bootstrap_password:
                st.error(
                    "Aún no hay una cuenta de administrador con usuario y contraseña. Configura bootstrap_admin_username y bootstrap_admin_password en los secretos del servidor; después de iniciar sesión, cámbiala y retira ambas credenciales temporales."
                )
                st.stop()
            users = bootstrap_first_admin(
                client, users, bootstrap_username, bootstrap_password
            )
    except AppError as error:
        st.error(str(error))
        st.stop()
    if app_settings.get("reset_admin_password"):
        st.warning(
            "Restablecimiento listo. Desactiva app.reset_admin_password en Secrets antes de iniciar sesión y cambiar la contraseña."
        )

    username = str(st.session_state.get("authenticated_username", ""))
    current_user = find_user(username, users) if username else None
    if current_user is None or record_fields(current_user).get("Activo") is not True:
        st.session_state.pop("authenticated_username", None)
        st.session_state.pop("authenticated_password_hash", None)
        show_login(users)
    current_fields = record_fields(current_user)
    role = current_fields.get("Rol")
    stored_hash = current_fields.get("Hash contraseña")
    authenticated_hash = st.session_state.get("authenticated_password_hash")
    if (
        role not in ("admin", "trabajador")
        or not isinstance(stored_hash, str)
        or not isinstance(authenticated_hash, str)
        or not secrets.compare_digest(stored_hash, authenticated_hash)
    ):
        st.session_state.pop("authenticated_username", None)
        st.session_state.pop("authenticated_password_hash", None)
        st.warning("La contraseña o el acceso de esta cuenta cambió. Inicia sesión otra vez.")
        show_login(users)

    username = str(current_fields.get("Usuario", username))
    try:
        if role == "admin":
            products = ensure_default_products(client)
        else:
            products = get_products(client)
        movements = get_movements(client)
        stock_by_product(products, movements)
    except AppError as error:
        st.error(str(error))
        st.stop()

    with st.sidebar:
        st.write(f"Sesión: **{username}**")
        st.write("Rol: **Administrador**" if role == "admin" else "Rol: **Trabajador**")
        render_password_change(client, current_user, username)
        if st.button("Cerrar sesión"):
            st.session_state.pop("authenticated_username", None)
            st.session_state.pop("authenticated_password_hash", None)
            st.session_state.pop("login_failures", None)
            st.session_state.pop("login_locked_until", None)
            st.rerun()

    stock = render_stock(products, movements, role, currency)
    st.divider()
    render_movement_form(client, products, movements, username)

    if role == "admin":
        if app_settings.get("bootstrap_admin_password"):
            st.warning(
                "Por seguridad, elimina bootstrap_admin_username y bootstrap_admin_password de los secretos del servidor: la cuenta administradora ya se creó en Airtable."
            )
        st.divider()
        report_tab, products_tab, users_tab, data_tab = st.tabs(
            ["Reportes", "Productos y precios", "Usuarios", "Airtable y respaldos"]
        )
        with report_tab:
            render_report(movements, products, currency)
        with products_tab:
            render_product_admin(client, products, currency)
        with users_tab:
            render_user_admin(client, users)
        with data_tab:
            render_admin_data(
                client,
                products,
                movements,
                users,
                str(airtable_settings["base_id"]),
            )
    st.caption(
        "Las fotos del OCR se procesan temporalmente en Streamlit; los datos de inventario se guardan en Airtable."
    )


if __name__ == "__main__":
    run_app()
