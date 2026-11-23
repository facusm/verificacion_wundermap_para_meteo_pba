# -*- coding: utf-8 -*-
"""
Módulo geoespacial: mapeo determinístico FID → Zona DC
y generación de GeoJSON enriquecido.
"""

import json
import pathlib
from typing import Dict, Tuple, Optional

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

# ─── Coordenadas de referencia verificadas (lon, lat) ──────────────────
REFERENCE_POINTS: Dict[str, tuple] = {
    "Zona I":    (-58.2546, -34.7206),   # Quilmes
    "Zona II":   (-58.6198, -34.6534),   # Morón
    "Zona III":  (-60.0167, -34.8957),   # Chivilcoy
    "Zona IV":   (-60.9433, -34.5838),   # Junín
    "Zona V":    (-60.3225, -36.8927),   # Olavarría
    "Zona VI":   (-62.2663, -38.7183),   # Bahía Blanca
    "Zona VII":  (-57.5426, -38.0055),   # Mar del Plata
    "Zona VIII": (-58.9591, -34.1687),   # Campana
    "Zona IX":   (-57.9536, -34.9205),   # La Plata
    "Zona X":    (-60.5736, -33.8900),   # Pergamino
    "Zona XI":   (-62.7306, -35.9703),   # Trenque Lauquen
    "Zona XII":  (-57.6792, -36.3132),   # Dolores
}

# Orden romano correcto para ordenamiento
ZONA_ORDER = [
    "Zona I", "Zona II", "Zona III", "Zona IV", "Zona V", "Zona VI",
    "Zona VII", "Zona VIII", "Zona IX", "Zona X", "Zona XI", "Zona XII",
]

# Zonas con frente costero marítimo/estuarial (Río de la Plata, Costa Atlántica, ría de Bahía Blanca)
COASTAL_ZONAS = {"Zona I", "Zona VI", "Zona VII", "Zona VIII", "Zona IX", "Zona XII"}

# Tolerancia costera por defecto: 0.015° (~1.5 km)
COASTAL_TOLERANCE_DEG = 0.015


def _is_excluded_from_coastal_tolerance(lon: float, lat: float) -> bool:
    """
    Verifica si una coordenada cae en una zona excluida de la tolerancia costera:
    1. CABA (Capital Federal): cuadrante aproximado [-58.535, -58.330] x [-34.715, -34.525].
    2. Margen sur del Río Negro (Viedma y sur de latitud -40.50°): latitud <= -40.50°.
    3. Límite norte con Santa Fe (Arroyo del Medio / paralelo 34°10'): lat > -34.00° y lon < -59.50°.
    4. Límite oeste con La Pampa / Córdoba: lon < -62.50° y lat > -38.50°.
    """
    # 1. CABA
    if (-58.535 <= lon <= -58.330) and (-34.715 <= lat <= -34.525):
        return True
    # 2. Viedma / Margen sur del Río Negro
    if lat <= -40.50:
        return True
    # 3. Límite norte con Santa Fe
    if lat > -34.00 and lon < -59.50:
        return True
    # 4. Límite oeste con La Pampa / Córdoba
    if lon < -62.50 and lat > -38.50:
        return True

    return False


def _resolve_fid_to_zona(gdf: gpd.GeoDataFrame) -> Dict[int, str]:
    """
    Resuelve el mapeo FID → zona_dc mediante point-in-polygon
    usando las coordenadas de referencia verificadas.
    """
    gdf = gdf.to_crs("EPSG:4326")
    mapping: Dict[int, str] = {}

    for zona_name, (lon, lat) in REFERENCE_POINTS.items():
        pt = Point(lon, lat)
        matches = gdf[gdf.geometry.contains(pt)]
        if len(matches) == 0:
            raise ValueError(
                f"Ningún polígono contiene el punto de referencia de {zona_name} "
                f"({lon}, {lat}). Verificar el GeoJSON."
            )
        if len(matches) > 1:
            raise ValueError(
                f"Múltiples polígonos contienen el punto de {zona_name}: "
                f"FIDs={matches['FID'].tolist()}"
            )
        fid = int(matches.iloc[0]["FID"])
        if fid in mapping:
            raise ValueError(
                f"FID {fid} ya mapeado a {mapping[fid]}, no puede mapearse también a {zona_name}"
            )
        mapping[fid] = zona_name

    return mapping


def build_enriched_geojson(
    source_geojson: str | pathlib.Path,
    output_dir: str | pathlib.Path,
) -> gpd.GeoDataFrame:
    """
    Lee el GeoJSON fuente, mapea FID→zona_dc, valida unicidad 1-a-1,
    y guarda las versiones enriquecida y simplificada.

    Returns:
        GeoDataFrame enriquecido con la columna ``zona_dc``.
    """
    source_geojson = pathlib.Path(source_geojson)
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    gdf = gpd.read_file(source_geojson)
    gdf = gdf.to_crs("EPSG:4326")

    # Resolver mapeo
    mapping = _resolve_fid_to_zona(gdf)

    # ─── Validaciones ─────────────────────────────────────────────────
    assert len(mapping) == 12, (
        f"Se esperaban 12 mapeos, se obtuvieron {len(mapping)}"
    )
    assert set(mapping.values()) == set(ZONA_ORDER), (
        f"Zonas mapeadas no coinciden: {set(mapping.values())} vs {set(ZONA_ORDER)}"
    )
    # Sin FIDs duplicados (ya validado en _resolve, pero doble check)
    assert len(set(mapping.keys())) == 12, "FIDs duplicados en el mapeo"
    # Sin zonas duplicadas
    assert len(set(mapping.values())) == 12, "Zonas duplicadas en el mapeo"

    # Enriquecer
    gdf["zona_dc"] = gdf["FID"].map(mapping)
    assert gdf["zona_dc"].isna().sum() == 0, (
        "Hay polígonos sin zona_dc asignada"
    )

    # ─── Guardar versión completa ─────────────────────────────────────
    full_path = output_dir / "zonas_dc_pba.geojson"
    gdf.to_file(full_path, driver="GeoJSON")

    # ─── Guardar versión simplificada para Folium ─────────────────────
    gdf_simple = gdf.copy()
    gdf_simple["geometry"] = gdf_simple.geometry.simplify(
        0.004, preserve_topology=True
    )
    simple_path = output_dir / "zonas_dc_pba_simple.geojson"
    gdf_simple.to_file(simple_path, driver="GeoJSON")

    print(f"✅ GeoJSON enriquecido guardado en: {full_path}")
    print(f"✅ GeoJSON simplificado guardado en: {simple_path}")
    print(f"   Mapeo FID → Zona DC: {json.dumps(mapping, indent=2)}")

    return gdf


def load_zonas_gdf(data_dir: str | pathlib.Path = "data") -> gpd.GeoDataFrame:
    """Carga el GeoJSON enriquecido (versión completa) desde data/."""
    path = pathlib.Path(data_dir) / "zonas_dc_pba.geojson"
    if not path.exists():
        raise FileNotFoundError(
            f"No se encontró {path}. Ejecutar primero build_catalog.py "
            "para generar los archivos geoespaciales."
        )
    return gpd.read_file(path).to_crs("EPSG:4326")


def load_zonas_simple(data_dir: str | pathlib.Path = "data") -> dict:
    """Carga el GeoJSON simplificado como dict para Folium."""
    path = pathlib.Path(data_dir) / "zonas_dc_pba_simple.geojson"
    if not path.exists():
        raise FileNotFoundError(f"No se encontró {path}.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def classify_point(
    lon: float,
    lat: float,
    zonas_gdf: gpd.GeoDataFrame,
    coastal_tolerance: float = COASTAL_TOLERANCE_DEG,
    return_details: bool = False,
):
    """
    Dado un punto (lon, lat), clasifica a qué Zona DC de PBA pertenece:
    1. Pasada 1 (Exacta): si está dentro de algún polígono exacto de zonas_dc_pba.geojson,
       devuelve su zona_dc (ajuste_costero=False).
    2. Pasada 2 (Tolerancia costera de coastal_tolerance=0.015° ~1.5 km):
       Si está dentro del frente costero marítimo/estuarial (Río de la Plata,
       Costa Atlántica o ría de Bahía Blanca) y no cae en zonas excluidas
       (CABA, margen sur del Río Negro/Viedma, límites con Santa Fe o La Pampa),
       asigna la Zona DC costera más cercana (ajuste_costero=True).
    3. Si cae fuera, devuelve None (o (None, False) si return_details=True).
    """
    pt = Point(lon, lat)

    # ─── Pasada 1: Exacta ─────────────────────────────────────────────
    for _, row in zonas_gdf.iterrows():
        if row.geometry.contains(pt):
            return (row["zona_dc"], False) if return_details else row["zona_dc"]

    # ─── Pasada 2: Tolerancia costera ─────────────────────────────────
    if not _is_excluded_from_coastal_tolerance(lon, lat):
        zonas_coastal = zonas_gdf[zonas_gdf["zona_dc"].isin(COASTAL_ZONAS)]
        best_zona = None
        min_dist = float("inf")

        for _, zrow in zonas_coastal.iterrows():
            d = zrow.geometry.distance(pt)
            if d < min_dist:
                min_dist = d
                best_zona = zrow["zona_dc"]

        if best_zona is not None and min_dist <= coastal_tolerance:
            return (best_zona, True) if return_details else best_zona

    return (None, False) if return_details else None


def spatial_join_stations(
    stations_gdf: gpd.GeoDataFrame,
    zonas_gdf: gpd.GeoDataFrame,
    coastal_tolerance: float = COASTAL_TOLERANCE_DEG,
) -> gpd.GeoDataFrame:
    """
    Realiza spatial join en dos pasadas de estaciones contra zonas de DC de PBA:
    - Pasada 1 (Exacta): gpd.sjoin con predicate="within" contra los polígonos exactos
      de zonas_dc_pba.geojson. Marca ajuste_costero = False.
    - Pasada 2 (Tolerancia costera de coastal_tolerance=0.015° ~1.5 km):
      Toma únicamente las estaciones que no matchearon en la Pasada 1 y busca la
      zona costera más cercana dentro de coastal_tolerance (asignación 1 a 1, sin duplicados),
      restringida exclusivamente al frente costero marítimo/estuarial:
      - Zonas costeras permitidas: {"Zona I", "Zona VI", "Zona VII", "Zona VIII", "Zona IX", "Zona XII"}
      - Exclusión estricta de CABA, margen sur del Río Negro (Viedma), límite con Santa Fe
        y límite oeste con La Pampa/Córdoba.
      Marca ajuste_costero = True.

    Returns:
        GeoDataFrame con las estaciones asignadas a PBA y la columna `ajuste_costero`.
    """
    stations_gdf = stations_gdf.to_crs("EPSG:4326").copy()
    zonas_gdf = zonas_gdf.to_crs("EPSG:4326").copy()

    # ─── Pasada 1: Exacta (within) ────────────────────────────────────
    joined_p1 = gpd.sjoin(stations_gdf, zonas_gdf, predicate="within", how="inner")
    if "index_right" in joined_p1.columns:
        joined_p1 = joined_p1.drop(columns=["index_right"])

    if "station_id" in joined_p1.columns:
        joined_p1 = joined_p1.drop_duplicates(subset=["station_id"], keep="first")
    joined_p1["ajuste_costero"] = False

    if "station_id" in joined_p1.columns:
        matched_ids = set(joined_p1["station_id"])
    else:
        matched_ids = set(joined_p1.index)

    # ─── Pasada 2: Tolerancia costera (0.015°) ────────────────────────
    if "station_id" in stations_gdf.columns:
        unmatched = stations_gdf[~stations_gdf["station_id"].isin(matched_ids)].copy()
    else:
        unmatched = stations_gdf.loc[~stations_gdf.index.isin(matched_ids)].copy()

    p2_rows = []
    if not unmatched.empty:
        zonas_coastal = zonas_gdf[zonas_gdf["zona_dc"].isin(COASTAL_ZONAS)].copy()

        for _, row in unmatched.iterrows():
            pt = row.geometry
            lon, lat = pt.x, pt.y

            # Excluir CABA, Viedma/sur de Río Negro, límite Santa Fe, etc.
            if _is_excluded_from_coastal_tolerance(lon, lat):
                continue

            # Encontrar polígono costero más cercano
            best_zona = None
            min_dist = float("inf")
            best_fid = None

            for _, zrow in zonas_coastal.iterrows():
                d = zrow.geometry.distance(pt)
                if d < min_dist:
                    min_dist = d
                    best_zona = zrow["zona_dc"]
                    best_fid = zrow.get("FID")

            if best_zona is not None and min_dist <= coastal_tolerance:
                row_dict = row.to_dict()
                row_dict["zona_dc"] = best_zona
                row_dict["ajuste_costero"] = True
                if best_fid is not None:
                    row_dict["FID"] = best_fid
                p2_rows.append(row_dict)

    if p2_rows:
        joined_p2 = gpd.GeoDataFrame(p2_rows, crs="EPSG:4326")
        # Asegurar columnas consistentes
        for col in joined_p1.columns:
            if col not in joined_p2.columns:
                joined_p2[col] = None
        for col in joined_p2.columns:
            if col not in joined_p1.columns:
                joined_p1[col] = None

        result_gdf = pd.concat([joined_p1, joined_p2], ignore_index=True)
        result_gdf = gpd.GeoDataFrame(result_gdf, crs="EPSG:4326")
    else:
        result_gdf = joined_p1

    if "station_id" in result_gdf.columns:
        result_gdf = result_gdf.drop_duplicates(subset=["station_id"], keep="first")

    return result_gdf
