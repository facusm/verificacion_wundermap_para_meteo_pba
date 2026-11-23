# -*- coding: utf-8 -*-
"""
Gestión del catálogo de Estaciones Meteorológicas Automáticas (EMAs/PWS)
dentro de la Provincia de Buenos Aires mediante descubrimiento exhaustivo
con grilla adaptativa (Quadtree) sobre api.weather.com/v3/location/near
y spatial join estricto contra zonas_dc_pba.geojson.
"""

import json
import logging
import pathlib
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict, List, Set
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point, box

from src.geo import load_zonas_gdf, spatial_join_stations, ZONA_ORDER

logger = logging.getLogger(__name__)

# Lista histórica base del script original (PBA verificadas y AMBA)
BASE_STATIONS = [
    'IENSEN159','IBERISSO11','IBERIS9','IBARRI112','IBAESCOB4','IPILAR83',
    'IRINCN53','IPILAR8','ICOLN10','ISALTO18','ISALTO28','IROJAS6','IASCEN5',
    'IJUNN18','IJUNN13','IJUNN12','ILEAND30','ILEAND6','ILEAND3','IGENER189',
    'ILINCO220','ICORON63','IFLORE99','IFLORE129','IGENER183','IBUENOSA378',
    'IBSASGRA2','IELTRI2','ILINCO15','ILINCO23','IROBER34','IGENER327',
    'ICORON76','ILINCO110','ILINCO8','ICARLO31','IPIEDR16','IRIVAD13',
    'ITRENQ8','ICARLO19','IPEHUA7','ICARLO21','IDEJUL17','IDEJUL12',
    'IDEJUL25','IDELVA1','IDEMAY2','IDEMAY12','IGENER74','IGENER142',
    'ISALAD6','ILOBOS13','ILOBOS12','ISANMI117','IGENER107','IVILLA1170',
    'ICHASC8','IPILA9','IPILA10','ILEZAM2','ICASTE641','ICASTE502','ILEZAM3',
    'ILASTO5','ILACOS5','ISANBE38','ILACOSTA4','IPINAM2','ICARIL1',
    'IVILLA229','IGENER373','IMAIP24','IMAIP20','IAYACU1','ITANDI23',
    'ITANDI17','ITANDI12','ICHILL86','IBUENOSA207','IOLAVA7','IBOLVA2',
    'IHENDE25','ILAGUN31','ITRESL1','ILAGUN45','ILAGUN33','ICASBA8',
    'IGUAMI8','IGUAMI10','IPELLE9','IPUN2','ISIERR20','IBUENOSA387',
    'IELDIV1','ICORON5','IJUANE1','IPEHUE2','ITRESA10','ISANFR47',
    'IBALNE17','ITRESA7','IBUENOSA67','ISANMA140','ITRESA16','ISANCA77',
    'ITRESA17','IBUENOSA257','IBUENOSA163','ILOBER7','ILOBERA2','ILOBER4',
    'IMARDE48','ISANMA174','IPUNTA95','IBAHIA23','IBABAHIA6','IBAHIA7',
    'IBAHIA2','IGENER264','IGENERAL45','IOPEND11','IBAMANZA2','IEXALT8',
    'IEXALT6','ISANAN39','ISANAN9','ISANAN26','IMERCE22','ICASTI160',
    'ICAPIT44','ICAPIT69','IARREC9','ICAMPA12','ICAMPA20','IZRATE16',
    'IBALIMA2','ISANPEDR35','ISANNI7','ISANNICO24','IMARDE17','IMARDE19',
    'IMARDE37','IMARDE29','IMARDE21','IMARDE8','IMARDE35','IMARDE11',
    'IMARDE12','IMARDE20','IBUENOSA504','IPERGA17','IPERGAMI6','ITIGRE13',
    'ISANIS7','IVICEN22','IVICEN3','IBAVICEN2','IVICEN14','IVICEN5',
    'IBUENOSA476','IVICEN11','ISANMI22','IMORN8','ITRESDEF3','IITUZAIN9',
    'IBUENOSA84','ISANJU57','ICOMUNA93','IBUENOSA517','IBUENOSA110',
    'ICOMUN37','IDFBARRI31','ILAPLATA12','IBACITYB3','ILAPLA22','IBUENOSA49',
    'ILOMASDE4','IBAVILLA9','ILOMASDE2','ILANS14','IBUENOSA236','ILAPLA66',
    'ILAPLA72','ILAPLA62','ILAPLATA7','ILAPLATA10','ILAPLA87','ILAPLA69',
    'ILAPLA25','ILAPLA23',
]
BASE_STATIONS_SET: Set[str] = set(BASE_STATIONS)

CATALOG_PATH = pathlib.Path("data/estaciones_pba.json")
BLACKLIST_PATH = pathlib.Path("data/blacklist_emas.json")
GEOREF_UBICACION_URL = "https://apis.datos.gob.ar/georef/api/ubicacion"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

_CACHED_API_KEY = "f6d2efe5720d47ea92efe5720df7eaa8"


def get_wu_api_key(force_refresh: bool = False) -> str:
    """Obtiene o refresca la API key pública de Weather Underground."""
    global _CACHED_API_KEY
    if _CACHED_API_KEY and not force_refresh:
        return _CACHED_API_KEY

    try:
        resp = requests.get(
            "https://www.wunderground.com/dashboard/pws/IENSEN159",
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            match = re.search(r'"API_KEY"\s*:\s*"([a-zA-Z0-9]+)"', resp.text)
            if match:
                _CACHED_API_KEY = match.group(1)
                return _CACHED_API_KEY
    except Exception as e:
        logger.warning(f"No se pudo refrescar API_KEY de WU: {e}")

    return _CACHED_API_KEY or "f6d2efe5720d47ea92efe5720df7eaa8"


def _query_location_near(lat: float, lon: float, api_key: str, timeout: int = 10) -> List[dict]:
    """
    Consulta https://api.weather.com/v3/location/near?geocode={lat},{lon}&product=pws
    Devuelve lista de diccionarios de estaciones con lat, lon, station_id, nombre, qc_status, dist_km.
    """
    url = f"https://api.weather.com/v3/location/near?geocode={lat:.5f},{lon:.5f}&product=pws&format=json&apiKey={api_key}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 401:
            api_key = get_wu_api_key(force_refresh=True)
            url = f"https://api.weather.com/v3/location/near?geocode={lat:.5f},{lon:.5f}&product=pws&format=json&apiKey={api_key}"
            r = requests.get(url, headers=HEADERS, timeout=timeout)

        if r.status_code != 200:
            return []

        loc = r.json().get("location") or {}
        sids = loc.get("stationId") or []
        lats = loc.get("latitude") or []
        lons = loc.get("longitude") or []
        names = loc.get("stationName") or []
        qcs = loc.get("qcStatus") or []
        dists = loc.get("distanceKm") or []

        stations = []
        for i in range(len(sids)):
            stations.append({
                "station_id": str(sids[i]).strip(),
                "lat": float(lats[i]),
                "lon": float(lons[i]),
                "nombre": str(names[i]).strip() if i < len(names) and names[i] else str(sids[i]),
                "qc_status": int(qcs[i]) if i < len(qcs) and qcs[i] is not None else 1,
                "dist_km": float(dists[i]) if i < len(dists) and dists[i] is not None else 0.0,
            })
        return stations
    except Exception:
        return []


def _fetch_single_station_meta(station_id: str, api_key: str, timeout: int = 10) -> Optional[dict]:
    """Fallback directo por stationId para asegurar metadatos de estaciones base."""
    url = (
        f"https://api.weather.com/v2/pws/observations/current?"
        f"stationId={station_id}&format=json&units=m&numericPrecision=decimal&apiKey={api_key}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            obs_list = r.json().get("observations") or []
            if obs_list:
                obs = obs_list[0]
                lat = obs.get("lat")
                lon = obs.get("lon")
                if lat is not None and lon is not None:
                    return {
                        "station_id": station_id,
                        "lat": float(lat),
                        "lon": float(lon),
                        "nombre": str(obs.get("neighborhood") or station_id).strip(),
                        "qc_status": int(obs.get("qcStatus", 1)),
                    }
    except Exception:
        pass

    # Si da 204 o falla, probar historial reciente
    now = datetime.now()
    for days_ago in (1, 2, 3):
        date_str = (now - timedelta(days=days_ago)).strftime("%Y%m%d")
        url_hist = (
            f"https://api.weather.com/v2/pws/history/all?"
            f"stationId={station_id}&format=json&units=m&date={date_str}&numericPrecision=decimal&apiKey={api_key}"
        )
        try:
            r = requests.get(url_hist, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                obs_list = r.json().get("observations") or []
                if obs_list:
                    obs = obs_list[0]
                    lat = obs.get("lat")
                    lon = obs.get("lon")
                    if lat is not None and lon is not None:
                        return {
                            "station_id": station_id,
                            "lat": float(lat),
                            "lon": float(lon),
                            "nombre": station_id,
                            "qc_status": int(obs.get("qcStatus", 1)),
                        }
        except Exception:
            pass

    return None


def discover_all_pba_stations(
    zonas_gdf: gpd.GeoDataFrame,
    api_key: Optional[str] = None,
    initial_step: float = 0.40,
    min_step: float = 0.03,
    max_workers: int = 8,
    progress_callback=None,
) -> Tuple[Dict[str, dict], dict]:
    """
    Descubre de forma exhaustiva las EMAs en PBA mediante una grilla adaptativa (Quadtree):
    1. Bounding box de PBA con buffer de 0.10° en celdas de 0.40° x 0.40°.
    2. Subdivisión adaptativa: si una celda devuelve 10 o más estaciones locales a la celda
       y su tamaño es > min_step (0.03°), se subdivide en 4 sub-celdas.
    3. Deduplica por station_id y conserva todas las estaciones devueltas.

    Returns:
        (discovered_stations_dict, stats_dict)
    """
    if not api_key:
        api_key = get_wu_api_key()

    # Polígono unificado de PBA con buffer
    try:
        pba_geom = zonas_gdf.union_all()
    except AttributeError:
        pba_geom = zonas_gdf.unary_union

    pba_buffered = pba_geom.buffer(0.10)
    minx, miny, maxx, maxy = pba_buffered.bounds

    # Generar celdas iniciales
    queue = deque()
    lat = miny
    while lat < maxy:
        lon = minx
        while lon < maxx:
            c_box = box(lon, lat, lon + initial_step, lat + initial_step)
            if c_box.intersects(pba_buffered):
                queue.append((lon, lat, lon + initial_step, lat + initial_step))
            lon += initial_step
        lat += initial_step

    initial_cell_count = len(queue)
    discovered_pool: Dict[str, dict] = {}
    total_queries = 0
    subdivided_cells = 0

    def process_cell(cell):
        min_x, min_y, max_x, max_y = cell
        c_lat = (min_y + max_y) / 2
        c_lon = (min_x + max_x) / 2
        cell_size = max_x - min_x
        stations = _query_location_near(c_lat, c_lon, api_key)

        diag_km = (cell_size * 111.0) * 0.85
        stations_local = [
            s for s in stations
            if s["dist_km"] <= diag_km or (min_x <= s["lon"] <= max_x and min_y <= s["lat"] <= max_y)
        ]
        should_subdivide = (len(stations) >= 10 and len(stations_local) >= 10 and cell_size > min_step)
        return cell, stations, should_subdivide

    # Procesar celdas en paralelo por tandas
    while queue:
        batch_size = min(len(queue), max_workers * 2)
        batch = [queue.popleft() for _ in range(batch_size)]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_cell, cell) for cell in batch]
            for future in as_completed(futures):
                cell, stations, should_subdivide = future.result()
                total_queries += 1

                for st in stations:
                    sid = st["station_id"]
                    if sid not in discovered_pool:
                        discovered_pool[sid] = st

                if should_subdivide:
                    subdivided_cells += 1
                    min_x, min_y, max_x, max_y = cell
                    mid_x = (min_x + max_x) / 2
                    mid_y = (min_y + max_y) / 2
                    subcells = [
                        (min_x, min_y, mid_x, mid_y),
                        (mid_x, min_y, max_x, mid_y),
                        (min_x, mid_y, mid_x, max_y),
                        (mid_x, mid_y, max_x, max_y),
                    ]
                    for subc in subcells:
                        if box(*subc).intersects(pba_buffered):
                            queue.append(subc)

                if progress_callback:
                    progress_callback(total_queries, len(discovered_pool), len(queue))

    stats = {
        "initial_cells": initial_cell_count,
        "total_queries": total_queries,
        "subdivided_cells": subdivided_cells,
        "total_discovered_raw": len(discovered_pool),
    }

    return discovered_pool, stats


def load_blacklist(blacklist_path: pathlib.Path = BLACKLIST_PATH) -> Set[str]:
    """Carga estaciones excluidas manualmente si el archivo existe."""
    if not blacklist_path.exists():
        return set()
    try:
        with open(blacklist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("excluded_stations", []))
    except Exception:
        return set()


def enrich_with_municipios(
    stations_list: list[dict],
    batch_size: int = 100,
    timeout: int = 25,
) -> list[dict]:
    """
    Asigna el Partido/Municipio oficial de PBA a cada estación usando la API
    pública oficial Georef AR (POST https://apis.datos.gob.ar/georef/api/ubicacion)
    en lotes de hasta 100 coordenadas.

    Para estaciones donde Georef devuelve null (por estar sobre un muelle,
    escollera o agua costera, como IPUERT440 o IGENERAL21), aplica fallback
    al vecino más cercano ya identificado dentro de la misma zona_dc.
    """
    if not stations_list:
        return stations_list

    # 1. Consulta en lotes a Georef AR
    for i in range(0, len(stations_list), batch_size):
        chunk = stations_list[i : i + batch_size]
        payload = {
            "ubicaciones": [
                {"lat": round(float(s["lat"]), 6), "lon": round(float(s["lon"]), 6), "aplanar": True}
                for s in chunk
            ]
        }
        try:
            resp = requests.post(
                GEOREF_UBICACION_URL,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]},
                timeout=timeout,
            )
            if resp.status_code == 200:
                results = resp.json().get("resultados", [])
                for st, res in zip(chunk, results):
                    ub = res.get("ubicacion") or {}
                    mun = ub.get("municipio_nombre") or ub.get("departamento_nombre")
                    if mun:
                        st["municipio"] = str(mun).strip()
                    else:
                        st["municipio"] = None
            else:
                logger.warning(f"Georef AR devolvió código HTTP {resp.status_code} para lote {i}")
                for st in chunk:
                    if "municipio" not in st:
                        st["municipio"] = None
        except Exception as e:
            logger.warning(f"Error consultando Georef AR en lote {i}: {e}")
            for st in chunk:
                if "municipio" not in st:
                    st["municipio"] = None

    # 2. Fallback para estaciones costeras o sin municipio devuelto por Georef
    resolved = [s for s in stations_list if s.get("municipio")]
    unresolved = [s for s in stations_list if not s.get("municipio")]

    if unresolved and resolved:
        for st in unresolved:
            target_lat, target_lon = float(st["lat"]), float(st["lon"])
            target_zona = st.get("zona_dc")

            # Prioridad 1: vecinos dentro de la misma Zona DC
            same_zona = [r for r in resolved if r.get("zona_dc") == target_zona]
            pool = same_zona if same_zona else resolved

            # Vecino más cercano por distancia euclidiana
            nearest = min(
                pool,
                key=lambda r: (float(r["lat"]) - target_lat) ** 2 + (float(r["lon"]) - target_lon) ** 2,
            )
            st["municipio"] = nearest["municipio"]
            logger.info(
                f"Municipio para {st['station_id']} resuelto por fallback costero con {nearest['station_id']}: {st['municipio']}"
            )

    # 3. Salvaguarda
    for st in stations_list:
        if not st.get("municipio"):
            st["municipio"] = "Sin asignar"

    return stations_list


def build_catalog(
    data_dir: str | pathlib.Path = "data",
    station_ids: Optional[list[str]] = None,
    progress_callback=None,
) -> Tuple[list[dict], list[dict], dict]:
    """
    Construye el catálogo completo de EMAs de PBA:
    1. Descubre todas las EMAs existentes mediante Quadtree adaptativo sobre PBA.
    2. Asegura metadatos de las estaciones históricas (BASE_STATIONS) si faltan.
    3. Excluye únicamente las presentes en data/blacklist_emas.json (vacío por defecto).
    4. Ejecuta spatial join estricto (predicate="within") contra data/zonas_dc_pba.geojson.
    5. Asigna origen ("base_verificada" o "descubierta"), qc_status y nombre.
    6. Guarda en data/estaciones_pba.json.

    Returns:
        (catalog, discarded_outside, stats)
    """
    data_dir = pathlib.Path(data_dir)
    zonas_gdf = load_zonas_gdf(data_dir)
    api_key = get_wu_api_key()

    # 1. Descubrimiento exhaustivo por grilla adaptativa
    discovered_pool, stats = discover_all_pba_stations(
        zonas_gdf=zonas_gdf,
        api_key=api_key,
        progress_callback=progress_callback,
    )

    # 2. Asegurar que las estaciones de BASE_STATIONS estén evaluadas
    base_to_check = BASE_STATIONS if station_ids is None else station_ids
    missing_base = [sid for sid in base_to_check if sid not in discovered_pool]

    if missing_base:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_fetch_single_station_meta, sid, api_key): sid
                for sid in missing_base
            }
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    res = future.result()
                    if res:
                        discovered_pool[sid] = res
                except Exception:
                    pass

    # 3. Aplicar Blacklist opcional
    blacklist = load_blacklist(data_dir / "blacklist_emas.json")
    filtered_pool = {
        sid: st for sid, st in discovered_pool.items()
        if sid not in blacklist
    }

    # 4. Spatial Join estricto contra Zonas de PBA
    stations_df = pd.DataFrame(list(filtered_pool.values()))
    stations_gdf = gpd.GeoDataFrame(
        stations_df,
        geometry=gpd.points_from_xy(stations_df["lon"], stations_df["lat"]),
        crs="EPSG:4326",
    )

    joined = spatial_join_stations(stations_gdf, zonas_gdf)

    # Identificar descartadas por caer fuera de PBA (ej. CABA, otras provincias)
    inside_sids = set(joined["station_id"])
    discarded_outside = [
        st for sid, st in filtered_pool.items() if sid not in inside_sids
    ]

    # 5. Generar catálogo con origen y qc_status
    now_str = datetime.now(timezone.utc).isoformat()
    catalog = []
    for _, row in joined.iterrows():
        sid = str(row["station_id"]).strip()
        origen = "base_verificada" if sid in BASE_STATIONS_SET else "descubierta"
        qc_val = int(row.get("qc_status", 1)) if pd.notna(row.get("qc_status")) else 1
        ajuste_costero = bool(row.get("ajuste_costero", False))

        catalog.append({
            "station_id": sid,
            "lat": round(float(row["lat"]), 6),
            "lon": round(float(row["lon"]), 6),
            "zona_dc": str(row["zona_dc"]),
            "nombre": str(row.get("nombre") or sid),
            "origen": origen,
            "qc_status": qc_val,
            "ajuste_costero": ajuste_costero,
            "municipio": str(row.get("municipio") or "").strip() or None,
            "ultima_verificacion": now_str,
        })

    # 6. Enriquecer con Partido/Municipio oficial de PBA (Georef AR)
    catalog = enrich_with_municipios(catalog)

    # Ordenar por Zona DC (I a XII) y station_id
    catalog.sort(key=lambda x: (
        ZONA_ORDER.index(x["zona_dc"]) if x["zona_dc"] in ZONA_ORDER else 99,
        x["station_id"]
    ))

    # Guardar data/estaciones_pba.json
    catalog_path = data_dir / "estaciones_pba.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    stats["total_pba_catalog"] = len(catalog)
    stats["total_discarded_outside"] = len(discarded_outside)

    return catalog, discarded_outside, stats


def load_catalog(data_dir: str | pathlib.Path = "data") -> list[dict]:
    """Carga el catálogo de estaciones desde el JSON persistido."""
    catalog_path = pathlib.Path(data_dir) / "estaciones_pba.json"
    if not catalog_path.exists():
        raise FileNotFoundError(
            f"No se encontró {catalog_path}. "
            "Ejecutar build_catalog.py primero."
        )
    with open(catalog_path, encoding="utf-8") as f:
        return json.load(f)


def get_stations_by_zona(
    catalog: list[dict], zonas: list[str] | None = None
) -> dict[str, list[dict]]:
    """Agrupa estaciones por zona_dc. Si zonas es None, devuelve todas."""
    from collections import defaultdict
    by_zona = defaultdict(list)
    for st in catalog:
        if zonas is None or st["zona_dc"] in zonas:
            by_zona[st["zona_dc"]].append(st)
    return dict(by_zona)


def catalog_summary(catalog: list[dict]) -> pd.DataFrame:
    """Resumen del catálogo: conteo por Zona DC, desglosando base_verificada, descubierta y ajuste_costero."""
    df = pd.DataFrame(catalog)
    if df.empty:
        return pd.DataFrame(columns=["Zona DC", "Total EMAs", "Base Verificada", "Descubierta", "Ajuste Costero"])

    if "origen" not in df.columns:
        df["origen"] = "descubierta"
    if "ajuste_costero" not in df.columns:
        df["ajuste_costero"] = False

    grouped = df.groupby(["zona_dc", "origen"]).size().unstack(fill_value=0).reset_index()
    if "base_verificada" not in grouped.columns:
        grouped["base_verificada"] = 0
    if "descubierta" not in grouped.columns:
        grouped["descubierta"] = 0

    costero_counts = df[df["ajuste_costero"] == True].groupby("zona_dc").size()
    grouped["Ajuste Costero"] = grouped["zona_dc"].map(costero_counts).fillna(0).astype(int)

    grouped["Total EMAs"] = grouped["base_verificada"] + grouped["descubierta"]
    grouped.rename(columns={
        "zona_dc": "Zona DC",
        "base_verificada": "Base Verificada",
        "descubierta": "Descubierta",
    }, inplace=True)

    grouped = grouped[["Zona DC", "Total EMAs", "Base Verificada", "Descubierta", "Ajuste Costero"]]
    grouped["_order"] = grouped["Zona DC"].map(
        {z: i for i, z in enumerate(ZONA_ORDER)}
    )
    grouped.sort_values("_order", inplace=True)
    grouped.drop(columns="_order", inplace=True)
    return grouped.reset_index(drop=True)
