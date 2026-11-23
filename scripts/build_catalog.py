#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script CLI para regenerar los GeoJSON enriquecidos y descubrir exhaustivamente
el catálogo de todas las EMAs de PBA mediante grilla adaptativa (Quadtree),
con enriquecimiento de Partidos/Municipios vía Georef AR.

Uso:
    python scripts/build_catalog.py [--skip-stations] [--only-municipios]
"""

import json
import sys
import pathlib
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.geo import build_enriched_geojson, ZONA_ORDER
from src.stations import build_catalog, catalog_summary, BASE_STATIONS, enrich_with_municipios


def report_municipios(catalog: list[dict]):
    """Imprime reporte detallado del enriquecimiento con Municipio."""
    assigned = [s for s in catalog if s.get("municipio") and s.get("municipio") != "Sin asignar"]
    print("\n" + "-" * 75)
    print(f"🏛️ ENRIQUECIMIENTO CON PARTIDOS / MUNICIPIOS DE PBA ({len(assigned)} / {len(catalog)}):")
    print("-" * 75)
    print(f" • Estaciones con municipio oficial asignado: {len(assigned)} / {len(catalog)}")

    # Muestra de 10 estaciones incluyendo explícitamente las críticas
    targets = ["IPUERT440", "IGENERAL21", "IITUZAIN9", "IHUDSO15", "ILOBER7"]
    sample_targets = [s for s in catalog if s["station_id"] in targets]
    other_samples = [s for s in catalog if s["station_id"] not in targets][: 10 - len(sample_targets)]
    sample_all = sample_targets + other_samples

    sample_df = pd.DataFrame(sample_all)
    cols = [c for c in ["station_id", "zona_dc", "municipio", "nombre", "lat", "lon"] if c in sample_df.columns]
    print("\n📋 Muestra de verificación de Municipios (incluye estaciones clave):")
    print(sample_df[cols].to_string(index=False))


def main():
    data_dir = ROOT / "data"
    source_geojson = ROOT / "Zonas_DCs_windy.geojson"

    # Flag de enriquecimiento exclusivo
    if "--only-municipios" in sys.argv:
        print("=" * 75)
        print("🏛️ MODO --only-municipios: Enriqueciendo catálogo existente con Georef AR...")
        print("=" * 75)
        cat_file = data_dir / "estaciones_pba.json"
        if not cat_file.exists():
            print(f"❌ No se encontró {cat_file}. Ejecutá el script sin banderas primero.")
            sys.exit(1)

        with open(cat_file, "r", encoding="utf-8") as f:
            catalog = json.load(f)

        print(f"📡 Consultando Georef AR para {len(catalog)} estaciones...")
        catalog = enrich_with_municipios(catalog)

        with open(cat_file, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)

        report_municipios(catalog)
        print("\n" + "=" * 75)
        print("✅ Enriquecimiento de municipios completado y guardado.")
        print("=" * 75)
        return

    skip_stations = "--skip-stations" in sys.argv

    # 1. Generar GeoJSON enriquecido
    print("=" * 75)
    print("PASO 1: Generando GeoJSON enriquecido con Zonas DC...")
    print("=" * 75)
    build_enriched_geojson(source_geojson, data_dir)
    print()

    # 2. Descubrimiento exhaustivo de EMAs
    if not skip_stations:
        print("=" * 75)
        print("PASO 2: Descubrimiento exhaustivo de EMAs en PBA (Grilla Adaptativa)...")
        print("=" * 75)

        def progress(queries, discovered, queue_size):
            print(
                f"\r   Consultas realizadas: {queries:<4} | "
                f"EMAs descubiertas: {discovered:<4} | "
                f"Celdas en cola: {queue_size:<4}",
                end="",
                flush=True,
            )

        catalog, discarded_outside, stats = build_catalog(
            data_dir=data_dir,
            station_ids=BASE_STATIONS,
            progress_callback=progress,
        )

        print("\n\n" + "=" * 75)
        print("RESUMEN DE DESCUBRIMIENTO EXHAUSTIVO:")
        print("=" * 75)
        print(f" • Celdas iniciales (0.40° x 0.40°):    {stats.get('initial_cells', 0)}")
        print(f" • Celdas de alta densidad subdivididas: {stats.get('subdivided_cells', 0)}")
        print(f" • Total de consultas de grilla:         {stats.get('total_queries', 0)}")
        print(f" • Total estaciones brutas encontradas:  {stats.get('total_discovered_raw', 0)}")
        print(f" • Total estaciones válidas dentro PBA:  {stats.get('total_pba_catalog', len(catalog))}")
        print(f" • Total descartadas fuera de PBA:       {stats.get('total_discarded_outside', len(discarded_outside))}")

        # A. Tabla final de EMAs por Zona DC desglosada
        print("\n" + "-" * 75)
        print("📍 Catálogo Final de EMAs en PBA por Zona DC (I a XII):")
        print("-" * 75)
        summary_df = catalog_summary(catalog)
        print(summary_df.to_string(index=False))

        # Totales
        tot_base = summary_df["Base Verificada"].sum() if "Base Verificada" in summary_df.columns else 0
        tot_desc = summary_df["Descubierta"].sum() if "Descubierta" in summary_df.columns else 0
        tot_cost = summary_df["Ajuste Costero"].sum() if "Ajuste Costero" in summary_df.columns else 0
        tot_all = summary_df["Total EMAs"].sum() if "Total EMAs" in summary_df.columns else len(catalog)
        print(f"\n👉 TOTAL GENERAL: {tot_all} EMAs (Base Verificadas: {tot_base} | Nuevas Descubiertas: {tot_desc} | Ajuste Costero: {tot_cost})")

        # B. Estaciones costeras recuperadas gracias a la tolerancia de 0.015° (~1.5 km)
        costeras_recuperadas = [s for s in catalog if s.get("ajuste_costero")]
        print("\n" + "-" * 75)
        print(f"🌊 Estaciones costeras recuperadas con buffer de 0.015° ({len(costeras_recuperadas)}):")
        print("-" * 75)
        if costeras_recuperadas:
            rec_df = pd.DataFrame(costeras_recuperadas)
            cols = [c for c in ["station_id", "zona_dc", "lat", "lon", "nombre"] if c in rec_df.columns]
            print(rec_df[cols].to_string(index=False))
        else:
            print("   Ninguna estación entró por ajuste costero.")

        # Verificación explícita de estaciones solicitadas
        cat_sids = {s["station_id"]: s for s in catalog}
        puert_ok = "IPUERT440" in cat_sids and cat_sids["IPUERT440"]["zona_dc"] == "Zona VI"
        gen_ok = "IGENERAL21" in cat_sids and cat_sids["IGENERAL21"]["zona_dc"] == "Zona VII"
        print("\n   Verificación de estaciones críticas de costa:")
        print(f"   • IPUERT440 en Zona VI:   {'✅ SÍ' if puert_ok else '❌ NO'} ({cat_sids.get('IPUERT440', {}).get('zona_dc')})")
        print(f"   • IGENERAL21 en Zona VII: {'✅ SÍ' if gen_ok else '❌ NO'} ({cat_sids.get('IGENERAL21', {}).get('zona_dc')})")

        # C. Estaciones descartadas por caer fuera de PBA (ej. CABA, Viedma, Santa Fe)
        print("\n" + "-" * 75)
        print(f"🚫 Estaciones descartadas por caer FUERA de PBA ({len(discarded_outside)}):")
        print("-" * 75)
        disc_sids = {s["station_id"] for s in discarded_outside}
        caba_check = [s for s in ["ICOMUNA93", "IBUENOSA110", "IBUENOSA517", "ICOMUN37"] if s in disc_sids]
        viedma_check = [s for s in ["IVIEDM6", "IVIEDM8", "IVIEDM1"] if s in disc_sids]
        print(f"   • Confirmación CABA descartadas:   {', '.join(caba_check) if caba_check else 'No encontradas en pool'}")
        print(f"   • Confirmación Viedma descartadas: {', '.join(viedma_check) if viedma_check else 'No encontradas en pool'}")

        if discarded_outside:
            disc_df = pd.DataFrame(discarded_outside)
            cols = [c for c in ["station_id", "lat", "lon", "nombre"] if c in disc_df.columns]
            print(disc_df[cols].head(25).to_string(index=False))
            if len(disc_df) > 25:
                print(f"   ... y {len(disc_df) - 25} estaciones más fuera de PBA.")
        else:
            print("   Ninguna estación cayó fuera de PBA.")

        # D. Reporte de Municipios
        report_municipios(catalog)

    else:
        print("⏭️ Saltando generación de catálogo de estaciones (--skip-stations)")

    print("\n" + "=" * 75)
    print("✅ Proceso completado exitosamente.")
    print("=" * 75)


if __name__ == "__main__":
    main()
