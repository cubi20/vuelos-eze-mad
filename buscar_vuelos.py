#!/usr/bin/env python3
"""
buscar_vuelos.py
Monitor de tarifas aereas para una ventana acotada de fechas, con alerta a Telegram.

Por defecto: Ezeiza (EZE) -> Madrid (MAD), salidas del 23 al 27 de agosto,
viajes de 4 noches. Fechas objetivo: 25 al 29.

Fuentes, en orden:
  1) SerpApi / Google Flights (en vivo). Fuente principal.
     Plan gratuito: 250 busquedas por mes, se resetea el dia 1.
  2) Travelpayouts (cache de hasta 7 dias). Solo si SerpApi falla o se agota.

Mensajes que manda:
  - ALERTA: cuando una fecha marca un minimo nuevo por debajo del umbral.
    No repite si el precio rebota y vuelve al mismo valor.
  - RESUMEN: una vez por dia, las combinaciones ordenadas por precio, con la
    variacion respecto al minimo previo y la tendencia de la ultima semana.
  - AVISO DE FALLA: si SerpApi deja de responder y quedamos solo con cache.

Codigos de salida:
  0 = todo bien (o degradado a cache, pero con datos)
  1 = ninguna fuente respondio -> GitHub Actions marca la corrida como fallida
      y manda un mail automatico.

Uso:
    pip install requests python-dotenv
    cp .env.example .env      # y completar las credenciales
    python buscar_vuelos.py
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------
SERP_API_KEY = os.getenv("SERP_API_KEY", "")
TP_TOKEN = os.getenv("TP_TOKEN", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

ORIGEN = os.getenv("ORIGEN", "EZE")
DESTINO = os.getenv("DESTINO", "MAD")

SALIDA_DESDE = os.getenv("SALIDA_DESDE", "2026-08-23")
SALIDA_HASTA = os.getenv("SALIDA_HASTA", "2026-08-27")
NOCHES = int(os.getenv("NOCHES", "4"))
FECHA_OBJETIVO = os.getenv("FECHA_OBJETIVO", "2026-08-25")

UMBRAL = float(os.getenv("UMBRAL", "1000"))
MONEDA = os.getenv("MONEDA", "usd")
RESUMEN_DIARIO = os.getenv("RESUMEN_DIARIO", "true").lower() == "true"

SERP_URL = "https://serpapi.com/search"
TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
ESTADO = BASE_DIR / "estado.json"
HISTORIAL = BASE_DIR / "historial.csv"
PAUSA = 1.0

MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "vuelos.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vuelos")


def dia_mes(iso: str) -> str:
    """'2026-08-25' -> '25 ago'"""
    d = date.fromisoformat(iso)
    return f"{d.day} {MESES[d.month - 1]}"


# --------------------------------------------------------------------------
# Estado persistente
# --------------------------------------------------------------------------
def cargar_estado() -> dict:
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("estado.json corrupto, arranco de cero")
    return {}


def guardar_estado(estado: dict) -> None:
    estado["ultima_corrida"] = datetime.now().isoformat(timespec="seconds")
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Historial en CSV
# --------------------------------------------------------------------------
COLUMNAS = ["timestamp", "ida", "vuelta", "precio", "moneda", "fuente"]


def anotar_historial(resultados: list[dict]) -> None:
    nuevo = not HISTORIAL.exists()
    ahora = datetime.now().isoformat(timespec="seconds")
    try:
        with HISTORIAL.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(COLUMNAS)
            for o in resultados:
                w.writerow([
                    ahora, o["ida"], o["vuelta"],
                    f"{o['precio']:.0f}", MONEDA.upper(), o["fuente"],
                ])
    except OSError as e:
        log.error("No pude escribir el historial: %s", e)


def minimo_por_dia() -> dict[str, float]:
    """{fecha_de_observacion: precio mas bajo visto ese dia en toda la ventana}"""
    if not HISTORIAL.exists():
        return {}
    minimos: dict[str, float] = {}
    try:
        with HISTORIAL.open(newline="", encoding="utf-8") as f:
            for fila in csv.DictReader(f):
                try:
                    dia = fila["timestamp"][:10]
                    precio = float(fila["precio"])
                except (KeyError, ValueError, TypeError):
                    continue
                if dia not in minimos or precio < minimos[dia]:
                    minimos[dia] = precio
    except OSError as e:
        log.error("No pude leer el historial: %s", e)
    return minimos


def tendencia(actual: float) -> str:
    """Compara contra la observacion mas cercana a 7 dias atras."""
    minimos = minimo_por_dia()
    if not minimos:
        return ""

    objetivo = date.today() - timedelta(days=7)
    candidatos = [d for d in minimos if date.fromisoformat(d) <= objetivo]
    if not candidatos:
        candidatos = [d for d in minimos if d != date.today().isoformat()]
    if not candidatos:
        return ""

    ref_dia = min(candidatos, key=lambda d: abs((date.fromisoformat(d) - objetivo).days))
    ref = minimos[ref_dia]
    dias = (date.today() - date.fromisoformat(ref_dia)).days
    delta = actual - ref

    if abs(delta) < 1:
        return f"\n📈 Sin cambios respecto a hace {dias} dia(s)."
    signo = "bajo" if delta < 0 else "subio"
    return (
        f"\n📈 En {dias} dia(s) {signo} <b>{abs(delta):.0f} {MONEDA.upper()}</b> "
        f"(estaba en {ref:.0f})."
    )


# --------------------------------------------------------------------------
# Combinaciones a monitorear
# --------------------------------------------------------------------------
def pares_a_monitorear() -> list[tuple[str, str]]:
    desde = date.fromisoformat(SALIDA_DESDE)
    hasta = date.fromisoformat(SALIDA_HASTA)
    manana = date.today() + timedelta(days=1)

    pares = []
    dia = max(desde, manana)
    while dia <= hasta:
        pares.append((dia.isoformat(), (dia + timedelta(days=NOCHES)).isoformat()))
        dia += timedelta(days=1)
    return pares


# --------------------------------------------------------------------------
# Fuente 1: SerpApi / Google Flights (en vivo)
# --------------------------------------------------------------------------
def precio_serpapi(ida: str, vuelta: str) -> dict | None:
    if not SERP_API_KEY:
        return None

    params = {
        "engine": "google_flights",
        "departure_id": ORIGEN,
        "arrival_id": DESTINO,
        "outbound_date": ida,
        "return_date": vuelta,
        "type": "1",
        "currency": MONEDA.upper(),
        "hl": "es",
        "gl": "ar",
        "api_key": SERP_API_KEY,
    }
    try:
        r = requests.get(SERP_URL, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        log.error("SerpApi fallo %s->%s: %s", ida, vuelta, e)
        return None

    if data.get("error"):
        log.error("SerpApi: %s", data["error"])
        return None

    vuelos = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    vuelos = [v for v in vuelos if v.get("price") is not None]
    if not vuelos:
        log.warning("SerpApi sin resultados para %s->%s", ida, vuelta)
        return None

    mejor = min(vuelos, key=lambda v: v["price"])
    tramos = mejor.get("flights") or []
    aerolinea = tramos[0].get("airline", "??") if tramos else "??"
    escalas = len(mejor.get("layovers") or [])
    minutos = mejor.get("total_duration") or 0

    return {
        "ida": ida,
        "vuelta": vuelta,
        "precio": float(mejor["price"]),
        "aerolinea": aerolinea,
        "escalas": escalas,
        "duracion": f"{minutos // 60}h {minutos % 60:02d}m" if minutos else "",
        "link": (data.get("search_metadata") or {}).get("google_flights_url", ""),
        "fuente": "vivo",
    }


# --------------------------------------------------------------------------
# Fuente 2: Travelpayouts (cache) - respaldo
# --------------------------------------------------------------------------
def precio_travelpayouts(ida: str, vuelta: str) -> dict | None:
    if not TP_TOKEN:
        return None

    params = {
        "origin": ORIGEN,
        "destination": DESTINO,
        "departure_at": ida,
        "return_at": vuelta,
        "currency": MONEDA,
        "sorting": "price",
        "unique": "false",
        "limit": 5,
        "one_way": "false",
    }
    try:
        r = requests.get(
            TP_URL, params=params, headers={"X-Access-Token": TP_TOKEN}, timeout=25
        )
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        log.error("Travelpayouts fallo %s->%s: %s", ida, vuelta, e)
        return None

    datos = [d for d in (payload.get("data") or []) if d.get("price") is not None]
    if not datos:
        return None

    mejor = min(datos, key=lambda d: d["price"])
    return {
        "ida": ida,
        "vuelta": vuelta,
        "precio": float(mejor["price"]),
        "aerolinea": mejor.get("airline", "??"),
        "escalas": mejor.get("transfers", 0),
        "duracion": "",
        "link": "https://www.aviasales.com" + (mejor.get("link") or ""),
        "fuente": "cache",
    }


def consultar(ida: str, vuelta: str) -> dict | None:
    r = precio_serpapi(ida, vuelta)
    if r:
        return r
    log.info("Cayendo al cache para %s->%s", ida, vuelta)
    return precio_travelpayouts(ida, vuelta)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def telegram(texto: str) -> bool:
    if not (TG_TOKEN and TG_CHAT_ID):
        log.error("Falta TG_TOKEN o TG_CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("No pude enviar a Telegram: %s", e)
        return False


def bloque_alerta(o: dict, previo: float | None) -> str:
    escalas = "directo" if o["escalas"] == 0 else f"{o['escalas']} escala(s)"
    sello = "✅ en vivo" if o["fuente"] == "vivo" else "⚠️ cache, sin confirmar"
    detalle = " · ".join(x for x in [o["aerolinea"], escalas, o["duracion"]] if x)
    baja = f"  <i>(antes {previo:.0f})</i>" if previo else ""
    return (
        f"✈️ <b>{ORIGEN} → {DESTINO}</b>\n"
        f"💵 <b>{o['precio']:.0f} {MONEDA.upper()}</b>{baja}  {sello}\n"
        f"📅 {dia_mes(o['ida'])} → {dia_mes(o['vuelta'])}\n"
        f"🛫 {detalle}\n"
        f"🔗 {o['link']}"
    )


def bloque_resumen(resultados: list[dict], minimos: dict) -> str:
    lineas = [
        f"📊 <b>{ORIGEN} → {DESTINO}</b> · {NOCHES} noches",
        "<i>Ordenado de mas barato a mas caro</i>\n",
    ]

    for i, o in enumerate(sorted(resultados, key=lambda x: x["precio"]), 1):
        marca = " 🎯" if o["ida"] == FECHA_OBJETIVO else ""
        previo = minimos.get(o["ida"])
        if previo is None:
            flecha = ""
        elif o["precio"] < previo:
            flecha = f" 🔻{previo - o['precio']:.0f}"
        elif o["precio"] > previo:
            flecha = f" 🔺{o['precio'] - previo:.0f}"
        else:
            flecha = " ="
        lineas.append(
            f"{i}. {dia_mes(o['ida'])} → {dia_mes(o['vuelta'])} · "
            f"<b>{o['precio']:.0f}</b>{marca}{flecha}"
        )

    barato = min(resultados, key=lambda x: x["precio"])
    objetivo = next((o for o in resultados if o["ida"] == FECHA_OBJETIVO), None)

    if objetivo and objetivo["ida"] != barato["ida"]:
        ahorro = objetivo["precio"] - barato["precio"]
        if ahorro > 0:
            lineas.append(
                f"\n💡 Saliendo el {dia_mes(barato['ida'])} en vez del "
                f"{dia_mes(FECHA_OBJETIVO)} ahorras <b>{ahorro:.0f} {MONEDA.upper()}</b>."
            )
    elif objetivo:
        lineas.append("\n💡 Tus fechas son las mas baratas de la ventana.")

    lineas.append(tendencia(barato["precio"]))

    degradadas = sum(1 for o in resultados if o["fuente"] == "cache")
    if degradadas:
        lineas.append(f"\n⚠️ {degradadas} de {len(resultados)} vienen de cache, sin confirmar.")

    lineas.append(f"\n🎯 = tus fechas · umbral: {UMBRAL:.0f} {MONEDA.upper()}")
    return "\n".join(x for x in lineas if x)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    if not SERP_API_KEY and not TP_TOKEN:
        log.error("No hay ninguna fuente configurada")
        return 1

    estado = cargar_estado()
    minimos = estado.setdefault("minimos", {})          # minimo visto por fecha
    avisado_min = estado.setdefault("avisado_min", {})  # ultimo precio avisado por fecha
    hoy = date.today().isoformat()

    pares = pares_a_monitorear()
    if not pares:
        log.info("No quedan fechas futuras en la ventana. Nada que hacer.")
        return 0

    log.info("Consultando %d combinacion(es) de %s a %s", len(pares), SALIDA_DESDE, SALIDA_HASTA)

    resultados = []
    for ida, vuelta in pares:
        r = consultar(ida, vuelta)
        if r:
            resultados.append(r)
            log.info("%s -> %s : %.0f %s (%s)", ida, vuelta, r["precio"], MONEDA.upper(), r["fuente"])
        time.sleep(PAUSA)

    # --- Falla total: ninguna fuente respondio ---
    if not resultados:
        log.error("Ninguna fuente devolvio datos")
        if estado.get("aviso_caida") != hoy:
            if telegram(
                "❌ <b>El monitor no pudo consultar precios</b>\n\n"
                "Ni Google Flights ni el respaldo respondieron. "
                "Revisa las credenciales o la cuota de SerpApi."
            ):
                estado["aviso_caida"] = hoy
        guardar_estado(estado)
        return 1  # marca la corrida como fallida en GitHub Actions

    estado.pop("aviso_caida", None)
    anotar_historial(resultados)

    # --- Degradado: SerpApi no respondio en ninguna fecha ---
    solo_cache = all(o["fuente"] == "cache" for o in resultados)
    if solo_cache and SERP_API_KEY and estado.get("aviso_degradado") != hoy:
        if telegram(
            "⚠️ <b>Google Flights no responde</b>\n\n"
            "El monitor esta funcionando solo con precios de cache, que pueden "
            "tener hasta 7 dias. Puede ser la cuota mensual de SerpApi agotada "
            "o la key vencida."
        ):
            estado["aviso_degradado"] = hoy
    elif not solo_cache:
        estado.pop("aviso_degradado", None)

    # --- Alertas: solo minimos nuevos por debajo del umbral ---
    nuevas = []
    for o in resultados:
        if o["precio"] >= UMBRAL:
            continue
        previo = avisado_min.get(o["ida"])
        if previo is None or o["precio"] < previo:
            nuevas.append((o, previo))
            avisado_min[o["ida"]] = o["precio"]

    if nuevas:
        cabecera = f"🚨 <b>{len(nuevas)} tarifa(s) por debajo de {UMBRAL:.0f} {MONEDA.upper()}</b>\n"
        cuerpo = "\n\n".join(bloque_alerta(o, prev) for o, prev in nuevas)
        if telegram(cabecera + "\n" + cuerpo):
            log.info("Avise %d minimo(s) nuevo(s)", len(nuevas))

    # --- Resumen diario ---
    if RESUMEN_DIARIO and estado.get("ultimo_resumen") != hoy:
        if telegram(bloque_resumen(resultados, minimos)):
            estado["ultimo_resumen"] = hoy
            log.info("Resumen diario enviado")

    # --- Actualizar minimos ---
    for o in resultados:
        previo = minimos.get(o["ida"])
        if previo is None or o["precio"] < previo:
            minimos[o["ida"]] = o["precio"]

    guardar_estado(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())