"""Enregistre (lecture seule) des réponses REELLES du site pour les tests hors ligne :
  tests/fixtures/asp/home.html                 page d'accueil (moteurs de recherche, options)
  tests/fixtures/asp/<moteur>__<requete>.txt   réponse brute du moteur ASP (VF / VOSTFR) pour chaque requête
  tests/fixtures/asp/pages/<slug>.html         pages d'anime utiles aux tests
Le site n'est jamais modifié : ce sont les mêmes requêtes que celles des deux boîtes de recherche du site.
Usage :  python scripts/record_asp_corpus.py
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from v2_automation import app_config
from v2_automation.search import AspEngine, slugify_query

OUT = ROOT / "tests" / "fixtures" / "asp"
QUERIES = [
    "bestiale", "wakfu", "carmen sandiego", "avatar", "avatar le dernier maitre de l'air", "attack on titan",
    "shingeki no kyojin", "demon slayer", "kimetsu no yaiba", "one piece", "naruto", "bleach", "dragon ball",
    "fairy tail", "detective conan", "black torch", "mob psycho 100", "re zero", "solo leveling", "jujutsu kaisen",
    "hunter x hunter", "one punch man", "spy x family", "my hero academia", "fullmetal alchemist", "death note",
    "tokyo ghoul", "sword art online", "boruto", "code geass", "cowboy bebop", "steins gate", "haikyuu",
    "the king's avatar", "quanzhi gaoshou", "zzzzqqqq", "a", "dr stone", "vinland saga", "made in abyss",
    "one piece film red", "naruto shippuden", "frieren", "tomb raider king", "classroom of the elite",
]
PAGES = ["wakfu-s2", "wakfu-s1", "carmen-sandiego-s2-vf", "avatar-le-dernier-maitre-de-lair", "bestiale-vf", "one-piece",
         "kimetsu-no-yaiba", "kimetsu-no-yaiba-vf", "kimetsu-no-yaiba-2", "spy-x-family", "mob-psycho-100",
         "quan-zhi-gao-shou", "shingeki-no-kyojin", "one-piece-film-red-vf", "naruto", "naruto-shippuuden"]


def main():
    import httpx
    cfg = app_config.load_config()
    headers = {"User-Agent": "v2_automation-research-bot/2.0 (+contact: lionelyvan24@gmail.com)"}
    client = httpx.Client(headers=headers, timeout=30, follow_redirects=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "pages").mkdir(exist_ok=True)
    base = cfg.source["base_url"].rstrip("/")
    home = client.get(base + "/").text
    (OUT / "home.html").write_text(home, encoding="utf-8")
    engine = AspEngine(cfg, get=lambda u: home, post=lambda u, d: client.post(u, data=d).text)
    engines = engine.engines()
    print("moteurs découverts :", {k: v["asid"] for k, v in engines.items()})
    for q in QUERIES:
        for version, info in engines.items():
            raw = client.post(base + "/wp-admin/admin-ajax.php", data=engine.payload(q, info)).text
            (OUT / f"{version}__{slugify_query(q)}.txt").write_text(raw, encoding="utf-8")
            time.sleep(0.4)
    for slug in PAGES:
        r = client.get(f"{base}/anime/{slug}/")
        if r.status_code == 200:
            (OUT / "pages" / f"{slug}.html").write_text(r.text, encoding="utf-8")
        else:
            print("page absente :", slug, r.status_code)
        time.sleep(0.4)
    print("terminé :", len(list(OUT.glob("*.txt"))), "réponses,", len(list((OUT / "pages").glob("*.html"))), "pages")


if __name__ == "__main__":
    main()
