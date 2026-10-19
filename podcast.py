"""Podcast d'actus quotidien : RSS -> Gemini (sélection + script) -> edge-tts -> flux podcast (GitHub Pages).
100 % gratuit. À écouter dans AntennaPod (Play Store) en ajoutant l'adresse du flux."""
import asyncio, datetime, email.utils, json, os, re, shutil, time
from pathlib import Path
from xml.sax.saxutils import escape

import edge_tts
import feedparser
import trafilatura
from google import genai

# Modèle Gemini : change-le via la variable GEMINI_MODEL si besoin (voir AI Studio).
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
VOICE = "fr-FR-DeniseNeural"  # ou fr-FR-HenriNeural
NB_SUJETS = 7
MOTS_CIBLE = 1450  # ≈ 10 min à l'oral
SITE_URL = os.environ.get("SITE_URL", "https://example.github.io/mon-depot").rstrip("/")
SITE = Path("site")
GARDER = 7  # nombre d'épisodes conservés dans le flux
TITRE_PODCAST = "Mes actus du jour"

FEEDS = {
    "franceinfo": "https://www.francetvinfo.fr/titres.rss",
    "Le Monde": "https://www.lemonde.fr/rss/une.xml",
    "Le Figaro": "https://www.lefigaro.fr/rss/figaro_actualites-a-la-une.xml",
    "France 24": "https://www.france24.com/fr/rss",
}


def ask(prompt: str) -> str:
    """Appel Gemini avec réessais (le quota gratuit renvoie parfois des 429)."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for i in range(5):
        try:
            return client.models.generate_content(model=MODEL, contents=prompt).text
        except Exception as e:
            print(f"Gemini : erreur ({e}), nouvel essai...")
            time.sleep(20 * (i + 1))
    raise RuntimeError("Gemini indisponible")


def collect() -> list[dict]:
    items = []
    for source, url in FEEDS.items():
        for e in feedparser.parse(url).entries[:15]:
            items.append({"source": source, "title": e.get("title", ""),
                          "summary": re.sub("<[^>]+>", "", e.get("summary", "")),
                          "link": e.get("link", "")})
    return items


def select(items: list[dict]) -> list[dict]:
    """Étape « agent » : Gemini choisit les sujets les plus importants."""
    liste = "\n".join(f"{i} | {x['source']} | {x['title']}" for i, x in enumerate(items))
    rep = ask(
        f"Voici les titres de l'actualité du jour :\n{liste}\n\n"
        f"Choisis les {NB_SUJETS} sujets les plus importants, variés (France, international, "
        "économie, société, science...). Regroupe les doublons entre médias. "
        "Réponds UNIQUEMENT par une liste JSON d'identifiants, ex : [3, 12, 25]."
    )
    ids = json.loads(re.search(r"\[.*?\]", rep, re.S).group())
    return [items[i] for i in ids if 0 <= i < len(items)][:NB_SUJETS]


def enrich(sujets: list[dict]) -> str:
    """Récupère le texte des articles choisis (sinon, le résumé RSS)."""
    blocs = []
    for s in sujets:
        texte = ""
        try:
            html = trafilatura.fetch_url(s["link"])
            texte = (trafilatura.extract(html) or "") if html else ""
        except Exception:
            pass
        texte = (texte or s["summary"])[:2500]
        blocs.append(f"[{s['source']}] {s['title']}\n{texte}")
    return "\n\n---\n\n".join(blocs)


def write_script(matiere: str) -> str:
    date = datetime.date.today().strftime("%A %d %B %Y")
    texte = ask(
        f"Tu es le présentateur d'un podcast quotidien d'actualités en français. Nous sommes le {date}.\n"
        f"À partir de la matière ci-dessous, écris le texte à lire à voix haute : environ {MOTS_CIBLE} mots.\n"
        "Règles : ton oral, clair et chaleureux ; phrases courtes ; transitions entre sujets ; "
        "cite les sources à l'oral (« selon Le Monde ») ; reste factuel et neutre ; "
        "n'invente RIEN qui ne soit pas dans la matière ; ouvre par un bonjour, ferme par un au revoir. "
        "Sortie : uniquement le texte parlé, sans titres, listes, markdown ni indications scéniques.\n\n"
        f"MATIÈRE :\n{matiere}"
    )
    return re.sub(r"[*#_>`]", "", texte).strip()


def tts(texte: str, path: str):
    asyncio.run(edge_tts.Communicate(texte, VOICE).save(path))


def update_site(mp3: str, description: str):
    """Ajoute l'épisode du jour, supprime les plus anciens, régénère feed.xml."""
    ep = SITE / "episodes"
    ep.mkdir(parents=True, exist_ok=True)
    jour = datetime.date.today().isoformat()
    shutil.move(mp3, ep / f"{jour}.mp3")
    (ep / f"{jour}.txt").write_text(description, encoding="utf-8")

    jours = sorted((p.stem for p in ep.glob("*.mp3")), reverse=True)
    for vieux in jours[GARDER:]:
        (ep / f"{vieux}.mp3").unlink(missing_ok=True)
        (ep / f"{vieux}.txt").unlink(missing_ok=True)

    items = []
    for j in jours[:GARDER]:
        f = ep / f"{j}.mp3"
        d = datetime.datetime.strptime(j, "%Y-%m-%d").replace(hour=6, tzinfo=datetime.timezone.utc)
        desc = (ep / f"{j}.txt").read_text(encoding="utf-8") if (ep / f"{j}.txt").exists() else ""
        titre = f"Actus du {d:%d/%m/%Y}"
        items.append(f"""<item>
<title>{escape(titre)}</title>
<description>{escape(desc)}</description>
<enclosure url="{SITE_URL}/episodes/{j}.mp3" length="{f.stat().st_size}" type="audio/mpeg"/>
<guid isPermaLink="false">actus-{j}</guid>
<pubDate>{email.utils.format_datetime(d)}</pubDate>
</item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
<title>{escape(TITRE_PODCAST)}</title>
<link>{SITE_URL}/</link>
<description>Résumé quotidien de l'actualité, généré automatiquement.</description>
<language>fr</language>
<itunes:author>Moi</itunes:author>
{chr(10).join(items)}
</channel>
</rss>
"""
    (SITE / "feed.xml").write_text(feed, encoding="utf-8")


if __name__ == "__main__":
    sujets = select(collect())
    script = write_script(enrich(sujets))
    print(f"{len(script.split())} mots")
    tts(script, "episode.mp3")
    update_site("episode.mp3", "Sujets du jour : " + " ; ".join(s["title"] for s in sujets))
