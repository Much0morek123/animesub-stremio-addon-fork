"""
Stremio Addon – Polskie napisy do anime z animesub.info
Przepisany na Python/FastAPI na podstawie działającego addonu JS.
"""

import os
import re
import io
import time
import asyncio
import logging
import zipfile
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, PlainTextResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime


with open("version", "r") as f:
    app_version = f.read().strip()

# ── Konfiguracja ──────────────────────────────────────────────

def _detect_base_url() -> str:
    """AS_BASE_URL, a na Hugging Face Spaces adres wykryty ze zmiennych Space (także pod uvicorn)."""
    explicit = os.environ.get("AS_BASE_URL")
    if explicit:
        return explicit
    render_url = os.environ.get("RENDER_EXTERNAL_URL")  # ustawiane automatycznie przez Render
    if render_url:
        return render_url.rstrip("/")
    host = os.environ.get("AS_SPACE_HOST") or os.environ.get("SPACE_HOST")
    if host:
        return f"https://{host}"
    space_id = os.environ.get("AS_SPACE_ID") or os.environ.get("SPACE_ID")
    if space_id:
        return f"https://{space_id.replace('/', '-').replace('_', '-').lower()}.hf.space"
    return "http://localhost:8080"


BASE_URL = _detect_base_url()
ANIMESUB_BASE = os.environ.get("ANIMESUB_BASE_URL", "http://animesub.info").rstrip("/")
SEARCH_URL = f"{ANIMESUB_BASE}/szukaj.php"
DOWNLOAD_URL = f"{ANIMESUB_BASE}/sciagnij.php"

# Stałe przesunięcie napisów w ms. Wartość ujemna = napisy pojawiają się wcześniej.
# 1 = pokazuj napisy z dobrym numerem odcinka, ale bez znanego sezonu (może być zły sezon).
ALLOW_UNCERTAIN_SEASON = os.environ.get("ALLOW_UNCERTAIN_SEASON", "0") == "1"

# Formaty, których Stremio nie odtwarza – tylko te są konwertowane do SRT.
UNSUPPORTED_BY_STREMIO = {"microdvd", "tmplayer"}

# Łączny czas na szukanie napisów (s). Stremio i tak porzuca zbyt wolne odpowiedzi,
# więc po tym czasie zwracamy to, co już znaleziono.
SEARCH_BUDGET_S = float(os.environ.get("SEARCH_BUDGET_S", "9"))
# Ile zapytań do animesub naraz (grzecznie, żeby nie zostać zablokowanym)
SEARCH_BATCH = int(os.environ.get("SEARCH_BATCH", "3"))

# Maksymalna liczba zapytań do animesub na jeden odcinek
MAX_STRATEGIES = int(os.environ.get("MAX_STRATEGIES", "80"))

SUBTITLE_OFFSET_MS = int(os.environ.get("SUBTITLE_OFFSET_MS", "0"))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("animesub")
year = str(datetime.now().year)

# ── Cache ─────────────────────────────────────────────────────

search_cache: dict[str, dict] = {}
CACHE_TTL = 60 * 30  # 30 minut

# ── FastAPI ───────────────────────────────────────────────────

app = FastAPI(title="AnimeSub.info Stremio Addon")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

MANIFEST = {
    # Własne ID/nazwa dla forka: AS_ADDON_ID, AS_ADDON_NAME (inaczej Stremio uzna dodatek
    # za tę samą instalację co oryginał o identycznym ID).
    "id": os.environ.get("AS_ADDON_ID", "org.stremio.addon.info.animesub"),
    "version": app_version,
    "name": os.environ.get("AS_ADDON_NAME", "AnimeSub.info Subtitles"),
    "description": "Dodatek wyszukuje polskie napisy do anime z animesub.info",
    "logo": f"{BASE_URL}/static/icon.jpg",
    "resources": ["subtitles"],
    "types": ["movie", "series", "anime"],
    "idPrefixes": ["tt", "kitsu"],
    "catalogs": [],
    "behaviorHints": {"configurable": False, "configurationRequired": False},
}

# Opcjonalne, własne dane publikacji (np. na stremio-addons.net) – tylko z env:
if os.environ.get("AS_CONTACT_EMAIL"):
    MANIFEST["contactEmail"] = os.environ["AS_CONTACT_EMAIL"]
if os.environ.get("AS_STREMIO_ADDONS_SIGNATURE"):
    MANIFEST["stremioAddonsConfig"] = {
        "issuer": "https://stremio-addons.net",
        "signature": os.environ["AS_STREMIO_ADDONS_SIGNATURE"],
    }

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/version")
async def version():
    try:
        with open("version") as f: return PlainTextResponse(f.read().strip())
    except: return PlainTextResponse("?", status_code=404)

async def index(request: Request):
    host = request.headers.get("host", "127.0.0.1:8080")
    protocol = "https" if "duckdns.org" in host else "http"
    full_url = f"{protocol}://{host}"
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("{public_url}", full_url)
        content = content.replace("{stremio_url}", f"stremio://{host}/manifest.json")
        content = content.replace("{version_placeholder}", app_version)
        content = content.replace("{current_year}", year)
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse("<h1>static/index.html not found</h1>", status_code=404)
app.add_api_route("/", index, methods=["GET", "HEAD"])

@app.get("/manifest.json")
@app.get("/")
async def manifest():
    return JSONResponse(content=MANIFEST)

@app.get("/static/icon.jpg")
async def logo():
    return FileResponse("icon.jpg", media_type="image/jpeg")

# ══════════════════════════════════════════════════════════════
#  METADATA: IMDB/Kitsu → tytuł anime
# ══════════════════════════════════════════════════════════════

# ── Sezony serialu, które na animesub są osobnymi wpisami ─────────────
# Np. w Cinemeta/TVDB "Bleach" S17E01-E40 = cały Thousand-Year Blood War,
# a na animesub to osobna seria (podzielona na części po 13-14 odcinków).
# klucz: znormalizowany tytuł -> {sezon: (nowy tytuł, [(pierwszy, ostatni) odcinek części])}
SERIES_REDIRECTS = {
    "bleach": {
        17: ("Bleach: Thousand-Year Blood War", [(1, 13), (14, 26), (27, 60)]),
    },
}


def apply_series_redirect(meta: dict) -> dict:
    """Przekierowuje np. Bleach S17E02 -> TYBW część 1 odc. 2 (numer ciągły = 2)."""
    title, season, episode = meta.get("title"), meta.get("season"), meta.get("episode")
    if not title or season is None or episode is None:
        return meta
    cfg = SERIES_REDIRECTS.get(_normalize_title(title), {}).get(season)
    if not cfg:
        return meta
    new_title, parts = cfg
    for part_no, (first, last) in enumerate(parts, 1):
        if first <= episode <= last:
            log.info(
                f"[Redirect] {title} S{season}E{episode} -> {new_title} "
                f"część {part_no} odc. {episode - first + 1} (ciągły {episode})"
            )
            return {
                **meta,
                "title": new_title,
                "season": part_no,
                "episode": episode - first + 1,
                "abs_episode": episode,
            }
    return meta


def _year_of(value) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(m.group(0)) if m else None


def _pick_kitsu_alt_titles(data: list[dict], target_title: str, year: Optional[int]) -> list[str]:
    """Wybiera z wyników Kitsu właściwy film i zwraca jego tytuły (bez japońskiego pisma)."""
    target_norm = _normalize_title(target_title)
    for idx, item in enumerate(data or []):
        a = item.get("attributes", {})
        titles = dict.fromkeys(filter(None, [
            *(a.get("titles") or {}).values(), a.get("canonicalTitle"),
            *(a.get("abbreviatedTitles") or []),
        ]))
        norms = {_normalize_title(t) for t in titles}
        item_year = _year_of(a.get("startDate"))
        same_title = target_norm in norms
        year_ok = bool(year and item_year and abs(year - item_year) <= 1)
        # Dokładny tytuł (+ zgodny rok, jeśli znany) albo najlepszy wynik z pasującym rokiem
        if (same_title and (year_ok or not year or not item_year)) or (idx == 0 and year_ok):
            return [t for t in titles if t.isascii() and _normalize_title(t) != target_norm]
    return []


async def fetch_alt_titles(title: str, year: Optional[int]) -> list[str]:
    """Alternatywne tytuły (romaji itd.) – animesub często nazywa filmy inaczej niż Cinemeta."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(
                "https://kitsu.io/api/edge/anime",
                params={"filter[text]": title, "page[limit]": 5},
                headers={"Accept": "application/vnd.api+json"},
            )
            if resp.status_code == 200:
                alts = _pick_kitsu_alt_titles(resp.json().get("data", []), title, year)
                log.info(f"[Kitsu] alternatywne tytuły dla \"{title}\": {alts}")
                return alts
    except Exception as e:
        log.warning(f"[Kitsu] Nie udało się pobrać alternatywnych tytułów: {e}")
    return []


def _split_season_from_title(title: str) -> tuple[str, Optional[int]]:
    """'Attack on Titan Season 2' -> ('Attack on Titan', 2). Tylko jawne oznaczenia."""
    m = re.search(r"[\s:]*\b(?:Season|Series)\s*(\d+)\s*$", title, re.I)
    if not m:
        m = re.search(r"[\s:]*\b(\d+)(?:st|nd|rd|th)\s+Season\s*$", title, re.I)
    if m:
        base = title[:m.start()].strip(" :-")
        if base:
            return base, int(m.group(1))
    return title, None


async def get_meta_info(content_type: str, content_id: str) -> dict:
    """
    Pobiera tytuł i info o sezonie/odcinku.
    Obsługuje zarówno IMDB (tt...) jak i Kitsu (kitsu:...) ID.
    Używa Cinemeta (nie wymaga klucza API).

    abs_episode = numer odcinka liczony przez wszystkie poprzednie sezony
    (animesub często numeruje odcinki ciągiem, np. One Piece ep 1015).
    """
    parts = content_id.split(":")
    prefix = parts[0]

    result = {
        "title": None, "year": None,
        "season": None, "episode": None, "abs_episode": None,
        "imdb_id": None, "kitsu_id": None,
        "is_movie": content_type == "movie", "alt_titles": [],
    }

    if prefix == "kitsu":
        kitsu_id = parts[1]
        result["kitsu_id"] = kitsu_id
        result["season"] = 1
        result["episode"] = int(parts[2]) if len(parts) >= 3 else None

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"https://kitsu.io/api/edge/anime/{kitsu_id}",
                    headers={
                        "Accept": "application/vnd.api+json",
                        "Content-Type": "application/vnd.api+json",
                    },
                )
                if resp.status_code == 200:
                    anime = resp.json()["data"]["attributes"]
                    titles = anime.get("titles", {})
                    full_title = (
                        titles.get("en") or titles.get("en_jp")
                        or anime.get("canonicalTitle") or titles.get("ja_jp")
                    )
                    if full_title:
                        # Wpis Kitsu to zwykle jeden sezon: "Title Season 2"
                        base, season_from_title = _split_season_from_title(full_title)
                        result["title"] = base
                        if season_from_title:
                            result["season"] = season_from_title
                    start_date = anime.get("startDate", "")
                    result["year"] = int(start_date[:4]) if start_date else None
                    log.info(f"[Kitsu] {result['title']} S{result['season']} ({result['year']})")
        except Exception as e:
            log.error(f"[Kitsu] Błąd: {e}")
        result["abs_episode"] = result["episode"]
    else:
        result["imdb_id"] = parts[0]
        if content_type == "series" and len(parts) >= 3:
            result["season"] = int(parts[1])
            result["episode"] = int(parts[2])

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"https://v3-cinemeta.strem.io/meta/{content_type}/{result['imdb_id']}.json"
                )
                if resp.status_code == 200:
                    meta = resp.json().get("meta", {})
                    result["title"] = meta.get("name")
                    result["year"] = meta.get("year")
                    log.info(f"[Cinemeta] {result['title']} ({result['year']})")

                    # Numer absolutny = odcinki z poprzednich sezonów + bieżący
                    if result["season"] and result["episode"]:
                        eps = set()
                        for v in meta.get("videos") or []:
                            try:
                                eps.add((int(v.get("season")), int(v.get("episode"))))
                            except (TypeError, ValueError):
                                continue
                        before = sum(1 for sn, _ in eps if 1 <= sn < result["season"])
                        result["abs_episode"] = before + result["episode"]
        except Exception as e:
            log.error(f"[Cinemeta] Błąd: {e}")
        if result["abs_episode"] is None:
            result["abs_episode"] = result["episode"]
        result = apply_series_redirect(result)

    if result["is_movie"] and result["title"]:
        result["alt_titles"] = await fetch_alt_titles(result["title"], _year_of(result["year"]))

    return result


# ══════════════════════════════════════════════════════════════
#  WYSZUKIWANIE na animesub.info
# ══════════════════════════════════════════════════════════════

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Charset": "ISO-8859-2,utf-8;q=0.7,*;q=0.3",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl,en;q=0.9",
}


NEGATIVE_CACHE_TTL = 60  # puste wyniki pamiętamy tylko krótko
_http_sem: Optional[asyncio.Semaphore] = None


def _sem() -> asyncio.Semaphore:
    global _http_sem
    if _http_sem is None:
        _http_sem = asyncio.Semaphore(4)
    return _http_sem


async def _fetch_page(client: httpx.AsyncClient, params: dict) -> Optional[httpx.Response]:
    """Pobiera stronę wyników; 1 ponowienie przy błędzie sieci lub 5xx. None = nieudane."""
    for attempt in range(2):
        try:
            async with _sem():
                resp = await client.get(SEARCH_URL, params=params, headers=COMMON_HEADERS)
            if resp.status_code < 500:
                return resp
            log.warning(f"[Szukanie] HTTP {resp.status_code} (próba {attempt + 1})")
        except Exception as e:
            log.warning(f"[Szukanie] Błąd sieci: {type(e).__name__}: {e} (próba {attempt + 1})")
        await asyncio.sleep(0.4)
    return None


async def _search_one_sort(
    client: httpx.AsyncClient, title: str, title_type: str, sort: str
) -> tuple[list[dict], bool]:
    """Zwraca (wyniki, czy_był_błąd) dla jednego sortowania (do 3 stron)."""
    out, seen, failed = [], set(), False
    for page in range(3):
        params = {"szukane": title, "pTitle": title_type, "pSortuj": sort}
        if page > 0:
            params["od"] = page

        resp = await _fetch_page(client, params)
        if resp is None:
            failed = True
            break
        results = _parse_search_results(resp.content.decode("iso-8859-2", errors="replace"))
        if not results:
            break  # brak wyników = koniec stron

        new_count = 0
        for r in results:
            if r["id"] not in seen:
                seen.add(r["id"])
                r["sort"], r["page"] = sort, page
                out.append(r)
                new_count += 1
        log.info(f"[Szukanie] sort={sort} page={page} → {len(results)} wyników ({new_count} nowych)")
        if new_count == 0:
            break
    return out, failed


async def search_subtitles(title: str, title_type: str = "en") -> list[dict]:
    """
    Szuka napisów na animesub.info (2 sortowania równolegle, do 3 stron każde).
    Wyniki z błędem sieci NIE trafiają do cache, a puste tylko na chwilę –
    inaczej jedna chwilowa awaria blokowałaby napisy na 30 minut.
    """
    cache_key = f"{title}:{title_type}"
    cached = search_cache.get(cache_key)
    if cached and time.time() - cached["timestamp"] < cached.get("ttl", CACHE_TTL):
        log.info(f"[Cache hit] {title}")
        return cached["results"]

    log.info(f'[Szukanie] "{title}" (typ: {title_type})')
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            parts = await asyncio.gather(
                _search_one_sort(client, title, title_type, "pobrn"),
                _search_one_sort(client, title, title_type, "t_ang"),
            )
    except Exception as e:
        log.error(f"[Szukanie] Błąd: {e}")
        return []

    all_results, seen_ids, failed = [], set(), False
    for results, part_failed in parts:
        failed = failed or part_failed
        for r in results:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                all_results.append(r)

    if failed:
        log.warning(f'[Szukanie] "{title}": były błędy sieci – wyniki nie trafiają do cache')
    elif all_results:
        search_cache[cache_key] = {"results": all_results, "timestamp": time.time(), "ttl": CACHE_TTL}
    else:
        search_cache[cache_key] = {
            "results": [], "timestamp": time.time(), "ttl": NEGATIVE_CACHE_TTL,
        }
    log.info(f"[Znaleziono] {len(all_results)} napisów łącznie")
    return all_results


def _parse_search_results(html: str) -> list[dict]:
    """
    Parsuje HTML wyników wyszukiwania animesub.info.
    Struktura: table.Napisy > tr.KNap (3 wiersze) + tr.KKom (formularz)
    """
    soup = BeautifulSoup(html, "html.parser")
    subtitles = []

    for table in soup.find_all(
        "table", class_="Napisy",
        style=lambda s: s and "text-align:center" in s
    ):
        try:
            rows = table.find_all("tr", class_="KNap")
            if len(rows) < 3:
                continue

            # Wiersz 1: tytuł oryginalny, data, format
            r1 = rows[0].find_all("td")
            title_org = r1[0].get_text(strip=True) if len(r1) > 0 else ""
            format_type = r1[3].get_text(strip=True) if len(r1) > 3 else ""

            # Wiersz 2: tytuł angielski, autor
            r2 = rows[1].find_all("td")
            title_eng = r2[0].get_text(strip=True) if len(r2) > 0 else ""
            author_el = r2[1].find("a") if len(r2) > 1 else None
            author = (
                author_el.get_text(strip=True) if author_el
                else r2[1].get_text(strip=True).lstrip("~") if len(r2) > 1
                else ""
            )

            # Wiersz 3: tytuł alternatywny, pobrania
            r3 = rows[2].find_all("td")
            title_alt = r3[0].get_text(strip=True) if len(r3) > 0 else ""
            download_count = 0
            if len(r3) > 3:
                m = re.match(r"(\d+)", r3[3].get_text(strip=True))
                if m:
                    download_count = int(m.group(1))

            # Formularz pobierania w tr.KKom
            dl_row = table.find("tr", class_="KKom")
            if not dl_row:
                continue
            form = dl_row.find("form", attrs={"method": "POST"})
            if not form:
                continue

            id_inp = form.find("input", attrs={"name": "id"})
            sh_inp = form.find("input", attrs={"name": "sh"})
            if not id_inp or not sh_inp:
                continue

            sub_id = id_inp.get("value", "")
            dl_hash = sh_inp.get("value", "")
            if not sub_id or not dl_hash:
                continue

            # Opis
            desc_cell = dl_row.find("td", class_="KNap", attrs={"align": "left"})
            description = desc_cell.get_text(strip=True) if desc_cell else ""

            ep_info = _parse_episode_info(title_org, title_eng, title_alt)

            subtitles.append({
                "id": sub_id, "hash": dl_hash,
                "title_org": title_org, "title_eng": title_eng,
                "title_alt": title_alt, "author": author,
                "format_type": format_type,
                "download_count": download_count,
                "description": description,
                **ep_info,
            })
        except Exception as e:
            log.warning(f"Błąd parsowania tabeli: {e}")

    return subtitles


_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}

_EP_RE = re.compile(
    r"(?<![a-z])(?:episode|ep|odc(?:inek)?)\.?\s*(\d{1,4})"
    r"(?:\s*[-~–]\s*(?:(?:episode|ep|odc(?:inek)?)\.?\s*)?(\d{1,4}))?(?!\d)",
    re.I,
)


def _parse_episode_info(title_org: str, title_eng: str, title_alt: str) -> dict:
    """
    Wyciąga sezon, odcinek (także zakres ep01-12) i znacznik 'późniejszy sezon'
    (np. 'Final Season') z tytułów.
    """
    season = None
    episode = None
    episode_end = None
    later_season = False

    for title in [title_org, title_eng, title_alt]:
        if not title:
            continue

        if episode is None:
            m = _EP_RE.search(title)
            if m:
                episode = int(m.group(1))
                episode_end = int(m.group(2)) if m.group(2) else episode
                if episode_end < episode:
                    episode_end = episode

        if season is None:
            m = (
                re.search(r"\bseason\s*(\d+)\b", title, re.I)
                or re.search(r"\b(\d+)(?:st|nd|rd|th)\s+season\b", title, re.I)
                or re.search(r"\bS(\d{1,2})\b", title, re.I)
            )
            if m:
                season = int(m.group(1))

        if season is None:
            # "Title 2 ep 05"
            m = re.search(r"(?:^|\s)([1-9])\s+(?:ep|episode)\s*\d+", title, re.I)
            if m:
                season = int(m.group(1))

        if season is None:
            # "Title II ep 05"
            m = re.search(r"\s(II|III|IV|V|VI)\s+(?:ep|episode)\s*\d+", title, re.I)
            if m:
                season = _ROMAN[m.group(1).lower()]

        if re.search(r"\bfinal\s+season\b", title, re.I):
            later_season = True

    if season is not None:
        later_season = False

    return {
        "season": season, "episode": episode,
        "episode_end": episode_end, "later_season": later_season,
    }


# ══════════════════════════════════════════════════════════════
#  STRATEGIE WYSZUKIWANIA
# ══════════════════════════════════════════════════════════════

def _movie_strategies(titles: list[str]) -> list[dict]:
    """Zapytania dla filmów: każdy tytuł (też alternatywny) w kilku wariantach zapisu."""
    out = []

    def add(kind: str, q: str):
        item = {"type": kind, "query": q}
        if q and item not in out:
            out.append(item)

    for t in dict.fromkeys(filter(None, titles)):
        base = re.sub(r"\s+", " ", t).strip()
        variants = [
            base.rstrip(" .!?"),                      # "Your Name." -> "Your Name"
            base,
            re.sub(r"\s+", " ", base.replace(":", " ").replace("-", " ")).strip().rstrip(" .!?"),
            base.split(":")[0].strip(),               # część przed dwukropkiem
        ]
        for v in dict.fromkeys(variants):
            if len(v) >= 3:
                add("en", v)
                add("org", v)
    return out[:MAX_STRATEGIES]


def generate_search_strategies(
    title: str, season: Optional[int], episode: Optional[int],
    abs_episode: Optional[int] = None, alt_titles: Optional[list[str]] = None,
) -> list[dict]:
    """Lista strategii wyszukiwania — od najdokładniejszej do najszerszej."""
    if episode is None:
        return _movie_strategies([title] + list(alt_titles or []))
    strategies = []
    # Warianty tytułu: oryginalny (z myślnikami, np. "Thousand-Year") i bez myślników
    variants = list(dict.fromkeys([
        re.sub(r"\s+", " ", title).strip(),
        re.sub(r"\s+", " ", title.replace("-", " ")).strip(),
        re.sub(r"\s+", " ", title.replace(":", " ")).strip(),
        re.sub(r"\s+", " ", re.sub(r"[:\-]", " ", title)).strip(),
    ]))

    def add(kind: str, query: str):
        item = {"type": kind, "query": query}
        if item not in strategies:
            strategies.append(item)

    def add_episode_variants(num: int):
        # Ze spacją i bez (animesub rozróżnia!). Bez dopełnienia zerami też:
        # "ep 5" / "ep5" obok "ep 05" / "ep05" (długie serie: "ep202").
        for form in dict.fromkeys([str(num).zfill(2), str(num)]):
            for clean in variants:
                add("org", f"{clean} ep {form}")
                add("en", f"{clean} ep {form}")
                add("org", f"{clean} ep{form}")
                add("en", f"{clean} ep{form}")

    # Nazwy używane na animesub (np. romaji, angielska bez dwukropka) – szukamy też pod nimi
    tnorm = _normalize_title(title)
    for key, cfg in ARC_SERIES.items():
        if key not in tnorm or not cfg.get("search_names"):
            continue
        arcs = (cfg.get("search_arcs") or {}).get(season or 1) or []
        if isinstance(arcs, str):
            arcs = [arcs]
        nums = [n for n in (episode, abs_episode) if n is not None]
        for name in cfg["search_names"]:
            # nazwa z częścią: "Name - Arc" i "Name Arc"
            named = [f"{name} - {a}" for a in arcs] + [f"{name} {a}" for a in arcs]
            # najpierw formy z zerem (ep 05) dla obu numerów, dopiero potem bez (ep 5)
            for padded in (True, False):
                for n in dict.fromkeys(nums):
                    form = str(n).zfill(2) if padded else str(n)
                    if not padded and form == str(n).zfill(2):
                        continue
                    for base in named + [name]:
                        add("org", f"{base} ep {form}")
                        add("org", f"{base} ep{form}")
            for base in named + [name]:
                add("org", base)

    if episode is not None:
        ep_padded = str(episode).zfill(2)

        if season and season > 1:
            for clean in variants:
                add("en", f"{clean} Season {season} ep {ep_padded}")
                add("org", f"{clean} {season} ep {ep_padded}")

        # Numeracja ciągła (Bleach ep202, One Piece ep1015) – animesub często
        # nie dzieli takich serii na sezony, więc szukamy po numerze absolutnym.
        if abs_episode is not None and abs_episode != episode:
            add_episode_variants(abs_episode)

        add_episode_variants(episode)

        if season and season > 1:
            for clean in variants:
                add("en", f"{clean} Season {season}")

    for clean in variants:
        add("org", clean)
        add("en", clean)
    return strategies[:MAX_STRATEGIES]


def _normalize_title(title: str) -> str:
    """Normalizuje tytuł do porównywania: lowercase, bez interpunkcji, bez zbędnych spacji."""
    t = title.lower()
    t = re.sub(r"[:\-–—.,!?'\"()\[\]{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_TITLE_NOISE_RES = [
    r"\b(?:ep|episode|odc|odcinek)\s*\d+",
    r"\b\d+(?:st|nd|rd|th)\s+season\b", r"\bseason\s*\d+", r"\bfinal\s+season\b",
    r"\bpart\s*\d+", r"\bcour\s*\d+", r"\bs\d+\b",
    r"\b(?:ii|iii|iv|v|vi)\b", r"\btv\b", r"\barc\b", r"\d+",
]


def _has_extra_title_words(remainder: str) -> bool:
    """
    True, gdy po szukanym tytule zostają jeszcze jakieś słowa poza numerem
    odcinka/sezonu – czyli to inna seria ("Bleach Sennen Kessen hen" vs "Bleach").
    """
    r = remainder
    for pat in _TITLE_NOISE_RES:
        r = re.sub(pat, " ", r)
    return bool(re.search(r"[^\W\d_]", r))


_MOVIE_WORDS_RE = re.compile(r"\b(?:movie|film|gekijouban|eiga|the)\b")


def _title_matches(sub_titles: list[str], target_title: str, movie: bool = False) -> bool:
    """
    Sprawdza czy napisy pasują do szukanego anime.
    Odrzuca: inne serie, openingi, endingi, filmy, OVA, spin-offy.
    """
    target_norm = _normalize_title(target_title)

    non_episode_markers = {"opening", "ending", "op", "ed", "ost", "soundtrack",
                           "amv", "trailer", "pv", "movie", "film", "ova", "special"}
    if movie:
        # Szukamy filmu – słowa "movie"/"film"/"gekijouban" w tytule są OK
        non_episode_markers = non_episode_markers - {"movie", "film"}
    different_series = {"shippuuden", "shippuden", "shipuden", "sd", "boruto",
                        "next generations", "rock lee"}

    # Faza 1: sprawdź WSZYSTKIE tytuły pod kątem markerów
    for raw_title in sub_titles:
        if not raw_title:
            continue
        sub_norm = _normalize_title(raw_title)
        sub_words = set(sub_norm.split())

        # Odrzuć openingi / endingi typu Ending3, Opening2, OP05, ED12
        if re.search(
            r"\b(?:opening|ending|op|ed)\s*\d+\b",
            sub_norm,
            re.I,
        ):
            log.debug(f"[Filter] Odrzucam OP/ED: {raw_title}")
            return False

        # Odrzuć filmy (chyba że szukamy filmu)
        if not movie and re.search(r"\b(?:gekijouban|movie|film)\b", sub_norm, re.I):
            log.debug(f"[Filter] Odrzucam film: {raw_title}")
            return False

        if sub_words & non_episode_markers:
            log.debug(f"[Filter] Odrzucam (marker): {raw_title}")
            return False

    # Faza 2: sprawdź czy którykolwiek tytuł pasuje
    for raw_title in sub_titles:
        if not raw_title:
            continue
        sub_norm = _normalize_title(raw_title)

        if sub_norm.startswith(target_norm):
            remainder = sub_norm[len(target_norm):].strip()
            if any(remainder.startswith(ds) for ds in different_series):
                log.debug(f"[Filter] Odrzucam (inna seria): {raw_title}")
                return False
            if _has_extra_title_words(_MOVIE_WORDS_RE.sub(" ", remainder) if movie else remainder):
                log.debug(f"[Filter] Inna seria (dodatkowe słowa w tytule): {raw_title}")
                continue
            return True

        if target_norm in sub_norm:
            idx = sub_norm.index(target_norm)
            if idx == 0 or sub_norm[idx - 1] == " ":
                after = sub_norm[idx + len(target_norm):]
                if not after or after[0] == " ":
                    before = sub_norm[:idx].strip()
                    if before and before.split()[-1] in {"boruto", "sd"}:
                        return False
                    if _has_extra_title_words(_MOVIE_WORDS_RE.sub(" ", after) if movie else after):
                        log.debug(f"[Filter] Inna seria (dodatkowe słowa w tytule): {raw_title}")
                        continue
                    return True

    return False


def episode_score(
    s: dict, target_season: Optional[int], target_episode: Optional[int],
    abs_episode: Optional[int] = None,
) -> Optional[int]:
    """
    Ocena dopasowania napisów do sezonu/odcinka.
    None = odrzucone; wyżej = lepsze dopasowanie.
    Odcinek MUSI się zgadzać, a sezon nie może być inny niż szukany.
    """
    if target_episode is None:
        return 5

    ep = s.get("episode")
    if ep is None:
        return None
    ep_end = s.get("episode_end") or ep
    is_single = ep == ep_end

    def hit(n: Optional[int]) -> bool:
        return n is not None and ep <= n <= ep_end

    season = s.get("season")
    later = s.get("later_season")
    base = 10 if is_single else 2  # paczki wieloodcinkowe niżej niż pojedyncze

    if target_season in (None, 1):
        # S1 – odrzuć napisy z jawnie innego sezonu ("2nd Season", "Final Season")
        if (season not in (None, 1)) or later:
            return None
        return base + (5 if season == 1 else 0) if hit(target_episode) else None

    # Sezon >= 2
    if season is not None:
        if season != target_season:
            return None
        if hit(target_episode):
            return base + 5
        # Sezon się zgadza, ale numeracja ciągła ("Ketsubetsu Tan ep 18" = S2E5)
        if abs_episode is not None and hit(abs_episode):
            return base + 3
        return None

    if later:
        # np. "Final Season ep 05" – numeracja w obrębie sezonu
        return base if hit(target_episode) else None

    # Brak informacji o sezonie → to albo S1, albo numeracja ciągła.
    # Akceptujemy tylko numer absolutny (inaczej dostaniemy odcinek z S1).
    if abs_episode is not None and abs_episode != target_episode and hit(abs_episode):
        return max(base - 1, 2)
    # Ostatnia deska ratunku: numer się zgadza, ale nie wiadomo z którego sezonu.
    # Wynik 1 = "niepewne"; handler pokaże takie napisy tylko gdy nie ma lepszych.
    return 1 if (ALLOW_UNCERTAIN_SEASON and hit(target_episode)) else None


# ── Serie, w których sezony mają nazwy części zamiast numerów ──────────
# klucz: fragment znormalizowanego tytułu ze Stremio
# aliasy: inne nazwy serii na animesub (zamieniane na nazwę ze Stremio)
# arcs: (regex nazwy części, sezon w Stremio, przesunięcie numeru odcinka w sezonie)
ARC_SERIES = {
    "thousand year blood war": {
        # Jak seria jest nazwana na animesub (do zapytań wyszukiwania)
        "search_names": [
            "Bleach Sennen Kessen Hen",
            "Bleach: Sennen Kessen-hen",
            "Bleach: Thousand-Year Blood War",
            "Bleach Thousand-Year Blood War",
            "Bleach Thousand Year Blood War",
        ],
        "search_arcs": {
            1: ["Arc"],
            2: ["Arc - The Separation", "Ketsubetsu Tan", "The Separation"],
            3: ["Arc - The Conflict", "Soukoku Tan", "The Conflict"],
        },
        "aliases": [r"bleach\s*[:\-]?\s*sennen\s*kessen[\s-]*hen"],
        "arcs": [
            (r"the\s*separation|ketsubetsu[\s-]*tan", 2, 0),
            (r"the\s*conflict|soukoku[\s-]*tan", 3, 0),
        ],
    },
    "jojo": {
        "aliases": [r"jojo\s*no\s*kimyou\s*na\s*bouken", r"jojo\s*no\s*kimyo+\s*na\s*b[oō]+ken"],
        "arcs": [
            (r"phantom\s*blood", 1, 0),
            (r"battle\s*tendency", 1, 9),
            (r"stardust\s*crusaders?\s*[:\-]?\s*egypt(?:\s*hen)?|egypt\s*hen", 2, 24),
            (r"stardust\s*crusaders?", 2, 0),
            (r"diamond\s*(?:is\s*unbreakable|wa\s*kudakenai)", 3, 0),
            (r"golden\s*wind|vento\s*aureo|ougon\s*no\s*kaze", 4, 0),
            (r"stone\s*ocean", 5, 0),
        ],
    },
}


def _arc_rewrite(sub: dict, target_title: str) -> dict:
    """
    Dla serii z nazwanymi częściami: usuwa nazwę części z tytułów (żeby filtr tytułu
    nie uznał jej za inną serię), ustawia sezon i koryguje numer odcinka.
    """
    tnorm = _normalize_title(target_title)
    for key, cfg in ARC_SERIES.items():
        if key not in tnorm:
            continue
        titles = [sub.get("title_org", ""), sub.get("title_eng", ""), sub.get("title_alt", "")]
        arc = None
        new_titles = []
        for t in titles:
            nt = t or ""
            for al in cfg["aliases"]:
                nt = re.sub(al, lambda _m: target_title, nt, flags=re.I)
            for pat, sn, off in cfg["arcs"]:
                m = re.search(pat, nt, re.I)
                if m:
                    arc = arc or (sn, off)
                    nt = (nt[:m.start()] + " " + nt[m.end():]).strip(" :-")
                    break
            new_titles.append(nt)
        out = {**sub, "title_org": new_titles[0], "title_eng": new_titles[1], "title_alt": new_titles[2]}
        if arc:
            sn, off = arc
            out["season"] = sn
            out["later_season"] = False
            if out.get("episode") is not None:
                out["episode"] += off
                out["episode_end"] = (out.get("episode_end") or out["episode"] - off) + off
        return out
    return sub


def match_subtitles(
    subs: list[dict], target_title: str,
    target_season: Optional[int], target_episode: Optional[int],
    abs_episode: Optional[int] = None,
    alt_titles: Optional[list[str]] = None, movie: bool = False,
) -> list[dict]:
    """Filtruje napisy po tytule (także alternatywnych), sezonie i odcinku."""
    matched = []
    all_targets = [target_title] + [t for t in (alt_titles or []) if t and t != target_title]
    for s in subs:
        s = _arc_rewrite(s, target_title)
        # Filtr tytułu — najważniejszy
        sub_titles = [s.get("title_org", ""), s.get("title_eng", ""), s.get("title_alt", "")]
        if not any(_title_matches(sub_titles, t, movie=movie) for t in all_targets):
            log.debug(f"[Odrzucono: tytuł] {sub_titles} vs {all_targets}")
            continue

        score = episode_score(s, target_season, target_episode, abs_episode)
        if score is None:
            log.info(
                f"[Odrzucono: sezon/odcinek] \"{s.get('title_org') or s.get('title_eng')}\" "
                f"(wykryto S{s.get('season')} E{s.get('episode')}-{s.get('episode_end')}, "
                f"szukano S{target_season} E{target_episode}, abs {abs_episode})"
            )
            continue

        matched.append({**s, "score": score})
    return matched


# ══════════════════════════════════════════════════════════════
#  KONWERSJA ASS → SRT
# ══════════════════════════════════════════════════════════════

def _ass_time_to_ms(value: str) -> int:
    """Konwertuje czas ASS/SSA do milisekund."""
    match = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3})\s*", value)
    if not match:
        raise ValueError(f"Nieprawidłowy czas ASS: {value!r}")

    hours, minutes, seconds, fraction = match.groups()
    minutes_i = int(minutes)
    seconds_i = int(seconds)
    if minutes_i > 59 or seconds_i > 59:
        raise ValueError(f"Nieprawidłowy czas ASS: {value!r}")

    milliseconds = int(fraction.ljust(3, "0")[:3])
    return (((int(hours) * 60) + minutes_i) * 60 + seconds_i) * 1000 + milliseconds


def _ms_to_srt(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _strip_ass_tags(text: str) -> str:
    """
    Usuwa tagi ASS i fragmenty rysunkowe \\p1...\\p0.

    Wcześniej po usunięciu samego tagu {\\p1} komendy wektorowe mogły
    trafić do SRT jako zwykły tekst.
    """
    tag_pattern = re.compile(r"\{([^}]*)\}")
    drawing_pattern = re.compile(r"\\p(\d+)", re.I)

    output = []
    cursor = 0
    drawing_mode = 0

    for match in tag_pattern.finditer(text):
        if drawing_mode == 0:
            output.append(text[cursor:match.start()])

        drawing_tags = drawing_pattern.findall(match.group(1))
        if drawing_tags:
            drawing_mode = int(drawing_tags[-1])

        cursor = match.end()

    if drawing_mode == 0:
        output.append(text[cursor:])

    result = "".join(output)
    result = result.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return "\n".join(line.strip() for line in result.splitlines() if line.strip()).strip()

def _deoverlap_srt(srt_text: str, gap_ms: int = 50) -> str:
    """
    Przycina czas końca napisu, jeśli nachodzi na następny.

    ASS obsługuje równoległe warstwy i pozycjonowanie, SRT nie.
    Po konwersji kilka eventów może więc zostać wyświetlonych
    w tym samym miejscu. Ta funkcja ogranicza takie nakładanie.
    """

    # Normalizacja CRLF/LF
    text = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    blocks = re.split(r"\n\s*\n", text)
    parsed = []

    time_pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2},\d{3})"
    )

    def parse_time(value: str) -> int:
        h, m, rest = value.split(":")
        s, ms = rest.split(",")
        return (
            int(h) * 3_600_000
            + int(m) * 60_000
            + int(s) * 1000
            + int(ms)
        )

    def format_time(value: int) -> str:
        value = max(0, value)

        h, remainder = divmod(value, 3_600_000)
        m, remainder = divmod(remainder, 60_000)
        s, ms = divmod(remainder, 1000)

        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    for block in blocks:
        lines = block.strip().split("\n")

        if len(lines) < 3:
            continue

        match = time_pattern.search(lines[1])
        if not match:
            continue

        parsed.append({
            "start": parse_time(match.group(1)),
            "end": parse_time(match.group(2)),
            "text": "\n".join(lines[2:]).strip(),
        })

    parsed.sort(key=lambda item: (item["start"], item["end"]))

    for i in range(len(parsed) - 1):
        current = parsed[i]
        nxt = parsed[i + 1]

        if current["end"] <= nxt["start"]:
            continue

        # Jeśli następny napis faktycznie zaczyna się później,
        # kończymy obecny 50 ms wcześniej.
        if nxt["start"] > current["start"]:
            current["end"] = max(
                current["start"] + 1,
                nxt["start"] - gap_ms,
            )

    output = []

    for i, item in enumerate(parsed, 1):
        output.extend([
            str(i),
            f"{format_time(item['start'])} --> {format_time(item['end'])}",
            item["text"],
            "",
        ])

    return "\r\n".join(output).rstrip() + "\r\n"

def convert_ass_to_srt(ass_content: str) -> str:
    """Konwertuje ASS/SSA -> SRT bez zmieniania celowych overlapów."""
    lines = ass_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_events = False
    format_fields: list[str] = []
    dialogues = []
    seen = set()

    for raw_line in lines:
        line = raw_line.strip()
        lowered = line.lower()

        if lowered == "[events]":
            in_events = True
            format_fields = []
            continue

        if line.startswith("[") and line.endswith("]"):
            in_events = False
            continue

        if not in_events:
            continue

        if lowered.startswith("format:"):
            format_fields = [field.strip().lower() for field in line[7:].split(",")]
            continue

        if not lowered.startswith("dialogue:") or not format_fields:
            continue

        # maxsplit zachowuje przecinki znajdujące się w polu Text.
        values = line[9:].lstrip().split(",", len(format_fields) - 1)
        if len(values) != len(format_fields):
            continue

        event = dict(zip(format_fields, values))
        try:
            start_ms = _ass_time_to_ms(event["start"])
            end_ms = _ass_time_to_ms(event["end"])
        except (KeyError, ValueError):
            continue

        if end_ms <= start_ms:
            continue

        plain_text = _strip_ass_tags(event.get("text", ""))
        if not plain_text:
            continue

        # Warstwy ASS często duplikują ten sam tekst dla cienia/obrysu.
        key = (start_ms, end_ms, plain_text)
        if key in seen:
            continue
        seen.add(key)
        dialogues.append(key)

    if not dialogues:
        raise ValueError("ASS nie zawiera dialogów możliwych do konwersji")

    dialogues.sort(key=lambda item: (item[0], item[1], item[2]))

    output = []
    for index, (start_ms, end_ms, plain_text) in enumerate(dialogues, 1):
        output.extend([
            str(index),
            f"{_ms_to_srt(start_ms)} --> {_ms_to_srt(end_ms)}",
            plain_text,
            "",
        ])

    return "\r\n".join(output).rstrip() + "\r\n"


def _decode_subtitle_bytes(content: bytes) -> str:
    """Dekoduje najczęstsze kodowania napisów z AnimeSub."""
    for encoding in ("utf-8-sig", "windows-1250", "iso-8859-2"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("Nie udało się rozpoznać kodowania napisów")


def _looks_like_html(text: str) -> bool:
    sample = text.lstrip("\ufeff \t\r\n").lower()[:1500]
    return (
        sample.startswith("<!doctype html")
        or sample.startswith("<html")
        or "<html" in sample
        or "<body" in sample
    )


SRT_TIMING_RE = re.compile(
    r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+"
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*$"
)


def _detect_subtitle_format(text: str, extension: Optional[str] = None) -> Optional[str]:
    """Wykrywa format po zawartości; rozszerzenie jest tylko fallbackiem."""
    sample = text.lstrip("\ufeff \t\r\n")

    if (
        re.search(r"(?mi)^\s*\[events\]\s*$", sample)
        and re.search(r"(?mi)^\s*dialogue\s*:", sample)
    ):
        return "ass"

    if SRT_TIMING_RE.search(sample):
        return "srt"

    if sample.startswith("WEBVTT"):
        return "vtt"

    if re.search(r"(?m)^\{\d+\}\{\d+\}", sample):
        return "microdvd"

    if re.search(r"(?m)^\d{1,2}:\d{2}:\d{2}[:|]", sample):
        return "tmplayer"

    ext = (extension or "").lower().lstrip(".")
    return {
        "ass": "ass",
        "ssa": "ass",
        "srt": "srt",
        "sub": "microdvd",
    }.get(ext)


def _normalize_srt(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized.replace("\n", "\r\n") + "\r\n"


def _is_valid_srt(text: str) -> bool:
    if _looks_like_html(text):
        return False
    if re.search(r"(?mi)^\s*\[events\]\s*$", text):
        return False
    return bool(SRT_TIMING_RE.search(text))


def convert_microdvd_to_srt(content: str, fps: float = 23.976) -> str:
    """Konwertuje napisy MicroDVD ({start}{stop}tekst) do SRT."""
    pattern = re.compile(r"\{(\d+)\}\{(\d+)\}(.+)")
    dialogues = []

    for line in content.split("\n"):
        line = line.strip()
        m = pattern.match(line)
        if not m:
            continue
        start_frame, end_frame, text = int(m.group(1)), int(m.group(2)), m.group(3)

        # Pierwsza linia może zawierać info o FPS: {1}{1}23.976
        if start_frame <= 1 and end_frame <= 1:
            try:
                detected_fps = float(text.replace(",", "."))
                if 10 < detected_fps < 60:
                    fps = detected_fps
                    log.info(f"[MicroDVD] Wykryto FPS: {fps}")
            except ValueError:
                pass
            continue

        text = text.replace("|", "\n")

        def frames_to_time(frames: int) -> str:
            total_seconds = frames / fps
            h = int(total_seconds // 3600)
            m = int((total_seconds % 3600) // 60)
            s = int(total_seconds % 60)
            ms = int((total_seconds % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        dialogues.append({
            "start": frames_to_time(start_frame),
            "end": frames_to_time(end_frame),
            "text": text,
        })

    out = []
    for i, d in enumerate(dialogues, 1):
        out.extend([str(i), f"{d['start']} --> {d['end']}", d["text"], ""])
    return "\n".join(out)

def convert_tmplayer_to_srt(content: str) -> str:
    """Konwertuje napisy TMPlayer (HH:MM:SS:tekst) do SRT."""
    pattern = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[:\|](.+)")
    entries = []

    for line in content.split("\n"):
        line = line.strip()
        m = pattern.match(line)
        if not m:
            continue
        h, mi, s, text = m.groups()
        text = text.replace("|", "\n")
        start_sec = int(h) * 3600 + int(mi) * 60 + int(s)
        entries.append({"start_sec": start_sec, "text": text})

    # Wylicz end na podstawie następnej linijki + długości tekstu
    dialogues = []
    for i, e in enumerate(entries):
        # Bazowy czas trwania — zależny od długości tekstu (ok. 15 znaków/sekunda + 1s zapasu)
        text_len = len(e["text"].replace("\n", " "))
        natural_duration = max(1.5, min(7.0, text_len / 15 + 1))

        if i + 1 < len(entries):
            # Zostaw małą przerwę przed następną (0.1s)
            max_duration = entries[i + 1]["start_sec"] - e["start_sec"] - 0.1
            duration = min(natural_duration, max_duration) if max_duration > 0.5 else max_duration
            if duration < 0.5:
                duration = 0.5  # minimum
        else:
            duration = natural_duration

        end_sec = e["start_sec"] + duration

        def fmt(t: float) -> str:
            h_ = int(t // 3600)
            m_ = int((t % 3600) // 60)
            s_ = int(t % 60)
            ms_ = int((t % 1) * 1000)
            return f"{h_:02d}:{m_:02d}:{s_:02d},{ms_:03d}"

        dialogues.append({
            "start": fmt(e["start_sec"]),
            "end": fmt(end_sec),
            "text": e["text"],
        })

    out = []
    for i, d in enumerate(dialogues, 1):
        out.extend([str(i), f"{d['start']} --> {d['end']}", d["text"], ""])
    return "\n".join(out)

# ══════════════════════════════════════════════════════════════
#  PRZESUNIĘCIE CZASU (bez zmiany formatu)
# ══════════════════════════════════════════════════════════════

def _shift_srt(text: str, off: int) -> str:
    rx = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})([,.])(\d{1,3})")

    def sh(m):
        ms = ((int(m[1]) * 60 + int(m[2])) * 60 + int(m[3])) * 1000 + int(m[5].ljust(3, "0"))
        return ms + off

    def fmt(ms):
        ms = max(0, ms)
        h, r = divmod(ms, 3_600_000)
        mi, r = divmod(r, 60_000)
        se, ms = divmod(r, 1000)
        return f"{h:02d}:{mi:02d}:{se:02d},{ms:03d}"

    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        times = list(rx.finditer(lines[idx]))
        if len(times) < 2:
            continue
        start, end = sh(times[0]), sh(times[1])
        if end <= 0:
            continue
        lines[idx] = f"{fmt(start)} --> {fmt(end)}"
        out.append(lines[idx:])
    return "\r\n".join(
        f"{n}\r\n" + "\r\n".join(b) + "\r\n" for n, b in enumerate(out, 1)
    )


def _shift_vtt(text: str, off: int) -> str:
    rx = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")

    def sub(m):
        ms = ((int(m[1] or 0) * 60 + int(m[2])) * 60 + int(m[3])) * 1000 + int(m[4]) + off
        ms = max(0, ms)
        h, r = divmod(ms, 3_600_000)
        mi, r = divmod(r, 60_000)
        se, ms = divmod(r, 1000)
        return f"{h:02d}:{mi:02d}:{se:02d}.{ms:03d}"

    return "\n".join(
        rx.sub(sub, l) if "-->" in l else l for l in text.split("\n")
    )


def _shift_ass(text: str, off: int) -> str:
    rx = re.compile(r"\s*(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3})\s*")

    def parse(v):
        m = rx.fullmatch(v)
        if not m:
            return None
        return ((int(m[1]) * 60 + int(m[2])) * 60 + int(m[3])) * 1000 + int(m[4].ljust(3, "0")[:3])

    def fmt(ms):
        ms = max(0, ms)
        cs = round(ms / 10)
        h, r = divmod(cs, 360_000)
        mi, r = divmod(r, 6000)
        se, cs = divmod(r, 100)
        return f"{h}:{mi:02d}:{se:02d}.{cs:02d}"

    in_events, fields, out = False, [], []
    for line in text.split("\n"):
        low = line.strip().lower()
        if low == "[events]":
            in_events, fields = True, []
        elif low.startswith("[") and low.endswith("]"):
            in_events = False
        elif in_events and low.startswith("format:"):
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
        elif in_events and fields and low.startswith(("dialogue:", "comment:")):
            head, _, rest = line.partition(":")
            vals = rest.lstrip().split(",", len(fields) - 1)
            if len(vals) == len(fields) and "start" in fields and "end" in fields:
                si, ei = fields.index("start"), fields.index("end")
                st, en = parse(vals[si]), parse(vals[ei])
                if st is not None and en is not None:
                    if en + off <= 0:
                        continue
                    vals[si], vals[ei] = fmt(st + off), fmt(en + off)
                    line = f"{head}: " + ",".join(vals)
        out.append(line)
    return "\n".join(out)


def _shift_microdvd(text: str, off: int, fps: float = 23.976) -> str:
    rx = re.compile(r"^\{(\d+)\}\{(\d+)\}(.*)$")
    lines = text.split("\n")
    for l in lines[:5]:
        m = rx.match(l.strip())
        if m and int(m[1]) <= 1 and int(m[2]) <= 1:
            try:
                f = float(m[3].replace(",", "."))
                if 10 < f < 60:
                    fps = f
            except ValueError:
                pass
    delta = round(off / 1000 * fps)
    out = []
    for l in lines:
        m = rx.match(l.strip())
        if not m or (int(m[1]) <= 1 and int(m[2]) <= 1):
            out.append(l)
            continue
        st, en = int(m[1]) + delta, int(m[2]) + delta
        if en <= 0:
            continue
        out.append("{%d}{%d}%s" % (max(0, st), en, m[3]))
    return "\n".join(out)


def _shift_tmplayer(text: str, off: int) -> str:
    rx = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:|])(.*)$")
    delta = round(off / 1000)  # TMPlayer ma rozdzielczość 1 s
    out = []
    for l in text.split("\n"):
        m = rx.match(l.strip())
        if not m:
            out.append(l)
            continue
        t = (int(m[1]) * 60 + int(m[2])) * 60 + int(m[3]) + delta
        if t < 0:
            continue
        h, r = divmod(t, 3600)
        mi, se = divmod(r, 60)
        out.append(f"{h:02d}:{mi:02d}:{se:02d}{m[4]}{m[5]}")
    return "\n".join(out)


def shift_subtitles(text: str, fmt: str, offset_ms: int) -> str:
    """Przesuwa czasy napisów o offset_ms, zachowując oryginalny format."""
    if not offset_ms:
        return text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fn = {
        "srt": _shift_srt, "vtt": _shift_vtt, "ass": _shift_ass,
        "microdvd": _shift_microdvd, "tmplayer": _shift_tmplayer,
    }.get(fmt)
    return fn(text, offset_ms) if fn else text


# ══════════════════════════════════════════════════════════════
#  WYBÓR PLIKU Z ARCHIWUM (paczki wieloodcinkowe)
# ══════════════════════════════════════════════════════════════

_EXT_PRIORITY = {".srt": 0, ".vtt": 0, ".ass": 1, ".ssa": 1, ".sub": 2, ".txt": 3}


def _ext_priority(name: str) -> int:
    return _EXT_PRIORITY.get("." + name.rsplit(".", 1)[-1].lower(), 99)


def _episode_from_filename(name: str) -> tuple[Optional[int], Optional[int]]:
    """Zwraca (sezon, odcinek) wykryte w nazwie pliku; None gdy nie znaleziono."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", base)

    # Szum, który wyglądałby jak numer odcinka
    base = re.sub(r"\[[0-9a-f]{8}\]", " ", base, flags=re.I)          # CRC32
    base = re.sub(r"\((?:19|20)\d{2}\)", " ", base)                    # (2019)
    base = re.sub(r"(?<![0-9])(?:2160|1080|720|480|360)[pi]?(?![0-9])", " ", base, flags=re.I)
    base = re.sub(r"\b[xh]\.?26[45]\b|\bhevc\b|\b(?:8|10)\s?-?bit\b", " ", base, flags=re.I)
    base = re.sub(r"(?<=\d)v\d\b", "", base, flags=re.I)                # 05v2 -> 05

    m = re.search(r"(?<![a-z0-9])S(\d{1,2})[\s._-]*E(\d{1,4})(?!\d)", base, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(
        r"(?<![a-z])(?:episode|ep|odc(?:inek)?)[\s._#-]*(\d{1,4})(?!\d)"
        r"|(?<![a-z])e(\d{1,4})(?!\d)",
        base, re.I,
    )
    if m:
        return None, int(m.group(1) or m.group(2))

    nums = re.findall(r"(?<![0-9])(\d{1,4})(?![0-9])", base)
    if nums:
        return None, int(nums[-1])
    return None, None


def _pick_subtitle_file(
    names: list[str], season: Optional[int],
    episode: Optional[int], abs_episode: Optional[int],
) -> Optional[str]:
    """
    Wybiera z archiwum plik napisów dla konkretnego odcinka.
    Zwraca None, gdy w paczce jest kilka plików i żaden nie pasuje do odcinka
    (lepiej nie zwrócić nic niż napisy z innego odcinka).
    """
    if not names:
        return None

    # .txt (readme/info) pomijamy, jeśli są prawdziwe pliki napisów
    pool = [n for n in names if not n.lower().endswith(".txt")] or names

    if episode is None or len(pool) == 1:
        return min(pool, key=_ext_priority)

    parsed = [(n, *_episode_from_filename(n)) for n in pool]

    hits = [
        n for n, sn, ep in parsed
        if ep == episode and (sn is None or season is None or sn == season)
    ]
    if hits:
        return min(hits, key=_ext_priority)

    if abs_episode is not None and abs_episode != episode:
        hits = [n for n, _sn, ep in parsed if ep == abs_episode]
        if hits:
            return min(hits, key=_ext_priority)

    return None


# ══════════════════════════════════════════════════════════════
#  POBIERANIE NAPISÓW (proxy endpoint)
#
#  Kluczowa sprawa: animesub.info wiąże hash z sesją (ciasteczkami).
#  Trzeba:
#  1. Wejść na stronę wyszukiwania → ciasteczka + świeży hash
#  2. POSTnąć na sciagnij.php z tymi ciasteczkami i świeżym hashem
# ══════════════════════════════════════════════════════════════

@app.get("/subtitles/download")
async def download_subtitle(
    id: str, hash: str, query: str = "test", type: str = "org",
    sort: str = "pobrn", page: int = 0,
    season: Optional[int] = None, ep: Optional[int] = None, aep: Optional[int] = None,
):
    """Proxy do pobierania napisów z animesub.info."""
    log.info(f"[Download] id={id}, query={query}, season={season}, ep={ep}, aep={aep}")

    def find_hash(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        for form in soup.find_all("form"):
            method = (form.get("method") or "").lower()
            action = (form.get("action") or "").split("?", 1)[0].rstrip("/")
            if method != "post" or not action.endswith("sciagnij.php"):
                continue

            form_id = form.find("input", attrs={"name": "id"})
            if not form_id or form_id.get("value") != str(id):
                continue

            sh = form.find("input", attrs={"name": "sh"})
            if sh and sh.get("value"):
                return sh.get("value")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            cookies=httpx.Cookies(),
        ) as client:
            # Krok 1: ta sama sesja musi pobrać stronę i świeży hash.
            search_params = {"szukane": query, "pTitle": type, "pSortuj": sort}
            if page > 0:
                search_params["od"] = page
            search_full_url = f"{SEARCH_URL}?{urlencode(search_params)}"

            log.info("[Download] Krok 1: Pobieram stronę wyszukiwania")
            search_resp = await client.get(search_full_url, headers=COMMON_HEADERS)
            if search_resp.status_code != 200:
                log.error(f"[Download] Search HTTP {search_resp.status_code}")
                return PlainTextResponse("AnimeSub search failed", status_code=502)

            search_html = search_resp.content.decode("iso-8859-2", errors="replace")
            fresh_hash = find_hash(search_html)

            # Gdy wynik z listy nie zawiera już ID, spróbuj bezpośredniej strony wpisu.
            if not fresh_hash:
                direct_url = f"{SEARCH_URL}?ID={id}"
                log.info(f"[Download] Hash nie znaleziony na liście, próbuję {direct_url}")
                direct_resp = await client.get(direct_url, headers=COMMON_HEADERS)
                if direct_resp.status_code == 200:
                    direct_html = direct_resp.content.decode("iso-8859-2", errors="replace")
                    fresh_hash = find_hash(direct_html)
                    if fresh_hash:
                        search_full_url = str(direct_resp.url)

            if not fresh_hash:
                log.error(f"[Download] Brak świeżego hasha dla id={id}")
                return PlainTextResponse("Fresh AnimeSub hash not found", status_code=502)

            log.info(f"[Download] ✓ Świeży hash dla id={id}")

            # Krok 2: POST musi iść od razu po HTTPS. Przy HTTP -> HTTPS
            # redirect 301/302 POST może zostać zamieniony na GET.
            log.info("[Download] Krok 2: Pobieram napisy")
            dl_resp = await client.post(
                DOWNLOAD_URL,
                data={"id": id, "sh": fresh_hash, "single_file": "Pobierz napisy"},
                headers={
                    **COMMON_HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": search_full_url,
                    "Origin": ANIMESUB_BASE,
                },
            )

            if dl_resp.status_code != 200:
                log.error(
                    f"[Download] Download HTTP {dl_resp.status_code}; final_url={dl_resp.url}"
                )
                return PlainTextResponse("AnimeSub download failed", status_code=502)

            content = dl_resp.content
            content_type = dl_resp.headers.get("content-type", "")
            log.info(
                f"[Download] Pobrano {len(content)} bajtów; "
                f"content-type={content_type}; final_url={dl_resp.url}"
            )

            if not content:
                return PlainTextResponse("AnimeSub returned empty data", status_code=502)

            raw = content.decode("latin-1", errors="ignore")
            if "zabezpiecze" in raw or "Błąd" in raw or "B³±d" in raw:
                log.error("[Download] ✗ BŁĄD ZABEZPIECZEŃ")
                return PlainTextResponse("Security error", status_code=502)

            subtitle_ext: Optional[str] = None

            if zipfile.is_zipfile(io.BytesIO(content)):
                log.info("[Download] Rozpakowuję ZIP...")
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as zf:
                        all_files = [name for name in zf.namelist() if not name.endswith("/")]
                        subtitle_files = [
                            name for name in all_files
                            if re.search(r"\.(srt|ass|ssa|sub|txt)$", name, re.I)
                        ]
                        log.info(f"[Download] Pliki napisów w ZIP: {subtitle_files}")

                        if not subtitle_files:
                            return PlainTextResponse("No supported subtitle in ZIP", status_code=502)

                        sub_name = _pick_subtitle_file(subtitle_files, season, ep, aep)
                        if sub_name is None:
                            log.error(
                                f"[Download] ✗ Brak odcinka {ep} w archiwum: {subtitle_files}"
                            )
                            return PlainTextResponse("Episode not found in archive", status_code=404)
                        content = zf.read(sub_name)
                        subtitle_ext = "." + sub_name.rsplit(".", 1)[-1].lower()
                        log.info(f"[Download] Rozpakowano: {sub_name}")
                except zipfile.BadZipFile:
                    return PlainTextResponse("Bad ZIP", status_code=502)

            try:
                text = _decode_subtitle_bytes(content)
            except ValueError as exc:
                log.error(f"[Download] {exc}")
                return PlainTextResponse("Unsupported subtitle encoding", status_code=502)

            # Najważniejsza ochrona: nigdy nie wysyłaj strony PHP/HTML jako SRT.
            if _looks_like_html(text):
                log.error(
                    f"[Download] AnimeSub zwrócił HTML zamiast napisów; final_url={dl_resp.url}"
                )
                return PlainTextResponse("AnimeSub returned HTML instead of subtitles", status_code=502)

            subtitle_format = _detect_subtitle_format(text, subtitle_ext)
            log.info(f"[Download] Wykryty format: {subtitle_format}; ext={subtitle_ext}")

            # Konwertujemy TYLKO formaty, których Stremio nie obsługuje
            # (MicroDVD i TMPlayer -> SRT). SRT, ASS i VTT zostają bez zmian.
            # Zawsze: kodowanie -> UTF-8 (inaczej polskie znaki się psują)
            # i ujednolicenie końców linii.
            if subtitle_format is None:
                return PlainTextResponse("Unsupported subtitle format", status_code=415)

            text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")

            converted = False
            if subtitle_format in UNSUPPORTED_BY_STREMIO:
                try:
                    if subtitle_format == "microdvd":
                        text = convert_microdvd_to_srt(text)
                    else:
                        text = convert_tmplayer_to_srt(text)
                except Exception as exc:
                    log.error(f"[Download] Błąd konwersji {subtitle_format}->SRT: {exc}", exc_info=True)
                    return PlainTextResponse("Subtitle conversion failed", status_code=502)
                if not _is_valid_srt(text):
                    log.error(f"[Download] Konwersja {subtitle_format}->SRT dała pusty wynik")
                    return PlainTextResponse("Subtitle conversion failed", status_code=502)
                log.info(f"[Download] Skonwertowano {subtitle_format} -> srt (Stremio nie obsługuje)")
                subtitle_format = "srt"
                converted = True

            try:
                text = shift_subtitles(text, subtitle_format, SUBTITLE_OFFSET_MS)
            except Exception as exc:
                log.error(f"[Download] Błąd przesunięcia czasu: {exc}", exc_info=True)
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")

            media_types = {
                "ass": ("text/x-ssa", "ass"),
                "srt": ("application/x-subrip", "srt"),
                "vtt": ("text/vtt", "vtt"),
                "microdvd": ("text/plain", "sub"),
                "tmplayer": ("text/plain", "txt"),
            }
            media_type, out_ext = media_types.get(subtitle_format, ("text/plain", "txt"))

            log.info(
                f"[Download] ✓ Wysyłam {'po konwersji' if converted else 'bez konwersji'} "
                f"({len(text)} znaków, format={subtitle_format})"
            )
            return Response(
                content=text.encode("utf-8"),
                media_type=f"{media_type}; charset=utf-8",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Content-Disposition": f'inline; filename="subtitle.{out_ext}"',
                    "X-Subtitle-Source-Format": subtitle_format,
                },
            )

    except Exception as e:
        log.error(f"[Download] Błąd: {e}", exc_info=True)
        return PlainTextResponse("Download failed", status_code=500)


# ══════════════════════════════════════════════════════════════
#  DOPASOWANIE DO WYDANIA (nazwa pliku wideo ze Stremio)
# ══════════════════════════════════════════════════════════════

def _parse_extra(extra: str) -> dict:
    """'videoHash=a&filename=%5BGrp%5D%20x.mkv.json' -> {'videoHash': 'a', 'filename': '[Grp] x.mkv'}"""
    from urllib.parse import unquote
    extra = re.sub(r"\.json$", "", extra or "")
    out = {}
    for part in extra.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = unquote(v)
    return out


def _release_info(filename: Optional[str]) -> dict:
    """Wyciąga grupę i rozdzielczość z nazwy pliku wideo."""
    if not filename:
        return {}
    name = filename.rsplit("/", 1)[-1]
    group = None
    m = re.match(r"\s*\[([^\]]{2,40})\]", name)
    if m:
        group = m.group(1)
    else:
        m = re.search(r"-([A-Za-z0-9]{2,20})(?:\.[A-Za-z0-9]{2,4})?$", name)
        if m:
            group = m.group(1)
    res = re.search(r"(?<![0-9])(2160|1080|720|480)p?(?![0-9])", name)
    return {"group": group, "res": res.group(1) if res else None}


def release_bonus(sub: dict, release: dict) -> int:
    """Premia za napisy, które w opisie wspominają to samo wydanie co plik wideo."""
    if not release:
        return 0
    text = " ".join(
        str(sub.get(k) or "") for k in ("description", "title_org", "title_eng", "title_alt")
    ).lower()
    bonus = 0
    group = (release.get("group") or "").lower()
    if group and len(group) >= 3 and group in text:
        bonus += 2
    if release.get("res") and release["res"] in text:
        bonus += 1
    return bonus


# ══════════════════════════════════════════════════════════════
#  GŁÓWNY ENDPOINT NAPISÓW DLA STREMIO
# ══════════════════════════════════════════════════════════════

def format_priority(format_text: str) -> int:
    """
    2 = SRT / ASS / SSA / VTT (Stremio odtwarza bez konwersji) – zawsze na początku listy,
    1 = format nieznany, 0 = MicroDVD / TMPlayer itp. (wymagają konwersji).
    """
    f = (format_text or "").lower()
    if re.search(r"\bsrt\b|subrip|\bass\b|\bssa\b|substation|\bvtt\b|webvtt", f):
        return 2
    if re.search(r"microdvd|tmplayer|\bmpl2?\b|\bsub\b|\btxt\b|mpsub|subviewer", f):
        return 0
    return 1


@app.get("/subtitles/{content_type}/{content_id}/{extra:path}")
async def subtitles_handler_extra(content_type: str, content_id: str, extra: str = ""):
    return await subtitles_handler(content_type, content_id, _parse_extra(extra).get("filename"))


@app.get("/subtitles/{content_type}/{content_id}.json")
async def subtitles_handler(content_type: str, content_id: str, filename: Optional[str] = None):
    """Endpoint wywoływany przez Stremio."""
    log.info(f"\n[Request] type={content_type}, id={content_id}")

    try:
        meta = await get_meta_info(content_type, content_id)
        log.info(f'[Meta] title="{meta["title"]}", S{meta["season"]}E{meta["episode"]}')

        if not meta["title"]:
            return JSONResponse(content={"subtitles": []})

        is_movie = meta.get("is_movie", False)
        all_subs = []
        seen = set()
        deadline = time.monotonic() + SEARCH_BUDGET_S

        # Seriale: alternatywne tytuły (romaji itd.) pobieramy w tle, równolegle z 1. etapem
        alt_task = None
        if not is_movie:
            alt_task = asyncio.create_task(
                fetch_alt_titles(meta["title"], _year_of(meta.get("year")))
            )

        async def run_loop(strategies: list[dict], alt_titles: Optional[list[str]]) -> bool:
            """Wykonuje zapytania paczkami. Zwraca True, gdy można przestać szukać."""

            async def run_strategy(strat: dict):
                log.info(f'[Strategia] "{strat["query"]}" ({strat["type"]})')
                results = await search_subtitles(strat["query"], strat["type"])
                return match_subtitles(
                    results, meta["title"], meta["season"], meta["episode"], meta["abs_episode"],
                    alt_titles=alt_titles, movie=is_movie,
                )

            step = max(1, SEARCH_BATCH)
            for i in range(0, len(strategies), step):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("[Wynik] Przekroczono limit czasu szukania – zwracam, co znaleziono")
                    return True

                batch = strategies[i:i + step]
                tasks = [asyncio.create_task(run_strategy(st)) for st in batch]
                done, pending = await asyncio.wait(tasks, timeout=remaining)
                for t in pending:
                    t.cancel()

                exact = False
                for strat, t in zip(batch, tasks):  # w kolejności priorytetu strategii
                    if t not in done or t.cancelled() or t.exception() is not None:
                        continue
                    matched = t.result()
                    for sub in matched:
                        if sub["id"] not in seen:
                            seen.add(sub["id"])
                            all_subs.append({**sub, "sq": strat["query"], "st": strat["type"]})
                    if any(s["score"] >= 10 for s in matched):
                        exact = True

                if exact:
                    log.info("[Znaleziono] Dokładne dopasowanie")
                    return True
                if sum(1 for x in all_subs if x["score"] > 1) >= 5:
                    return True
                if is_movie and len(all_subs) >= 3:
                    return True
                if pending:
                    log.warning("[Wynik] Przekroczono limit czasu szukania – zwracam, co znaleziono")
                    return True
            return False

        strategies = generate_search_strategies(
            meta["title"], meta["season"], meta["episode"], meta["abs_episode"],
            meta.get("alt_titles"),
        )
        stopped = await run_loop(strategies, meta.get("alt_titles"))

        # Etap 2 (seriale): nic nie znaleziono pod tytułem z Cinemeta/Kitsu -> spróbuj tytułów alternatywnych
        if alt_task is not None:
            if not any(x["score"] > 1 for x in all_subs) and not stopped:
                try:
                    alts = await asyncio.wait_for(alt_task, timeout=max(0.1, deadline - time.monotonic()))
                except Exception:
                    alts = []
                if alts:
                    log.info(f"[Etap 2] Szukam pod alternatywnymi tytułami: {alts}")
                    done_queries = {(st["query"], st["type"]) for st in strategies}
                    extra = []
                    for alt in alts[:3]:
                        for st in generate_search_strategies(
                            alt, meta["season"], meta["episode"], meta["abs_episode"]
                        ):
                            key = (st["query"], st["type"])
                            if key not in done_queries:
                                done_queries.add(key)
                                extra.append(st)
                    await run_loop(extra[:MAX_STRATEGIES], alts)
            elif not alt_task.done():
                alt_task.cancel()

        release = _release_info(filename)
        if release:
            log.info(f"[Wydanie] plik=\"{filename}\" grupa={release.get('group')} rozdz={release.get('res')}")
        for x in all_subs:
            x["bonus"] = release_bonus(x, release)

        if any(x["score"] > 1 for x in all_subs):
            all_subs = [x for x in all_subs if x["score"] > 1]
        elif all_subs:
            log.info("[Wynik] Tylko niepewne dopasowania sezonu – pokazuję z oznaczeniem")

        # Najpierw SRT/ASS/SSA/VTT, potem reszta; w grupie: dopasowanie, wydanie, popularność
        all_subs.sort(
            key=lambda s: (
                format_priority(s.get("format_type", "")),
                s.get("score", 0), s.get("bonus", 0), s.get("download_count", 0),
            ),
            reverse=True,
        )

        stremio_subs = []
        for sub in all_subs[:10]:
            uncertain = sub.get("score", 10) <= 1 and meta["episode"] is not None
            label = " | ".join(filter(None, [
                "[sezon niepewny]" if uncertain else None,
                "[pasuje do wydania]" if sub.get("bonus") else None,
                sub["title_eng"] or sub["title_org"],
                f"by {sub['author']}" if sub["author"] else None,
                sub["format_type"] or None,
                f"{sub['download_count']} pobrań" if sub["download_count"] else None,
            ]))

            dl_params = {
                "id": sub["id"], "hash": sub["hash"],
                "query": sub["sq"], "type": sub["st"],
                "sort": sub.get("sort", "pobrn"),
                "page": sub.get("page", 0),
            }
            if meta["episode"] is not None:
                dl_params["ep"] = meta["episode"]
                dl_params["season"] = meta["season"] or 1
                if meta["abs_episode"] is not None:
                    dl_params["aep"] = meta["abs_episode"]
            params = urlencode(dl_params)

            stremio_subs.append({
                "id": f"animesub-{sub['id']}",
                "url": f"{BASE_URL}/subtitles/download?{params}",
                "lang": "pol",
                "SubtitleName": label,
            })

        log.info(f"[Wynik] Zwracam {len(stremio_subs)} napisów")
        return JSONResponse(content={"subtitles": stremio_subs})

    except Exception as e:
        log.error(f"[Błąd] {e}", exc_info=True)
        return JSONResponse(content={"subtitles": []})


# ── Uruchomienie ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("AS_PORT", 8080))

    if not os.environ.get("AS_BASE_URL"):
        sh = os.environ.get("AS_SPACE_HOST")
        si = os.environ.get("AS_SPACE_ID")
        if sh:
            BASE_URL = f"https://{sh}"
        elif si:
            BASE_URL = f"https://{si.replace('/', '-').lower()}.hf.space"
        else:
            BASE_URL = f"http://localhost:{port}"

    log.info(f"BASE_URL: {BASE_URL}")
    uvicorn.run(app, host="0.0.0.0", port=port)