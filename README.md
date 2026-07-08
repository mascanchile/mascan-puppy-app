# MasCan Puppy APP

Mini app online para operar pedidos de Mercado Libre con pistola lectora.

## Objetivo

Primera versión simple para Manuel:

- cargar ventas del día;
- revisar pedidos;
- escanear etiqueta/pedido;
- escanear productos;
- marcar pedidos como listos;
- preparar luego descarga de etiquetas MELI.

## Estado actual

Versión mínima navegable. Todavía no conecta directo con MELI.

## Ejecutar localmente

```powershell
streamlit run app.py
```

## Streamlit Community Cloud

1. Subir esta carpeta a GitHub.
2. Crear app en Streamlit Community Cloud.
3. Archivo principal: `app.py`.
4. Agregar secretos MELI cuando conectemos la API.

## Columnas esperadas para cargar pedidos

- `MELI_ID`
- `CB`
- `CB alt`
- `Nombre Producto`
- `Cant.`
- `Tipo de Despacho`
