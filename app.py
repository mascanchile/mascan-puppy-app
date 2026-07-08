from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pandas as pd
import streamlit as st


APP_NAME = "MasCan Puppy APP"

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

    product_rows = dataframe[dataframe["Estado"].isin(PRODUCT_STATES)].copy()
    if product_rows.empty:
        raise ValueError("No encontré filas de productos en el archivo.")

    title_column = "Título de la publicación" if "Título de la publicación" in product_rows.columns else "SKU"
    shipping_column = first_existing_column(
        product_rows,
        ["Tipo de Despacho", "Centro de envío", "Centro de envio", "Forma de entrega"],
    )

    converted = pd.DataFrame(
        {
            "MELI_ID": product_rows["# de venta"],
            "CB": product_rows["SKU"],
            "CB alt": product_rows["SKU"],
            "Nombre Producto": product_rows[title_column],
            "Cant.": product_rows["Unidades"],
            "Tipo de Despacho": product_rows[shipping_column] if shipping_column else "",
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
    st.session_state.selected_order = None
    st.session_state.last_message = "Pedidos cargados."


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
    rows = order_rows(order_id)
    for _, row in rows.iterrows():
        cb = normalize_code(row["CB"])
        alt = normalize_code(row["CB alt"])
        if code in {cb, alt}:
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


def process_scan(raw_code: str) -> None:
    code = normalize_code(raw_code)
    if not code:
        return

    all_orders = set(st.session_state.orders["MELI_ID"].astype(str))
    if code in all_orders:
        st.session_state.selected_order = code
        st.session_state.last_message = f"Pedido seleccionado: {code}"
        return

    order_id = st.session_state.get("selected_order")
    if not order_id:
        st.session_state.last_message = "Primero escanea una etiqueta/pedido."
        return

    canonical = canonical_for_scan(order_id, code)
    if not canonical:
        st.session_state.last_message = "Producto no pertenece al pedido."
        return

    expected = expected_by_code(order_id)
    scanned = scanned_for_order(order_id)
    if scanned.get(canonical, 0) >= expected.get(canonical, 0):
        st.session_state.last_message = "Producto sobrante."
        return

    scanned[canonical] = scanned.get(canonical, 0) + 1
    if is_order_complete(order_id):
        st.session_state.last_message = "Pedido terminado con éxito."
    else:
        st.session_state.last_message = "Producto correcto."


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


def render_home() -> None:
    st.subheader("Inicio")
    st.write(
        "Esta es la versión mínima de MasCan Puppy APP. Primero la usaremos con archivo cargado; "
        "después conectamos MELI y etiquetas directamente."
    )
    st.info("La pistola lectora funciona como teclado: escanea y presiona Enter automáticamente.")


def render_daily_sales() -> None:
    st.subheader("Ventas del día")
    uploaded = st.file_uploader("Cargar ventas preparadas para Manuel (.xlsx o .csv)", type=["xlsx", "csv"])
    try:
        orders = clean_orders(load_uploaded_orders(uploaded))
        initialize_state(orders)
        st.success(f"Pedidos cargados: {orders['MELI_ID'].nunique()} · Productos: {len(orders)}")
        st.dataframe(orders, use_container_width=True, hide_index=True)
    except Exception as error:
        st.error("No pude cargar las ventas.")
        st.caption(str(error))

    st.divider()
    st.write("Conexión MELI directa")
    st.caption("Pendiente: leer ventas del día desde la API usando secretos de Streamlit Cloud.")
    if st.button("Actualizar ventas desde MELI", disabled=True):
        st.write("Pendiente")


def render_labels() -> None:
    st.subheader("Etiquetas")
    st.caption("Pendiente: descargar etiquetas desde MELI y dejarlas listas para impresión.")
    st.button("Descargar etiquetas desde MELI", disabled=True)
    uploaded = st.file_uploader("Mientras tanto, cargar PDF de etiquetas manualmente", type=["pdf"])
    if uploaded is not None:
        st.success(f"PDF cargado: {uploaded.name}")


def render_order_control() -> None:
    st.subheader("Control de pedidos")
    if "orders" not in st.session_state:
        initialize_state(sample_orders())

    st.info(st.session_state.get("last_message", "Listo para escanear."))
    scan = st.text_input("Escanear etiqueta o producto", key="scan_input", placeholder="Escanea aquí")
    if scan:
        process_scan(scan)
        st.session_state.scan_input = ""
        st.rerun()

    selected = st.session_state.get("selected_order")
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
    st.dataframe(status_table(), use_container_width=True, hide_index=True)


def render_load_control() -> None:
    st.subheader("Control de carga")
    st.caption("Segunda etapa: escanear cada paquete antes de subirlo al transporte.")
    st.info("Todavía no implementado. Primero validamos Control de pedidos.")


def render_download_state() -> None:
    if "orders" not in st.session_state:
        return
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        st.session_state.orders.to_excel(writer, index=False, sheet_name="Pedidos")
        status_table().to_excel(writer, index=False, sheet_name="Estado")
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
        ["Inicio", "Ventas del día", "Etiquetas", "Control de pedidos", "Control de carga"],
    )

    if module == "Inicio":
        render_home()
    elif module == "Ventas del día":
        render_daily_sales()
    elif module == "Etiquetas":
        render_labels()
    elif module == "Control de pedidos":
        render_order_control()
    elif module == "Control de carga":
        render_load_control()

    st.sidebar.divider()
    render_download_state()


if __name__ == "__main__":
    main()
