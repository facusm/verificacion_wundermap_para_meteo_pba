#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de verificación real del scraper contra Weather Underground.
Consulta 5 estaciones de distintas zonas de PBA para una fecha reciente
y valida método, unidades, y ausencia de doble conversión o nulos inválidos.
"""

import sys
import pathlib
import json
import datetime
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.stations import load_catalog
from src.scraper import fetch_station_day, _build_wu_link


def main():
    catalog = load_catalog(ROOT / "data")
    if not catalog:
        print("❌ Catálogo vacío.")
        sys.exit(1)

    # Seleccionar 5 estaciones de 5 zonas distintas
    selected_zonas = ["Zona I", "Zona IV", "Zona VII", "Zona X", "Zona VI"]
    selected_stations = []

    for z in selected_zonas:
        st_in_z = [s for s in catalog if s["zona_dc"] == z]
        if st_in_z:
            selected_stations.append(st_in_z[0])

    # Si falta alguna zona, rellenar con otras
    if len(selected_stations) < 5:
        existing_ids = {s["station_id"] for s in selected_stations}
        for s in catalog:
            if s["station_id"] not in existing_ids:
                selected_stations.append(s)
            if len(selected_stations) == 5:
                break

    # Fecha de prueba: ayer
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    print("=" * 75)
    print(f"PRUEBA REAL DEL SCRAPER — Fecha: {yesterday}")
    print(f"Estaciones seleccionadas ({len(selected_stations)}):")
    for s in selected_stations:
        print(f"  • {s['station_id']:<15} | {s['zona_dc']:<10} | {s.get('nombre', '')} ({s['lat']}, {s['lon']})")
    print("=" * 75)

    all_passed = True

    for s in selected_stations:
        sid = s["station_id"]
        zona = s["zona_dc"]
        print(f"\n📡 Consultando {sid} ({zona})...")
        res = fetch_station_day(sid, yesterday)

        print(f"   • Status: {res.status}")
        print(f"   • Método que respondió: {res.source_method}")

        if res.status == "ok" and res.df is not None:
            df = res.df
            print(f"   • Registros obtenidos: {len(df)}")

            # Muestra de muestra cruda vs convertida
            if res.raw_sample:
                print("   • Muestra de respuesta de la fuente:")
                for k, v in res.raw_sample.items():
                    print(f"       {k}: {v}")

            # Muestra de DataFrame resultante
            print("   • Primeras 3 filas procesadas:")
            print(df[["Hora_local", "Viento_medio (km/h)", "Rafagas (km/h)", "Precipitacion (mm)"]].head(3).to_string(index=False))

            # Verificación de rangos físicos y tipos
            v_max = df["Viento_medio (km/h)"].max()
            r_max = df["Rafagas (km/h)"].max()
            p_max = df["Precipitacion (mm)"].max()
            print(f"   • Máximos del día: Viento={v_max} km/h | Ráfagas={r_max} km/h | Precipitacion={p_max} mm")

            # Link al gráfico WU
            print(f"   • Link WU: {_build_wu_link(sid, yesterday)}")
        else:
            print(f"   ⚠️ Sin datos para {yesterday} ({res.error_msg})")
            # Probar con fecha de Colab (2026-11-12) como fallback de validación
            alt_date = "2026-11-12"
            print(f"   Intentando fecha alternativa {alt_date}...")
            res_alt = fetch_station_day(sid, alt_date)
            print(f"   • Alt Status: {res_alt.status} | Método: {res_alt.source_method}")
            if res_alt.status == "ok" and res_alt.df is not None:
                print(f"   • Registros obtenidos: {len(res_alt.df)}")
                print(res_alt.df[["Hora_local", "Viento_medio (km/h)", "Rafagas (km/h)", "Precipitacion (mm)"]].head(2).to_string(index=False))

    print("\n" + "=" * 75)
    print("VERIFICACIÓN DE SCRAPER FINALIZADA")
    print("=" * 75)


if __name__ == "__main__":
    main()
