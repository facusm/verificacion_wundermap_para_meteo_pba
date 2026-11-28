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
from datetime import date, datetime, timedelta, timezone
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
WU_API_DAILY_BASE = "https://api.weather.com/v2/pws/history/daily"

MAX_PREVIOUS_DAYS_FOR_ALL = 3
DAILY_CHUNK_DAYS = 30
WIND_KEEP_THRESHOLD_KMH = 25.0
TZ_AR = timezone(timedelta(hours=-3))

MAX_WORKERS = 12
TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # segundos


@dataclass
class ScrapeResult:
    """Resultado del scraping de una estación para una fecha o bloque."""
    station_id: str
    fecha: str
    status: str  # "ok", "sin_datos", "error"
    df: Optional[pd.DataFrame] = None
    source_method: str = ""  # "api_json", "api_daily", o "html_fallback"
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
    metodo_daily: int = 0
    metodo_html: int = 0
    mode: str = "all"
    detalle_errores: list = field(default_factory=list)


def _build_wu_link(station_id: str, fecha: str) -> str:
    """Construye el enlace al gráfico diario de WU."""
    return (
        f"https://www.wunderground.com/dashboard/pws/{station_id}"
        f"/graph/{fecha}/{fecha}/daily"
    )


def _compact_station_day_records(
    records: list[dict],
    wind_keep_threshold: float = WIND_KEEP_THRESHOLD_KMH,
) -> list[dict]:
    """
    Compacta observaciones sub-horarias de un día de una estación conservando:
    - Por cada qcStatus presente (1, 0, -1, None): el registro con mayor
      precipitación y el registro con mayor ráfaga/viento.
    - Todas las observaciones individuales donde ráfaga o viento >= wind_keep_threshold.
    - Al menos 1 registro representativo en días secos/calmos.
    Elimina duplicados y conserva el orden cronológico original.
    """
    if not records:
        return []

    keep_indices: set[int] = set()
    by_qc: dict = {}

    for idx, rec in enumerate(records):
        qc = rec.get("qcStatus")
        by_qc.setdefault(qc, []).append((idx, rec))

        gust = rec.get("Rafagas (km/h)")
        wind = rec.get("Viento_medio (km/h)")
        if wind is None:
            wind = rec.get("Viento (km/h)")

        if (gust is not None and gust >= wind_keep_threshold) or (
            wind is not None and wind >= wind_keep_threshold
        ):
            keep_indices.add(idx)

    for qc_group in by_qc.values():
        # Máximo de precipitación del grupo qcStatus
        best_pp_idx = max(
            qc_group,
            key=lambda item: (
                item[1].get("Precipitacion (mm)")
                if item[1].get("Precipitacion (mm)") is not None
                else float("-inf"),
                -item[0],
            ),
        )[0]
        keep_indices.add(best_pp_idx)

        # Máximo de ráfaga / viento del grupo qcStatus
        def _wind_score(item):
            rec = item[1]
            gust = rec.get("Rafagas (km/h)")
            wind = rec.get("Viento_medio (km/h)")
            if wind is None:
                wind = rec.get("Viento (km/h)")
            return (
                gust if gust is not None else float("-inf"),
                wind if wind is not None else float("-inf"),
                -item[0],
            )

        best_wind_idx = max(qc_group, key=_wind_score)[0]
        keep_indices.add(best_wind_idx)

    if not keep_indices:
        keep_indices.add(0)

    return [records[i] for i in sorted(keep_indices)]


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

    rows = _compact_station_day_records(rows)
    df = pd.DataFrame(rows)
    df["Fecha"] = fecha
    df["Estacion"] = station_id
    return df, raw_sample


def _parse_daily_observations(
    observations: list[dict],
    station_id: str,
) -> Tuple[Optional[pd.DataFrame], Optional[dict]]:
    """
    Normaliza observaciones de /v2/pws/history/daily al mismo esquema que history/all.
    """
    if not observations:
        return None, None

    rows = []
    raw_sample = None

    for obs in observations:
        metric = obs.get("metric") or {}
        obs_time = obs.get("obsTimeLocal", "") or ""
        fecha_str = ""
        if len(obs_time) >= 10 and obs_time[4] == "-" and obs_time[7] == "-":
            fecha_str = obs_time[:10]
        else:
            try:
                fecha_str = datetime.fromisoformat(obs_time.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                continue

        precip = metric.get("precipTotal")
        wind_gust = metric.get("windgustHigh")
        wind_speed = metric.get("windspeedHigh")
        if wind_speed is None:
            wind_speed = metric.get("windspeedAvg")
        qc_status = obs.get("qcStatus")

        if raw_sample is None and (precip is not None or wind_speed is not None or wind_gust is not None):
            raw_sample = {
                "source": "api_daily (units=m)",
                "obsTimeLocal": obs_time,
                "raw_precipTotal_mm": precip,
                "raw_windspeedHigh_kmh": wind_speed,
                "raw_windgustHigh_kmh": wind_gust,
                "qcStatus": qc_status,
            }

        rows.append({
            "Hora_local": "Resumen diario",
            "Viento_medio (km/h)": round(float(wind_speed), 2) if wind_speed is not None else None,
            "Rafagas (km/h)": round(float(wind_gust), 2) if wind_gust is not None else None,
            "Precipitacion (mm)": round(float(precip), 2) if precip is not None else None,
            "qcStatus": int(qc_status) if qc_status is not None else None,
            "Fecha": fecha_str,
            "Estacion": station_id,
        })

    if not rows:
        return None, None

    df = pd.DataFrame(rows)
    return df, raw_sample


def _fetch_via_daily_api(
    station_id: str,
    start_date: date,
    end_date: date,
) -> Tuple[Optional[pd.DataFrame], Optional[dict], bool]:
    """
    Obtiene datos diarios resumidos vía /v2/pws/history/daily (bloques <= 30 días).
    Returns:
        (DataFrame | None, raw_sample | None, transient_error: bool)
    """
    api_key = get_wu_api_key()
    params = {
        "stationId": station_id,
        "format": "json",
        "units": "m",
        "startDate": start_date.strftime("%Y%m%d"),
        "endDate": end_date.strftime("%Y%m%d"),
        "numericPrecision": "decimal",
        "apiKey": api_key,
    }

    try:
        resp = requests.get(
            WU_API_DAILY_BASE, params=params, headers=HEADERS, timeout=TIMEOUT
        )
        if resp.status_code == 401:
            api_key = get_wu_api_key(force_refresh=True)
            params["apiKey"] = api_key
            resp = requests.get(
                WU_API_DAILY_BASE, params=params, headers=HEADERS, timeout=TIMEOUT
            )

        if resp.status_code == 204:
            return None, None, False

        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None, None, True

    observations = data.get("observations")
    if not observations:
        return None, None, False

    df, raw_sample = _parse_daily_observations(observations, station_id)
    return df, raw_sample, False


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


def fetch_station_daily(
    station_id: str,
    start_date: date,
    end_date: date,
) -> ScrapeResult:
    """Obtiene datos de un bloque de fechas (<= 30 días) vía history/daily con reintentos."""
    import time

    label = f"{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
    for attempt in range(MAX_RETRIES):
        df, raw_sample, transient_error = _fetch_via_daily_api(station_id, start_date, end_date)
        if df is not None and not df.empty:
            return ScrapeResult(
                station_id=station_id,
                fecha=label,
                status="ok",
                df=df,
                source_method="api_daily",
                raw_sample=raw_sample,
            )
        if not transient_error:
            break
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_BASE ** (attempt + 1))

    return ScrapeResult(
        station_id=station_id,
        fecha=label,
        status="sin_datos",
        error_msg="No se obtuvieron registros diarios para el rango consultado.",
    )


def split_date_chunks(
    fecha_inicio: date,
    fecha_fin: date,
    chunk_days: int = DAILY_CHUNK_DAYS,
) -> list[tuple[date, date]]:
    """Divide [fecha_inicio, fecha_fin] en bloques inclusivos de hasta chunk_days días."""
    if fecha_inicio > fecha_fin:
        return []
    chunks = []
    current = fecha_inicio
    while current <= fecha_fin:
        chunk_end = min(fecha_fin, current + timedelta(days=chunk_days - 1))
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def plan_scrape_strategy(
    fecha_inicio: date,
    fecha_fin: date,
    hoy_ar: Optional[date] = None,
    max_previous_days_for_all: int = MAX_PREVIOUS_DAYS_FOR_ALL,
    daily_supports_today: bool = True,
) -> dict:
    """
    Determina dinámicamente la estrategia de extracción entre history/all y history/daily.

    Reglas:
    - Caso 1 (all): incluye hoy y dias_previos <= MAX_PREVIOUS_DAYS_FOR_ALL (<= 3) -> history/all compactado.
    - Caso 2 (daily): no incluye hoy -> history/daily en bloques <= 30 días.
    - Caso 3 (rango largo con hoy): incluye hoy y dias_previos > MAX_PREVIOUS_DAYS_FOR_ALL ->
      usa history/daily en bloques <= 30 días (incluyendo hoy si daily_supports_today=True,
      o 'daily+hoy_all' consultando solo hoy_ar con history/all si daily_supports_today=False).
    """
    if hoy_ar is None:
        hoy_ar = datetime.now(TZ_AR).date()

    contiene_hoy = fecha_inicio <= hoy_ar <= fecha_fin
    dias_previos = (hoy_ar - fecha_inicio).days

    if contiene_hoy and dias_previos <= max_previous_days_for_all:
        return {
            "mode": "all",
            "daily_chunks": [],
            "all_dates": generate_date_range(fecha_inicio, fecha_fin),
            "contiene_hoy": contiene_hoy,
            "dias_previos": dias_previos,
        }

    if not contiene_hoy:
        return {
            "mode": "daily",
            "daily_chunks": split_date_chunks(fecha_inicio, fecha_fin, DAILY_CHUNK_DAYS),
            "all_dates": [],
            "contiene_hoy": contiene_hoy,
            "dias_previos": dias_previos,
        }

    # Caso 3: contiene hoy y dias_previos > max_previous_days_for_all
    if daily_supports_today:
        return {
            "mode": "daily",
            "daily_chunks": split_date_chunks(fecha_inicio, fecha_fin, DAILY_CHUNK_DAYS),
            "all_dates": [],
            "contiene_hoy": contiene_hoy,
            "dias_previos": dias_previos,
        }

    closed_end = hoy_ar - timedelta(days=1)
    daily_chunks = split_date_chunks(fecha_inicio, closed_end, DAILY_CHUNK_DAYS) if fecha_inicio <= closed_end else []
    return {
        "mode": "daily+hoy_all",
        "daily_chunks": daily_chunks,
        "all_dates": [hoy_ar.strftime("%Y-%m-%d")],
        "contiene_hoy": contiene_hoy,
        "dias_previos": dias_previos,
    }


def scrape_date_range(
    station_ids: list[str],
    fecha_inicio: date,
    fecha_fin: date,
    max_workers: int = MAX_WORKERS,
    progress_callback=None,
    hoy_ar: Optional[date] = None,
) -> tuple[list[pd.DataFrame], RunMetrics]:
    """
    Ejecuta el scraping concurrente eligiendo dinámicamente history/all o history/daily.
    """
    strategy = plan_scrape_strategy(fecha_inicio, fecha_fin, hoy_ar=hoy_ar)
    metrics = RunMetrics(mode=strategy["mode"])
    all_dfs = []

    tasks = []
    for sid in station_ids:
        for chunk_start, chunk_end in strategy["daily_chunks"]:
            tasks.append(("daily", sid, chunk_start, chunk_end))
        for f in strategy["all_dates"]:
            tasks.append(("all", sid, f, None))

    metrics.total = len(tasks)
    if not tasks:
        return all_dfs, metrics

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for kind, sid, arg1, arg2 in tasks:
            if kind == "daily":
                fut = executor.submit(fetch_station_daily, sid, arg1, arg2)
                label = f"{arg1.strftime('%Y-%m-%d')}..{arg2.strftime('%Y-%m-%d')}"
            else:
                fut = executor.submit(fetch_station_day, sid, arg1)
                label = arg1
            futures[fut] = (sid, label)

        for i, future in enumerate(as_completed(futures)):
            sid, label = futures[future]
            status = "error"
            try:
                result = future.result()
                status = result.status
                if result.status == "ok" and result.df is not None:
                    metrics.ok += 1
                    if result.source_method == "api_daily":
                        metrics.metodo_daily += 1
                    elif result.source_method == "api_json":
                        metrics.metodo_api += 1
                    else:
                        metrics.metodo_html += 1
                    all_dfs.append(result.df)
                elif result.status == "sin_datos":
                    metrics.sin_datos += 1
                else:
                    metrics.errores += 1
                    metrics.detalle_errores.append(f"{sid}/{label}: {result.error_msg}")
            except Exception as e:
                metrics.errores += 1
                metrics.detalle_errores.append(f"{sid}/{label}: {str(e)}")

            if progress_callback:
                progress_callback(i + 1, metrics.total, sid, label, status)

    return all_dfs, metrics


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
