from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import re
import ssl
import tempfile
from time import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from labels.letter_labels import procesar_etiquetas_carta
except Exception as error:
    procesar_etiquetas_carta = None
    LABEL_PROCESSOR_IMPORT_ERROR = error
else:
    LABEL_PROCESSOR_IMPORT_ERROR = None


APP_NAME = "MasCan Puppy APP"
MELI_API_BASE_URL = "https://api.mercadolibre.com"
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
MELI_SSL_CONTEXT = ssl.create_default_context()
CHILE_TZ = ZoneInfo("America/Santiago")

REQUIRED_COLUMNS = [
    "MELI_ID",
    "CB",
    "CB alt",
    "Nombre Producto",
    "Cant.",
    "Tipo de Despacho",
]

PRODUCT_STATES = {
    "Etiqueta impresa",
    "Etiqueta lista para imprimir",
    "Venta Full",
}

PACK_COMPONENTS = {
    "1909000061510-Pack-x4": [
        {
            "CB": "1909000061510",
            "CB alt": "1909000061510",
            "Nombre Producto": "HG 00006-Bolsas sanitarias biodeg. 6x20-STD-Verde",
            "Cant.": 4,
        }
    ],
    "0764451134799Packx2": [
        {
            "CB": "0764451134799",
            "CB alt": "0764451134799",
            "Nombre Producto": "HG 50003-Arena Sanitaria-14KG-Lavanda",
            "Cant.": 2,
        }
    ],
    "0764451134799+base+pala-pack": [
        {
            "CB": "0764451134799",
            "CB alt": "0764451134799",
            "Nombre Producto": "HG 50003-Arena Sanitaria-14KG-Lavanda",
            "Cant.": 1,
        },
        {
            "CB": "09500021503",
            "CB alt": "0764451134393",
            "Nombre Producto": "HG 50002-Base para Pala Sanitaria-STD-Blanco",
            "Cant.": 1,
        },
        {
            "CB": "SD09500011503",
            "CB alt": "0764451134386",
            "Nombre Producto": "HG 50001-Pala sanitaria para gatos-STD-Blanco",
            "Cant.": 1,
        },
    ],
}

SILENT_VOICE_MESSAGES = {
    "Producto correcto.",
}


def page_setup() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="M", layout="wide")
    st.title(APP_NAME)
    st.caption("Mini app online para pedidos MELI, etiquetas y control con pistola.")


def require_app_password() -> bool:
    expected_password = st.secrets.get("APP_PASSWORD", "")
    if not expected_password:
        st.warning("Falta configurar APP_PASSWORD en Streamlit Secrets.")
        return False

    if st.session_state.get("authenticated"):
        return True

    password = st.text_input("Clave de acceso", type="password")
    if st.button("Entrar"):
        if password == expected_password:
            st.session_state.authenticated = True
            st.rerun()
        st.error("Clave incorrecta.")
    return False


def normalize_code(value) -> str:
    text = "" if value is None else str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def code_variants(value) -> set[str]:
    code = normalize_code(value)
    variants = {code} if code else set()
    if code.isdigit():
        variants.add(code.lstrip("0") or "0")
    return variants


def set_last_message(message: str) -> None:
    st.session_state.last_message = message


def speak_once(message: str) -> None:
    if message in SILENT_VOICE_MESSAGES:
        return
    if not message or st.session_state.get("last_spoken_message") == message:
        return
    st.session_state.last_spoken_message = message
    safe_message = json.dumps(message, ensure_ascii=False)
    components.html(
        f"""
        <script>
        const message = {safe_message};
        if ("speechSynthesis" in window.parent) {{
            window.parent.speechSynthesis.cancel();
            const utterance = new SpeechSynthesisUtterance(message);
            utterance.lang = "es-CL";
            utterance.rate = 1;
            window.parent.speechSynthesis.speak(utterance);
        }}
        </script>
        """,
        height=0,
    )


def auto_download_bytes(data: bytes, file_name: str, mime: str) -> None:
    if not data:
        return
    encoded = base64.b64encode(data).decode("ascii")
    safe_name = json.dumps(file_name)
    safe_mime = json.dumps(mime)
    components.html(
        f"""
        <script>
        const byteCharacters = atob("{encoded}");
        const byteNumbers = new Array(byteCharacters.length);
        for (let i = 0; i < byteCharacters.length; i++) {{
            byteNumbers[i] = byteCharacters.charCodeAt(i);
        }}
        const blob = new Blob([new Uint8Array(byteNumbers)], {{type: {safe_mime}}});
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = {safe_name};
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(link.href);
        </script>
        """,
        height=0,
    )


def focus_text_input(label: str) -> None:
    safe_label = json.dumps(label, ensure_ascii=False)
    components.html(
        f"""
        <script>
        const label = {safe_label};
        function focusInput() {{
            const input = window.parent.document.querySelector(`input[aria-label="${{label}}"]`);
            if (input) {{
                input.focus();
                input.select();
            }}
        }}
        setTimeout(focusInput, 150);
        setTimeout(focusInput, 500);
        </script>
        """,
        height=0,
    )


def process_labels_pdf(pdf_bytes: bytes, output_name: str | None = None) -> dict:
    if not pdf_bytes:
        raise ValueError("MELI no entrego un PDF de etiquetas.")
    if procesar_etiquetas_carta is None:
        raise RuntimeError(f"No pude cargar el procesador de etiquetas: {LABEL_PROCESSOR_IMPORT_ERROR}")

    labels_dir = Path(tempfile.gettempdir()) / "mascan_puppy_labels"
    result = procesar_etiquetas_carta(
        pdf_bytes,
        carpeta_salida=labels_dir,
        nombre_salida=output_name or f"Etiquetas_MELI_depuradas_{datetime.now(CHILE_TZ).strftime('%Y-%m-%d_%H%M')}.pdf",
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("mensaje") or "No pude depurar las etiquetas MELI.")
    if not result.get("pdf_bytes"):
        raise RuntimeError("El procesador no devolvio el PDF depurado.")
    return result


def meli_secret(name: str) -> str:
    return str(st.secrets.get(name, "") or "").strip()


def refresh_meli_access_token() -> str:
    client_id = meli_secret("MELI_CLIENT_ID")
    client_secret = meli_secret("MELI_CLIENT_SECRET")
    refresh_token = st.session_state.get("meli_refresh_token") or meli_secret("MELI_REFRESH_TOKEN")
    missing = [
        name
        for name, value in {
            "MELI_CLIENT_ID": client_id,
            "MELI_CLIENT_SECRET": client_secret,
            "MELI_REFRESH_TOKEN": refresh_token,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Faltan secretos de MELI en Streamlit: " + ", ".join(missing))

    data = post_form_json(
        MELI_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("MELI no devolvio access token.")

    st.session_state.meli_access_token = access_token
    st.session_state.meli_token_expires_at = int(time()) + int(data.get("expires_in") or 0)
    if data.get("refresh_token"):
        st.session_state.meli_refresh_token = str(data["refresh_token"])
    return access_token


def get_meli_access_token() -> str:
    access_token = st.session_state.get("meli_access_token")
    expires_at = int(st.session_state.get("meli_token_expires_at") or 0)
    if access_token and expires_at > int(time()) + 120:
        return access_token
    return refresh_meli_access_token()


def post_form_json(url: str, data: dict) -> dict:
    payload = urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    return read_json_response(request)


def meli_get(path: str, params: dict | None = None, extra_headers: dict | None = None) -> dict | list:
    query = f"?{urlencode(params or {})}" if params else ""
    headers = {
        "Authorization": f"Bearer {get_meli_access_token()}",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = Request(f"{MELI_API_BASE_URL}{path}{query}", headers=headers, method="GET")
    return read_json_response(request)


def meli_get_bytes(path: str, params: dict | None = None) -> bytes:
    query = f"?{urlencode(params or {})}" if params else ""
    request = Request(
        f"{MELI_API_BASE_URL}{path}{query}",
        headers={"Authorization": f"Bearer {get_meli_access_token()}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=45, context=MELI_SSL_CONTEXT) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(parse_meli_error(error)) from error


def read_json_response(request: Request) -> dict | list:
    try:
        with urlopen(request, timeout=45, context=MELI_SSL_CONTEXT) as response:
            raw_body = response.read()
    except HTTPError as error:
        raise RuntimeError(parse_meli_error(error)) from error

    try:
        return json.loads(raw_body.decode("utf-8"))
    except ValueError as error:
        raise RuntimeError("MELI respondio con un formato inesperado.") from error


def parse_meli_error(error: HTTPError) -> str:
    try:
        data = json.loads(error.read().decode("utf-8"))
    except Exception:
        return str(error)
    return str(data.get("message") or data.get("error_description") or data.get("error") or error)


def get_meli_current_user() -> dict:
    if "meli_current_user" not in st.session_state:
        st.session_state.meli_current_user = meli_get("/users/me")
    return dict(st.session_state.meli_current_user)


def get_meli_orders_page(limit: int = 50, offset: int = 0, order_status: str = "paid") -> list[dict]:
    user = get_meli_current_user()
    data = meli_get(
        "/orders/search",
        params={
            "seller": user["id"],
            "sort": "date_desc",
            "limit": min(int(limit), 50),
            "offset": int(offset),
            "order.status": order_status,
        },
    )
    return data.get("results", []) if isinstance(data, dict) else []


def get_meli_shipment(shipment_id: int | str) -> dict:
    return meli_get(f"/shipments/{shipment_id}", extra_headers={"x-format-new": "true"})


def get_meli_order_shipments(order_id: int | str) -> list[dict]:
    data = meli_get(f"/orders/{order_id}/shipments", extra_headers={"x-format-new": "true"})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data["results"]
        if data.get("id"):
            return [data]
    return []


def download_meli_shipment_labels(shipment_ids: list[str]) -> bytes:
    clean_ids = []
    for shipment_id in shipment_ids:
        text = normalize_code(shipment_id)
        if text and text not in clean_ids:
            clean_ids.append(text)
    if not clean_ids:
        raise RuntimeError("No hay etiquetas disponibles para descargar.")
    if len(clean_ids) > 50:
        raise RuntimeError("MELI permite descargar hasta 50 etiquetas por lote.")
    return meli_get_bytes(
        "/shipment_labels",
        params={"shipment_ids": ",".join(clean_ids), "response_type": "pdf"},
    )


def order_display_id(order: dict) -> str:
    return normalize_code(order.get("pack_id") or order.get("id"))


def order_shipment_ids(order: dict) -> list[str]:
    shipment_ids = []
    shipping = order.get("shipping") or {}
    shipment_id = shipping.get("id") if isinstance(shipping, dict) else None
    if shipment_id:
        shipment_ids.append(normalize_code(shipment_id))
    if shipment_ids:
        return shipment_ids

    order_id = order.get("id")
    if not order_id:
        return []
    try:
        for shipment in get_meli_order_shipments(order_id):
            shipment_id = normalize_code(shipment.get("id"))
            if shipment_id and shipment_id not in shipment_ids:
                shipment_ids.append(shipment_id)
    except Exception:
        return shipment_ids
    return shipment_ids


def extract_order_item_sku(order_item: dict) -> str:
    item = order_item.get("item") or {}
    for source in (order_item, item):
        seller_sku = normalize_code(source.get("seller_sku"))
        if seller_sku:
            return seller_sku
    for attribute_source in (order_item.get("sale_fee_details") or [], item.get("attributes") or []):
        if not isinstance(attribute_source, dict):
            continue
        if str(attribute_source.get("id") or "").upper() in {"SELLER_SKU", "SKU"}:
            value = normalize_code(attribute_source.get("value_name") or attribute_source.get("name"))
            if value:
                return value
    for variation_attribute in item.get("variation_attributes") or []:
        if str(variation_attribute.get("id") or "").upper() in {"SELLER_SKU", "SKU"}:
            value = normalize_code(variation_attribute.get("value_name") or variation_attribute.get("name"))
            if value:
                return value
    return ""


def shipping_label_from_shipment(shipment: dict) -> str:
    logistic_type = meli_logistic_type(shipment)
    mode = str(shipment.get("mode") or "").strip().lower()
    if logistic_type == "fulfillment":
        return "MELI Full"
    if logistic_type == "self_service":
        return "Mercado Envíos Flex"
    if "fulfillment" in mode:
        return "MELI Full"
    return "Colecta"


def meli_logistic_type(source: dict) -> str:
    logistic = source.get("logistic") or {}
    shipping = source.get("shipping") or {}
    tags = {str(tag).strip().lower() for tag in source.get("tags") or []}
    shipping_tags = {str(tag).strip().lower() for tag in shipping.get("tags") or []} if isinstance(shipping, dict) else set()
    logistic_type = str(
        source.get("logistic_type")
        or logistic.get("type")
        or (shipping.get("logistic_type") if isinstance(shipping, dict) else "")
        or ""
    ).strip().lower()
    mode = str(
        source.get("mode")
        or (shipping.get("mode") if isinstance(shipping, dict) else "")
        or ""
    ).strip().lower()
    all_tags = tags | shipping_tags
    if not logistic_type and ("self_service" in all_tags or "self_service_in" in all_tags):
        return "self_service"
    if not logistic_type and ("fulfillment" in all_tags or "fulfillment" in mode):
        return "fulfillment"
    return logistic_type


def shipment_is_label_ready(shipment: dict) -> bool:
    status = str(shipment.get("status") or "").strip().lower()
    substatus = str(shipment.get("substatus") or "").strip().lower()
    if status != "ready_to_ship":
        return False
    return substatus in {"ready_to_print", "printed"} or bool(shipment.get("date_first_printed"))


def shipping_label_from_order_and_shipment(order: dict, shipment: dict) -> str:
    label = shipping_label_from_shipment(shipment)
    if label != "Colecta":
        return label

    logistic_type = meli_logistic_type(order)
    mode = str((order.get("shipping") or {}).get("mode") or "").strip().lower()
    if logistic_type == "fulfillment" or "fulfillment" in mode:
        return "MELI Full"
    if logistic_type == "self_service":
        return "Mercado Envíos Flex"
    return label


def rows_for_order_item(display_id: str, sku: str, title: str, quantity: int, shipping_type: str) -> list[dict]:
    if sku in PACK_COMPONENTS:
        rows = []
        for component in PACK_COMPONENTS[sku]:
            rows.append(
                {
                    "MELI_ID": display_id,
                    "CB": component["CB"],
                    "CB alt": component["CB alt"],
                    "Nombre Producto": component["Nombre Producto"],
                    "Cant.": int(component["Cant."]) * quantity,
                    "Tipo de Despacho": shipping_type,
                }
            )
        return rows

    return [
        {
            "MELI_ID": display_id,
            "CB": sku,
            "CB alt": sku,
            "Nombre Producto": title,
            "Cant.": quantity,
            "Tipo de Despacho": shipping_type,
        }
    ]


def read_meli_daily_operation(max_pages: int = 4, page_size: int = 50) -> tuple[pd.DataFrame, list[str], pd.DataFrame]:
    rows = []
    label_shipments = []
    shipment_rows = []
    shipment_cache = {}

    for page_number in range(max_pages):
        orders = get_meli_orders_page(
            limit=page_size,
            offset=page_number * page_size,
            order_status="paid",
        )
        if not orders:
            break

        for order in orders:
            display_id = order_display_id(order)
            shipment_ids = order_shipment_ids(order)
            shipment_details = []
            ready_shipments = []
            for shipment_id in shipment_ids:
                if shipment_id not in shipment_cache:
                    try:
                        shipment_cache[shipment_id] = get_meli_shipment(shipment_id)
                    except Exception:
                        shipment_cache[shipment_id] = {}
                shipment = shipment_cache.get(shipment_id) or {}
                if shipment:
                    shipment_details.append(shipment)
                    label_ready = shipment_is_label_ready(shipment)
                    if label_ready:
                        ready_shipments.append(shipment)
                    if label_ready and shipment_id not in label_shipments:
                        label_shipments.append(shipment_id)
                    shipment_rows.append(
                        {
                            "MELI_ID": display_id,
                            "shipment_id": shipment_id,
                            "estado": shipment.get("status", ""),
                            "subestado": shipment.get("substatus", ""),
                            "despacho": shipping_label_from_order_and_shipment(order, shipment),
                            "etiqueta": "Disponible" if label_ready else "No disponible",
                        }
                    )

            if not ready_shipments:
                continue

            shipping_type = "Colecta"
            if ready_shipments:
                shipping_type = shipping_label_from_order_and_shipment(order, ready_shipments[0])

            for order_item in order.get("order_items") or []:
                item = order_item.get("item") or {}
                sku = extract_order_item_sku(order_item)
                rows.extend(
                    rows_for_order_item(
                        display_id=display_id,
                        sku=sku,
                        title=item.get("title") or "",
                        quantity=int(order_item.get("quantity") or 0),
                        shipping_type=shipping_type,
                    )
                )

        if len(orders) < page_size:
            break

    orders_table = pd.DataFrame(rows, columns=REQUIRED_COLUMNS)
    if not orders_table.empty:
        orders_table = clean_orders(orders_table)
    shipments_table = pd.DataFrame(
        shipment_rows,
        columns=["MELI_ID", "shipment_id", "estado", "subestado", "despacho", "etiqueta"],
    ).drop_duplicates()
    return orders_table, label_shipments, shipments_table


def sample_orders() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "MELI_ID": "2000013898103277",
                "CB": "SD06000030621",
                "CB alt": "SD06000030621",
                "Nombre Producto": "BW 00003-Plato Z doble-S-Fucsia",
                "Cant.": 1,
                "Tipo de Despacho": "Colecta",
            },
            {
                "MELI_ID": "2000013906247377",
                "CB": "SD06000140619",
                "CB alt": "SD06000140619",
                "Nombre Producto": "BW 00014-Plato huellitas-S-Celeste",
                "Cant.": 1,
                "Tipo de Despacho": "Mercado Envíos Flex",
            },
            {
                "MELI_ID": "2000013906247377",
                "CB": "SD06000140625",
                "CB alt": "SD06000140625",
                "Nombre Producto": "BW 00014-Plato huellitas-S-Rosado",
                "Cant.": 1,
                "Tipo de Despacho": "Mercado Envíos Flex",
            },
        ]
    )


def clean_orders(dataframe: pd.DataFrame) -> pd.DataFrame:
    dataframe = dataframe.copy()

    if not all(column in dataframe.columns for column in REQUIRED_COLUMNS):
        dataframe = convert_mascan_daily_sales(dataframe)

    missing = [column for column in REQUIRED_COLUMNS if column not in dataframe.columns]
    if missing:
        raise ValueError("Faltan columnas: " + ", ".join(missing))

    orders = dataframe[REQUIRED_COLUMNS].copy()
    for column in ("MELI_ID", "CB", "CB alt"):
        orders[column] = orders[column].map(normalize_code)
    orders["Cant."] = pd.to_numeric(orders["Cant."], errors="coerce").fillna(0).astype(int)
    orders = orders[(orders["MELI_ID"] != "") & (orders["CB"] != "") & (orders["Cant."] > 0)]
    return orders.reset_index(drop=True)


def convert_mascan_daily_sales(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Convierte Ventas_del_dia_MELI_depuradas.xlsx al formato simple Puppy."""
    required = {"# de venta", "Estado", "Unidades", "SKU"}
    if not required.issubset(set(dataframe.columns)):
        raise ValueError(
            "No reconozco el formato. Puedes cargar el Excel depurado de MasCan APP "
            "o una tabla con columnas: " + ", ".join(REQUIRED_COLUMNS)
        )

    dataframe = dataframe.copy()
    shipping_column = first_existing_column(
        dataframe,
        ["Tipo de Despacho", "Centro de envío", "Centro de envio", "Forma de entrega"],
    )
    dataframe["__puppy_meli_id"] = dataframe["# de venta"].map(normalize_code)
    dataframe["__puppy_shipping"] = dataframe[shipping_column] if shipping_column else ""

    active_package_id = ""
    active_package_shipping = ""
    package_rows_left = 0
    for index, row in dataframe.iterrows():
        state = str(row["Estado"]).strip()
        package_match = re.fullmatch(r"Paquete de\s+(\d+)\s+productos?", state, flags=re.IGNORECASE)
        if package_match:
            active_package_id = normalize_code(row["# de venta"])
            active_package_shipping = str(row[shipping_column]).strip() if shipping_column else ""
            package_rows_left = int(package_match.group(1))
            continue

        if state in PRODUCT_STATES and active_package_id and package_rows_left > 0:
            dataframe.at[index, "__puppy_meli_id"] = active_package_id
            if not str(dataframe.at[index, "__puppy_shipping"]).strip():
                dataframe.at[index, "__puppy_shipping"] = active_package_shipping
            package_rows_left -= 1
            if package_rows_left == 0:
                active_package_id = ""
                active_package_shipping = ""

    product_rows = dataframe[dataframe["Estado"].isin(PRODUCT_STATES)].copy()
    if product_rows.empty:
        raise ValueError("No encontré filas de productos en el archivo.")

    title_column = "Título de la publicación" if "Título de la publicación" in product_rows.columns else "SKU"

    converted = pd.DataFrame(
        {
            "MELI_ID": product_rows["__puppy_meli_id"],
            "CB": product_rows["SKU"],
            "CB alt": product_rows["SKU"],
            "Nombre Producto": product_rows[title_column],
            "Cant.": product_rows["Unidades"],
            "Tipo de Despacho": product_rows["__puppy_shipping"],
        }
    )
    return converted


def first_existing_column(dataframe: pd.DataFrame, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in dataframe.columns:
            return candidate
    return None


def make_unique_headers(headers: list[str]) -> list[str]:
    seen = {}
    unique_headers = []
    for header in headers:
        header = str(header).strip()
        if not header:
            unique_headers.append("")
            continue
        count = seen.get(header, 0) + 1
        seen[header] = count
        unique_headers.append(header if count == 1 else f"{header}__{count}")
    return unique_headers


def read_excel_with_detected_header(uploaded_file) -> pd.DataFrame:
    sheets = pd.read_excel(uploaded_file, sheet_name=None, header=None, dtype=str)
    sheet = sheets.get("Ventas depuradas")
    if sheet is None:
        sheet = next(iter(sheets.values()))

    header_row = None
    for idx, row in sheet.iterrows():
        values = {str(value).strip() for value in row.tolist() if pd.notna(value)}
        if "MELI_ID" in values or "# de venta" in values:
            header_row = idx
            break

    if header_row is None:
        return pd.read_excel(uploaded_file, dtype=str)

    headers = make_unique_headers(sheet.iloc[header_row].fillna("").astype(str).str.strip().tolist())
    data = sheet.iloc[header_row + 1 :].copy()
    data.columns = headers
    data = data.loc[:, [column for column in data.columns if column]]
    return data.reset_index(drop=True)


def load_uploaded_orders(uploaded_file) -> pd.DataFrame:
    if uploaded_file is None:
        return sample_orders()
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file, dtype=str)
    return read_excel_with_detected_header(uploaded_file)


def initialize_state(orders: pd.DataFrame) -> None:
    fingerprint = "|".join(orders["MELI_ID"].astype(str).tolist()) + f":{len(orders)}"
    if st.session_state.get("orders_fingerprint") == fingerprint:
        return

    st.session_state.orders_fingerprint = fingerprint
    st.session_state.orders = orders
    st.session_state.scanned = {}
    st.session_state.scan_history = []
    st.session_state.loaded_packages = []
    st.session_state.load_history = []
    st.session_state.selected_order = None
    set_last_message("Pedidos cargados.")


def order_rows(order_id: str) -> pd.DataFrame:
    orders = st.session_state.orders
    return orders[orders["MELI_ID"] == order_id].copy()


def expected_by_code(order_id: str) -> dict[str, int]:
    rows = order_rows(order_id)
    expected = {}
    for _, row in rows.iterrows():
        cb = normalize_code(row["CB"])
        alt = normalize_code(row["CB alt"])
        qty = int(row["Cant."])
        expected[cb] = expected.get(cb, 0) + qty
        if alt and alt != cb:
            expected[alt] = expected.get(alt, 0) + qty
    return expected


def canonical_for_scan(order_id: str, code: str) -> str | None:
    code = normalize_code(code)
    scanned_variants = code_variants(code)
    rows = order_rows(order_id)
    for _, row in rows.iterrows():
        cb = normalize_code(row["CB"])
        alt = normalize_code(row["CB alt"])
        valid_variants = code_variants(cb) | code_variants(alt)
        if scanned_variants & valid_variants:
            return cb
    return None


def scanned_for_order(order_id: str) -> dict[str, int]:
    return st.session_state.scanned.setdefault(order_id, {})


def is_order_complete(order_id: str) -> bool:
    rows = order_rows(order_id)
    scanned = scanned_for_order(order_id)
    for _, row in rows.iterrows():
        cb = normalize_code(row["CB"])
        if scanned.get(cb, 0) < int(row["Cant."]):
            return False
    return True


def all_orders_complete() -> bool:
    if "orders" not in st.session_state:
        return False
    order_ids = st.session_state.orders["MELI_ID"].astype(str).unique()
    return len(order_ids) > 0 and all(is_order_complete(order_id) for order_id in order_ids)


def loaded_package_ids() -> set[str]:
    return set(st.session_state.setdefault("loaded_packages", []))


def all_packages_loaded() -> bool:
    if "orders" not in st.session_state:
        return False
    order_ids = set(st.session_state.orders["MELI_ID"].astype(str).unique())
    return len(order_ids) > 0 and order_ids.issubset(loaded_package_ids())


def product_exists_anywhere(code: str) -> bool:
    scanned_variants = code_variants(code)
    if not scanned_variants or "orders" not in st.session_state:
        return False
    for _, row in st.session_state.orders.iterrows():
        variants = code_variants(row["CB"]) | code_variants(row["CB alt"])
        if scanned_variants & variants:
            return True
    return False


def process_order_scan(raw_order_id: str) -> None:
    order_id = normalize_code(raw_order_id)
    if not order_id:
        return
    all_orders = set(st.session_state.orders["MELI_ID"].astype(str))
    if order_id in all_orders:
        if is_order_complete(order_id):
            st.session_state.selected_order = None
            set_last_message("Pedido ya ingresado.")
            return
        st.session_state.selected_order = order_id
        set_last_message("Pedido cargado.")
        return

    st.session_state.selected_order = None
    set_last_message("Pedido no existe.")


def process_product_scan(raw_code: str) -> None:
    code = normalize_code(raw_code)
    if not code:
        return

    order_id = st.session_state.get("selected_order")
    if not order_id:
        set_last_message("Primero ingresa un MELI ID.")
        return

    canonical = canonical_for_scan(order_id, code)
    if not canonical:
        if product_exists_anywhere(code):
            set_last_message("Producto no pertenece al pedido.")
        else:
            set_last_message("Producto no existe.")
        return

    expected = expected_by_code(order_id)
    scanned = scanned_for_order(order_id)
    if scanned.get(canonical, 0) >= expected.get(canonical, 0):
        set_last_message("Producto sobrante.")
        return

    scanned[canonical] = scanned.get(canonical, 0) + 1
    st.session_state.scan_history.append({"order_id": order_id, "code": canonical})
    if is_order_complete(order_id):
        st.session_state.selected_order = None
        if all_orders_complete():
            set_last_message("Todos los pedidos fueron revisados con éxito.")
        else:
            set_last_message("Pedido terminado con éxito.")
    else:
        set_last_message("Producto correcto.")


def undo_last_scan() -> None:
    history = st.session_state.get("scan_history", [])
    if not history:
        set_last_message("No hay lecturas para deshacer.")
        return

    last_scan = history.pop()
    order_id = last_scan["order_id"]
    code = last_scan["code"]
    scanned = scanned_for_order(order_id)
    scanned[code] = max(scanned.get(code, 0) - 1, 0)
    if scanned[code] == 0:
        scanned.pop(code, None)
    st.session_state.selected_order = order_id
    set_last_message("Última lectura deshecha.")


def process_package_scan(raw_order_id: str) -> None:
    order_id = normalize_code(raw_order_id)
    if not order_id:
        return

    all_orders = set(st.session_state.orders["MELI_ID"].astype(str))
    if order_id not in all_orders:
        set_last_message("Paquete no existe.")
        return

    if not is_order_complete(order_id):
        set_last_message("Pedido pendiente de revisión.")
        return

    loaded = st.session_state.setdefault("loaded_packages", [])
    if order_id in loaded:
        set_last_message("Paquete ya cargado.")
        return

    loaded.append(order_id)
    st.session_state.setdefault("load_history", []).append(
        {
            "MELI_ID": order_id,
            "fecha_hora": datetime.now(CHILE_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    if all_packages_loaded():
        summary = shipping_summary(st.session_state.orders)
        message = (
            "Todos los paquetes fueron cargados con éxito. "
            f"Hay {summary['flex']} Flex, {summary['colecta']} Colecta y {summary['full']} Full."
        )
        set_last_message(message)
    else:
        set_last_message("Paquete cargado.")


def reset_day_state() -> None:
    keys_to_clear = [
        "orders",
        "orders_fingerprint",
        "scanned",
        "scan_history",
        "loaded_packages",
        "load_history",
        "selected_order",
        "meli_label_shipments",
        "meli_shipments_table",
        "meli_labels_pdf",
        "meli_labels_raw_pdf",
        "meli_labels_file_name",
        "meli_labels_message",
        "meli_labels_error",
        "meli_labels_summary",
        "meli_labels_auto_downloaded",
        "last_message",
        "last_spoken_message",
    ]
    for key in keys_to_clear:
        st.session_state.pop(key, None)
    st.session_state.order_input_counter = st.session_state.get("order_input_counter", 0) + 1
    st.session_state.product_input_counter = st.session_state.get("product_input_counter", 0) + 1
    st.session_state.package_input_counter = st.session_state.get("package_input_counter", 0) + 1


def status_table() -> pd.DataFrame:
    rows = []
    for order_id, group in st.session_state.orders.groupby("MELI_ID", sort=False):
        rows.append(
            {
                "MELI_ID": order_id,
                "Productos": int(len(group)),
                "Unidades": int(group["Cant."].sum()),
                "Tipo de Despacho": group["Tipo de Despacho"].iloc[0],
                "Estado": "Listo" if is_order_complete(order_id) else "Pendiente",
            }
        )
    return pd.DataFrame(rows)


def status_table_style(dataframe: pd.DataFrame):
    def color_estado(value):
        text = str(value).strip().lower()
        if text == "listo":
            return "background-color: #d9f7d9; color: #0b6b0b; font-weight: 700"
        if text == "cargado":
            return "background-color: #d9f7d9; color: #0b6b0b; font-weight: 700"
        if text == "pendiente":
            return "background-color: #ffd9d9; color: #9b111e; font-weight: 700"
        return ""

    status_columns = [column for column in dataframe.columns if "Estado" in str(column)]
    if dataframe.empty or not status_columns:
        return dataframe
    styler = dataframe.style
    if hasattr(styler, "map"):
        return styler.map(color_estado, subset=status_columns)
    return styler.applymap(color_estado, subset=status_columns)


def detail_status_table() -> pd.DataFrame:
    rows = []
    grouped = st.session_state.orders.groupby(
        ["MELI_ID", "CB", "CB alt", "Nombre Producto", "Tipo de Despacho"],
        sort=False,
        dropna=False,
    )
    for keys, group in grouped:
        order_id, cb, cb_alt, product_name, shipping_type = keys
        canonical = normalize_code(cb)
        expected = int(group["Cant."].sum())
        read = scanned_for_order(str(order_id)).get(canonical, 0)
        pending = max(expected - read, 0)
        rows.append(
            {
                "MELI_ID": order_id,
                "CB": canonical,
                "CB alt": normalize_code(cb_alt),
                "Nombre Producto": product_name,
                "Tipo de Despacho": shipping_type,
                "Esperado": expected,
                "Leido": read,
                "Pendiente": pending,
                "Estado": "Listo" if pending == 0 else "Pendiente",
            }
        )
    return pd.DataFrame(rows)


def scan_history_table() -> pd.DataFrame:
    rows = []
    for index, scan in enumerate(st.session_state.get("scan_history", []), start=1):
        order_id = scan["order_id"]
        code = scan["code"]
        product_rows = order_rows(order_id)
        product_rows = product_rows[product_rows["CB"].map(normalize_code) == code]
        product_name = ""
        if not product_rows.empty:
            product_name = product_rows["Nombre Producto"].iloc[0]
        rows.append(
            {
                "N": index,
                "MELI_ID": order_id,
                "CB": code,
                "Nombre Producto": product_name,
            }
        )
    return pd.DataFrame(rows)


def package_status_table() -> pd.DataFrame:
    rows = []
    loaded = loaded_package_ids()
    for order_id, group in st.session_state.orders.groupby("MELI_ID", sort=False):
        order_id = str(order_id)
        order_ready = is_order_complete(order_id)
        package_loaded = order_id in loaded
        rows.append(
            {
                "MELI_ID": order_id,
                "Productos": int(len(group)),
                "Unidades": int(group["Cant."].sum()),
                "Tipo de Despacho": group["Tipo de Despacho"].iloc[0],
                "Estado pedido": "Listo" if order_ready else "Pendiente",
                "Estado paquete": "Cargado" if package_loaded else "Pendiente",
            }
        )
    return pd.DataFrame(rows)


def load_history_table() -> pd.DataFrame:
    rows = []
    for index, item in enumerate(st.session_state.get("load_history", []), start=1):
        rows.append(
            {
                "N": index,
                "MELI_ID": item.get("MELI_ID", ""),
                "Fecha y hora": item.get("fecha_hora", ""),
            }
        )
    return pd.DataFrame(rows)


def shipping_summary(orders: pd.DataFrame) -> dict[str, int]:
    order_summary = orders.groupby("MELI_ID", sort=False)["Tipo de Despacho"].first().fillna("")
    flex_count = int(order_summary.str.contains("flex", case=False, na=False).sum())
    full_count = int(order_summary.str.contains("full", case=False, na=False).sum())
    total_orders = int(order_summary.shape[0])
    return {
        "total": total_orders,
        "flex": flex_count,
        "colecta": total_orders - flex_count - full_count,
        "full": full_count,
    }


def order_control_summary() -> dict[str, int]:
    summary = shipping_summary(st.session_state.orders)
    status = status_table()
    ready = int((status["Estado"] == "Listo").sum()) if not status.empty else 0
    summary["ready"] = ready
    summary["pending"] = summary["total"] - ready
    return summary


def render_order_metrics() -> None:
    summary = order_control_summary()
    total_col, flex_col, colecta_col, full_col, ready_col, pending_col = st.columns(6)
    total_col.metric("Pedidos", summary["total"])
    flex_col.metric("Flex", summary["flex"])
    colecta_col.metric("Colecta", summary["colecta"])
    full_col.metric("Full", summary["full"])
    ready_col.metric("Revisados", summary["ready"])
    pending_col.metric("Pendientes", summary["pending"])


def meli_update_window_status(now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now(CHILE_TZ)
    current_minutes = (now.hour * 60) + now.minute
    start_minutes = (11 * 60) + 5
    end_minutes = 15 * 60

    if current_minutes < start_minutes:
        return (
            False,
            f"La descarga MELI se habilita a las 11:05 hora Chile. Hora actual: {now.strftime('%H:%M')}.",
        )
    if current_minutes >= end_minutes:
        return (
            False,
            f"La descarga MELI se bloquea desde las 15:00 hora Chile. Hora actual: {now.strftime('%H:%M')}.",
        )
    return True, f"Descarga MELI habilitada hasta las 15:00 hora Chile. Hora actual: {now.strftime('%H:%M')}."


def render_home() -> None:
    st.subheader("Inicio")
    st.write(
        "Esta es la versión mínima de MasCan Puppy APP. Primero la usaremos con archivo cargado; "
        "después conectamos MELI y etiquetas directamente."
    )
    st.info("La pistola lectora funciona como teclado: escanea y presiona Enter automáticamente.")


def render_daily_sales() -> None:
    st.subheader("Ventas del día")
    st.write("Conexión MELI directa")
    can_update_meli, update_window_message = meli_update_window_status()
    if can_update_meli:
        st.info(update_window_message)
    else:
        st.warning(update_window_message)

    if st.button("Actualizar ventas y etiquetas desde MELI", disabled=not can_update_meli):
        try:
            with st.spinner("Leyendo ventas y etiquetas desde MELI..."):
                orders, shipment_ids, shipments_table = read_meli_daily_operation()
                initialize_state(orders)
                st.session_state.meli_label_shipments = shipment_ids
                st.session_state.meli_shipments_table = shipments_table
                st.session_state.meli_labels_pdf = b""
                st.session_state.meli_labels_raw_pdf = b""
                st.session_state.meli_labels_file_name = ""
                st.session_state.meli_labels_message = ""
                st.session_state.meli_labels_error = ""
                st.session_state.meli_labels_summary = {}
                st.session_state.meli_labels_auto_downloaded = False
                if shipment_ids:
                    raw_pdf = download_meli_shipment_labels(shipment_ids)
                    st.session_state.meli_labels_raw_pdf = raw_pdf
                    label_file_name = f"Etiquetas_MELI_{datetime.now(CHILE_TZ).strftime('%Y-%m-%d_%H%M')}.pdf"
                    processed = process_labels_pdf(raw_pdf, label_file_name)
                    st.session_state.meli_labels_pdf = processed["pdf_bytes"]
                    st.session_state.meli_labels_file_name = label_file_name
                    st.session_state.meli_labels_message = processed.get("mensaje", "")
                    st.session_state.meli_labels_summary = processed.get("resumen", {})
                set_last_message("Ventas cargadas desde MELI.")
                st.success("Ventas y etiquetas leídas desde MELI.")
        except Exception as error:
            st.error("No pude leer ventas o etiquetas desde MELI.")
            st.caption(str(error))

    if st.session_state.get("meli_labels_pdf") and not st.session_state.get("meli_labels_auto_downloaded"):
        auto_download_bytes(
            st.session_state.meli_labels_pdf,
            st.session_state.meli_labels_file_name or "Etiquetas_MELI.pdf",
            "application/pdf",
        )
        st.session_state.meli_labels_auto_downloaded = True

    with st.expander("Modo prueba temporal: cargar archivo manual"):
        st.caption("Solo para probar Control de pedidos y Control de carga cuando ya no se puede leer MELI.")
        uploaded = st.file_uploader("Cargar ventas preparadas (.xlsx o .csv)", type=["xlsx", "csv"])
        if uploaded is not None:
            try:
                orders = clean_orders(load_uploaded_orders(uploaded))
                initialize_state(orders)
                st.session_state.meli_label_shipments = []
                st.session_state.meli_shipments_table = pd.DataFrame()
                st.session_state.meli_labels_pdf = b""
                st.session_state.meli_labels_raw_pdf = b""
                st.session_state.meli_labels_file_name = ""
                st.session_state.meli_labels_message = ""
                st.session_state.meli_labels_error = ""
                st.session_state.meli_labels_summary = {}
                st.session_state.meli_labels_auto_downloaded = False
                set_last_message("Ventas cargadas desde archivo de prueba.")
                st.success("Archivo de prueba cargado.")
                st.rerun()
            except Exception as error:
                st.error("No pude cargar el archivo de prueba.")
                st.caption(str(error))

    try:
        if "orders" in st.session_state:
            orders = st.session_state.orders
        else:
            st.info("Todavía no hay ventas cargadas. Usa el botón de MELI o el modo prueba temporal.")
            return
        summary = shipping_summary(orders)
        st.success(f"Pedidos cargados: {summary['total']} · Productos: {len(orders)}")
        total_col, flex_col, colecta_col, full_col, products_col = st.columns(5)
        total_col.metric("Pedidos", summary["total"])
        flex_col.metric("Flex", summary["flex"])
        colecta_col.metric("Colecta", summary["colecta"])
        full_col.metric("Full", summary["full"])
        products_col.metric("Productos", len(orders))
        st.dataframe(orders, use_container_width=True, hide_index=True)
    except Exception as error:
        st.error("No pude cargar las ventas.")
        st.caption(str(error))


def render_labels() -> None:
    st.subheader("Etiquetas")
    st.caption("Etiquetas descargadas desde MELI y depuradas con el procesador MasCan.")
    labels_pdf = st.session_state.get("meli_labels_pdf", b"")
    label_shipments = st.session_state.get("meli_label_shipments", [])
    if labels_pdf:
        st.success(f"Etiquetas depuradas listas: {len(label_shipments)} envíos.")
        if st.session_state.get("meli_labels_message"):
            st.caption(st.session_state.meli_labels_message)
        if st.session_state.get("meli_labels_summary"):
            summary = st.session_state.meli_labels_summary
            st.caption(
                "Resumen: "
                f"{summary.get('validadas', 0)} validadas, "
                f"{summary.get('revision_manual', 0)} en revision manual."
            )
        st.download_button(
            "Descargar etiquetas MELI depuradas",
            data=labels_pdf,
            file_name=st.session_state.get("meli_labels_file_name") or "Etiquetas_MELI.pdf",
            mime="application/pdf",
        )
    else:
        st.info("Primero usa `Actualizar ventas y etiquetas desde MELI` en Ventas del dia.")

def render_order_control() -> None:
    st.subheader("Control de pedidos")
    if "orders" not in st.session_state:
        st.info("Primero carga las ventas del día.")
        return
    if "order_input_counter" not in st.session_state:
        st.session_state.order_input_counter = 0
    if "product_input_counter" not in st.session_state:
        st.session_state.product_input_counter = 0

    current_message = st.session_state.get("last_message", "Listo para escanear.")
    st.info(current_message)
    speak_once(current_message)
    render_order_metrics()

    if st.button("Deshacer última lectura", disabled=not st.session_state.get("scan_history")):
        undo_last_scan()
        st.rerun()

    selected = st.session_state.get("selected_order")
    if not selected:
        order_key = f"order_input_{st.session_state.order_input_counter}"
        order_scan = st.text_input("MELI ID del pedido", key=order_key, placeholder="Escanea o escribe el MELI ID")
        focus_text_input("MELI ID del pedido")
        if order_scan:
            process_order_scan(order_scan)
            st.session_state.order_input_counter += 1
            st.rerun()
        st.divider()
        st.write("Resumen")
        order_status = status_table()
        st.dataframe(status_table_style(order_status), use_container_width=True, hide_index=True)
        return

    product_key = f"product_input_{st.session_state.product_input_counter}"
    product_scan = st.text_input("Código del producto", key=product_key, placeholder="Escanea el producto")
    focus_text_input("Código del producto")
    if product_scan:
        process_product_scan(product_scan)
        st.session_state.product_input_counter += 1
        st.rerun()

    if selected:
        st.write(f"Pedido activo: `{selected}`")
        rows = order_rows(selected)
        scanned = scanned_for_order(selected)
        detail_rows = []
        for _, row in rows.iterrows():
            cb = normalize_code(row["CB"])
            expected = int(row["Cant."])
            read = scanned.get(cb, 0)
            detail_rows.append(
                {
                    "CB": cb,
                    "Producto": row["Nombre Producto"],
                    "Esperado": expected,
                    "Leído": read,
                    "Pendiente": max(expected - read, 0),
                }
            )
        st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)

    st.divider()
    st.write("Resumen")
    order_status = status_table()
    st.dataframe(status_table_style(order_status), use_container_width=True, hide_index=True)


def render_package_metrics() -> None:
    status = package_status_table()
    summary = shipping_summary(st.session_state.orders)
    loaded_count = int((status["Estado paquete"] == "Cargado").sum()) if not status.empty else 0
    pending_count = summary["total"] - loaded_count
    total_col, flex_col, colecta_col, full_col, loaded_col, pending_col = st.columns(6)
    total_col.metric("Paquetes", summary["total"])
    flex_col.metric("Flex", summary["flex"])
    colecta_col.metric("Colecta", summary["colecta"])
    full_col.metric("Full", summary["full"])
    loaded_col.metric("Cargados", loaded_count)
    pending_col.metric("Pendientes", pending_count)


def render_package_control() -> None:
    st.subheader("Control de paquetes")
    st.caption("Escanea cada etiqueta antes de subir el paquete al transporte.")
    if "orders" not in st.session_state:
        st.info("Primero carga las ventas del día.")
        return
    if "package_input_counter" not in st.session_state:
        st.session_state.package_input_counter = 0

    current_message = st.session_state.get("last_message", "Listo para escanear paquetes.")
    st.info(current_message)
    speak_once(current_message)
    render_package_metrics()

    package_key = f"package_input_{st.session_state.package_input_counter}"
    package_scan = st.text_input("MELI ID del paquete", key=package_key, placeholder="Escanea el MELI ID de la etiqueta")
    focus_text_input("MELI ID del paquete")
    if package_scan:
        process_package_scan(package_scan)
        st.session_state.package_input_counter += 1
        st.rerun()

    st.divider()
    st.write("Resumen")
    status = package_status_table()
    st.dataframe(status_table_style(status), use_container_width=True, hide_index=True)

    loaded_history = load_history_table()
    if not loaded_history.empty:
        with st.expander("Ver paquetes cargados"):
            st.dataframe(loaded_history, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Inicializar día")
    st.warning("Esto borra las ventas cargadas, lecturas, etiquetas y avance del día en esta sesión.")
    confirmation = st.text_input("Para inicializar escribe INICIALIZAR", key="reset_day_confirmation")
    if st.button("Inicializar", disabled=confirmation.strip().upper() != "INICIALIZAR"):
        reset_day_state()
        st.success("Día inicializado. Vuelve a Ventas del día para cargar MELI nuevamente.")
        st.rerun()


def render_download_state() -> None:
    if "orders" not in st.session_state:
        return
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        st.session_state.orders.to_excel(writer, index=False, sheet_name="Pedidos")
        status_table().to_excel(writer, index=False, sheet_name="Estado")
        detail_status_table().to_excel(writer, index=False, sheet_name="Detalle")
        scan_history_table().to_excel(writer, index=False, sheet_name="Lecturas")
        package_status_table().to_excel(writer, index=False, sheet_name="Paquetes")
        load_history_table().to_excel(writer, index=False, sheet_name="Carga paquetes")
    st.download_button(
        "Descargar estado Excel",
        data=output.getvalue(),
        file_name=f"mascan_puppy_estado_{datetime.now().strftime('%Y-%m-%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main() -> None:
    page_setup()
    if not require_app_password():
        return

    module = st.sidebar.radio(
        "Módulo",
        ["Ventas del día", "Etiquetas", "Control de pedidos", "Control de paquetes"],
    )

    if module == "Ventas del día":
        render_daily_sales()
    elif module == "Etiquetas":
        render_labels()
    elif module == "Control de pedidos":
        render_order_control()
    elif module == "Control de paquetes":
        render_package_control()

    st.sidebar.divider()
    render_download_state()


if __name__ == "__main__":
    main()
