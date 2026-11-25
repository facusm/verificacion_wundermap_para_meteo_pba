# -*- coding: utf-8 -*-
"""
Verificación Wundermap para Meteo PBA — Aplicación Streamlit
=============================================================
Consulta registros diarios de EMAs/PWS de Weather Underground
dentro de la Provincia de Buenos Aires, clasificándolas por
Zona de Defensa Civil (I–XII), con soporte para catálogo completo
y filtrado previo por Zona, Origen y QC Status.
"""

import json
import datetime
from datetime import datetime, timezone, timedelta
import pathlib

import pandas as pd
import streamlit as st

from src.geo import ZONA_ORDER, load_zonas_simple
from src.stations import load_catalog, build_catalog, catalog_summary, BASE_STATIONS
from src.scraper import scrape_stations, generate_date_range
from src.processing import (
    consolidate_raw_data,
    verificacion_precipitacion_diaria,
    verificacion_precipitacion_acumulada,
    verificacion_viento,
    resumen_rafaga_maxima,
    resumen_por_zona,
    get_audit_info,
)
from src.export import export_precipitacion, export_viento

# ─── Configuración de página ──────────────────────────────────────────
st.set_page_config(
    page_title="Verificación Meteo PBA",
    page_icon="🌦️",
    layout="wide",
    initial_sidebar_state="expanded",
)

DATA_DIR = pathlib.Path("data")


# ═══════════════════════════════════════════════════════════════════════
# FUNCIONES DE CACHÉ
# ═══════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600)
def cached_load_catalog():
    """Carga el catálogo de estaciones (cache 1h)."""
    return load_catalog(DATA_DIR)


@st.cache_data(ttl=900, show_spinner=False)
def cached_scrape(station_ids_tuple, fechas_tuple):
    """
    Ejecuta scraping con caché.
    TTL 15 min (incluye fecha actual) / tupla para hashability.
    """
    dfs, metrics = scrape_stations(
        list(station_ids_tuple),
        list(fechas_tuple),
    )
    return dfs, metrics


# ═══════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════

# Cargar catálogo de forma anticipada
try:
    catalog = cached_load_catalog()
except Exception:
    catalog = []

st.sidebar.title("🌦️ Meteo PBA")
st.sidebar.markdown("### Verificación de EMAs")

# ── SECCIÓN 1: PARÁMETROS DE CONSULTA (DESCARGA) ─────────────────────
# Fecha actual en Argentina (UTC-3)
hoy_ar = datetime.now(timezone(timedelta(hours=-3))).date()

st.sidebar.markdown("---")
st.sidebar.subheader("📅 Rango de fechas")
col_fi, col_ff = st.sidebar.columns(2)
with col_fi:
    fecha_inicio = st.date_input(
        "Fecha inicio",
        value=hoy_ar,
        max_value=hoy_ar,
    )
with col_ff:
    fecha_fin = st.date_input(
        "Fecha fin",
        value=hoy_ar,
        max_value=hoy_ar,
    )

# Validar fechas
if fecha_inicio > fecha_fin:
    st.sidebar.error("⚠️ La fecha de inicio debe ser anterior o igual a la fecha de fin.")
    st.stop()

n_dias = (fecha_fin - fecha_inicio).days + 1
if n_dias > 7:
    st.sidebar.warning(
        f"⚠️ Rango de {n_dias} días. La consulta puede tardar varios minutos."
    )

# Selector de Zonas DC
st.sidebar.markdown("---")
st.sidebar.subheader("🗺️ Zonas de Defensa Civil")
zonas_seleccionadas = st.sidebar.multiselect(
    "Zonas a consultar",
    options=ZONA_ORDER,
    default=ZONA_ORDER,
)

if not zonas_seleccionadas:
    st.sidebar.error("Seleccioná al menos una zona.")
    st.stop()

st.sidebar.caption("⚡ El **Rango de fechas** y las **Zonas a consultar** definen los datos que se descargan de Weather Underground al presionar el botón.")

# Botón principal
st.sidebar.markdown("---")
ejecutar = st.sidebar.button(
    "🚀 Ejecutar verificación",
    type="primary",
    use_container_width=True,
)

# ── SECCIÓN 2: FILTROS REACTIVOS EN MEMORIA (TIEMPO REAL) ─────────────
st.sidebar.markdown("---")
st.sidebar.subheader("⚡ Filtros en tiempo real")
st.sidebar.caption("Se aplican de forma inmediata sobre los datos ya cargados sin re-consultar la API.")

universo_emas = st.sidebar.radio(
    "Selección de estaciones",
    options=["Todas las EMAs de PBA", "Solo EMAs base verificadas (lista histórica)"],
    index=1,
)
ocultar_qc = st.sidebar.checkbox(
    "Ocultar datos con fallo de QC en WU (qcStatus = 0)",
    value=False,
    help="Códigos de Weather Underground: 1 = Aprobado | 0 = Posible dato erróneo (falló QC) | -1 = Sin chequeo automático",
)

st.sidebar.markdown("**📊 Umbrales**")
umbral_pp = st.sidebar.number_input(
    "Precipitación (mm)", value=15.0, min_value=0.0, step=1.0,
    help="Umbral mínimo para alertar precipitación diaria y acumulada",
)
umbral_viento = st.sidebar.number_input(
    "Viento/Ráfagas (km/h)", value=60.0, min_value=0.0, step=5.0,
    help="Umbral mínimo para alertar viento medio o ráfagas",
)

# Catálogo de EMAs
st.sidebar.markdown("---")
with st.sidebar.expander("📋 Catálogo de EMAs PBA"):
    if catalog:
        summary = catalog_summary(catalog)
        st.dataframe(summary, use_container_width=True, hide_index=True)
        tot_b = sum(1 for s in catalog if s.get("origen") == "base_verificada")
        tot_d = sum(1 for s in catalog if s.get("origen") == "descubierta")
        st.caption(f"Total: {len(catalog)} estaciones (Base: {tot_b} | Descubiertas: {tot_d})")

        # Descargar JSON del catálogo
        catalog_json = json.dumps(catalog, ensure_ascii=False, indent=2)
        st.download_button(
            "📥 Descargar catálogo (JSON)",
            data=catalog_json,
            file_name="estaciones_pba.json",
            mime="application/json",
        )

        # Botón refrescar catálogo
        if st.button("🔄 Redescubrir catálogo completo", help="Ejecuta descubrimiento adaptativo sobre toda PBA"):
            with st.spinner("Descubriendo EMAs en PBA... Esto puede tardar unos minutos."):
                new_catalog, _, _ = build_catalog(data_dir=DATA_DIR)
                st.cache_data.clear()
                st.success(f"✅ Catálogo actualizado: {len(new_catalog)} estaciones")
                st.rerun()

    else:
        st.warning(
            "No se encontró el catálogo. "
            "Ejecutá `python scripts/build_catalog.py` primero."
        )

# ═══════════════════════════════════════════════════════════════════════
# PANEL PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════

st.title("🌦️ Verificación Meteorológica — Provincia de Buenos Aires")
st.markdown(
    "Consulta y clasificación operativa de Estaciones Meteorológicas Automáticas (EMAs) "
    "de Weather Underground por Zona de Defensa Civil."
)

# Si se presiona Ejecutar verificación
if ejecutar:
    if not catalog:
        st.error(
            "❌ No se encontró el catálogo de estaciones. "
            "Ejecutá `python scripts/build_catalog.py` para generarlo."
        )
        st.stop()

    # DESCARGA COMPLETA:
    # Se consultan TODAS las estaciones del catálogo pertenecientes a las zonas_seleccionadas,
    # sin pre-filtrar por universo_emas ni por qc_status == 0.
    # Esto permite alternar entre "Solo base" y "Todas" al instante en memoria sin re-consultar WU.
    stations_to_query = [
        st_item for st_item in catalog
        if st_item["zona_dc"] in zonas_seleccionadas
    ]

    station_ids = [s["station_id"] for s in stations_to_query]
    fechas = generate_date_range(fecha_inicio, fecha_fin)

    if not station_ids:
        st.warning("No hay estaciones que cumplan los filtros seleccionados.")
        st.stop()

    # ─── Scraping ─────────────────────────────────────────────────────
    progress_bar = st.progress(0, text=f"Consultando {len(station_ids)} estaciones...")

    total_tasks = len(station_ids) * len(fechas)

    def update_progress(current, total, sid, fecha, status):
        pct = current / total
        progress_bar.progress(pct, text=f"[{current}/{total}] {sid} — {fecha}")

    with st.spinner(f"Consultando {len(station_ids)} EMAs para {len(fechas)} día(s)..."):
        dfs, metrics = cached_scrape(
            tuple(station_ids), tuple(fechas)
        )

    progress_bar.progress(1.0, text=f"✅ Consulta completada ({metrics.ok} exitosas, {metrics.sin_datos} sin datos)")

    # ─── Consolidación de datos crudos (sin filtrar por umbrales ni QC) ──
    df_raw = consolidate_raw_data(dfs, catalog)

    # Guardar estado crudo en session_state
    st.session_state["df_raw"] = df_raw
    st.session_state["raw_metrics"] = metrics
    st.session_state["stations_queried"] = stations_to_query
    st.session_state["station_ids_queried"] = station_ids
    st.session_state["fetched_meta"] = {
        "fecha_inicio": fecha_inicio,
        "fecha_fin": fecha_fin,
        "zonas_consultadas": set(zonas_seleccionadas),
    }


# ═══════════════════════════════════════════════════════════════════════
# MOSTRAR RESULTADOS (REACTIVO E INMEDIATO)
# ═══════════════════════════════════════════════════════════════════════

if "df_raw" in st.session_state and st.session_state["df_raw"] is not None:
    df_raw = st.session_state["df_raw"]
    raw_metrics = st.session_state.get("raw_metrics")
    stations_queried = st.session_state.get("stations_queried", catalog)
    station_ids_queried = st.session_state.get("station_ids_queried", [])
    meta = st.session_state["fetched_meta"]

    # ─── Aviso cuando se requiere re-consultar Wunderground ─────────────
    needs_requery = False
    requery_reasons = []

    if fecha_inicio != meta["fecha_inicio"] or fecha_fin != meta["fecha_fin"]:
        needs_requery = True
        requery_reasons.append("el rango de fechas")

    nuevas_zonas = set(zonas_seleccionadas) - meta["zonas_consultadas"]
    if nuevas_zonas:
        needs_requery = True
        requery_reasons.append(f"nuevas Zonas DC ({', '.join(sorted(nuevas_zonas))})")

    if needs_requery:
        st.info(
            f"ℹ️ Has modificado {', '.join(requery_reasons)}. "
            "Presioná **🚀 Ejecutar verificación** para descargar los datos actualizados desde Weather Underground."
        )

    # ─── Filtrado reactivo en memoria sobre copia de df_raw ─────────────
    df_active = df_raw.copy()

    # 1. Filtro Universo de estaciones
    if universo_emas == "Solo EMAs base verificadas (lista histórica)":
        if not df_active.empty and "Origen" in df_active.columns:
            df_active = df_active[df_active["Origen"] == "base_verificada"].copy()

    # 2. Filtro Control de Calidad (QC == 0)
    if ocultar_qc:
        if not df_active.empty and "QC" in df_active.columns:
            df_active = df_active[df_active["QC"] != 0].copy()

    # 3. Filtro por Zonas DC actualmente seleccionadas
    if not df_active.empty and "Zona DC" in df_active.columns:
        df_active = df_active[df_active["Zona DC"].isin(zonas_seleccionadas)].copy()

    # 4. Umbrales reactivos y tablas derivadas
    n_dias_periodo = (meta["fecha_fin"] - meta["fecha_inicio"]).days + 1
    pp_diaria = verificacion_precipitacion_diaria(df_active, umbral_pp)
    pp_acumulada = verificacion_precipitacion_acumulada(
        df_active, umbral_pp, total_dias=n_dias_periodo
    )
    viento_detalle = verificacion_viento(df_active, umbral_viento)
    viento_resumen = resumen_rafaga_maxima(df_active, umbral_viento)
    resumen_zonas = resumen_por_zona(pp_diaria, viento_resumen)
    resumen_zonas_view = resumen_zonas[resumen_zonas["Zona DC"].isin(zonas_seleccionadas)].reset_index(drop=True)

    # Estaciones activas esperadas para auditoría y KPIs (reflejan reactivamente el universo y filtros activos)
    active_stations = [
        s for s in stations_queried
        if s["zona_dc"] in zonas_seleccionadas
        and (universo_emas == "Todas las EMAs de PBA" or s.get("origen") == "base_verificada")
        and (not ocultar_qc or s.get("qc_status") != 0)
    ]
    active_station_ids = [s["station_id"] for s in active_stations]
    audit_df = get_audit_info(active_station_ids, df_active, catalog)

    fi_str = meta["fecha_inicio"].strftime("%Y-%m-%d")
    ff_str = meta["fecha_fin"].strftime("%Y-%m-%d")

    # ─── KPIs ─────────────────────────────────────────────────────────
    st.markdown("---")
    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    total_consultadas = len(active_station_ids)
    total_con_datos = df_active["Estacion"].nunique() if not df_active.empty else 0

    kpi1.metric("EMAs consultadas", total_consultadas)
    kpi2.metric("EMAs con datos", total_con_datos)
    kpi3.metric("🌧️ Alertas precipitación",
                len(pp_diaria) if not pp_diaria.empty else 0)
    kpi4.metric("💨 Alertas ráfagas",
                len(viento_resumen) if not viento_resumen.empty else 0)

    zonas_con_alerta = set()
    if not pp_diaria.empty:
        zonas_con_alerta.update(pp_diaria["Zona DC"].unique())
    if not viento_detalle.empty:
        zonas_con_alerta.update(viento_detalle["Zona DC"].unique())
    kpi5.metric("🗺️ Zonas afectadas", len(zonas_con_alerta))

    # ─── Botones de descarga ──────────────────────────────────────────
    st.markdown("---")
    dl_col1, dl_col2, dl_col3 = st.columns(3)

    with dl_col1:
        xlsx_pp = export_precipitacion(
            pp_diaria, pp_acumulada, resumen_zonas_view, fi_str, ff_str, por_zona_dc=False
        )
        st.download_button(
            "📥 Descargar Precipitación (.xlsx)",
            data=xlsx_pp,
            file_name=f"Acumulados_PBA_{fi_str}_{ff_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

    with dl_col2:
        xlsx_viento = export_viento(
            viento_detalle, viento_resumen, resumen_zonas_view, fi_str, ff_str
        )
        st.download_button(
            "📥 Descargar Viento (.xlsx)",
            data=xlsx_viento,
            file_name=f"Viento_PBA_{fi_str}_{ff_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

    with dl_col3:
        xlsx_pp_zona = export_precipitacion(
            pp_diaria, pp_acumulada, resumen_zonas_view, fi_str, ff_str, por_zona_dc=True
        )
        st.download_button(
            "📥 Precipitación por Zona DC (.xlsx)",
            data=xlsx_pp_zona,
            file_name=f"Acumulados_por_Zona_PBA_{fi_str}_{ff_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    st.caption("ℹ️ Los archivos .xlsx reflejan los filtros activos del panel izquierdo (fechas, zonas, universo de estaciones, QC y umbrales).")

    # ─── Pestañas ─────────────────────────────────────────────────────
    tab_zona, tab_pp, tab_viento, tab_mapa, tab_audit = st.tabs([
        "🗺️ Por Zona DC", "🌧️ Precipitación", "💨 Viento y Ráfagas",
        "🗺️ Mapa PBA", "🔍 Auditoría",
    ])

    # ── TAB: Por Zona DC ──────────────────────────────────────────────
    with tab_zona:
        st.subheader("Vista por Zona de Defensa Civil")

        st.markdown("#### Resumen comparativo de máximos por Zona DC")
        st.dataframe(resumen_zonas_view, use_container_width=True, hide_index=True)

        st.markdown("---")

        zonas_ordenadas = sorted(
            zonas_seleccionadas,
            key=lambda z: (
                0 if z in zonas_con_alerta else 1,
                ZONA_ORDER.index(z),
            ),
        )

        for zona in zonas_ordenadas:
            has_alert = zona in zonas_con_alerta
            icon = "🔴" if has_alert else "🟢"
            with st.expander(f"{icon} {zona}", expanded=has_alert):
                col_pp, col_v = st.columns(2)

                with col_pp:
                    st.markdown("**Precipitación**")
                    zona_pp = pp_diaria[pp_diaria["Zona DC"] == zona] if not pp_diaria.empty else pd.DataFrame()
                    if not zona_pp.empty:
                        cols_show_p = [c for c in ["Municipio", "Estacion", "Origen", "QC", "Fecha", "Precipitacion (mm)"] if c in zona_pp.columns]
                        st.dataframe(
                            zona_pp[cols_show_p],
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info(f"No se registraron precipitaciones ≥ {umbral_pp:g} mm")

                with col_v:
                    st.markdown("**Ráfagas**")
                    zona_v = viento_resumen[viento_resumen["Zona DC"] == zona] if not viento_resumen.empty else pd.DataFrame()
                    if not zona_v.empty:
                        cols_show_v = [c for c in ["Municipio", "Estacion", "Origen", "QC", "Fecha", "Rafaga_max (km/h)"] if c in zona_v.columns]
                        st.dataframe(
                            zona_v[cols_show_v],
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.info(f"No se registraron ráfagas ≥ {umbral_viento:g} km/h")

    # ── TAB: Precipitación ────────────────────────────────────────────
    with tab_pp:
        st.subheader("🌧️ Precipitación")

        vista_pp = st.radio(
            "Vista", ["Por fecha", "Acumulado total"],
            horizontal=True, key="vista_pp",
        )

        col_pp_z, col_pp_m = st.columns(2)
        with col_pp_z:
            filtro_zona_pp = st.multiselect(
                "Filtrar por Zona DC",
                options=ZONA_ORDER,
                default=[],
                key="filtro_zona_pp",
                placeholder="Todas las zonas",
            )

        if vista_pp == "Por fecha":
            df_show = pp_diaria.copy()
        else:
            df_show = pp_acumulada.copy()

        if filtro_zona_pp and not df_show.empty:
            df_show = df_show[df_show["Zona DC"].isin(filtro_zona_pp)]

        with col_pp_m:
            mun_opts = sorted([str(m) for m in df_show["Municipio"].unique() if pd.notna(m)]) if not df_show.empty and "Municipio" in df_show.columns else []
            filtro_mun_pp = st.multiselect(
                "Filtrar por Municipio",
                options=mun_opts,
                default=[],
                key="filtro_mun_pp",
                placeholder="Todos los municipios",
            )

        if filtro_mun_pp and not df_show.empty:
            df_show = df_show[df_show["Municipio"].isin(filtro_mun_pp)]

        if not df_show.empty:
            column_config = {}
            if "Link" in df_show.columns:
                column_config["Link"] = st.column_config.LinkColumn(
                    "Link", display_text="Ver gráfico"
                )
            st.dataframe(
                df_show,
                use_container_width=True,
                hide_index=True,
                column_config=column_config,
            )
        else:
            st.info(f"No se registraron precipitaciones ≥ {umbral_pp:g} mm.")

    # ── TAB: Viento ───────────────────────────────────────────────────
    with tab_viento:
        st.subheader("💨 Viento y Ráfagas")

        vista_viento = st.radio(
            "Vista",
            ["Máximo diario por estación", "Todos los registros sobre umbral"],
            horizontal=True, key="vista_viento",
        )

        col_v_z, col_v_m = st.columns(2)
        with col_v_z:
            filtro_zona_v = st.multiselect(
                "Filtrar por Zona DC",
                options=ZONA_ORDER,
                default=[],
                key="filtro_zona_v",
                placeholder="Todas las zonas",
            )

        if vista_viento == "Máximo diario por estación":
            df_show_v = viento_resumen.copy()
        else:
            df_show_v = viento_detalle.copy()

        if filtro_zona_v and not df_show_v.empty:
            df_show_v = df_show_v[df_show_v["Zona DC"].isin(filtro_zona_v)]

        with col_v_m:
            mun_opts_v = sorted([str(m) for m in df_show_v["Municipio"].unique() if pd.notna(m)]) if not df_show_v.empty and "Municipio" in df_show_v.columns else []
            filtro_mun_v = st.multiselect(
                "Filtrar por Municipio",
                options=mun_opts_v,
                default=[],
                key="filtro_mun_v",
                placeholder="Todos los municipios",
            )

        if filtro_mun_v and not df_show_v.empty:
            df_show_v = df_show_v[df_show_v["Municipio"].isin(filtro_mun_v)]

        if not df_show_v.empty:
            column_config_v = {}
            if "Link" in df_show_v.columns:
                column_config_v["Link"] = st.column_config.LinkColumn(
                    "Link", display_text="Ver gráfico"
                )
            st.dataframe(
                df_show_v,
                use_container_width=True,
                hide_index=True,
                column_config=column_config_v,
            )
        else:
            st.info(f"No se registraron ráfagas ≥ {umbral_viento:g} km/h.")

    # ── TAB: Mapa ─────────────────────────────────────────────────────
    with tab_mapa:
        st.subheader("🗺️ Mapa de PBA — Zonas de Defensa Civil")

        # Leyenda horizontal responsive de tipos de alerta y umbrales configurados
        st.markdown(
            f"""
            <div style="display: flex; flex-wrap: wrap; gap: 16px; align-items: center; margin-bottom: 12px; font-size: 13.5px;">
              <span style="display: inline-flex; align-items: center; gap: 6px;">
                <span style="display: inline-block; width: 12px; height: 12px; border-radius: 50%; background-color: #2563eb; border: 1.5px solid #ffffff; box-shadow: 0 0 0 1px #94a3b8;"></span>
                <span><b>Precipitación</b> (&ge; {umbral_pp:g} mm)</span>
              </span>
              <span style="display: inline-flex; align-items: center; gap: 6px;">
                <span style="display: inline-block; width: 12px; height: 12px; border-radius: 50%; background-color: #ea580c; border: 1.5px solid #ffffff; box-shadow: 0 0 0 1px #94a3b8;"></span>
                <span><b>Viento / Ráfagas</b> (&ge; {umbral_viento:g} km/h)</span>
              </span>
              <span style="display: inline-flex; align-items: center; gap: 6px;">
                <span style="display: inline-block; width: 12px; height: 12px; border-radius: 50%; background-color: #9333ea; border: 1.5px solid #ffffff; box-shadow: 0 0 0 1px #94a3b8;"></span>
                <span><b>Precipitación y Viento</b> (ambos umbrales)</span>
              </span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        try:
            import folium
            from streamlit_folium import st_folium
            from shapely.geometry import shape

            # Mapa base OpenStreetMap (libre, sin API key)
            m = folium.Map(
                location=[-36.5, -59.5],
                zoom_start=7,
                tiles="OpenStreetMap",
                name="OpenStreetMap",
            )

            # Capa alternativa gratuita: Esri Gris Claro
            folium.TileLayer(
                tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                attr="Esri",
                name="Esri Gris Claro",
                overlay=False,
                control=True,
            ).add_to(m)

            try:
                geojson_data = load_zonas_simple(DATA_DIR)

                ZONA_COLORS = {
                    "Zona I": "#e6194b", "Zona II": "#3cb44b",
                    "Zona III": "#ffe119", "Zona IV": "#4363d8",
                    "Zona V": "#f58231", "Zona VI": "#911eb4",
                    "Zona VII": "#42d4f4", "Zona VIII": "#f032e6",
                    "Zona IX": "#bfef45", "Zona X": "#fabed4",
                    "Zona XI": "#469990", "Zona XII": "#dcbeff",
                }

                def style_function(feature):
                    zona = feature["properties"].get("zona_dc", "")
                    return {
                        "fillColor": ZONA_COLORS.get(zona, "#808080"),
                        "color": "#333333",
                        "weight": 1.5,
                        "fillOpacity": 0.3,
                    }

                folium.GeoJson(
                    geojson_data,
                    name="Polígonos Zonas DC",
                    style_function=style_function,
                    tooltip=folium.GeoJsonTooltip(
                        fields=["zona_dc"],
                        aliases=["Zona DC:"],
                    ),
                ).add_to(m)

                # Etiquetas con números romanos (I a XII) centradas en cada polígono
                fg_labels = folium.FeatureGroup(name="Etiquetas Zonas DC", show=True)
                for feature in geojson_data.get("features", []):
                    props = feature.get("properties", {})
                    zona_name = props.get("zona_dc", "")
                    roman = zona_name.replace("Zona ", "").strip()
                    geom = shape(feature["geometry"])
                    rep_pt = geom.representative_point()

                    label_html = (
                        f'<div style="'
                        f'font-size: 13px; font-weight: bold; color: #1f2937; '
                        f'text-shadow: -1px -1px 0 #fff, 1px -1px 0 #fff, -1px 1px 0 #fff, 1px 1px 0 #fff; '
                        f'pointer-events: none; text-align: center; white-space: nowrap;'
                        f'">{roman}</div>'
                    )
                    folium.Marker(
                        location=[rep_pt.y, rep_pt.x],
                        icon=folium.DivIcon(
                            html=label_html,
                            icon_size=(30, 20),
                            icon_anchor=(15, 10),
                        ),
                    ).add_to(fg_labels)
                fg_labels.add_to(m)

            except FileNotFoundError:
                st.warning("No se encontró el GeoJSON simplificado para el mapa.")

            # Marcadores compactos para las EMAs con alertas (CircleMarker)
            if not pp_diaria.empty or not viento_detalle.empty:
                catalog_map = {
                    s["station_id"]: s for s in
                    (cached_load_catalog() if catalog else [])
                }

                emas_pp = set(pp_diaria["Estacion"].unique()) if not pp_diaria.empty else set()
                emas_viento = set(viento_detalle["Estacion"].unique()) if not viento_detalle.empty else set()
                emas_alertas = emas_pp | emas_viento

                fg_emas = folium.FeatureGroup(name="EMAs con alerta", show=True)

                for ema_id in emas_alertas:
                    if ema_id not in catalog_map:
                        continue
                    info = catalog_map[ema_id]

                    has_pp = ema_id in emas_pp
                    has_v = ema_id in emas_viento

                    if has_pp and has_v:
                        fill_color = "#9333ea"
                        tipo = "🌧️💨 Precipitación y Viento"
                    elif has_pp:
                        fill_color = "#2563eb"
                        tipo = "🌧️ Precipitación"
                    else:
                        fill_color = "#ea580c"
                        tipo = "💨 Ráfagas"

                    qc_val = info.get("qc_status", 1)
                    qc_desc = "Aprobado" if qc_val == 1 else ("Fallo QC" if qc_val == 0 else "Sin verificar")
                    popup_parts = [
                        f"<b>{ema_id}</b><br>",
                        f"{info.get('nombre', '')}<br>",
                        f"<b>Municipio:</b> {info.get('municipio', 'N/D')}<br>",
                        f"<b>Zona DC:</b> {info['zona_dc']}<br>",
                        f"Origen: {info.get('origen', 'descubierta')}<br>",
                        f"QC Catálogo: {qc_val} ({qc_desc})<br>",
                        f"Fenómeno: {tipo}<br>",
                    ]

                    if has_pp:
                        max_pp = pp_diaria[pp_diaria["Estacion"] == ema_id]["Precipitacion (mm)"].max()
                        popup_parts.append(f"Max precip: {max_pp:.1f} mm<br>")

                    if has_v:
                        max_v_df = viento_resumen[viento_resumen["Estacion"] == ema_id]
                        if not max_v_df.empty:
                            max_v = max_v_df["Rafaga_max (km/h)"].max()
                            popup_parts.append(f"Max ráfaga: {max_v:.1f} km/h<br>")

                    if info.get("ajuste_costero"):
                        popup_parts.append("Ajuste costero: Sí (tolerancia marina)<br>")

                    link = f"https://www.wunderground.com/dashboard/pws/{ema_id}"
                    popup_parts.append(f'<a href="{link}" target="_blank">Ver en WU</a>')

                    mun_label = f" - {info['municipio']}" if info.get("municipio") else ""
                    folium.CircleMarker(
                        location=[info["lat"], info["lon"]],
                        radius=7,
                        color="#ffffff",
                        weight=1.5,
                        fill=True,
                        fill_color=fill_color,
                        fill_opacity=0.9,
                        popup=folium.Popup("".join(popup_parts), max_width=250),
                        tooltip=f"{ema_id} ({info['zona_dc']}{mun_label})",
                    ).add_to(fg_emas)

                fg_emas.add_to(m)

            # Control de capas
            folium.LayerControl(position="topright", collapsed=True).add_to(m)

            # Renderizado sin re-runs innecesarios al hacer zoom o pan
            st_folium(m, width=None, height=600, use_container_width=True, returned_objects=[])

        except ImportError:
            st.warning("Instalá folium y streamlit-folium para visualizar el mapa.")

    # ── TAB: Auditoría ────────────────────────────────────────────────
    with tab_audit:
        st.subheader("🔍 Auditoría de corrida")

        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("Consultas exitosas", total_con_datos)
        col_m2.metric("Sin datos para la fecha", len(audit_df))
        col_m3.metric("Errores HTTP/parseo", raw_metrics.errores if raw_metrics else 0)

        if not audit_df.empty:
            st.markdown("#### Estaciones sin datos")
            st.dataframe(audit_df, use_container_width=True, hide_index=True)
        else:
            st.success("Todas las estaciones del universo seleccionado devolvieron datos.")

        if raw_metrics and raw_metrics.detalle_errores:
            with st.expander("Detalle de errores"):
                for err in raw_metrics.detalle_errores:
                    st.text(err)

else:
    st.info(
        "👈 Configurá los parámetros en la barra lateral y presioná "
        "**Ejecutar verificación** para comenzar."
    )

    try:
        catalog = cached_load_catalog()
        tot_b = sum(1 for s in catalog if s.get("origen") == "base_verificada")
        tot_d = sum(1 for s in catalog if s.get("origen") == "descubierta")
        st.markdown(
            f"📋 **Catálogo cargado:** {len(catalog)} estaciones en PBA "
            f"({tot_b} base verificadas, {tot_d} descubiertas)"
        )
    except (FileNotFoundError, Exception):
        st.warning(
            "⚠️ No se encontró el catálogo de estaciones. "
            "Ejecutá `python scripts/build_catalog.py` para generarlo."
        )
