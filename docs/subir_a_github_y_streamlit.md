# Subir MasCan Puppy APP a GitHub y Streamlit

## 1. Crear repositorio en GitHub

1. Entrar a GitHub.
2. Presionar `New repository`.
3. Nombre sugerido:

```text
mascan-puppy-app
```

4. Elegir `Private`.
5. No marcar opciones extra por ahora.
6. Presionar `Create repository`.

## 2. Subir archivos

1. En el repositorio vacío, elegir `uploading an existing file`.
2. Arrastrar el contenido de esta carpeta:

```text
C:\Users\marti\Documents\MasCan APP Temp\MasCan Puppy APP
```

3. Deben subirse:

```text
app.py
requirements.txt
README.md
.gitignore
.streamlit/config.toml
.streamlit/secrets.toml.example
docs/plan.md
docs/subir_a_github_y_streamlit.md
```

4. Abajo, en el mensaje, escribir:

```text
Primera version MasCan Puppy APP
```

5. Presionar `Commit changes`.

## 3. Crear app en Streamlit Community Cloud

1. Entrar a Streamlit Community Cloud.
2. Presionar `Create app`.
3. Elegir el repositorio:

```text
mascan-puppy-app
```

4. Archivo principal:

```text
app.py
```

5. Presionar `Deploy`.

## 4. Secretos

Por ahora no cargar secretos. La primera versión funciona con archivo manual o datos de ejemplo.

Cuando conectemos MELI, los secretos se cargarán en:

```text
App settings > Secrets
```

Nunca subir claves reales a GitHub.
