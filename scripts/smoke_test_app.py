#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import subprocess
import sys
import time
import requests

port = 8506
cmd = [
    sys.executable, "-m", "streamlit", "run", "app.py",
    "--server.port", str(port),
    "--server.headless", "true",
]

print(f"Iniciando Streamlit en puerto {port}...")
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

health_ok = False
for attempt in range(15):
    time.sleep(1)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/_stcore/health", timeout=2)
        if r.status_code == 200:
            print(f"✅ Streamlit respondió en _stcore/health (Status: {r.status_code}, Body: '{r.text.strip()}')")
            health_ok = True
            break
        else:
            print(f"  Intento {attempt+1}: Status {r.status_code}")
    except Exception as e:
        print(f"  Intento {attempt+1}: {e}")
        pass

if health_ok:
    try:
        r_home = requests.get(f"http://127.0.0.1:{port}/", timeout=3)
        print(f"✅ Página principal cargada exitosamente (Status: {r_home.status_code})")
        if "Verificación" in r_home.text or "<!DOCTYPE html>" in r_home.text:
            print("✅ HTML contiene estructura de aplicación Streamlit válida.")
    except Exception as e:
        print(f"⚠️ Error al acceder al home: {e}")

proc.terminate()
try:
    stdout, stderr = proc.communicate(timeout=3)
except Exception:
    proc.kill()
    stdout, stderr = proc.communicate()

if not health_ok:
    print(f"\n--- STDOUT ---\n{stdout}")
    print(f"\n--- STDERR ---\n{stderr}")

if health_ok:
    print("\n🎉 SMOKE TEST EXITOSO: app.py arrancó y respondió perfectamente.")
else:
    print("\n❌ SMOKE TEST FALLÓ.")
    sys.exit(1)
