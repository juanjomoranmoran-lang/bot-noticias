"""Bot de noticias para Telegram.

Cada ejecución: lee los feeds, puntúa las noticias nuevas con IA, envía alerta
inmediata si algo es excepcional y, a las horas fijadas, envía el resumen.
"""
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests

# ---------- Configuración (lo que querrás tocar) ----------
TZ = ZoneInfo("Europe/Madrid")
HORA_MANANA = 8        # resumen de la mañana a partir de esta hora
HORA_NOCHE = 21        # resumen de la noche a partir de esta hora
UMBRAL_ALERTA = 9      # nota mínima para alerta inmediata
UMBRAL_RESUMEN = 6     # nota mínima para entrar en el resumen
UMBRAL_BLANDO = 8      # nota mínima para deporte, espectáculos, famosos
MAX_RESUMEN = 10       # máximo de noticias por resumen
# Modelos gratuitos de Groq, por orden de preferencia. Si uno agota su cupo
# diario o deja de existir, el bot pasa solo al siguiente.
MODELOS = [
    ("openai/gpt-oss-120b", {"reasoning_effort": "low"}),
    ("llama-3.3-70b-versatile", {}),
    ("openai/gpt-oss-20b", {"reasoning_effort": "low"}),
    ("llama-3.1-8b-instant", {}),
]

FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("El País", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada"),
    ("El Mundo", "https://e00-elmundo.uecdn.es/elmundo/rss/portada.xml"),
    ("RTVE", "https://api2.rtve.es/rss/temas_noticias.xml"),
    ("France 24", "https://www.france24.com/es/rss"),
    ("Google Noticias", "https://news.google.com/rss?hl=es&gl=ES&ceid=ES:es"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    ("Marca", "https://e00-marca.uecdn.es/rss/portada.xml"),
]

CRITERIO = """Eres el editor de un servicio de noticias para un lector en España que solo quiere enterarse de lo realmente importante que pasa en el mundo. Puntúa cada noticia NUEVA de 1 a 10:
- 9-10: excepcional o histórico, merece interrumpirle: estalla una guerra, gran atentado, catástrofe con muchas víctimas, muerte o caída del líder de un país importante, crisis financiera global, hecho de enorme impacto en España.
- 7-8: muy importante, titular principal del día.
- 6: relevante, cabe en un resumen diario.
- 1-5: menor, local, opinión, análisis sin hecho nuevo, curiosidades, seguimiento rutinario de un tema ya conocido.
Deporte, espectáculos y famosos: marca "soft": true y sé muy exigente. 8 o más solo para hitos (final de un Mundial, combate por el título de una figura española como Topuria, muerte de una leyenda). Una jornada normal, 3 o menos.
Marca "dup": true si cuenta lo mismo que una YA ENVIADA sin novedad sustancial, o si repite otra NUEVA (deja sin marcar solo la mejor de cada grupo).
Responde SOLO con un array JSON, un objeto por noticia nueva: {"i":0,"s":5,"soft":false,"dup":false}. Si s>=6 y no es dup, añade "t" (titular en español, máximo 12 palabras) y "r" (una o dos frases en español que aporten datos que no estén ya en el titular: cifras, quién, dónde, consecuencias). Usa solo lo que diga el texto: no añadas años, nombres ni datos de tu memoria, y no repitas el titular con otras palabras. Si el texto no aporta nada más que el titular, deja "r" vacío."""
# -----------------------------------------------------------

ESTADO = "state.json"
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def cargar_estado():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def limpiar(texto):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(texto or ""))).strip()


def leer_feeds(vistos):
    ahora, nuevas, ids = time.time(), [], set()
    for fuente, url in FEEDS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (bot-noticias)"})
            r.raise_for_status()
            entradas = feedparser.parse(r.content).entries[:25]
        except Exception as e:  # un feed caído no debe parar el resto
            print(f"Feed caído ({fuente}): {e}")
            continue
        for e in entradas:
            enlace = e.get("link")
            if not enlace or not e.get("title"):
                continue
            fecha = e.get("published_parsed") or e.get("updated_parsed")
            if fecha and ahora - time.mktime(fecha) > 36 * 3600:
                continue
            uid = hashlib.sha1(enlace.encode()).hexdigest()[:16]
            if uid in vistos or uid in ids:
                continue
            ids.add(uid)
            medio = fuente
            if fuente == "Google Noticias":  # trae el medio real de cada noticia
                medio = (e.get("source") or {}).get("title") or fuente
            nuevas.append({"id": uid, "src": medio, "url": enlace,
                           "title": limpiar(e.title), "sum": limpiar(e.get("summary"))[:160]})
    return nuevas


def puntuar(noticias, recientes):
    texto = f"FECHA DE HOY: {datetime.now(TZ):%d/%m/%Y}\n\nYA ENVIADAS:\n" + ("\n".join(f"- {t}" for t in recientes) or "(ninguna)")
    texto += "\n\nNUEVAS:\n" + "\n".join(
        f"{i} | [{n['src']}] {n['title']} — {n['sum']}" for i, n in enumerate(noticias))
    error = None
    for modelo, extra in MODELOS:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                         "Content-Type": "application/json"},
                json={"model": modelo, "max_tokens": 4000, "temperature": 0,
                      "messages": [{"role": "system", "content": CRITERIO},
                                   {"role": "user", "content": texto}], **extra},
                timeout=90)
            r.raise_for_status()
            salida = r.json()["choices"][0]["message"]["content"]
            return json.loads(salida[salida.index("["):salida.rindex("]") + 1])
        except Exception as e:  # cupo agotado, modelo retirado o respuesta mal formada
            error = e
            print(f"Fallo con {modelo}: {e}")
    raise error


def telegram(texto):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
    trozo = ""
    for bloque in texto.split("\n\n") + [None]:
        if bloque is None or len(trozo) + len(bloque) > 3800:
            if trozo:
                r = requests.post(url, timeout=30, json={
                    "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": trozo,
                    "parse_mode": "HTML", "disable_web_page_preview": True})
                r.raise_for_status()
            trozo = ""
        if bloque:
            trozo += bloque + "\n\n"


def linea(n):
    resumen = html.escape(n["r"]) + " " if n.get("r") else ""
    return (f"<b>{html.escape(n['t'])}</b>\n{resumen}"
            f"<a href=\"{html.escape(n['url'], quote=True)}\">{html.escape(n['src'])}</a>")


def enviar_resumen(titulo, pendientes, ahora):
    fecha = f"{DIAS[ahora.weekday()]} {ahora.day} {MESES[ahora.month - 1]}"
    cabecera = f"{titulo} · {fecha}"
    recientes = [n for n in pendientes if time.time() - n["ts"] < 24 * 3600]
    elegidas = sorted(recientes, key=lambda n: -n["s"])[:MAX_RESUMEN]
    if not elegidas:
        telegram(f"{cabecera}\n\nNada realmente relevante desde el último resumen.")
        return
    duras = [linea(n) for n in elegidas if not n["soft"]]
    blandas = [linea(n) for n in elegidas if n["soft"]]
    partes = [cabecera] + duras + (["<i>Deporte y otros</i>"] + blandas if blandas else [])
    telegram("\n\n".join(partes))


def main():
    estado = cargar_estado()
    primera = "seen" not in estado
    vistos = estado.setdefault("seen", {})
    pendientes = estado.setdefault("pending", [])
    recientes = estado.setdefault("recent", [])
    ultimo = estado.setdefault("last", {})
    ts = time.time()

    nuevas = leer_feeds(vistos)
    print(f"{len(nuevas)} noticias nuevas")
    # Lotes de 25 y máximo 3 por ejecución, para no pasar los límites gratuitos;
    # lo que sobre se procesa en la siguiente ejecución.
    for k in range(0, min(len(nuevas), 75), 25):
        lote = nuevas[k:k + 25]
        try:
            notas = puntuar(lote, [x["t"] for x in recientes[-25:]])
        except Exception as e:  # no se marcan como vistas: se reintenta en la próxima ejecución
            print(f"Fallo al puntuar: {e}")
            continue
        for n in lote:
            vistos[n["id"]] = ts
        for nota in notas:
            try:
                n, s, soft = lote[int(nota["i"])], int(nota["s"]), bool(nota.get("soft"))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if nota.get("dup") or not nota.get("t") or s < (UMBRAL_BLANDO if soft else UMBRAL_RESUMEN):
                continue
            item = {"t": nota["t"], "r": nota.get("r", ""), "url": n["url"],
                    "src": n["src"], "s": s, "soft": soft, "ts": ts}
            recientes.append({"t": item["t"], "ts": ts})
            if s >= UMBRAL_ALERTA and not soft and not primera:
                telegram("🚨 " + linea(item))
            else:
                pendientes.append(item)

    ahora = datetime.now(TZ)
    hoy = ahora.strftime("%Y-%m-%d")
    if ahora.hour >= HORA_NOCHE and ultimo.get("night") != hoy:
        enviar_resumen("🌙 <b>Resumen de la noche</b>", pendientes, ahora)
        ultimo["night"] = hoy
        ultimo["morning"] = hoy  # si arrancó tarde, no manda el de la mañana después
        pendientes.clear()
    elif HORA_MANANA <= ahora.hour < HORA_NOCHE and ultimo.get("morning") != hoy:
        enviar_resumen("🌅 <b>Resumen de la mañana</b>", pendientes, ahora)
        ultimo["morning"] = hoy
        pendientes.clear()

    estado["seen"] = {k: v for k, v in vistos.items() if ts - v < 5 * 86400}
    estado["recent"] = [x for x in recientes if ts - x["ts"] < 3 * 86400]
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
