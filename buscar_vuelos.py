#!/usr/bin/env python3
"""
buscar_vuelos.py
Monitor diario de tarifas aereas con alerta a Telegram.

Por defecto: Ezeiza (EZE) -> Madrid (MAD), salidas en agosto, viajes de 4 dias,
avisa cuando encuentra algo por debajo del umbral configurado.

Estrategia de dos etapas:
  1) BARRIDO (Travelpayouts, gratis e ilimitado): recorre las 31 fechas del mes.
     Son precios de cache, de hasta 7 dias de antiguedad -> sirven de radar.
  2) VERIFICACION (SerpApi / Google Flights, 250 busquedas gratis al mes):
     solo para los pares de fechas que dieron por debajo del umbral, una
     consulta en vivo para confirmar que el precio existe de verdad.

Si no configuras SERP_API_KEY el script funciona igual, salteando la etapa 2.

Uso:
    pip install requests python-dotenv
    cp .env.example .env      # y completar las credenciales
    python buscar_vuelos.py

Docs:
    https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
    https://serpapi.com/google-flights-api
"""

from __future__ import annotations

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
# Configuracion (todo se puede pisar desde el .env o desde el workflow)
# --------------------------------------------------------------------------
TP_TOKEN = os.getenv("TP_TOKEN", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
SERP_API_KEY = os.getenv("SERP_API_KEY", "")

ORIGEN = os.getenv("ORIGEN", "EZE")
DESTINO = os.getenv("DESTINO", "MAD")
MES = os.getenv("MES", "2026-08")              # formato YYYY-MM
NOCHES = int(os.getenv("NOCHES", "4"))         # duracion del viaje en dias
UMBRAL = float(os.getenv("UMBRAL", "1000"))    # precio maximo que dispara la alerta
MONEDA = os.getenv("MONEDA", "usd")
SOLO_DIRECTOS = os.getenv("SOLO_DIRECTOS", "false").lower() == "true"
AVISAR_MINIMO = os.getenv("AVISAR_MINIMO", "true").lower() == "true"

# Tope de consultas en vivo por corrida. Protege la cuota gratuita de SerpApi.
SERP_MAX_VERIF = int(os.getenv("SERP_MAX_VERIF", "5"))

TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERP_URL = "https://serpapi.com/search"
ESTADO = BASE_DIR / "estado.json"
PAUSA = 0.5  # segundos entre requests, para no pegarle a los rate limits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "vuelos.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vuelos")


# --------------------------------------------------------------------------
# Estado persistente
# --------------------------------------------------------------------------
def cargar_estado() -> dict:
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("estado.json corrupto, arranco de cero")
    return {"avisados": [], "minimo_historico": None, "verificados": {}}


def guardar_estado(estado: dict) -> None:
    estado["avisados"] = estado["avisados"][-500:]
    # Solo guardamos verificaciones de hoy: manana se pueden repetir.
    hoy = date.today().isoformat()
    estado["verificados"] = {
        k: v for k, v in estado.get("verificados", {}).items() if v == hoy
    }
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Fechas a consultar
# --------------------------------------------------------------------------
def fechas_del_mes(mes: str, noches: int) -> list[tuple[date, date]]:
    anio, m = (int(x) for x in mes.split("-"))
    primero = date(anio, m, 1)
    ultimo = date(anio + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)

    pares = []
    dia = max(primero, date.today() + timedelta(days=1))  # nunca fechas pasadas
    while dia <= ultimo:
        pares.append((dia, dia + timedelta(days=noches)))
        dia += timedelta(days=1)
    return pares


# --------------------------------------------------------------------------
# Etapa 1: barrido con Travelpayouts (cache, gratis)
# --------------------------------------------------------------------------
def buscar(ida: date, vuelta: date) -> list[dict]:
    params = {
        "origin": ORIGEN,
        "destination": DESTINO,
        "departure_at": ida.isoformat(),
        "return_at": vuelta.isoformat(),
        "currency": MONEDA,
        "sorting": "price",
        "direct": "true" if SOLO_DIRECTOS else "false",
        "unique": "false",
        "limit": 10,
        "one_way": "false",
    }
    try:
        r = requests.get(
            TP_URL, params=params, headers={"X-Access-Token": TP_TOKEN}, timeout=25
        )
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as e:
        log.error("Fallo la consulta %s -> %s: %s", ida, vuelta, e)
        return []
    except json.JSONDecodeError:
        log.error("Respuesta no-JSON para %s -> %s", ida, vuelta)
        return []

    if not payload.get("success", False):
        log.warning("La API devolvio success=false para %s: %s", ida, payload)
        return []

    ofertas = []
    for item in payload.get("data", []) or []:
        precio = item.get("price")
        if precio is None:
            continue
        ofertas.append(
            {
                "precio": float(precio),
                "aerolinea": item.get("airline", "??"),
                "vuelo": item.get("flight_number", ""),
                "salida": item.get("departure_at", ida.isoformat())[:16],
                "regreso": item.get("return_at", vuelta.isoformat())[:16],
                "escalas": item.get("transfers", 0),
                "link": "https://www.aviasales.com" + (item.get("link") or ""),
                "en_vivo": None,  # se completa en la etapa 2
            }
        )
    return ofertas


def clave(o: dict) -> str:
    """Identidad de una oferta, para no repetir avisos."""
    return f"{o['salida'][:10]}|{o['regreso'][:10]}|{o['aerolinea']}|{int(o['precio'])}"


# --------------------------------------------------------------------------
# Etapa 2: verificacion en vivo con SerpApi (Google Flights)
# --------------------------------------------------------------------------
def precio_en_vivo(ida: str, vuelta: str) -> float | None:
    """Precio mas bajo real para ese par de fechas. None si no se pudo consultar."""
    params = {
        "engine": "google_flights",
        "departure_id": ORIGEN,
        "arrival_id": DESTINO,
        "outbound_date": ida,
        "return_date": vuelta,
        "type": "1",              # 1 = ida y vuelta
        "currency": MONEDA.upper(),
        "hl": "es",
        "gl": "ar",
        "api_key": SERP_API_KEY,
    }
    try:
        r = requests.get(SERP_URL, params=params, timeout=45)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        log.error("SerpApi fallo para %s -> %s: %s", ida, vuelta, e)
        return None

    if "error" in data:
        log.error("SerpApi devolvio error: %s", data["error"])
        return None

    precios = [
        f["price"]
        for f in (data.get("best_flights", []) or []) + (data.get("other_flights", []) or [])
        if f.get("price") is not None
    ]
    insight = (data.get("price_insights") or {}).get("lowest_price")
    if insight is not None:
        precios.append(insight)

    return float(min(precios)) if precios else None


def verificar(candidatas: list[dict], estado: dict) -> list[dict]:
    """Confirma con precios en vivo. Una consulta por par de fechas, no por oferta."""
    hoy = date.today().isoformat()
    ya_vistos = estado.setdefault("verificados", {})

    pares: dict[tuple[str, str], list[dict]] = {}
    for o in candidatas:
        pares.setdefault((o["salida"][:10], o["regreso"][:10]), []).append(o)

    confirmadas: list[dict] = []
    gastadas = 0

    for par, ofertas in sorted(pares.items()):
        etiqueta = f"{par[0]}|{par[1]}"

        if ya_vistos.get(etiqueta) == hoy:
            log.info("Par %s ya verificado hoy, no gasto cuota", etiqueta)
            continue

        if gastadas >= SERP_MAX_VERIF:
            log.warning("Corte en %d verificaciones para cuidar la cuota", SERP_MAX_VERIF)
            break

        vivo = precio_en_vivo(*par)
        gastadas += 1
        time.sleep(PAUSA)

        if vivo is None:
            # SerpApi no respondio: avisamos igual, marcando que quedo sin confirmar.
            mejor = min(ofertas, key=lambda x: x["precio"])
            confirmadas.append(mejor)
            continue

        ya_vistos[etiqueta] = hoy
        barato = min(o["precio"] for o in ofertas)

        if vivo < UMBRAL:
            mejor = min(ofertas, key=lambda x: x["precio"])
            mejor["en_vivo"] = vivo
            confirmadas.append(mejor)
            log.info("CONFIRMADO %s: cache %.0f, en vivo %.0f", etiqueta, barato, vivo)
        else:
            log.info("DESCARTADO %s: cache decia %.0f, en vivo %.0f", etiqueta, barato, vivo)

    return confirmadas


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


def formatear(o: dict) -> str:
    escalas = "directo" if o["escalas"] == 0 else f"{o['escalas']} escala(s)"

    if o.get("en_vivo") is not None:
        precio = (
            f"💵 <b>{o['en_vivo']:.0f} {MONEDA.upper()}</b> ✅ verificado en vivo\n"
            f"   <i>(el cache decia {o['precio']:.0f})</i>\n"
        )
    else:
        precio = (
            f"💵 <b>{o['precio']:.0f} {MONEDA.upper()}</b>\n"
            f"   <i>(precio de cache, sin confirmar)</i>\n"
        )

    return (
        f"✈️ <b>{ORIGEN} → {DESTINO}</b>\n"
        f"{precio}"
        f"📅 Ida: {o['salida'].replace('T', ' ')}\n"
        f"📅 Vuelta: {o['regreso'].replace('T', ' ')}\n"
        f"🛫 {o['aerolinea']} {o['vuelo']} · {escalas}\n"
        f"🔗 {o['link']}"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    if not TP_TOKEN:
        log.error("Falta TP_TOKEN")
        return 1

    estado = cargar_estado()
    avisados = set(estado.get("avisados", []))
    todas: list[dict] = []

    pares = fechas_del_mes(MES, NOCHES)
    log.info(
        "Barrido %s->%s: %d combinaciones de %d dias en %s",
        ORIGEN, DESTINO, len(pares), NOCHES, MES,
    )

    for ida, vuelta in pares:
        todas.extend(buscar(ida, vuelta))
        time.sleep(PAUSA)

    if not todas:
        log.warning("No vino ninguna oferta. Revisar token, ruta o fechas.")
        return 0

    todas.sort(key=lambda o: o["precio"])
    mejor = todas[0]
    log.info(
        "Mejor precio de cache: %.0f %s (%s)",
        mejor["precio"], MONEDA.upper(), mejor["salida"][:10],
    )

    # Candidatas: bajo umbral y todavia no avisadas
    candidatas = [o for o in todas if o["precio"] < UMBRAL and clave(o) not in avisados]

    if candidatas and SERP_API_KEY:
        log.info("%d candidata(s) bajo umbral, verificando en vivo...", len(candidatas))
        nuevas = verificar(candidatas, estado)
    elif candidatas:
        log.info("Sin SERP_API_KEY: aviso sin verificar")
        nuevas = candidatas
    else:
        nuevas = []

    if nuevas:
        cabecera = f"🚨 <b>{len(nuevas)} tarifa(s) por debajo de {UMBRAL:.0f} {MONEDA.upper()}</b>\n"
        cuerpo = "\n\n".join(formatear(o) for o in nuevas[:5])
        if telegram(cabecera + "\n" + cuerpo):
            avisados.update(clave(o) for o in nuevas)
            log.info("Avise %d oferta(s)", len(nuevas))

    # Nuevo minimo historico aunque siga arriba del umbral
    minimo = estado.get("minimo_historico")
    if AVISAR_MINIMO and not nuevas and (minimo is None or mejor["precio"] < minimo):
        telegram(
            "📉 <b>Nuevo minimo historico</b> (todavia arriba del umbral)\n\n"
            + formatear(mejor)
        )

    if minimo is None or mejor["precio"] < minimo:
        estado["minimo_historico"] = mejor["precio"]

    estado["avisados"] = sorted(avisados)
    estado["ultima_corrida"] = datetime.now().isoformat(timespec="seconds")
    guardar_estado(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())