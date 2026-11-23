# -*- coding: utf-8 -*-
"""
Exportación a Excel en memoria para descarga vía Streamlit.
Formato con openpyxl: encabezados en negrita, filtros automáticos,
ancho autoajustado, links clickeables y columnas Origen y QC.
"""

import io
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from src.geo import ZONA_ORDER


# ─── Estilos ──────────────────────────────────────────────────────────
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center")


def _apply_header_style(ws):
    """Aplica estilo a la fila de encabezados."""
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT


def _auto_width(ws, min_width=10, max_width=50):
    """Autoajusta el ancho de columnas."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value) if cell.value else ""
                max_len = max(max_len, len(val))
            except Exception:
                pass
        adjusted = min(max(max_len + 2, min_width), max_width)
        ws.column_dimensions[col_letter].width = adjusted


def _add_hyperlinks(ws, link_col_idx: int):
    """Convierte la columna de links en hipervínculos clickeables."""
    for row in ws.iter_rows(min_row=2, min_col=link_col_idx, max_col=link_col_idx):
        for cell in row:
            if cell.value and str(cell.value).startswith("http"):
                cell.hyperlink = str(cell.value)
                cell.font = Font(color="0563C1", underline="single")
                cell.value = "Ver gráfico"


def _add_auto_filter(ws):
    """Agrega filtros automáticos."""
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions


def _df_to_sheet(ws, df: pd.DataFrame, round_precip: bool = False):
    """Escribe un DataFrame en una hoja de Excel con formato."""
    for col_idx, col_name in enumerate(df.columns, 1):
        ws.cell(row=1, column=col_idx, value=col_name)

    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        for col_idx, value in enumerate(row, 1):
            col_name = df.columns[col_idx - 1]
            if round_precip and col_name == "Precipitacion (mm)":
                try:
                    value = int(round(float(value)))
                except (ValueError, TypeError):
                    pass
            ws.cell(row=row_idx, column=col_idx, value=value)

    _apply_header_style(ws)
    _auto_width(ws)
    _add_auto_filter(ws)

    if "Link" in df.columns:
        link_col = list(df.columns).index("Link") + 1
        _add_hyperlinks(ws, link_col)


def _zona_sort_key(zona: str) -> int:
    """Devuelve el índice de orden para una zona."""
    try:
        return ZONA_ORDER.index(zona)
    except ValueError:
        return 999


# ═══════════════════════════════════════════════════════════════════════
# EXCEL DE PRECIPITACIÓN
# ═══════════════════════════════════════════════════════════════════════

def export_precipitacion(
    pp_diaria: pd.DataFrame,
    pp_acumulada: pd.DataFrame,
    resumen_zona: pd.DataFrame,
    fecha_inicio: str,
    fecha_fin: str,
    por_zona_dc: bool = False,
) -> io.BytesIO:
    """Genera el Excel de precipitación en memoria con Origen y QC."""
    output = io.BytesIO()
    wb = Workbook()

    # Hoja Resumen por Zona DC
    ws_resumen = wb.active
    ws_resumen.title = "Resumen_por_Zona_DC"
    if not resumen_zona.empty:
        cols_pp = ["Zona DC", "Max_Precip (mm)", "EMAs_con_precip"]
        resumen_pp = resumen_zona[[c for c in cols_pp if c in resumen_zona.columns]].copy()
        _df_to_sheet(ws_resumen, resumen_pp)
    else:
        ws_resumen.cell(row=1, column=1, value="Sin datos de precipitación sobre umbral")

    # Hojas por fecha
    if not pp_diaria.empty:
        cols_order = ["Zona DC", "Municipio", "Estacion", "Origen", "QC", "Precipitacion (mm)", "Link"]
        for fecha in sorted(pp_diaria["Fecha"].unique()):
            df_fecha = pp_diaria[pp_diaria["Fecha"] == fecha].copy()
            df_fecha = df_fecha[[c for c in cols_order if c in df_fecha.columns]]
            df_fecha.sort_values(
                ["Zona DC", "Precipitacion (mm)"],
                ascending=[True, False],
                key=lambda col: col.map({z: i for i, z in enumerate(ZONA_ORDER)}) if col.name == "Zona DC" else col,
                inplace=True,
            )
            sheet_name = str(fecha)[:31]
            ws = wb.create_sheet(title=sheet_name)
            _df_to_sheet(ws, df_fecha.reset_index(drop=True), round_precip=True)

    # Hoja Acumulado Total
    if not pp_acumulada.empty:
        ws_acum = wb.create_sheet(title="Acumulado_Total")
        cols_acum = ["Zona DC", "Municipio", "Estacion", "Origen", "QC", "Dias_con_datos", "Precipitacion (mm)", "Link"]
        df_acum = pp_acumulada[[c for c in cols_acum if c in pp_acumulada.columns]].copy()
        _df_to_sheet(ws_acum, df_acum.reset_index(drop=True), round_precip=True)

    # Hojas por Zona DC (opcional)
    if por_zona_dc and not pp_diaria.empty:
        cols_zona = ["Municipio", "Estacion", "Origen", "QC", "Fecha", "Precipitacion (mm)", "Link"]
        zonas_afectadas = sorted(
            pp_diaria["Zona DC"].unique(), key=_zona_sort_key
        )
        for zona in zonas_afectadas:
            df_zona = pp_diaria[pp_diaria["Zona DC"] == zona].copy()
            df_zona = df_zona[[c for c in cols_zona if c in df_zona.columns]]
            df_zona.sort_values(
                ["Fecha", "Precipitacion (mm)"],
                ascending=[True, False],
                inplace=True,
            )
            sheet_name = zona.replace(" ", "_")[:31]
            ws = wb.create_sheet(title=sheet_name)
            _df_to_sheet(ws, df_zona.reset_index(drop=True), round_precip=True)

    wb.save(output)
    output.seek(0)
    return output


# ═══════════════════════════════════════════════════════════════════════
# EXCEL DE VIENTO
# ═══════════════════════════════════════════════════════════════════════

def export_viento(
    viento_detalle: pd.DataFrame,
    viento_resumen: pd.DataFrame,
    resumen_zona: pd.DataFrame,
    fecha_inicio: str,
    fecha_fin: str,
) -> io.BytesIO:
    """Genera el Excel de viento y ráfagas en memoria con Origen y QC."""
    output = io.BytesIO()
    wb = Workbook()

    # Hoja Resumen por Zona DC
    ws_resumen = wb.active
    ws_resumen.title = "Resumen_por_Zona_DC"
    if not viento_resumen.empty:
        cols_res = ["Zona DC", "Municipio", "Estacion", "Origen", "QC", "Fecha", "Rafaga_max (km/h)", "Link"]
        df_res = viento_resumen[[c for c in cols_res if c in viento_resumen.columns]].copy()
        _df_to_sheet(ws_resumen, df_res.reset_index(drop=True))
    else:
        ws_resumen.cell(row=1, column=1, value="Sin registros de viento sobre umbral")

    # Hojas por fecha
    if not viento_detalle.empty:
        cols_det = ["Zona DC", "Municipio", "Estacion", "Origen", "QC", "Hora_local", "Viento_medio (km/h)", "Rafagas (km/h)", "Link"]
        for fecha in sorted(viento_detalle["Fecha"].unique()):
            df_fecha = viento_detalle[viento_detalle["Fecha"] == fecha].copy()
            df_fecha = df_fecha[[c for c in cols_det if c in df_fecha.columns]]
            df_fecha.sort_values(
                ["Zona DC", "Rafagas (km/h)"],
                ascending=[True, False],
                key=lambda col: col.map({z: i for i, z in enumerate(ZONA_ORDER)}) if col.name == "Zona DC" else col,
                inplace=True,
            )
            sheet_name = str(fecha)[:31]
            ws = wb.create_sheet(title=sheet_name)
            _df_to_sheet(ws, df_fecha.reset_index(drop=True))

    wb.save(output)
    output.seek(0)
    return output
