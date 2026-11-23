# 🌦️ Verificación Wundermap para Meteo PBA

Aplicación web en **Streamlit** para consultar y verificar registros diarios de **Estaciones Meteorológicas Automáticas (EMAs/PWS)** de Weather Underground, ubicadas dentro de la **Provincia de Buenos Aires**, clasificadas por **Zona de Defensa Civil (I–XII)**.

## 🚀 Deploy en Streamlit Community Cloud

1. Subí este repositorio a GitHub.
2. Andá a [share.streamlit.io](https://share.streamlit.io) y conectá el repo.
3. Configurá `app.py` como archivo principal.
4. ¡Listo!

## 🛠️ Instalación local

```bash
# Clonar e instalar dependencias
git clone <tu-repo>
cd refactorizacion-verificacion-wundermap
pip install -r requirements.txt

# Generar GeoJSON enriquecido y catálogo de estaciones
python scripts/build_catalog.py

# Ejecutar la app
streamlit run app.py
```

## 📁 Estructura del proyecto

```
.
├── app.py                      # Interfaz Streamlit principal
├── .streamlit/
│   └── config.toml             # Configuración de tema
├── src/
│   ├── __init__.py
│   ├── geo.py                  # Mapeo FID→Zona DC, spatial join
│   ├── stations.py             # Catálogo de EMAs dentro de PBA
│   ├── scraper.py              # Cliente concurrente para WU (API + HTML)
│   ├── processing.py           # Lógica meteorológica: umbrales, resúmenes
│   └── export.py               # Exportación a Excel en memoria
├── data/
│   ├── zonas_dc_pba.geojson    # GeoJSON con zona_dc (generado)
│   ├── zonas_dc_pba_simple.geojson  # Versión simplificada para mapa
│   └── estaciones_pba.json     # Catálogo de EMAs filtrado (generado)
├── scripts/
│   └── build_catalog.py        # CLI para regenerar GeoJSON y catálogo
├── tests/
│   └── test_pipeline.py        # Tests unitarios
├── requirements.txt
├── Dockerfile                  # Para ejecución local o Cloud Run
├── .dockerignore
├── .gitignore
├── Zonas_DCs_windy.geojson     # GeoJSON fuente (12 polígonos con FID)
└── README.md
```

## 🗺️ Zonas de Defensa Civil

Las 12 zonas se mapean automáticamente desde el GeoJSON fuente usando coordenadas de referencia verificadas (point-in-polygon):

| Zona     | Referencia    | Coordenadas (lon, lat)    |
|----------|--------------|---------------------------|
| Zona I   | Quilmes      | (-58.2546, -34.7206)     |
| Zona II  | Morón        | (-58.6198, -34.6534)     |
| Zona III | Chivilcoy    | (-60.0167, -34.8957)     |
| Zona IV  | Junín        | (-60.9433, -34.5838)     |
| Zona V   | Olavarría    | (-60.3225, -36.8927)     |
| Zona VI  | Bahía Blanca | (-62.2663, -38.7183)     |
| Zona VII | Mar del Plata| (-57.5426, -38.0055)     |
| Zona VIII| Campana      | (-58.9591, -34.1687)     |
| Zona IX  | La Plata     | (-57.9536, -34.9205)     |
| Zona X   | Pergamino    | (-60.5736, -33.8900)     |
| Zona XI  | T. Lauquen   | (-62.7306, -35.9703)     |
| Zona XII | Dolores      | (-57.6792, -36.3132)     |

## ⚡ Funcionalidades

- **Consulta concurrente** a Weather Underground (API JSON + HTML fallback)
- **Filtrado geoespacial** estricto: solo EMAs dentro de PBA
- **Umbrales configurables**: precipitación (default ≥ 15 mm) y viento (default ≥ 60 km/h)
- **Vista por Zona DC** con indicadores de alerta
- **Mapa interactivo** con Folium
- **Exportación a Excel** formateado (.xlsx) con hojas por fecha, acumulado total, y por zona
- **Auditoría de corrida**: qué estaciones fallaron

## 🧪 Tests

```bash
pytest tests/ -v
```

## 🐳 Docker

```bash
docker build -t meteo-pba .
docker run -p 8501:8501 meteo-pba
```

## 📋 Primer uso

Antes de usar la app, generá los archivos de datos:

```bash
python scripts/build_catalog.py
```

Esto:
1. Lee `Zonas_DCs_windy.geojson` y genera `data/zonas_dc_pba.geojson` y `data/zonas_dc_pba_simple.geojson` con la propiedad `zona_dc`.
2. Consulta Wunderground para obtener coordenadas de cada estación del catálogo base.
3. Filtra por spatial join y guarda `data/estaciones_pba.json`.

Los archivos en `data/` se commitean al repo para que funcionen en Streamlit Community Cloud (filesystem efímero).
