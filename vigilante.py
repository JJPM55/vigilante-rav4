#!/usr/bin/env python3
"""
Vigilante de Toyota RAV4 Advance/Spirit híbrido (HEV), matriculado en 2026, hasta 10.000 km.

Uso:
  python vigilante.py                  # ejecución normal: envía email si hay novedades
  python vigilante.py --prueba         # solo muestra lo que ve; no envía email ni guarda memoria
  python vigilante.py --email-prueba   # envía un email de prueba y termina
  python vigilante.py --enchufables    # PRUEBA: busca RAV4 enchufables (urls_prueba.txt, seen_prueba.json)

Variables de entorno para el email (en GitHub: Settings > Secrets and variables > Actions):
  GMAIL_USER          cuenta de Gmail desde la que se envía
  GMAIL_APP_PASSWORD  contraseña de aplicación de Google (16 letras)
  EMAIL_TO            destinatario (si se deja vacío, se usa GMAIL_USER)
"""
import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------- criterios
KM_MAX = 10_000
ANIO = "2026"
ARCHIVO_URLS = Path("urls.txt")
ARCHIVO_MEMORIA = Path("seen.json")
# Modo de prueba (--enchufables): usa otras URLs y otra memoria para no mezclarse con la búsqueda real.
ARCHIVO_URLS_PRUEBA = Path("urls_prueba.txt")
ARCHIVO_MEMORIA_PRUEBA = Path("seen_prueba.json")
MODO_ENCHUFABLES = False
PREFIJO_ASUNTO = ""
CARPETA_DEBUG = Path("debug")
DIAS_ENTRE_INFORMES = 7

PATRON_KM = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)\s*km\b(?!/)", re.I)

# JavaScript que se ejecuta dentro de la página: localiza las tarjetas de coche
# sin depender de nombres de clases CSS (que pueden cambiar).
JS_TARJETAS = r"""
() => {
  const resultados = [];
  const vistos = new Set();
  const hojas = Array.from(document.querySelectorAll('body *')).filter(e => e.children.length === 0);
  for (const hoja of hojas) {
    const t = (hoja.textContent || '').trim();
    if (t.length === 0 || t.length > 150 || !/rav4/i.test(t)) continue;
    let nodo = hoja;
    for (let i = 0; i < 10 && nodo.parentElement; i++) {
      nodo = nodo.parentElement;
      const txt = (nodo.innerText || '').trim();
      if (txt.length > 1500) break;
      if (/\d[\d.]*\s*km/i.test(txt) && !/Mínimo|Máximo/i.test(txt)) {
        const enlace = nodo.querySelector('a[href]') || nodo.closest('a[href]');
        const href = enlace ? enlace.href : '';
        const clave = href || txt;
        if (!vistos.has(clave)) {
          vistos.add(clave);
          resultados.push({ texto: txt.replace(/\s+/g, ' '), href: href });
        }
        break;
      }
    }
  }
  return resultados;
}
"""


# ---------------------------------------------------------------- utilidades
def ahora():
    return datetime.now(timezone.utc)


def log(*partes):
    print(*partes, flush=True)


def leer_webs():
    """Lee urls.txt. Formato por línea: 'Nombre | URL' o solo 'URL'. # = comentario."""
    webs = {}
    for linea in ARCHIVO_URLS.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if "|" in linea:
            nombre, url = [p.strip() for p in linea.split("|", 1)]
        else:
            nombre, url = "", linea
        if not url.startswith("http"):
            url = "https://" + (url if url.startswith("www.") else "www." + url)
        if not nombre:
            m = re.search(r"/concesionarios/([^/]+)/", url)
            nombre = m.group(1) if m else url[:40]
        webs.setdefault(url, nombre)  # evita URLs repetidas
    return [(nombre, url) for url, nombre in webs.items()]


def cargar_memoria():
    if ARCHIVO_MEMORIA.exists():
        return json.loads(ARCHIVO_MEMORIA.read_text(encoding="utf-8"))
    return {"vistos": {}, "ultimo_informe": None, "alerta_fallo": False}


def guardar_memoria(memoria):
    ARCHIVO_MEMORIA.write_text(
        json.dumps(memoria, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def identificador(tarjeta):
    """Identifica un coche: su enlace, o (si no hay) un hash del texto sin el precio."""
    if tarjeta["href"]:
        return tarjeta["href"]
    sin_precio = re.sub(r"\d[\d.,]*\s*€|€\s*\d[\d.,]*", "", tarjeta["texto"])
    return "texto:" + hashlib.sha1(sin_precio.lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- filtro
def analizar(texto):
    """Devuelve (estado, motivo, datos). estado: 'ok', 'revisar' o 'descartar'."""
    t = texto.lower()
    kms = [int(m.replace(".", "")) for m in PATRON_KM.findall(t)]
    km = kms[0] if kms else None
    sin_km = PATRON_KM.sub(" ", t)
    acabado = re.search(r"\b(advance|spirit)\b", t)
    datos = {"acabado": acabado.group(1).capitalize() if acabado else None, "km": km}

    if MODO_ENCHUFABLES:  # solo para comprobar que el script funciona
        if re.search(r"enchufable|plug-?in|phev", t):
            return "ok", "PRUEBA: RAV4 híbrido enchufable", datos
        return "descartar", "PRUEBA: no es enchufable", datos

    if not acabado:
        return "descartar", "no es Advance ni Spirit", datos
    if re.search(r"enchufable|plug-?in|phev", t):
        return "descartar", "híbrido enchufable", datos
    anios = set(re.findall(r"\b(20[12]\d)\b", sin_km))
    if anios and ANIO not in anios:
        return "descartar", "matriculado en " + ", ".join(sorted(anios)), datos
    if km is not None and km > KM_MAX:
        return "descartar", f"{km} km (más de {KM_MAX})", datos

    avisos = []
    if km is None:
        avisos.append("no aparecen los kilómetros")
    if not re.search(r"h[ií]brido|hybrid|\bhev\b", t):
        avisos.append("no indica claramente que sea híbrido")
    if avisos:
        return "revisar", "; ".join(avisos), datos
    return "ok", "cumple todo", datos


# ---------------------------------------------------------------- navegador
def guardar_debug(pagina, indice, nombre):
    try:
        CARPETA_DEBUG.mkdir(exist_ok=True)
        base = CARPETA_DEBUG / f"{indice}_{re.sub(r'[^a-zA-Z0-9]+', '_', nombre)}"
        base.with_suffix(".txt").write_text(pagina.inner_text("body"), encoding="utf-8")
        pagina.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception as e:  # el debug nunca debe romper la ejecución
        log(f"  (no se pudo guardar el debug: {e})")


def revisar_web(contexto, nombre, url, indice, siempre_debug):
    pagina = contexto.new_page()
    try:
        pagina.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            pagina.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        pagina.wait_for_timeout(4_000)

        for _ in range(3):  # desplazarse para activar la carga perezosa
            pagina.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pagina.wait_for_timeout(1_500)

        try:  # pulsar "ver más / cargar más" si existe
            for _ in range(5):
                boton = pagina.get_by_role("button", name=re.compile(r"(ver|cargar|mostrar) más", re.I))
                if boton.count() == 0 or not boton.first.is_visible():
                    break
                boton.first.click(timeout=5_000)
                pagina.wait_for_timeout(2_500)
        except Exception:
            pass

        tarjetas = pagina.evaluate(JS_TARJETAS)
        if siempre_debug or not tarjetas:
            guardar_debug(pagina, indice, nombre)
        return tarjetas
    finally:
        pagina.close()


# ---------------------------------------------------------------- email
def enviar_email(asunto, cuerpo):
    usuario = os.environ["GMAIL_USER"]
    clave = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    destino = os.environ.get("EMAIL_TO") or usuario
    msg = EmailMessage()
    msg["Subject"] = PREFIJO_ASUNTO + asunto
    msg["From"] = usuario
    msg["To"] = destino
    msg.set_content(cuerpo)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(usuario, clave)
        smtp.send_message(msg)
    log(f"Email enviado a {destino}: {asunto}")


def texto_aviso(n):
    if n["estado"] == "ok":
        etiqueta = "CUMPLE TODO"
    else:
        etiqueta = f"REVISAR ({n['motivo']})"
    return f"Web: {n['web']}\nEstado: {etiqueta}\n{n['texto'][:300]}\nEnlace: {n['enlace']}\n"


# ---------------------------------------------------------------- programa
def vigilar(webs, modo_prueba):
    from playwright.sync_api import sync_playwright

    resultados = []  # una entrada por tarjeta de RAV4 vista
    fallos = []
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(
            locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1366, "height": 900}
        )
        for i, (nombre, url) in enumerate(webs):
            log(f"== {nombre}: {url}")
            try:
                tarjetas = revisar_web(contexto, nombre, url, i, siempre_debug=modo_prueba)
            except Exception as e:
                fallos.append((nombre, str(e).splitlines()[0][:200] if str(e) else repr(e)))
                log(f"  ERROR: {fallos[-1][1]}")
                continue
            log(f"  tarjetas de RAV4 encontradas: {len(tarjetas)}")
            for t in tarjetas:
                estado, motivo, datos = analizar(t["texto"])
                resultados.append(
                    {
                        "web": nombre,
                        "texto": t["texto"],
                        "enlace": t["href"] or url,
                        "id": identificador(t),
                        "estado": estado,
                        "motivo": motivo,
                    }
                )
                if modo_prueba:
                    log(f"  [{estado}] {motivo} | {t['texto'][:160]}")
        navegador.close()
    return resultados, fallos


def main():
    global ARCHIVO_URLS, ARCHIVO_MEMORIA, MODO_ENCHUFABLES, PREFIJO_ASUNTO
    ap = argparse.ArgumentParser()
    ap.add_argument("--prueba", action="store_true")
    ap.add_argument("--email-prueba", action="store_true")
    ap.add_argument("--enchufables", action="store_true")
    args = ap.parse_args()

    if args.enchufables:
        ARCHIVO_URLS = ARCHIVO_URLS_PRUEBA
        ARCHIVO_MEMORIA = ARCHIVO_MEMORIA_PRUEBA
        MODO_ENCHUFABLES = True
        PREFIJO_ASUNTO = "[PRUEBA] "

    if args.email_prueba:
        enviar_email("Vigilante RAV4: email de prueba", "Si lees esto, el envío de avisos funciona.")
        return

    webs = leer_webs()
    if not webs:
        log("urls.txt no tiene ninguna URL.")
        sys.exit(1)

    resultados, fallos = vigilar(webs, args.prueba)
    if args.prueba:
        log("\nModo prueba: no se envía email ni se guarda memoria.")
        return

    memoria = cargar_memoria()

    # Si no se pudo leer ninguna web, avisar una sola vez.
    if len(fallos) == len(webs):
        if not memoria.get("alerta_fallo"):
            detalle = "\n".join(f"- {n}: {e}" for n, e in fallos)
            enviar_email(
                "Vigilante RAV4: no puedo leer ninguna web",
                "En esta revisión han fallado todas las webs:\n" + detalle +
                "\n\nPuede ser algo puntual o que Toyota haya cambiado la página. "
                "Te avisaré de nuevo solo cuando vuelva a funcionar.",
            )
            memoria["alerta_fallo"] = True
            guardar_memoria(memoria)
        return
    memoria["alerta_fallo"] = False

    # Novedades: coches válidos (o por revisar) que no habíamos visto.
    nuevos = [
        r for r in resultados
        if r["estado"] in ("ok", "revisar") and r["id"] not in memoria["vistos"]
    ]
    if nuevos:
        webs_nuevos = sorted({n["web"] for n in nuevos})
        cuerpo = (
            f"Se han encontrado {len(nuevos)} RAV4 nuevo(s) que encajan con tu búsqueda "
            f"(Advance/Spirit, híbrido, {ANIO}, hasta {KM_MAX} km):\n\n"
            + "\n".join(texto_aviso(n) for n in nuevos)
        )
        enviar_email(f"Vigilante RAV4: {len(nuevos)} novedad(es) en {', '.join(webs_nuevos)}", cuerpo)
        for n in nuevos:
            memoria["vistos"][n["id"]] = {
                "primera_vez": ahora().isoformat(timespec="seconds"),
                "web": n["web"],
                "texto": n["texto"][:300],
            }

    # Email de activación la primera vez, e informe semanal después.
    ultimo = memoria.get("ultimo_informe")
    toca_informe = not MODO_ENCHUFABLES and (
        ultimo is None or ahora() - datetime.fromisoformat(ultimo) > timedelta(days=DIAS_ENTRE_INFORMES)
    )
    if toca_informe:
        lineas = [f"- {n}: {u}" for n, u in webs]
        problemas = "\n".join(f"- {n}: {e}" for n, e in fallos) or "ninguna"
        titulo = "Vigilante RAV4 activado" if ultimo is None else "Vigilante RAV4: informe semanal"
        enviar_email(
            titulo,
            "El vigilante funciona.\n\nWebs vigiladas:\n" + "\n".join(lineas) +
            f"\n\nTarjetas de RAV4 vistas en esta revisión: {len(resultados)}"
            f"\nAvisos enviados en total: {len(memoria['vistos'])}"
            f"\nWebs con problemas en esta revisión: {problemas}\n",
        )
        memoria["ultimo_informe"] = ahora().isoformat(timespec="seconds")

    guardar_memoria(memoria)
    log(f"Hecho. Novedades: {len(nuevos)}. Fallos: {len(fallos)}.")


if __name__ == "__main__":
    main()
