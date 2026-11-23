# -*- coding: utf-8 -*-
"""
Cliente robusto y concurrente para extracción de datos meteorológicos
de Weather Underground (PWS history).
"""

import re
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.stations import get_wu_api_key

logger = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

WU_API_BASE = "https://api.weather.com/v2/pws/history/all"

MAX_WORKERS = 12
TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # segundos


@dataclass
class ScrapeResult:
    """Resultado del scraping de una estación para una fecha."""
    station_id: str
    fecha: str
    status: str  # "ok", "sin_datos", "error"
    df: Optional[pd.DataFrame] = None
    source_method: str = ""  # "api_json" o "html_fallback"
    error_msg: str = ""
    raw_sample: Optional[dict] = None


@dataclass
class RunMetrics:
    """Métricas agregadas de una corrida de scraping."""
    total: int = 0
    ok: int = 0
    sin_datos: int = 0
    errores: int = 0
    metodo_api: int = 0
    metodo_html: int = 0
    detalle_errores: list = field(default_factory=list)


def _build_wu_link(station_id: str, fecha: str) -> str:
    """Construye el enlace al gráfico diario de WU."""
    return (
        f"https://www.wunderground.com/dashboard/pws/{station_id}"
        f"/graph/{fecha}/{fecha}/daily"
    )


def _fetch_via_api(station_id: str, fecha: str) -> Tuple[Optional[pd.DataFrame], Optional[dict]]:
    """
    Obtiene datos vía la API JSON de Weather Underground (api.weather.com).
    Con units="m":
      - windspeedAvg está en km/h
      - windgustHigh está en km/h
      - precipTotal está en mm
    No se aplica ninguna conversión adicional (evita doble conversión).

    Returns:
        (DataFrame, raw_sample_dict)
    """
    api_key = get_wu_api_key()
    params = {
        "stationId": station_id,
        "format": "json",
        "units": "m",  # Métrico: km/h, mm
        "date": fecha.replace("-", ""),
        "numericPrecision": "decimal",
        "apiKey": api_key,
    }

    try:
        resp = requests.get(
            WU_API_BASE, params=params, headers=HEADERS, timeout=TIMEOUT
        )
        if resp.status_code == 401:
            # Token expirado, refrescar una vez
            api_key = get_wu_api_key(force_refresh=True)
            params["apiKey"] = api_key
            resp = requests.get(
                WU_API_BASE, params=params, headers=HEADERS, timeout=TIMEOUT
            )

        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None, None

    observations = data.get("observations")
    if not observations:
        return None, None

    rows = []
    raw_sample = None
    for obs in observations:
        metric = obs.get("metric", {})
        obs_time = obs.get("obsTimeLocal", "")
        try:
            hora = datetime.fromisoformat(obs_time).strftime("%H:%M")
        except (ValueError, TypeError):
            hora = obs_time

        precip = metric.get("precipTotal")
        wind_speed = metric.get("windspeedAvg")
        wind_gust = metric.get("windgustHigh")
        qc_status = obs.get("qcStatus")

        if raw_sample is None and (precip is not None or wind_speed is not None or wind_gust is not None):
            raw_sample = {
                "source": "api_json (units=m)",
                "obsTimeLocal": obs_time,
                "raw_precipTotal_mm": precip,
                "raw_windspeedAvg_kmh": wind_speed,
                "raw_windgustHigh_kmh": wind_gust,
                "qcStatus": qc_status,
            }

        rows.append({
            "Hora_local": hora,
            "Viento_medio (km/h)": round(float(wind_speed), 2) if wind_speed is not None else None,
            "Rafagas (km/h)": round(float(wind_gust), 2) if wind_gust is not None else None,
            "Precipitacion (mm)": round(float(precip), 2) if precip is not None else None,
            "qcStatus": int(qc_status) if qc_status is not None else None,
        })

    if not rows:
        return None, None

    df = pd.DataFrame(rows)
    df["Fecha"] = fecha
    df["Estacion"] = station_id
    return df, raw_sample


def _fetch_via_html(station_id: str, fecha: str) -> Tuple[Optional[pd.DataFrame], Optional[dict]]:
    """
    Fallback: obtiene datos parseando la tabla HTML de WU.
    Usa clases semánticas (NO atributos _ngcontent-*).
    La tabla HTML estándar de WU se visualiza en unidades imperiales (in, mph):
      - Precipitacion (mm) = in * 25.4
      - Viento_medio (km/h) = mph * 1.60934
      - Rafagas (km/h) = mph * 1.60934

    Returns:
        (DataFrame, raw_sample_dict)
    """
    url = (
        f"https://www.wunderground.com/dashboard/pws/{station_id}"
        f"/table/{fecha}/{fecha}/daily"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return None, None

    soup = BeautifulSoup(resp.content, "html.parser")

    table = soup.find("table", class_="history-table")
    if not table:
        return None, None

    tbody = table.find("tbody")
    if not tbody:
        return None, None

    rows_data = []
    raw_sample = None

    for row in tbody.find_all("tr"):
        hora_tag = row.find("strong")
        hora = hora_tag.get_text(strip=True) if hora_tag else ""

        values = [
            span.get_text(strip=True)
            for span in row.find_all("span", class_="wu-value")
        ]

        if len(values) < 9:
            continue

        try:
            # Columnas imperiales: idx 4=wind speed (mph), 5=gust (mph), 8=precip (in)
            wind_speed_mph = float(values[4]) if values[4] != "--" else None
            gust_mph = float(values[5]) if values[5] != "--" else None
            precip_in = float(values[8]) if values[8] != "--" else None

            v_kmh = round(wind_speed_mph * 1.60934, 2) if wind_speed_mph is not None else None
            g_kmh = round(gust_mph * 1.60934, 2) if gust_mph is not None else None
            p_mm = round(precip_in * 25.4, 2) if precip_in is not None else None

            if raw_sample is None and (wind_speed_mph is not None or gust_mph is not None or precip_in is not None):
                raw_sample = {
                    "source": "html_table (imperial)",
                    "hora": hora,
                    "raw_wind_speed_mph": wind_speed_mph,
                    "converted_viento_kmh": v_kmh,
                    "raw_gust_mph": gust_mph,
                    "converted_rafagas_kmh": g_kmh,
                    "raw_precip_in": precip_in,
                    "converted_precip_mm": p_mm,
                }

            rows_data.append({
                "Hora_local": hora,
                "Viento_medio (km/h)": v_kmh,
                "Rafagas (km/h)": g_kmh,
                "Precipitacion (mm)": p_mm,
                "qcStatus": None,
            })
        except (ValueError, IndexError):
            continue

    if not rows_data:
        return None, None

    df = pd.DataFrame(rows_data)
    df["Fecha"] = fecha
    df["Estacion"] = station_id
    return df, raw_sample


def fetch_station_day(station_id: str, fecha: str) -> ScrapeResult:
    """
    Obtiene datos de una estación para un día.
    Prioriza la API JSON; si falla, usa HTML como fallback.
    Reintentos con backoff exponencial.
    """
    import time

    for attempt in range(MAX_RETRIES):
        # 1. Intentar API JSON estructurada primero
        df, raw_sample = _fetch_via_api(station_id, fecha)
        if df is not None and not df.empty:
            return ScrapeResult(
                station_id=station_id,
                fecha=fecha,
                status="ok",
                df=df,
                source_method="api_json",
                raw_sample=raw_sample,
            )

        # 2. Fallback HTML
        df_html, raw_sample_html = _fetch_via_html(station_id, fecha)
        if df_html is not None and not df_html.empty:
            return ScrapeResult(
                station_id=station_id,
                fecha=fecha,
                status="ok",
                df=df_html,
                source_method="html_fallback",
                raw_sample=raw_sample_html,
            )

        if attempt < MAX_RETRIES - 1:
            wait = BACKOFF_BASE ** (attempt + 1)
            time.sleep(wait)

    return ScrapeResult(
        station_id=station_id,
        fecha=fecha,
        status="sin_datos",
        error_msg="No se obtuvieron registros para la fecha consultada.",
    )


def scrape_stations(
    station_ids: list[str],
    fechas: list[str],
    max_workers: int = MAX_WORKERS,
    progress_callback=None,
) -> tuple[list[pd.DataFrame], RunMetrics]:
    """
    Ejecuta el scraping concurrente de múltiples estaciones y fechas.
    """
    metrics = RunMetrics()
    all_dfs = []

    tasks = [(sid, f) for sid in station_ids for f in fechas]
    metrics.total = len(tasks)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_station_day, sid, f): (sid, f)
            for sid, f in tasks
        }

        for i, future in enumerate(as_completed(futures)):
            sid, f = futures[future]
            try:
                result = future.result()
                if result.status == "ok" and result.df is not None:
                    metrics.ok += 1
                    if result.source_method == "api_json":
                        metrics.metodo_api += 1
                    else:
                        metrics.metodo_html += 1
                    all_dfs.append(result.df)
                elif result.status == "sin_datos":
                    metrics.sin_datos += 1
                else:
                    metrics.errores += 1
                    metrics.detalle_errores.append(f"{sid}/{f}: {result.error_msg}")
            except Exception as e:
                metrics.errores += 1
                metrics.detalle_errores.append(f"{sid}/{f}: {str(e)}")

            if progress_callback:
                progress_callback(i + 1, metrics.total, sid, f,
                                  result.status if 'result' in dir() else "error")

    return all_dfs, metrics


def generate_date_range(fecha_inicio: date, fecha_fin: date) -> list[str]:
    """Genera lista de fechas como strings YYYY-MM-DD."""
    from datetime import timedelta
    fechas = []
    current = fecha_inicio
    while current <= fecha_fin:
        fechas.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return fechas
