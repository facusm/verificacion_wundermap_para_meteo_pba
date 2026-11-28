# -*- coding: utf-8 -*-
"""
Lógica meteorológica: conversión de unidades, filtrado por umbrales,
y generación de tablas de verificación para precipitación y viento,
con propagación de origen y QC.
"""

import pandas as pd

from src.geo import ZONA_ORDER
from src.scraper import _build_wu_link


def _sort_by_zona(df: pd.DataFrame, zona_col: str = "Zona DC") -> pd.DataFrame:
    """Ordena un DataFrame por zona en orden romano correcto."""
    if df.empty or zona_col not in df.columns:
        return df
    order_map = {z: i for i, z in enumerate(ZONA_ORDER)}
    df = df.copy()
    df["_zona_order"] = df[zona_col].astype(str).map(order_map)
    df.sort_values("_zona_order", inplace=True)
    df.drop(columns="_zona_order", inplace=True)
    return df.reset_index(drop=True)


def consolidate_raw_data(
    dfs: list[pd.DataFrame],
    catalog: list[dict],
) -> pd.DataFrame:
    """
    Consolida los DataFrames crudos del scraping, les agrega la información de
    Zona DC, Municipio, Origen y QC desde el catálogo, y realiza downcasting
    de tipos (category, float32, Int8) para minimizar uso de RAM.
    """
    if not dfs:
        return _empty_consolidated()

    df = pd.concat(dfs, ignore_index=True)

    # Mapear datos desde el catálogo
    zona_map = {st["station_id"]: st["zona_dc"] for st in catalog}
    municipio_map = {st["station_id"]: st.get("municipio", "Sin asignar") for st in catalog}
    origen_map = {st["station_id"]: st.get("origen", "descubierta") for st in catalog}
    qc_map = {st["station_id"]: st.get("qc_status", 1) for st in catalog}

    df["Zona DC"] = df["Estacion"].map(zona_map)
    df["Municipio"] = df["Estacion"].map(municipio_map).fillna("Sin asignar")
    df["Origen"] = df["Estacion"].map(origen_map).fillna("descubierta")

    # Fallback de QC: si la fila no trae qcStatus (o es None), toma el qc_status del catálogo
    catalog_qc = df["Estacion"].map(qc_map)
    if "qcStatus" in df.columns:
        merged_qc = df["qcStatus"].combine_first(catalog_qc)
        df["QC"] = pd.to_numeric(merged_qc, errors="coerce").fillna(1).astype("Int8")
        df.drop(columns=["qcStatus"], inplace=True)
    else:
        df["QC"] = pd.to_numeric(catalog_qc, errors="coerce").fillna(1).astype("Int8")

    # Agregar link antes de convertir Estacion/Fecha a category
    df["Link"] = df.apply(
        lambda row: _build_wu_link(str(row["Estacion"]), str(row["Fecha"])), axis=1
    )

    # Downcasting numérico (float32)
    for col in ["Viento_medio (km/h)", "Rafagas (km/h)", "Precipitacion (mm)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float32")

    # Downcasting categórico en columnas de baja cardinalidad
    for col in ["Estacion", "Zona DC", "Municipio", "Origen", "Fecha"]:
        if col in df.columns:
            df[col] = df[col].astype("category")

    return df


def _empty_consolidated() -> pd.DataFrame:
    """DataFrame vacío con las columnas esperadas."""
    return pd.DataFrame(columns=[
        "Estacion", "Fecha", "Hora_local",
        "Viento_medio (km/h)", "Rafagas (km/h)", "Precipitacion (mm)",
        "Zona DC", "Municipio", "Origen", "QC", "Link",
    ])


# ═══════════════════════════════════════════════════════════════════════
# PRECIPITACIÓN
# ═══════════════════════════════════════════════════════════════════════

def verificacion_precipitacion_diaria(
    df: pd.DataFrame,
    umbral_mm: float = 15.0,
) -> pd.DataFrame:
    """
    Verificación diaria de precipitación.
    El acumulado diario de cada estación es el MÁXIMO valor de
    Precipitacion (mm) registrado ese día (serie acumulada de PWS).
    Conserva el QC puntual de observación correspondiente a ese pico diario.

    Filtra registros que superen el umbral.
    """
    cols = ["Zona DC", "Municipio", "Estacion", "Fecha", "Precipitacion (mm)", "Origen", "QC", "Link"]
    if df.empty or "Precipitacion (mm)" not in df.columns:
        return pd.DataFrame(columns=cols)

    # Ordenar por Precipitacion descendente para tomar el registro con el pico diario
    # y así conservar el QC puntual exacto de esa observación
    sorted_df = df.sort_values(
        ["Estacion", "Fecha", "Precipitacion (mm)"],
        ascending=[True, True, False],
        na_position="last",
    )
    daily = sorted_df.drop_duplicates(subset=["Estacion", "Fecha"], keep="first").copy()

    over = daily[daily["Precipitacion (mm)"] >= umbral_mm].copy()

    if over.empty:
        return pd.DataFrame(columns=cols)

    over = over[[c for c in cols if c in over.columns]]
    over = _sort_by_zona(over)
    over.sort_values(
        ["Zona DC", "Fecha", "Precipitacion (mm)"],
        ascending=[True, True, False],
        inplace=True,
        key=lambda col: col.astype(str).map({z: i for i, z in enumerate(ZONA_ORDER)}) if col.name == "Zona DC" else col,
    )
    return over.reset_index(drop=True)


def verificacion_precipitacion_acumulada(
    df: pd.DataFrame,
    umbral_mm: float = 15.0,
    total_dias: int = 1,
) -> pd.DataFrame:
    """
    Acumulado total del período por estación.
    Suma los acumulados diarios (max por día) sobre todos los días con datos.
    """
    cols = ["Zona DC", "Municipio", "Estacion", "Dias_con_datos", "Precipitacion (mm)", "Origen", "QC", "Link"]
    if df.empty or "Precipitacion (mm)" not in df.columns:
        return pd.DataFrame(columns=cols)

    sorted_df = df.sort_values(
        ["Estacion", "Fecha", "Precipitacion (mm)"],
        ascending=[True, True, False],
        na_position="last",
    )
    daily = sorted_df.drop_duplicates(subset=["Estacion", "Fecha"], keep="first").copy()

    def _aggregate_qc(s):
        vals = set(s)
        if 0 in vals:
            return 0
        if -1 in vals:
            return -1
        return 1

    acum = (
        daily.groupby("Estacion", observed=True)
        .agg({
            "Precipitacion (mm)": "sum",
            "Zona DC": "first",
            "Municipio": "first",
            "Origen": "first",
            "QC": _aggregate_qc,
            "Link": "first",
            "Fecha": "count",
        })
        .reset_index()
        .rename(columns={"Fecha": "Dias_con_datos"})
    )

    over = acum[acum["Precipitacion (mm)"] >= umbral_mm].copy()

    if over.empty:
        return pd.DataFrame(columns=cols)

    over = over[[c for c in cols if c in over.columns]]
    over = _sort_by_zona(over)
    over.sort_values(
        ["Zona DC", "Precipitacion (mm)"],
        ascending=[True, False],
        inplace=True,
        key=lambda col: col.astype(str).map({z: i for i, z in enumerate(ZONA_ORDER)}) if col.name == "Zona DC" else col,
    )
    return over.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════
# VIENTO Y RÁFAGAS
# ═══════════════════════════════════════════════════════════════════════

def verificacion_viento(
    df: pd.DataFrame,
    umbral_kmh: float = 60.0,
) -> pd.DataFrame:
    """
    Filtra registros donde el viento medio o las ráfagas superan el umbral.
    """
    cols = ["Zona DC", "Municipio", "Estacion", "Fecha", "Hora_local", "Viento_medio (km/h)", "Rafagas (km/h)", "Origen", "QC", "Link"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    for col in ["Viento_medio (km/h)", "Rafagas (km/h)"]:
        if col not in df.columns:
            df[col] = None

    mask = (
        (df["Viento_medio (km/h)"].fillna(0) >= umbral_kmh) |
        (df["Rafagas (km/h)"].fillna(0) >= umbral_kmh)
    )

    over = df[mask].copy()

    if over.empty:
        return pd.DataFrame(columns=cols)

    result = over[[c for c in cols if c in over.columns]]
    result = _sort_by_zona(result)
    return result.reset_index(drop=True)


def resumen_rafaga_maxima(
    df: pd.DataFrame,
    umbral_kmh: float = 60.0,
) -> pd.DataFrame:
    """
    Tabla resumen con la ráfaga máxima por estación, fecha y zona DC,
    conservando la Hora_local exacta (o 'Resumen diario') y el QC de esa fila pico.
    """
    cols = ["Zona DC", "Municipio", "Estacion", "Fecha", "Hora_local", "Rafaga_max (km/h)", "Origen", "QC", "Link"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    wind_df = verificacion_viento(df, umbral_kmh)
    if wind_df.empty:
        return pd.DataFrame(columns=cols)

    # Ordenar por ráfagas descendente para que el primer registro por estación y fecha
    # sea la ráfaga pico y conserve su QC puntual exacto de observación
    sorted_wind = wind_df.sort_values(
        ["Zona DC", "Municipio", "Estacion", "Fecha", "Rafagas (km/h)"],
        ascending=[True, True, True, True, False],
        na_position="last",
    )
    resumen = (
        sorted_wind.drop_duplicates(
            subset=["Zona DC", "Municipio", "Estacion", "Fecha"],
            keep="first",
        )
        .rename(columns={"Rafagas (km/h)": "Rafaga_max (km/h)"})
        .copy()
    )

    resumen = _sort_by_zona(resumen)
    resumen.sort_values(
        ["Zona DC", "Rafaga_max (km/h)"],
        ascending=[True, False],
        inplace=True,
        key=lambda col: col.astype(str).map({z: i for i, z in enumerate(ZONA_ORDER)}) if col.name == "Zona DC" else col,
    )
    return resumen[[c for c in cols if c in resumen.columns]].reset_index(drop=True)


def format_max_gust_line(max_v_df: pd.DataFrame, n_dias_periodo: int):
    """
    Formatea la línea de ráfaga máxima para el popup del mapa incluyendo fecha/hora
    cuando el dato proviene de history/all ('HH:MM hs') o solo fecha en modo 'Resumen diario' multi-día.
    """
    if max_v_df.empty or "Rafaga_max (km/h)" not in max_v_df.columns:
        return None
    valid = max_v_df["Rafaga_max (km/h)"].dropna()
    if valid.empty:
        return None

    idx_max = valid.idxmax()
    max_row = max_v_df.loc[idx_max]
    max_v = float(max_row["Rafaga_max (km/h)"])

    fecha_str = str(max_row.get("Fecha", "") or "")
    hora_str = str(max_row.get("Hora_local", "") or "").strip()
    dd_mm = f"{fecha_str[8:10]}/{fecha_str[5:7]}" if len(fecha_str) >= 10 else fecha_str

    es_hora_real = bool(hora_str) and hora_str != "Resumen diario" and hora_str.lower() != "nan"
    if es_hora_real:
        if n_dias_periodo == 1:
            return f"Max ráfaga: {max_v:.1f} km/h ({hora_str} hs)"
        return (
            f"Max ráfaga: {max_v:.1f} km/h ({dd_mm} {hora_str} hs)"
            if dd_mm else f"Max ráfaga: {max_v:.1f} km/h ({hora_str} hs)"
        )

    if hora_str == "Resumen diario" and n_dias_periodo > 1 and dd_mm:
        return f"Max ráfaga: {max_v:.1f} km/h ({dd_mm})"

    return f"Max ráfaga: {max_v:.1f} km/h"


# ═══════════════════════════════════════════════════════════════════════
# RESUMEN POR ZONA DC
# ═══════════════════════════════════════════════════════════════════════

def resumen_por_zona(
    pp_diaria: pd.DataFrame,
    viento_resumen: pd.DataFrame,
) -> pd.DataFrame:
    """
    Genera tabla resumen comparativa de máximos por Zona DC.
    """
    if not pp_diaria.empty and "Zona DC" in pp_diaria.columns:
        pp_zona = (
            pp_diaria.groupby("Zona DC", observed=True)
            .agg({
                "Precipitacion (mm)": "max",
                "Estacion": "nunique",
            })
            .reset_index()
            .rename(columns={
                "Precipitacion (mm)": "Max_Precip (mm)",
                "Estacion": "EMAs_con_precip",
            })
        )
        pp_zona["Zona DC"] = pp_zona["Zona DC"].astype(str)
    else:
        pp_zona = pd.DataFrame(columns=["Zona DC", "Max_Precip (mm)", "EMAs_con_precip"])

    if not viento_resumen.empty and "Zona DC" in viento_resumen.columns:
        rafaga_col = "Rafaga_max (km/h)" if "Rafaga_max (km/h)" in viento_resumen.columns else "Rafagas (km/h)"
        viento_zona = (
            viento_resumen.groupby("Zona DC", observed=True)
            .agg({
                rafaga_col: "max",
                "Estacion": "nunique",
            })
            .reset_index()
            .rename(columns={
                rafaga_col: "Max_Rafaga (km/h)",
                "Estacion": "EMAs_con_viento",
            })
        )
        viento_zona["Zona DC"] = viento_zona["Zona DC"].astype(str)
    else:
        viento_zona = pd.DataFrame(columns=["Zona DC", "Max_Rafaga (km/h)", "EMAs_con_viento"])

    all_zonas = pd.DataFrame({"Zona DC": ZONA_ORDER})
    result = all_zonas.merge(pp_zona, on="Zona DC", how="left")
    result = result.merge(viento_zona, on="Zona DC", how="left")

    result["Max_Precip (mm)"] = result["Max_Precip (mm)"].fillna(0)
    result["EMAs_con_precip"] = result["EMAs_con_precip"].fillna(0).astype(int)
    result["Max_Rafaga (km/h)"] = result["Max_Rafaga (km/h)"].fillna(0)
    result["EMAs_con_viento"] = result["EMAs_con_viento"].fillna(0).astype(int)

    return result


def get_audit_info(
    station_ids: list[str],
    df_consolidated: pd.DataFrame,
    catalog: list[dict],
) -> pd.DataFrame:
    """
    Genera tabla de auditoría con Origen y QC.
    """
    if df_consolidated.empty:
        stations_with_data = set()
    else:
        stations_with_data = set(df_consolidated["Estacion"].unique())

    all_stations = set(station_ids)
    missing = all_stations - stations_with_data

    zona_map = {st["station_id"]: st["zona_dc"] for st in catalog}
    municipio_map = {st["station_id"]: st.get("municipio", "Sin asignar") for st in catalog}
    nombre_map = {st["station_id"]: st.get("nombre", "") for st in catalog}
    origen_map = {st["station_id"]: st.get("origen", "descubierta") for st in catalog}
    qc_map = {st["station_id"]: st.get("qc_status", 1) for st in catalog}

    rows = []
    for sid in sorted(missing):
        rows.append({
            "Estacion": sid,
            "Zona DC": zona_map.get(sid, "Desconocida"),
            "Municipio": municipio_map.get(sid, "Sin asignar"),
            "Nombre": nombre_map.get(sid, ""),
            "Origen": origen_map.get(sid, "descubierta"),
            "QC": qc_map.get(sid, 1),
            "Estado": "Sin datos",
        })

    return pd.DataFrame(rows)
