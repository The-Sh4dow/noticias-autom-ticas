"""
news_collector.py
Recolecta noticias tech desde RSS feeds y las resume en español usando Claude API.
Guarda los resultados en un archivo JSON listo para enviar a Notion o Telegram.

Dependencias:
    pip install feedparser httpx anthropic python-dateutil

Variables de entorno necesarias:
    ANTHROPIC_API_KEY=sk-ant-...
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import httpx
from anthropic import Anthropic
from dateutil import parser as dateparser

# ─── Configuración de logging ────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Fuentes RSS ─────────────────────────────────────────────────────────────
# Mezcla de fuentes en español e inglés (las inglesas se traducen con Claude)

FEEDS = [
    # --- Español ---
    {"url": "https://www.xataka.com/feed", "lang": "es", "name": "Xataka"},
    {"url": "https://feeds.feedburner.com/genbeta", "lang": "es", "name": "Genbeta"},
    {"url": "https://hipertextual.com/feed", "lang": "es", "name": "Hipertextual"},
    {"url": "https://www.enter.co/feed/", "lang": "es", "name": "Enter.co"},
    # --- Inglés (se traducen al español) ---
    {"url": "https://techcrunch.com/feed/", "lang": "en", "name": "TechCrunch"},
    {"url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "lang": "en", "name": "Ars Technica"},
    {"url": "https://www.theverge.com/rss/index.xml", "lang": "en", "name": "The Verge"},
    {"url": "https://www.wired.com/feed/rss", "lang": "en", "name": "Wired"},
]

# ─── Parámetros ───────────────────────────────────────────────────────────────

MAX_ARTICLES_PER_FEED = 5          # Cuántos artículos tomar por fuente
MAX_HOURS_OLD        = 24          # Ignorar noticias más viejas que esto
OUTPUT_FILE          = "noticias.json"   # Donde se guardan los resultados
SEEN_IDS_FILE        = "seen_ids.txt"    # Para no repetir artículos ya procesados

# ─── Cliente Anthropic ────────────────────────────────────────────────────────

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ─── Prompts ─────────────────────────────────────────────────────────────────

PROMPT_ES = """Eres el editor de un canal de Telegram sobre tecnología para audiencia hispanohablante.
Tu tarea es procesar la siguiente noticia y devolver SOLO un JSON válido (sin texto adicional, sin markdown, sin explicaciones).

Noticia original (en español):
TÍTULO: {title}
DESCRIPCIÓN: {description}
FUENTE: {source}

Devuelve exactamente este JSON:
{{
  "titulo": "Título en español, atractivo, máximo 80 caracteres",
  "resumen": "Resumen de 2-3 oraciones que explique qué pasó, por qué importa y qué viene después. Tono informativo pero dinámico.",
  "puntos_clave": ["punto 1", "punto 2", "punto 3"],
  "gancho": "Una pregunta o afirmación provocadora de máximo 1 línea para abrir la publicación",
  "hashtags": ["#tecnologia", "#otro_relevante"],
  "relevancia": 1-10
}}
"""

PROMPT_EN = """Eres el editor de un canal de Telegram sobre tecnología para audiencia hispanohablante.
Tu tarea es traducir y procesar la siguiente noticia del inglés al español, y devolver SOLO un JSON válido (sin texto adicional, sin markdown, sin explicaciones).

Noticia original (en inglés):
TÍTULO: {title}
DESCRIPCIÓN: {description}
FUENTE: {source}

Devuelve exactamente este JSON:
{{
  "titulo": "Título traducido al español, atractivo, máximo 80 caracteres",
  "resumen": "Resumen en español de 2-3 oraciones que explique qué pasó, por qué importa y qué viene después. Tono informativo pero dinámico.",
  "puntos_clave": ["punto 1 en español", "punto 2 en español", "punto 3 en español"],
  "gancho": "Una pregunta o afirmación provocadora en español de máximo 1 línea para abrir la publicación",
  "hashtags": ["#tecnologia", "#otro_relevante"],
  "relevancia": 1-10
}}
"""

# ─── Utilidades ───────────────────────────────────────────────────────────────

def article_id(url: str) -> str:
    """Genera un ID único por URL para evitar duplicados."""
    return hashlib.md5(url.encode()).hexdigest()


def load_seen_ids() -> set:
    """Carga los IDs de artículos ya procesados."""
    p = Path(SEEN_IDS_FILE)
    if not p.exists():
        return set()
    return set(p.read_text().splitlines())


def save_seen_ids(seen: set):
    """Persiste los IDs procesados."""
    Path(SEEN_IDS_FILE).write_text("\n".join(seen))


def is_recent(entry) -> bool:
    """Devuelve True si el artículo tiene menos de MAX_HOURS_OLD horas."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                pub = datetime(*t[:6], tzinfo=timezone.utc)
                return datetime.now(timezone.utc) - pub < timedelta(hours=MAX_HOURS_OLD)
            except Exception:
                pass
    # Si no tiene fecha, lo incluimos por las dudas
    return True


def clean_text(text: str) -> str:
    """Elimina HTML básico y espacios extra."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:800]  # Limitamos para no explotar el contexto


# ─── Recolección de feeds ─────────────────────────────────────────────────────

def fetch_articles() -> list[dict]:
    """Lee todos los feeds y devuelve artículos recientes sin duplicados."""
    seen = load_seen_ids()
    articles = []

    for feed_cfg in FEEDS:
        url  = feed_cfg["url"]
        lang = feed_cfg["lang"]
        name = feed_cfg["name"]
        log.info(f"Leyendo feed: {name}")

        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            log.warning(f"Error al leer {name}: {e}")
            continue

        count = 0
        for entry in parsed.entries:
            if count >= MAX_ARTICLES_PER_FEED:
                break

            link = getattr(entry, "link", "")
            if not link:
                continue

            aid = article_id(link)
            if aid in seen:
                continue

            if not is_recent(entry):
                continue

            title = clean_text(getattr(entry, "title", ""))
            description = clean_text(
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
            )

            if not title:
                continue

            articles.append({
                "id":          aid,
                "source":      name,
                "lang":        lang,
                "title":       title,
                "description": description,
                "url":         link,
                "fetched_at":  datetime.now(timezone.utc).isoformat(),
            })
            count += 1

        log.info(f"  → {count} artículos nuevos de {name}")

    log.info(f"Total artículos recolectados: {len(articles)}")
    return articles, seen


# ─── Procesamiento con Claude ─────────────────────────────────────────────────

def process_with_claude(article: dict) -> dict | None:
    """Envía el artículo a Claude para resumen/traducción. Devuelve dict o None."""
    prompt_template = PROMPT_EN if article["lang"] == "en" else PROMPT_ES

    prompt = prompt_template.format(
        title=article["title"],
        description=article["description"] or "(sin descripción disponible)",
        source=article["source"],
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Haiku: rápido y económico para esto
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Limpiar posibles bloques de código markdown que Claude añada
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)

        # Validar campos mínimos
        required = {"titulo", "resumen", "puntos_clave", "gancho", "hashtags", "relevancia"}
        if not required.issubset(data.keys()):
            log.warning(f"Respuesta incompleta para: {article['title']}")
            return None

        return {
            **article,
            "titulo_es":    data["titulo"],
            "resumen_es":   data["resumen"],
            "puntos_clave": data["puntos_clave"],
            "gancho":       data["gancho"],
            "hashtags":     data["hashtags"],
            "relevancia":   int(data.get("relevancia", 5)),
            "estado":       "pendiente",  # Para el filtro en Notion
        }

    except json.JSONDecodeError as e:
        log.error(f"JSON inválido de Claude para '{article['title']}': {e}")
    except Exception as e:
        log.error(f"Error Claude para '{article['title']}': {e}")

    return None


# ─── Pipeline principal ───────────────────────────────────────────────────────

def run():
    log.info("=== Iniciando recolección de noticias ===")

    articles, seen = fetch_articles()

    if not articles:
        log.info("No hay artículos nuevos. Saliendo.")
        return

    processed = []
    new_seen   = set()

    for i, article in enumerate(articles, 1):
        log.info(f"[{i}/{len(articles)}] Procesando: {article['title'][:60]}…")
        result = process_with_claude(article)

        if result:
            processed.append(result)
            new_seen.add(article["id"])
            log.info(f"  ✓ Relevancia {result['relevancia']}/10 | {result['titulo_es'][:50]}")
        else:
            log.warning(f"  ✗ Descartado")

    # Guardar resultados
    output_path = Path(OUTPUT_FILE)
    existing = []
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    all_news = existing + processed
    output_path.write_text(
        json.dumps(all_news, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Actualizar IDs vistos
    save_seen_ids(seen | new_seen)

    log.info(f"=== Listo: {len(processed)} noticias procesadas → {OUTPUT_FILE} ===")

    # Preview en consola
    print("\n── Preview de las 3 más relevantes ──")
    top = sorted(processed, key=lambda x: x["relevancia"], reverse=True)[:3]
    for n in top:
        print(f"\n[{n['source']}] Relevancia {n['relevancia']}/10")
        print(f"  {n['gancho']}")
        print(f"  📌 {n['titulo_es']}")
        print(f"  {n['resumen_es']}")
        print(f"  {' '.join(n['hashtags'])}")


if __name__ == "__main__":
    run()
