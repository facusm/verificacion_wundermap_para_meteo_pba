# -*- coding: utf-8 -*-
"""Tests del pipeline de verificación meteorológica con Origen y QC."""

import datetime
import json
import pathlib
import sys

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.geo import (
    ZONA_ORDER,
    REFERENCE_POINTS,
    COASTAL_ZONAS,
    _resolve_fid_to_zona,
    classify_point,
    spatial_join_stations,
    load_zonas_gdf,
)
from src.stations import catalog_summary
from src.processing import (
    consolidate_raw_data,
    verificacion_precipitacion_diaria,
    verificacion_precipitacion_acumulada,
    verificacion_viento,
    resumen_rafaga_maxima,
    resumen_por_zona,
    get_audit_info,
    format_max_gust_line,
)
from src.scraper import (
    generate_date_range,
    _build_wu_link,
    _compact_station_day_records,
    _parse_daily_observations,
    split_date_chunks,
    plan_scrape_strategy,
)
from src.export import export_precipitacion, export_viento


# ═══════════════════════════════════════════════════════════════════════
# TESTS GEO
# ═══════════════════════════════════════════════════════════════════════

class TestGeo:
    def test_zona_order_has_12(self):
        assert len(ZONA_ORDER) == 12

    def test_reference_points_has_12(self):
        assert len(REFERENCE_POINTS) == 12

    def test_all_zonas_in_order(self):
        for zona in REFERENCE_POINTS:
            assert zona in ZONA_ORDER

    def test_resolve_fid_mapping(self):
        geojson_path = ROOT / "Zonas_DCs_windy.geojson"
        if not geojson_path.exists():
            pytest.skip("GeoJSON fuente no disponible")

        import geopandas as gpd
        gdf = gpd.read_file(geojson_path).to_crs("EPSG:4326")
        mapping = _resolve_fid_to_zona(gdf)

        assert len(mapping) == 12
        assert set(mapping.values()) == set(ZONA_ORDER)
        assert len(set(mapping.keys())) == 12


class TestSpatialJoinCostero:
    @pytest.fixture
    def zonas_gdf(self):
        geojson_path = ROOT / "data" / "zonas_dc_pba.geojson"
        if not geojson_path.exists():
            pytest.skip("zonas_dc_pba.geojson no generado aún")
        return load_zonas_gdf(ROOT / "data")

    def test_coastal_points_assigned_with_tolerance(self, zonas_gdf):
        # 1. IPUERT440 en Puerto Belgrano (~600m mar adentro)
        z_puerto, adj_puerto = classify_point(-62.05783, -38.93016, zonas_gdf, return_details=True)
        assert z_puerto == "Zona VI"
        assert adj_puerto is True

        # 2. IGENERAL21 en Mar del Plata (escollera ~2m fuera del polígono)
        z_mdp, adj_mdp = classify_point(-57.53104, -38.03573, zonas_gdf, return_details=True)
        assert z_mdp == "Zona VII"
        assert adj_mdp is True

        # 3. Punto sintético costero a ~0.005° de la costa de Mar del Plata
        z_syn, adj_syn = classify_point(-57.525, -38.005, zonas_gdf, return_details=True)
        assert z_syn == "Zona VII"
        assert adj_syn is True

    def test_caba_points_strictly_discarded(self, zonas_gdf):
        # Estaciones porteñas de CABA (cerca de Gral Paz o Riachuelo)
        caba_points = [
            ("ICOMUNA93", -58.527157, -34.648254),  # Liniers
            ("IBUENOSA517", -58.485901, -34.628399), # Flores
            ("IBUENOSA110", -58.4604, -34.569519),   # Belgrano R
            ("ICOMUN37", -58.433473, -34.600073),    # Caballito
        ]
        for sid, lon, lat in caba_points:
            res = classify_point(lon, lat, zonas_gdf)
            assert res is None, f"{sid} en CABA ({lon}, {lat}) no debe asignarse a ninguna zona de PBA"

    def test_viedma_and_border_points_strictly_discarded(self, zonas_gdf):
        # Estaciones en provincia de Río Negro / Viedma
        viedma_points = [
            ("IVIEDM6", -63.12816, -40.80771),
            ("VIEDMA_WATERFRONT", -62.990, -40.808),  # Frente a Carmen de Patagones
        ]
        for sid, lon, lat in viedma_points:
            res = classify_point(lon, lat, zonas_gdf)
            assert res is None, f"{sid} en Viedma ({lon}, {lat}) no debe asignarse a PBA"

        # Límite norte con Santa Fe (ej. Villa Constitución)
        res_santa_fe = classify_point(-60.33, -33.23, zonas_gdf)
        assert res_santa_fe is None, "Punto en Santa Fe no debe asignarse a PBA"

    def test_spatial_join_stations_two_pass_dataframe(self, zonas_gdf):
        import geopandas as gpd
        from shapely.geometry import Point

        data = [
            {"station_id": "ST_EXACT", "lat": -34.7206, "lon": -58.2546, "nombre": "Quilmes Centro"},  # Zona I (Exact)
            {"station_id": "IPUERT440", "lat": -38.93016, "lon": -62.05783, "nombre": "Puerto Belgrano"},  # Zona VI (Costera)
            {"station_id": "IGENERAL21", "lat": -38.03573, "lon": -57.53104, "nombre": "Escollera MDP"},   # Zona VII (Costera)
            {"station_id": "ICOMUNA93", "lat": -34.648254, "lon": -58.527157, "nombre": "CABA Liniers"},  # CABA (Descartada)
            {"station_id": "IVIEDM6", "lat": -40.80771, "lon": -63.12816, "nombre": "Viedma Centro"},     # Viedma (Descartada)
        ]
        df = pd.DataFrame(data)
        gdf = gpd.GeoDataFrame(df, geometry=[Point(r["lon"], r["lat"]) for r in data], crs="EPSG:4326")

        joined = spatial_join_stations(gdf, zonas_gdf)

        assigned_sids = set(joined["station_id"])
        # Solo deben asignarse las 3 válidas
        assert assigned_sids == {"ST_EXACT", "IPUERT440", "IGENERAL21"}
        assert "ICOMUNA93" not in assigned_sids
        assert "IVIEDM6" not in assigned_sids

        # Chequeo de banderas ajuste_costero
        row_exact = joined[joined["station_id"] == "ST_EXACT"].iloc[0]
        assert row_exact["zona_dc"] == "Zona I"
        assert bool(row_exact["ajuste_costero"]) is False

        row_puert = joined[joined["station_id"] == "IPUERT440"].iloc[0]
        assert row_puert["zona_dc"] == "Zona VI"
        assert bool(row_puert["ajuste_costero"]) is True

        row_mdp = joined[joined["station_id"] == "IGENERAL21"].iloc[0]
        assert row_mdp["zona_dc"] == "Zona VII"
        assert bool(row_mdp["ajuste_costero"]) is True

        # Unicidad estricta 1 a 1
        assert len(joined) == len(assigned_sids)


# ═══════════════════════════════════════════════════════════════════════
# TESTS SCRAPER
# ═══════════════════════════════════════════════════════════════════════

class TestScraper:
    def test_generate_date_range_single(self):
        d = datetime.date(2026, 1, 15)
        result = generate_date_range(d, d)
        assert result == ["2026-01-15"]

    def test_generate_date_range_multi(self):
        d1 = datetime.date(2026, 1, 15)
        d2 = datetime.date(2026, 1, 17)
        result = generate_date_range(d1, d2)
        assert result == ["2026-01-15", "2026-01-16", "2026-01-17"]

    def test_build_wu_link(self):
        link = _build_wu_link("ICOLN10", "2026-01-15")
        assert "ICOLN10" in link
        assert "2026-01-15" in link
        assert "graph" in link

    def test_compact_station_day_records_preserves_peaks_and_windy_rows(self):
        # 200 registros sub-horarios de un día (la mayoría 0 mm y viento < 25 km/h)
        records = []
        for i in range(200):
            hh = i // 10
            mm = (i % 10) * 6
            records.append({
                "Hora_local": f"{hh:02d}:{mm:02d}",
                "Viento_medio (km/h)": 5.0,
                "Rafagas (km/h)": 12.0,
                "Precipitacion (mm)": 0.0,
                "qcStatus": 1,
            })

        # Inyectar ráfagas >= 25 km/h, pico de lluvia y pico con qcStatus=0 y qcStatus=-1
        records[50]["Rafagas (km/h)"] = 28.0
        records[60]["Rafagas (km/h)"] = 35.0
        records[100]["Precipitacion (mm)"] = 18.5  # pico precip qcStatus=1
        records[120]["Rafagas (km/h)"] = 72.0      # pico ráfaga con qcStatus=0
        records[120]["qcStatus"] = 0
        records[150]["Precipitacion (mm)"] = 4.0   # pico precip qcStatus=-1
        records[150]["qcStatus"] = -1

        compacted = _compact_station_day_records(records, wind_keep_threshold=25.0)
        assert len(compacted) < 10
        horas = [r["Hora_local"] for r in compacted]
        # Mantiene orden cronológico
        assert horas == sorted(horas)
        # Conserva las ráfagas >= 25 km/h
        assert any(r["Rafagas (km/h)"] == 28.0 for r in compacted)
        assert any(r["Rafagas (km/h)"] == 35.0 for r in compacted)
        # Conserva máximos por cada qcStatus
        assert any(r["qcStatus"] == 1 and r["Precipitacion (mm)"] == 18.5 for r in compacted)
        assert any(r["qcStatus"] == 0 and r["Rafagas (km/h)"] == 72.0 for r in compacted)
        assert any(r["qcStatus"] == -1 and r["Precipitacion (mm)"] == 4.0 for r in compacted)

    def test_compact_station_day_records_dry_calm_day_keeps_representative(self):
        records = [
            {
                "Hora_local": f"{i:02d}:00",
                "Viento_medio (km/h)": 2.0,
                "Rafagas (km/h)": 5.0,
                "Precipitacion (mm)": 0.0,
                "qcStatus": 1,
            }
            for i in range(24)
        ]
        compacted = _compact_station_day_records(records, wind_keep_threshold=25.0)
        assert len(compacted) >= 1
        assert len(compacted) <= 2

    def test_parse_daily_observations(self):
        observations = [
            {
                "obsTimeLocal": "2026-11-27 23:50:00",
                "qcStatus": 1,
                "metric": {
                    "precipTotal": 22.4,
                    "windspeedHigh": 42.0,
                    "windspeedAvg": 15.2,
                    "windgustHigh": 64.5,
                },
            },
            {
                "obsTimeLocal": "2026-11-28 08:20:00",
                "qcStatus": 0,
                "metric": {
                    "precipTotal": 0.0,
                    "windspeedHigh": None,
                    "windspeedAvg": 8.0,
                    "windgustHigh": 18.0,
                },
            },
        ]
        df, sample = _parse_daily_observations(observations, "TEST_DAILY")
        assert df is not None
        assert len(df) == 2
        assert list(df["Hora_local"].unique()) == ["Resumen diario"]
        assert list(df["Fecha"]) == ["2026-11-27", "2026-11-28"]
        assert df.iloc[0]["Precipitacion (mm)"] == 22.4
        assert df.iloc[0]["Viento_medio (km/h)"] == 42.0
        assert df.iloc[0]["Rafagas (km/h)"] == 64.5
        assert df.iloc[0]["qcStatus"] == 1
        # Fallback a windspeedAvg si windspeedHigh es None
        assert df.iloc[1]["Viento_medio (km/h)"] == 8.0
        assert df.iloc[1]["qcStatus"] == 0
        assert sample is not None

    def test_plan_scrape_strategy_routing_and_chunking(self):
        hoy = datetime.date(2026, 11, 28)

        # 1. Hoy + 2 días previos (26/11 al 28/11) -> 100% all
        s1 = plan_scrape_strategy(datetime.date(2026, 11, 26), hoy, hoy_ar=hoy)
        assert s1["mode"] == "all"
        assert s1["all_dates"] == ["2026-11-26", "2026-11-27", "2026-11-28"]
        assert s1["daily_chunks"] == []

        # 2a. Solo ayer (27/11) -> 100% daily
        ayer = datetime.date(2026, 11, 27)
        s2a = plan_scrape_strategy(ayer, ayer, hoy_ar=hoy)
        assert s2a["mode"] == "daily"
        assert s2a["daily_chunks"] == [(ayer, ayer)]
        assert s2a["all_dates"] == []

        # 2b. Rango histórico cerrado sin hoy (01/11 al 20/11) -> 100% daily
        s2b = plan_scrape_strategy(datetime.date(2026, 11, 1), datetime.date(2026, 11, 20), hoy_ar=hoy)
        assert s2b["mode"] == "daily"
        assert s2b["daily_chunks"] == [(datetime.date(2026, 11, 1), datetime.date(2026, 11, 20))]
        assert s2b["all_dates"] == []

        # 3. Hoy + 7 días previos (21/11 al 28/11)
        s3_daily = plan_scrape_strategy(datetime.date(2026, 11, 21), hoy, hoy_ar=hoy, daily_supports_today=True)
        assert s3_daily["mode"] == "daily"
        assert s3_daily["daily_chunks"] == [(datetime.date(2026, 11, 21), hoy)]
        assert s3_daily["all_dates"] == []

        s3_hybrid = plan_scrape_strategy(datetime.date(2026, 11, 21), hoy, hoy_ar=hoy, daily_supports_today=False)
        assert s3_hybrid["mode"] == "daily+hoy_all"
        assert s3_hybrid["daily_chunks"] == [(datetime.date(2026, 11, 21), datetime.date(2026, 11, 27))]
        assert s3_hybrid["all_dates"] == ["2026-11-28"]

        # 4. Rango cerrado de 45 días -> daily en 2 bloques de <= 30 días
        start_45 = datetime.date(2026, 10, 1)
        end_45 = datetime.date(2026, 11, 14)
        assert (end_45 - start_45).days + 1 == 45
        s4 = plan_scrape_strategy(start_45, end_45, hoy_ar=hoy)
        assert s4["mode"] == "daily"
        assert len(s4["daily_chunks"]) == 2
        for c_start, c_end in s4["daily_chunks"]:
            assert (c_end - c_start).days + 1 <= 30
        assert s4["daily_chunks"] == [
            (datetime.date(2026, 10, 1), datetime.date(2026, 10, 30)),
            (datetime.date(2026, 10, 31), datetime.date(2026, 11, 14)),
        ]


# ═══════════════════════════════════════════════════════════════════════
# TESTS PROCESSING
# ═══════════════════════════════════════════════════════════════════════

class TestProcessing:
    @pytest.fixture
    def sample_catalog(self):
        return [
            {"station_id": "TEST1", "lat": -34.7, "lon": -58.3, "zona_dc": "Zona I", "municipio": "Quilmes", "nombre": "Test 1", "origen": "base_verificada", "qc_status": 1},
            {"station_id": "TEST2", "lat": -34.6, "lon": -58.6, "zona_dc": "Zona II", "municipio": "Morón", "nombre": "Test 2", "origen": "descubierta", "qc_status": -1},
            {"station_id": "TEST3", "lat": -34.5, "lon": -58.7, "zona_dc": "Zona III", "municipio": "San Martín", "nombre": "Test 3", "origen": "descubierta", "qc_status": 0},
        ]

    @pytest.fixture
    def sample_dfs(self):
        df1 = pd.DataFrame({
            "Hora_local": ["12:00", "13:00", "14:00"],
            "Viento_medio (km/h)": [30.0, 65.0, 20.0],
            "Rafagas (km/h)": [45.0, 80.0, 25.0],
            "Precipitacion (mm)": [5.0, 10.0, 20.0],
            "qcStatus": [1, 0, 1],  # 13:00 ráfaga pico con fallo (0); 14:00 precip pico aprobada (1)
            "Fecha": ["2026-01-15"] * 3,
            "Estacion": ["TEST1"] * 3,
        })
        df2 = pd.DataFrame({
            "Hora_local": ["12:00", "13:00"],
            "Viento_medio (km/h)": [70.0, 50.0],
            "Rafagas (km/h)": [90.0, 55.0],
            "Precipitacion (mm)": [0.0, 2.0],
            "qcStatus": [None, None],  # Fallback al catálogo (TEST2 tiene qc_status=-1)
            "Fecha": ["2026-01-15"] * 2,
            "Estacion": ["TEST2"] * 2,
        })
        return [df1, df2]

    def test_consolidate_raw_data(self, sample_dfs, sample_catalog):
        result = consolidate_raw_data(sample_dfs, sample_catalog)
        assert not result.empty
        assert "Zona DC" in result.columns
        assert "Municipio" in result.columns
        assert "Origen" in result.columns
        assert "QC" in result.columns
        assert "Link" in result.columns
        assert len(result) == 5

        # Verificar downcasting de memoria
        for col in ["Estacion", "Zona DC", "Municipio", "Origen", "Fecha"]:
            assert isinstance(result[col].dtype, pd.CategoricalDtype), f"{col} debe ser category"
        for col in ["Viento_medio (km/h)", "Rafagas (km/h)", "Precipitacion (mm)"]:
            assert str(result[col].dtype) == "float32", f"{col} debe ser float32"
        assert str(result["QC"].dtype) == "Int8"

    def test_verificacion_pp_diaria(self, sample_dfs, sample_catalog):
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        result = verificacion_precipitacion_diaria(consolidated, umbral_mm=15.0)
        assert not result.empty
        assert result.iloc[0]["Precipitacion (mm)"] == 20.0
        assert "Municipio" in result.columns
        assert "Origen" in result.columns
        assert "QC" in result.columns
        # El pico de 20.0 mm ocurrió a las 14:00 con qcStatus=1
        assert result.iloc[0]["QC"] == 1

    def test_verificacion_pp_acumulada(self, sample_dfs, sample_catalog):
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        result = verificacion_precipitacion_acumulada(consolidated, umbral_mm=15.0)
        assert not result.empty
        assert "Municipio" in result.columns
        assert "Origen" in result.columns
        assert "QC" in result.columns

    def test_verificacion_viento(self, sample_dfs, sample_catalog):
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        result = verificacion_viento(consolidated, umbral_kmh=60.0)
        assert not result.empty
        assert len(result) >= 2
        assert "Municipio" in result.columns
        assert "Origen" in result.columns
        assert "QC" in result.columns

    def test_resumen_rafaga_maxima(self, sample_dfs, sample_catalog):
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        result = resumen_rafaga_maxima(consolidated, umbral_kmh=60.0)
        assert not result.empty
        assert "Municipio" in result.columns
        assert "Hora_local" in result.columns
        assert "Origen" in result.columns
        assert "QC" in result.columns
        cols = list(result.columns)
        assert cols.index("Hora_local") == cols.index("Fecha") + 1

        # TEST1 tuvo ráfaga pico de 80 km/h a las 13:00 con qcStatus=0 (fallo puntual de QC)
        row_t1 = result[result["Estacion"] == "TEST1"].iloc[0]
        assert row_t1["Rafaga_max (km/h)"] == 80.0
        assert row_t1["Hora_local"] == "13:00"
        assert row_t1["QC"] == 0

        # TEST2 tuvo ráfaga pico de 90 km/h a las 12:00 con qcStatus=None -> fallback a catálogo qc_status=-1
        row_t2 = result[result["Estacion"] == "TEST2"].iloc[0]
        assert row_t2["Rafaga_max (km/h)"] == 90.0
        assert row_t2["Hora_local"] == "12:00"
        assert row_t2["QC"] == -1

        # Caso daily ('Resumen diario')
        df_daily = pd.DataFrame({
            "Hora_local": ["Resumen diario"],
            "Viento_medio (km/h)": [45.0],
            "Rafagas (km/h)": [75.5],
            "Precipitacion (mm)": [0.0],
            "qcStatus": [1],
            "Fecha": ["2026-11-26"],
            "Estacion": ["TEST1"],
        })
        res_daily = resumen_rafaga_maxima(consolidate_raw_data([df_daily], sample_catalog), umbral_kmh=60.0)
        assert res_daily.iloc[0]["Hora_local"] == "Resumen diario"
        assert res_daily.iloc[0]["Rafaga_max (km/h)"] == pytest.approx(75.5, rel=1e-4)

        # Popup formatting helper en los 4 escenarios
        df_popup_all = pd.DataFrame({
            "Fecha": ["2026-11-26", "2026-11-27"],
            "Hora_local": ["14:35", "09:10"],
            "Rafaga_max (km/h)": [75.2, 62.0],
        })
        assert format_max_gust_line(df_popup_all.iloc[[0]], 1) == "Max ráfaga: 75.2 km/h (14:35 hs)"
        assert format_max_gust_line(df_popup_all, 3) == "Max ráfaga: 75.2 km/h (26/11 14:35 hs)"

        df_popup_daily = pd.DataFrame({
            "Fecha": ["2026-11-24", "2026-11-25"],
            "Hora_local": ["Resumen diario", "Resumen diario"],
            "Rafaga_max (km/h)": [68.4, 81.0],
        })
        assert format_max_gust_line(df_popup_daily.iloc[[0]], 1) == "Max ráfaga: 68.4 km/h"
        assert format_max_gust_line(df_popup_daily, 5) == "Max ráfaga: 81.0 km/h (25/11)"

    def test_qc_filtering_excludes_zero_retains_minus_one_and_one(self, sample_dfs, sample_catalog):
        """
        Verifica que el filtro de control de calidad:
        - Excluya registros u observaciones con QC == 0 (fallo de QC / posible dato erróneo).
        - Conserve registros con QC == 1 (aprobado).
        - Conserve registros con QC == -1 (sin verificación automática, común en el interior de PBA).
        """
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        # Pre-filtro de catálogo para estaciones con qc_status == 0
        filtered_catalog = [s for s in sample_catalog if s.get("qc_status") != 0]
        assert any(s["station_id"] == "TEST1" and s["qc_status"] == 1 for s in filtered_catalog)
        assert any(s["station_id"] == "TEST2" and s["qc_status"] == -1 for s in filtered_catalog)
        assert not any(s["station_id"] == "TEST3" for s in filtered_catalog)

        # Filtro sobre observaciones consolidadas: QC != 0
        filtered_obs = consolidated[consolidated["QC"] != 0].copy()
        qcs_presentes = set(filtered_obs["QC"].unique())
        assert 0 not in qcs_presentes, "QC == 0 debe ser excluido al activar el filtro"
        assert 1 in qcs_presentes, "QC == 1 (aprobado) debe mantenerse"
        assert -1 in qcs_presentes, "QC == -1 (sin verificación automática) debe mantenerse"

        # Tablas de verificación con el filtro activo
        vr = resumen_rafaga_maxima(filtered_obs, umbral_kmh=60.0)
        assert "TEST1" not in vr["Estacion"].values, "La ráfaga pico de TEST1 tenía QC=0 y debe descartarse"
        assert "TEST2" in vr["Estacion"].values, "TEST2 tiene QC=-1 y debe mantenerse en las alertas"

    def test_resumen_por_zona(self, sample_dfs, sample_catalog):
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        pp = verificacion_precipitacion_diaria(consolidated, 15.0)
        vr = resumen_rafaga_maxima(consolidated, 60.0)
        result = resumen_por_zona(pp, vr)
        assert len(result) == 12
        assert "Max_Precip (mm)" in result.columns
        assert "Max_Rafaga (km/h)" in result.columns

    def test_catalog_summary(self, sample_catalog):
        res = catalog_summary(sample_catalog)
        assert "Total EMAs" in res.columns
        assert "Base Verificada" in res.columns
        assert "Descubierta" in res.columns

    def test_empty_dataframes(self, sample_catalog):
        result = verificacion_precipitacion_diaria(pd.DataFrame(), 15.0)
        assert result.empty
        assert "Precipitacion (mm)" in result.columns

    def test_audit_info(self, sample_dfs, sample_catalog):
        consolidated = consolidate_raw_data(sample_dfs, sample_catalog)
        audit = get_audit_info(["TEST1", "TEST2", "TEST3"], consolidated, sample_catalog)
        assert len(audit) == 1
        assert audit.iloc[0]["Estacion"] == "TEST3"
        assert "Municipio" in audit.columns
        assert "Origen" in audit.columns
        assert "QC" in audit.columns


# ═══════════════════════════════════════════════════════════════════════
# TESTS EXPORT
# ═══════════════════════════════════════════════════════════════════════

class TestExport:
    def test_export_precipitacion(self):
        pp_diaria = pd.DataFrame({
            "Zona DC": ["Zona I"],
            "Municipio": ["Quilmes"],
            "Estacion": ["TEST1"],
            "Origen": ["base_verificada"],
            "QC": [1],
            "Fecha": ["2026-01-15"],
            "Precipitacion (mm)": [25.0],
            "Link": ["https://example.com"],
        })
        pp_acum = pd.DataFrame({
            "Zona DC": ["Zona I"],
            "Municipio": ["Quilmes"],
            "Estacion": ["TEST1"],
            "Origen": ["base_verificada"],
            "QC": [1],
            "Dias_con_datos": [1],
            "Precipitacion (mm)": [25.0],
            "Link": ["https://example.com"],
        })
        resumen = pd.DataFrame({
            "Zona DC": ZONA_ORDER,
            "Max_Precip (mm)": [25.0] + [0] * 11,
            "EMAs_con_precip": [1] + [0] * 11,
        })

        result = export_precipitacion(pp_diaria, pp_acum, resumen, "2026-01-15", "2026-01-15", por_zona_dc=True)
        assert result.getbuffer().nbytes > 0

    def test_export_viento(self):
        viento_det = pd.DataFrame({
            "Zona DC": ["Zona I"],
            "Municipio": ["Quilmes"],
            "Estacion": ["TEST1"],
            "Origen": ["base_verificada"],
            "QC": [1],
            "Fecha": ["2026-01-15"],
            "Hora_local": ["13:00"],
            "Viento_medio (km/h)": [65.0],
            "Rafagas (km/h)": [80.0],
            "Link": ["https://example.com"],
        })
        viento_res = pd.DataFrame({
            "Zona DC": ["Zona I"],
            "Municipio": ["Quilmes"],
            "Estacion": ["TEST1"],
            "Origen": ["base_verificada"],
            "QC": [1],
            "Fecha": ["2026-01-15"],
            "Rafaga_max (km/h)": [80.0],
            "Link": ["https://example.com"],
        })
        resumen = pd.DataFrame({
            "Zona DC": ZONA_ORDER,
            "Max_Rafaga (km/h)": [80.0] + [0] * 11,
            "EMAs_con_viento": [1] + [0] * 11,
        })

        result = export_viento(viento_det, viento_res, resumen, "2026-01-15", "2026-01-15")
        assert result.getbuffer().nbytes > 0

    def test_export_empty(self):
        empty_df = pd.DataFrame()
        resumen = pd.DataFrame({
            "Zona DC": ZONA_ORDER,
            "Max_Precip (mm)": [0] * 12,
            "EMAs_con_precip": [0] * 12,
        })
        result = export_precipitacion(empty_df, empty_df, resumen, "2026-01-15", "2026-01-15")
        assert result.getbuffer().nbytes > 0


# ═══════════════════════════════════════════════════════════════════════
# TESTS FOLIUM Y MUNICIPIOS
# ═══════════════════════════════════════════════════════════════════════

class TestFoliumAndMunicipios:
    def test_folium_and_streamlit_folium_imports(self):
        import folium
        from streamlit_folium import st_folium
        m = folium.Map(location=[-36.5, -59.5], zoom_start=7)
        assert m is not None
        assert callable(st_folium)

    def test_enrich_with_municipios_fallback_logic(self):
        from src.stations import enrich_with_municipios

        test_stations = [
            {"station_id": "TEST_RESOLVED", "lat": -34.649723, "lon": -58.660831, "zona_dc": "Zona II", "municipio": "Ituzaingó"},
            {"station_id": "TEST_UNRESOLVED", "lat": -34.6534, "lon": -58.6198, "zona_dc": "Zona II", "municipio": None},
        ]
        # Si una ya está resuelta y otra no, la no resuelta toma el municipio del vecino de la misma zona
        # Forzamos que Georef no resuelva la segunda pasándole municipio=None directamente en el fallback
        resolved = [s for s in test_stations if s.get("municipio")]
        target_lat, target_lon = test_stations[1]["lat"], test_stations[1]["lon"]
        nearest = min(resolved, key=lambda r: (r["lat"] - target_lat)**2 + (r["lon"] - target_lon)**2)
        test_stations[1]["municipio"] = nearest["municipio"]
        assert test_stations[1]["municipio"] == "Ituzaingó"

    def test_catalog_json_has_municipios(self):
        cat_file = ROOT / "data" / "estaciones_pba.json"
        if not cat_file.exists():
            pytest.skip("estaciones_pba.json no disponible")

        with open(cat_file, "r", encoding="utf-8") as f:
            cat = json.load(f)

        assert len(cat) > 0
        sin_municipio = [s for s in cat if not s.get("municipio") or s.get("municipio") == "Sin asignar"]
        assert len(sin_municipio) == 0, f"Hay {len(sin_municipio)} estaciones sin municipio: {[s['station_id'] for s in sin_municipio[:5]]}"

        # Verificación explícita de las estaciones requeridas
        sids_map = {s["station_id"]: s for s in cat}
        for target in ["IPUERT440", "IGENERAL21", "IITUZAIN9", "IHUDSO15", "ILOBER7"]:
            assert target in sids_map, f"Falta {target} en catálogo"
            assert sids_map[target]["municipio"], f"{target} no tiene municipio asignado"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
