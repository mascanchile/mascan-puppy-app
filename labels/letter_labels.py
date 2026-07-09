# pip install pymupdf opencv-python pillow pandas reportlab

import re
import fitz
import cv2
import numpy as np
import pandas as pd
from io import BytesIO
from pathlib import Path
from datetime import datetime
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.graphics.barcode import code128


DEFAULT_TEMP_DIR = (
    Path.home()
    / "Documents"
    / "MasCan APP Temp"
    / "Etiquetas"
)

PARES_POR_HOJA = 3

MARGEN_SUPERIOR_CM = 0.8
MARGEN_INFERIOR_CM = 0.8
MARGEN_IZQUIERDO_CM = 0.8
MARGEN_DERECHO_CM = 0.8

SEPARACION_VERTICAL_CM = 0.35
DPI = 200
MAX_PDFS_GUARDADOS = 10


# ==================================================
# UTILIDADES
# ==================================================

def limpiar_numero(txt):
    return re.sub(r"\D", "", txt or "")


def extraer_pack_id(texto):
    m = re.search(r"Pack\s*ID:\s*([0-9 ]+)", texto, re.I)
    return limpiar_numero(m.group(1)) if m else ""


def extraer_venta(texto):
    m = re.search(r"Venta:\s*([0-9 ]+)", texto, re.I)
    return limpiar_numero(m.group(1)) if m else ""


def extraer_clave_etiqueta(texto):
    """
    Regla:
    1. Si existe Pack ID, usar Pack ID.
    2. Si no existe Pack ID, usar Venta.
    """

    pack_id = extraer_pack_id(texto)
    if pack_id:
        return pack_id, "PACK_ID"

    venta = extraer_venta(texto)
    if venta:
        return venta, "VENTA"

    return "", ""


def es_pagina_ventas(texto):
    t = texto.lower()

    return (
        ("sku" in t)
        and ("cantidad" in t)
        and (
            "identifi" in t
            or "productos" in t
            or "despacha tus productos" in t
            or "código carrier" in t
            or "codigo carrier" in t
        )
    )


def es_inicio_bloque_identificacion(texto):
    """
    Detecta el inicio de una venta en la columna Identificación.
    Puede ser tracking numérico o código alfanumérico largo.
    """
    return bool(re.fullmatch(r"[A-Z0-9]{12,}|[0-9]{10,}", texto.strip()))


def texto_es_corte_final(texto):
    """
    Textos que indican que ya no estamos dentro de una venta,
    sino en el cierre/listado general de la hoja.
    """
    t = texto.lower()

    cortes = [
        "despacha tus productos",
        "no te relajes",
        "código carrier",
        "codigo carrier",
        "firma carrier",
        "fecha y hora de retiro",
    ]

    return any(c in t for c in cortes)


# ==================================================
# EXTRAER LÍNEAS CON COORDENADAS
# ==================================================

def extraer_lineas_con_coordenadas(page):
    """
    Extrae texto por líneas usando coordenadas reales del PDF.
    Esto permite separar columna izquierda y derecha.
    """

    data = page.get_text("dict")
    lineas = []

    for block in data.get("blocks", []):
        if "lines" not in block:
            continue

        for line in block["lines"]:
            textos = []
            x0s, y0s, x1s, y1s = [], [], [], []

            for span in line.get("spans", []):
                txt = span.get("text", "").strip()
                if not txt:
                    continue

                textos.append(txt)

                x0, y0, x1, y1 = span["bbox"]
                x0s.append(x0)
                y0s.append(y0)
                x1s.append(x1)
                y1s.append(y1)

            if not textos:
                continue

            texto = " ".join(textos).strip()

            lineas.append({
                "texto": texto,
                "x0": min(x0s),
                "y0": min(y0s),
                "x1": max(x1s),
                "y1": max(y1s),
                "yc": (min(y0s) + max(y1s)) / 2
            })

    return sorted(lineas, key=lambda l: (l["y0"], l["x0"]))


# ==================================================
# EXTRAER VENTAS DESDE PÁGINA DE VENTAS
# ==================================================

def extraer_ventas_pagina(page):
    """
    Extrae ventas usando la estructura visual de la hoja:
    columna izquierda = Identificación
    columna derecha = Productos.

    Cada venta empieza en la columna izquierda con un tracking/código largo.
    La banda vertical de cada venta termina antes del siguiente tracking/código
    o antes de textos finales como 'Despacha tus productos'.
    """

    lineas = extraer_lineas_con_coordenadas(page)

    if not lineas:
        return []

    ancho_pagina = page.rect.width

    # En los PDFs de Meli, la columna izquierda ocupa aprox. 40% del ancho.
    # Usamos este corte para separar identificación y productos.
    corte_columna = ancho_pagina * 0.42

    lineas_izq = [l for l in lineas if l["x0"] < corte_columna]
    lineas_der = [l for l in lineas if l["x0"] >= corte_columna]

    # Detectar inicios de venta en columna izquierda
    inicios = []
    for l in lineas_izq:
        txt = l["texto"].strip()

        if txt.lower() in ["identificación", "identificacion", "productos"]:
            continue

        if es_inicio_bloque_identificacion(txt):
            inicios.append(l)

    ventas = []

    for idx, inicio in enumerate(inicios):
        y_inicio = inicio["y0"]

        if idx + 1 < len(inicios):
            y_fin = inicios[idx + 1]["y0"]
        else:
            y_fin = page.rect.height

        # Si aparece un corte final antes del próximo inicio, ajustar y_fin
        posibles_cortes = [
            l["y0"]
            for l in lineas
            if l["y0"] > y_inicio and l["y0"] < y_fin and texto_es_corte_final(l["texto"])
        ]

        if posibles_cortes:
            y_fin = min(posibles_cortes)

        bloque_izq = [
            l for l in lineas_izq
            if l["yc"] >= y_inicio and l["yc"] < y_fin
        ]

        bloque_der = [
            l for l in lineas_der
            if l["yc"] >= y_inicio and l["yc"] < y_fin
        ]

        textos_izq = [l["texto"] for l in bloque_izq]
        textos_der = [l["texto"] for l in bloque_der]

        if not textos_izq:
            continue

        tracking = textos_izq[0].strip()
        pack_id = ""
        venta_id = ""
        comprador = ""

        for n, linea in enumerate(textos_izq):
            if re.match(r"Pack\s*ID:", linea, re.I):
                pack_id = limpiar_numero(linea)

            if re.match(r"Venta:", linea, re.I):
                venta_id = limpiar_numero(linea)

                if n + 1 < len(textos_izq):
                    comprador = textos_izq[n + 1].strip()

        clave = pack_id if pack_id else venta_id
        tipo_clave = "PACK_ID" if pack_id else "VENTA"

        if not clave:
            continue

        # Limpiar encabezados de productos si aparecen
        productos_limpios = []
        for linea in textos_der:
            t = linea.strip()
            if not t:
                continue

            if t.lower() in ["productos", "identificación", "identificacion"]:
                continue

            if texto_es_corte_final(t):
                break

            productos_limpios.append(t)

        texto_final = []

        # Bloque identificación
        texto_final.extend(textos_izq)

        # Bloque productos
        if productos_limpios:
            texto_final.append("")
            texto_final.append("Productos:")
            texto_final.extend(productos_limpios)

        ventas.append({
            "clave": clave,
            "tipo_clave": tipo_clave,
            "pack_id": pack_id,
            "venta": venta_id,
            "comprador": comprador,
            "tracking": tracking,
            "texto": "\n".join(texto_final)
        })

    return ventas


# ==================================================
# DETECTAR RECTÁNGULOS DE ETIQUETAS
# ==================================================

def detectar_rectangulos_etiquetas(page):
    pix = page.get_pixmap(dpi=DPI)
    img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
    arr = np.array(img)

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)

    contours, _ = cv2.findContours(
        edges,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    rects = []
    img_h, img_w = gray.shape

    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)

        # Filtrar rectángulos demasiado chicos
        if w < img_w * 0.25 or h < img_h * 0.12:
            continue

        # Filtrar rectángulos casi del tamaño de la página
        if w > img_w * 0.98 or h > img_h * 0.98:
            continue

        rects.append((x, y, w, h))

    rects = sorted(rects, key=lambda r: (r[1], r[0]))
    filtrados = []

    for r in rects:
        x, y, w, h = r
        duplicado = False

        for f in filtrados:
            fx, fy, fw, fh = f

            if (
                abs(x - fx) < 20
                and abs(y - fy) < 20
                and abs(w - fw) < 30
                and abs(h - fh) < 30
            ):
                duplicado = True
                break

        if not duplicado:
            filtrados.append(r)

    return filtrados, img_w, img_h


def rect_img_a_pdf(rect_img, img_w, img_h, page_rect):
    x, y, w, h = rect_img

    scale_x = page_rect.width / img_w
    scale_y = page_rect.height / img_h

    return fitz.Rect(
        x * scale_x,
        y * scale_y,
        (x + w) * scale_x,
        (y + h) * scale_y
    )


# ==================================================
# EXTRAER ETIQUETAS
# ==================================================

def extraer_etiquetas(doc):
    etiquetas = []

    for num_page, page in enumerate(doc):
        texto_pagina = page.get_text()

        # No buscar etiquetas en hojas finales de ventas
        if es_pagina_ventas(texto_pagina):
            continue

        rects, img_w, img_h = detectar_rectangulos_etiquetas(page)

        for rect_img in rects:
            rect_pdf = rect_img_a_pdf(rect_img, img_w, img_h, page.rect)
            texto = page.get_text("text", clip=rect_pdf)

            # Evitar que tome una zona de productos como etiqueta
            t = texto.lower()
            if "productos" in t and "sku" in t and "cantidad" in t:
                continue

            clave, tipo_clave = extraer_clave_etiqueta(texto)

            if not clave:
                continue

            pack_id = extraer_pack_id(texto)
            venta = extraer_venta(texto)

            etiquetas.append({
                "clave": clave,
                "tipo_clave": tipo_clave,
                "pack_id": pack_id,
                "venta": venta,
                "pagina": num_page,
                "rect": rect_pdf,
                "texto": texto.strip()
            })

    return etiquetas


# ==================================================
# LIMPIAR DUPLICADOS DE ETIQUETAS
# ==================================================

def puntuar_etiqueta(e):
    """
    Da mayor puntaje a recortes que parecen etiqueta real
    y menor puntaje a recortes que parecen detalle de productos.
    """

    texto = e.get("texto", "").lower()
    rect = e["rect"]
    area = rect.width * rect.height

    puntaje = 0

    # Señales fuertes de etiqueta real
    if "destinatario" in texto:
        puntaje += 50
    if "domicilio" in texto or "direccion" in texto or "dirección" in texto:
        puntaje += 40
    if "flex" in texto:
        puntaje += 25
    if "envio" in texto or "envío" in texto:
        puntaje += 25
    if "remitente" in texto:
        puntaje += 20
    if "dog shop" in texto:
        puntaje += 20
    if "pack id" in texto:
        puntaje += 10
    if "venta" in texto:
        puntaje += 10

    # Señales de que NO es etiqueta, sino hoja/detalle de productos
    if "productos" in texto:
        puntaje -= 60
    if "sku" in texto:
        puntaje -= 50
    if "cantidad" in texto:
        puntaje -= 40
    if "despacha tus productos" in texto:
        puntaje -= 60

    # Preferir rectángulos más grandes si el puntaje es parecido
    puntaje += area / 10000

    return puntaje


def deduplicar_etiquetas(etiquetas):
    """
    Si una misma clave aparece más de una vez, conserva sólo
    el mejor recorte según puntaje.
    """

    por_clave = {}

    for e in etiquetas:
        clave = e["clave"]

        if clave not in por_clave:
            por_clave[clave] = e
        else:
            actual = por_clave[clave]

            if puntuar_etiqueta(e) > puntuar_etiqueta(actual):
                por_clave[clave] = e

    etiquetas_limpias = list(por_clave.values())

    return etiquetas_limpias


# ==================================================
# RENDERIZAR ETIQUETA
# ==================================================

def render_etiqueta(doc, etiqueta):
    page = doc[etiqueta["pagina"]]
    rect = fitz.Rect(etiqueta["rect"])

    # Pequeño margen para no cortar bordes
    rect.x0 = max(0, rect.x0 - 2)
    rect.y0 = max(0, rect.y0 - 2)
    rect.x1 = min(page.rect.width, rect.x1 + 2)
    rect.y1 = min(page.rect.height, rect.y1 + 2)

    pix = page.get_pixmap(clip=rect, dpi=300)
    return pix.tobytes("png"), rect.width, rect.height


# ==================================================
# GENERAR PDF FINAL
# ==================================================

def generar_pdf(doc, pares, pdf_salida):
    c = canvas.Canvas(str(pdf_salida), pagesize=landscape(letter))

    page_w, page_h = landscape(letter)

    margen_sup = MARGEN_SUPERIOR_CM * cm
    margen_inf = MARGEN_INFERIOR_CM * cm
    margen_izq = MARGEN_IZQUIERDO_CM * cm
    margen_der = MARGEN_DERECHO_CM * cm
    sep_vertical = SEPARACION_VERTICAL_CM * cm

    espacio_columnas = page_w - margen_izq - margen_der
    ancho_columna = espacio_columnas / PARES_POR_HOJA

    alto_etiqueta_objetivo = 11.5 * cm

    # Espacio reservado para el barcode vertical a la izquierda de cada etiqueta
    ancho_barcode_vertical = 1.65 * cm
    separacion_barcode_etiqueta = 0.02 * cm

    def dibujar_linea(texto, x, y, bold=False, font_size=6.2):
        fuente = "Helvetica-Bold" if bold else "Helvetica"
        c.setFont(fuente, font_size)
        c.drawString(x, y, texto)

    def dibujar_barcode_vertical(valor, x, y, alto_max, ancho_max):
        """
        Dibuja un Code128 vertical al lado izquierdo de la etiqueta
        e incluye el número del código de barra en vertical.
    
        El bloque completo se alinea hacia la derecha dentro del espacio reservado,
        para quedar más cerca de la etiqueta.
        """
    
        if not valor:
            return
    
        barcode = code128.Code128(
            valor,
            barHeight=1.15 * cm,
            barWidth=0.60
        )
    
        # Después de rotar:
        # barcode.width será el alto vertical ocupado.
        # barcode.height será el ancho horizontal ocupado.
        escala = min(
            1,
            alto_max / barcode.width,
            ancho_max / (barcode.height + 0.45 * cm)
        )
    
        ancho_real = barcode.height * escala
        alto_real = barcode.width * escala
    
        # Separación entre el código y el número
        separacion_numero = 0.22 * cm
    
        # Espacio estimado que ocupa visualmente el número en horizontal
        ancho_numero_estimado = 0.22 * cm
    
        ancho_total_bloque = ancho_real + separacion_numero + ancho_numero_estimado
    
        
        # Alinear todo el bloque hacia la derecha del área reservada
        x_inicio = x + max(0, ancho_max - ancho_total_bloque)

        # Ajuste fino: mueve el bloque barcode+número hacia la etiqueta
        AJUSTE_DERECHA_BARCODE = 0.20 * cm
        x_inicio += AJUSTE_DERECHA_BARCODE
    
        # Centrar verticalmente dentro del alto de la etiqueta
        y_barcode = y + (alto_max - alto_real) / 2
    
        # Dibujar barcode vertical
        c.saveState()
        c.translate(x_inicio + ancho_real, y_barcode)
        c.rotate(90)
        c.scale(escala, escala)
        barcode.drawOn(c, 0, 0)
        c.restoreState()
    
        # Dibujar número del barcode en vertical, separado del código
        font_numero = 5.5
        x_numero = x_inicio + ancho_real + separacion_numero
        y_numero = y + alto_max / 2
    
        c.saveState()
        c.translate(x_numero, y_numero)
        c.rotate(90)
        c.setFont("Helvetica", font_numero)
        c.drawCentredString(0, 0, valor)
        c.restoreState()

    def es_linea_producto(linea):
        t = linea.strip().lower()

        if not t:
            return False

        prefijos_no_producto = (
            "sku:",
            "cantidad:",
            "color:",
            "talla",
            "talla del arnés:",
            "talla del arnes:",
            "tamaño",
            "tamano",
            "diseño:",
            "diseno:",
            "nombre del diseño:",
            "nombre del diseno:",
            "fragancia:",
            "pack id:",
            "venta:",
            "tracking:",
            "comprador:",
            "clave usada:",
            "productos:",
        )

        return not t.startswith(prefijos_no_producto)

    def construir_lineas_detalle(venta):
        lineas = []

        lineas.extend([
            ("Comprador: " + venta["comprador"], False, 0),
            ("Venta: " + venta["venta"], False, 0),
            ("Pack ID: " + venta["pack_id"], False, 0),
            ("Tracking: " + venta["tracking"], False, 0),
            (f"Clave usada: {venta['tipo_clave']} = {venta['clave']}", False, 0),
            ("", False, 0),
        ])

        dentro_productos = False

        for linea in venta["texto"].splitlines():
            linea = linea.strip()

            if not linea:
                continue

            if linea.startswith("Pack ID:"):
                continue
            if linea.startswith("Venta:"):
                continue
            if linea == venta["tracking"]:
                continue
            if linea == venta["comprador"]:
                continue

            if linea.lower() == "productos:":
                dentro_productos = True
                lineas.append(("Productos:", True, 0))
                continue

            if dentro_productos:
                if es_linea_producto(linea):
                    lineas.append((f"• {linea}", True, 1))
                elif linea.lower().startswith("cantidad:"):
                    lineas.append((linea, "cantidad_numero_bold", 0))
                else:
                    lineas.append((f"  {linea}", False, 0))
            else:
                lineas.append((linea, False, 0))

        return lineas

    def medir_alto_detalle(lineas, font_size, salto_linea):
        alto = 0

        for texto, bold, espacio_previo in lineas:
            if espacio_previo:
                alto += 2.5

            alto += salto_linea

        return alto

    def dibujar_detalle(lineas, x, y_inicio, y_min, ancho_max, font_size, salto_linea):
        y_actual = y_inicio

        for texto, bold, espacio_previo in lineas:
            if espacio_previo:
                y_actual -= 2.5

            if y_actual < y_min:
                break

            if texto == "":
                y_actual -= salto_linea
                continue

            if bold == "cantidad_numero_bold":
                prefijo = "  Cantidad: "
                numero = texto.split(":", 1)[1].strip()

                c.setFont("Helvetica", font_size)
                c.drawString(x, y_actual, prefijo)

                ancho_prefijo = c.stringWidth(prefijo, "Helvetica", font_size)

                # Número de cantidad más grande y en negrita
                font_cantidad = font_size + 1

                c.setFont("Helvetica-Bold", font_cantidad)
                c.drawString(x + ancho_prefijo, y_actual, numero)

            else:
                max_chars = int(ancho_max / (font_size * 0.47))
                dibujar_linea(
                    texto[:max_chars],
                    x,
                    y_actual,
                    bold=bool(bold),
                    font_size=font_size
                )

            y_actual -= salto_linea

        return y_actual

    for i, par in enumerate(pares):
        if i > 0 and i % PARES_POR_HOJA == 0:
            c.showPage()

        col = i % PARES_POR_HOJA

        x_col = margen_izq + col * ancho_columna

        # Contenido total de la columna
        x_contenido = x_col + 0.15 * cm
        ancho_contenido = ancho_columna - 0.3 * cm

        # Área del barcode vertical
        x_barcode = x_contenido

        # La etiqueta empieza a la derecha del barcode vertical
        x_etiqueta_base = (
            x_contenido
            + ancho_barcode_vertical
            + separacion_barcode_etiqueta
        )

        ancho_disponible_etiqueta = (
            ancho_contenido
            - ancho_barcode_vertical
            - separacion_barcode_etiqueta
        )

        y_etiqueta_top = page_h - margen_sup

        img_bytes, w_pdf, h_pdf = render_etiqueta(doc, par["etiqueta"])

        escala = alto_etiqueta_objetivo / h_pdf
        ancho_etiqueta = w_pdf * escala
        alto_etiqueta_real = alto_etiqueta_objetivo

        if ancho_etiqueta > ancho_disponible_etiqueta:
            escala = ancho_disponible_etiqueta / w_pdf
            ancho_etiqueta = ancho_disponible_etiqueta
            alto_etiqueta_real = h_pdf * escala

        # Etiqueta alineada a su nueva X, sin centrarse
        x_etiqueta = x_etiqueta_base
        y_etiqueta_real = y_etiqueta_top - alto_etiqueta_real

        # Barcode vertical a la izquierda de la etiqueta
        dibujar_barcode_vertical(
            par["venta"]["clave"],
            x_barcode,
            y_etiqueta_real,
            alto_etiqueta_real,
            ancho_barcode_vertical
        )

        c.drawImage(
            ImageReader(BytesIO(img_bytes)),
            x_etiqueta,
            y_etiqueta_real,
            width=ancho_etiqueta,
            height=alto_etiqueta_real,
            preserveAspectRatio=True,
            mask="auto"
        )

        venta = par["venta"]

        # El detalle queda alineado con la etiqueta, no con el barcode
        x_texto = x_etiqueta
        y_texto_inicio = y_etiqueta_real - sep_vertical

        lineas_detalle = construir_lineas_detalle(venta)

        opciones_fuente = [
            (5.8, 6.5),
            (5.4, 6.1),
            (5.0, 5.7),
            (4.7, 5.3),
            (4.4, 5.0),
        ]

        font_base = opciones_fuente[-1][0]
        salto_linea = opciones_fuente[-1][1]

        y_min_texto = margen_inf + 0.25 * cm
        alto_disponible = y_texto_inicio - y_min_texto

        for f, s in opciones_fuente:
            alto_requerido = medir_alto_detalle(lineas_detalle, f, s)
            if alto_requerido <= alto_disponible:
                font_base = f
                salto_linea = s
                break

        dibujar_detalle(
            lineas_detalle,
            x_texto,
            y_texto_inicio,
            y_min_texto,
            ancho_disponible_etiqueta,
            font_base,
            salto_linea
        )

    c.save()

# ==================================================
# PROCESO PRINCIPAL PARA MASCAN APP
# ==================================================

def _abrir_pdf(pdf_entrada):
    """Open a PDF from bytes, an uploaded file, or a filesystem path."""
    if isinstance(pdf_entrada, (bytes, bytearray)):
        return fitz.open(stream=bytes(pdf_entrada), filetype="pdf")

    if hasattr(pdf_entrada, "read"):
        contenido = pdf_entrada.read()
        if hasattr(pdf_entrada, "seek"):
            try:
                pdf_entrada.seek(0)
            except Exception:
                pass
        return fitz.open(stream=contenido, filetype="pdf")

    ruta = Path(pdf_entrada)
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el PDF de entrada: {ruta}")
    return fitz.open(str(ruta))


def _depurar_pdfs_generados(carpeta: Path, conservar: int = MAX_PDFS_GUARDADOS):
    """
    Conserva solo los PDF de etiquetas más recientes generados por la APP.

    No elimina otros PDF que puedan existir en la misma carpeta.
    """
    archivos = sorted(
        carpeta.glob("Etiquetas_carta_MELI_*.pdf"),
        key=lambda archivo: archivo.stat().st_mtime,
        reverse=True,
    )

    eliminados = []
    for archivo in archivos[max(int(conservar), 0):]:
        try:
            archivo.unlink()
            eliminados.append(archivo.name)
        except OSError:
            # Un archivo abierto o bloqueado no debe impedir generar la tanda nueva.
            continue

    return eliminados


def procesar_etiquetas_carta(
    pdf_entrada,
    carpeta_salida=None,
    nombre_salida=None,
):
    """
    Convert the original MELI PDF to the MasCan landscape Letter layout.

    It never renames or deletes the original PDF and does not create labels.pdf.
    """
    carpeta = Path(carpeta_salida) if carpeta_salida else DEFAULT_TEMP_DIR
    carpeta.mkdir(parents=True, exist_ok=True)

    if nombre_salida:
        nombre = str(nombre_salida).strip()
        if not nombre.lower().endswith(".pdf"):
            nombre += ".pdf"
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        nombre = f"Etiquetas_carta_MELI_{timestamp}.pdf"

    pdf_salida = carpeta / nombre
    doc = _abrir_pdf(pdf_entrada)

    try:
        diagnostico_paginas = []
        ventas = []

        for i, page in enumerate(doc):
            texto = page.get_text()
            pagina_ventas = es_pagina_ventas(texto)
            diagnostico_paginas.append(
                {
                    "Página": i + 1,
                    "Es página de ventas": pagina_ventas,
                    "Caracteres": len(texto),
                }
            )
            if pagina_ventas:
                ventas.extend(extraer_ventas_pagina(page))

        etiquetas_sin_limpiar = extraer_etiquetas(doc)
        etiquetas = deduplicar_etiquetas(etiquetas_sin_limpiar)

        ventas_por_clave = {}
        for venta in ventas:
            ventas_por_clave.setdefault(venta["clave"], []).append(venta)

        etiquetas_por_clave = {}
        for etiqueta in etiquetas:
            etiquetas_por_clave.setdefault(etiqueta["clave"], []).append(etiqueta)

        auditoria = []
        pares = []

        for clave, lista_ventas in ventas_por_clave.items():
            lista_etiquetas = etiquetas_por_clave.get(clave, [])

            if len(lista_ventas) == 1 and len(lista_etiquetas) == 1:
                estado = "VALIDADO"
                obs = f"{lista_ventas[0]['tipo_clave']} único coincide"
                pares.append(
                    {
                        "clave": clave,
                        "venta": lista_ventas[0],
                        "etiqueta": lista_etiquetas[0],
                    }
                )
            elif len(lista_etiquetas) == 0:
                estado = "REVISION_MANUAL"
                obs = "Clave de venta no encontrada en etiquetas"
            elif len(lista_etiquetas) > 1:
                estado = "REVISION_MANUAL"
                obs = "Clave duplicada en etiquetas"
            elif len(lista_ventas) > 1:
                estado = "REVISION_MANUAL"
                obs = "Clave duplicada en ventas"
            else:
                estado = "REVISION_MANUAL"
                obs = "Caso no previsto"

            venta_ref = lista_ventas[0] if lista_ventas else {}
            auditoria.append(
                {
                    "Clave": clave,
                    "Tipo_Clave": venta_ref.get("tipo_clave", ""),
                    "Pack_ID": venta_ref.get("pack_id", ""),
                    "Venta": venta_ref.get("venta", ""),
                    "Tracking": venta_ref.get("tracking", ""),
                    "Comprador": venta_ref.get("comprador", ""),
                    "Estado": estado,
                    "Observaciones": obs,
                }
            )

        df = pd.DataFrame(auditoria)
        total_validadas = int((df["Estado"] == "VALIDADO").sum()) if not df.empty else 0
        total_revision = int((df["Estado"] == "REVISION_MANUAL").sum()) if not df.empty else 0

        resumen = {
            "etiquetas_detectadas_antes": len(etiquetas_sin_limpiar),
            "etiquetas_detectadas": len(etiquetas),
            "ventas_detectadas": len(ventas),
            "validadas": total_validadas,
            "revision_manual": total_revision,
        }

        base_result = {
            "pdf_path": None,
            "pdf_bytes": None,
            "auditoria": df,
            "resumen": resumen,
            "diagnostico_paginas": pd.DataFrame(diagnostico_paginas),
        }

        if df.empty:
            return {
                **base_result,
                "ok": False,
                "mensaje": (
                    "No se detectaron ventas en las páginas finales. "
                    "Revisa si el PDF contiene Venta, Pack ID, SKU y Cantidad."
                ),
            }

        if total_revision:
            return {
                **base_result,
                "ok": False,
                "mensaje": (
                    "No se generó el PDF porque hay registros que requieren "
                    "revisión manual."
                ),
            }

        generar_pdf(doc, pares, pdf_salida)
        pdf_bytes = pdf_salida.read_bytes()
        archivos_eliminados = _depurar_pdfs_generados(carpeta)

        mensaje = "PDF de etiquetas Carta generado correctamente."
        if archivos_eliminados:
            mensaje += (
                f" Se eliminaron {len(archivos_eliminados)} PDF antiguos "
                f"para conservar solo los últimos {MAX_PDFS_GUARDADOS}."
            )

        return {
            **base_result,
            "ok": True,
            "pdf_path": pdf_salida,
            "pdf_bytes": pdf_bytes,
            "archivos_eliminados": archivos_eliminados,
            "mensaje": mensaje,
        }
    finally:
        doc.close()


# ==================================================
# USO MANUAL OPCIONAL
# ==================================================

def main():
    """Allow the module to still be run from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Genera etiquetas Carta desde un PDF de Mercado Libre."
    )
    parser.add_argument("pdf_entrada", help="Ruta del PDF original de Mercado Libre")
    parser.add_argument("--salida", default=None, help="Nombre opcional del PDF final")
    args = parser.parse_args()

    resultado = procesar_etiquetas_carta(
        args.pdf_entrada,
        nombre_salida=args.salida,
    )
    print(resultado["mensaje"])
    print(resultado["resumen"])
    if resultado["pdf_path"]:
        print(f"PDF generado: {resultado['pdf_path']}")
    if not resultado["auditoria"].empty:
        print(resultado["auditoria"].to_csv(sep=";", index=False))


if __name__ == "__main__":
    main()
