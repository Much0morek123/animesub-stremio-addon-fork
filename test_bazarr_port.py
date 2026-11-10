"""Testy logiki przeniesionej z Bazarra (animesubinfo.py). Uruchom z katalogu głównego: python3 tests/test_bazarr_port.py
Wymaga tylko beautifulsoup4 – FastAPI/httpx/uvicorn są podmieniane atrapami, bo testujemy czyste funkcje."""
import os, sys
from unittest.mock import MagicMock

sys.path.insert(0, os.getcwd())
for m in ("fastapi", "fastapi.middleware", "fastapi.middleware.cors", "fastapi.responses",
          "fastapi.staticfiles", "httpx", "uvicorn"):
    sys.modules.setdefault(m, MagicMock())
os.environ["PREFER_ADVANCED_SSA"] = "1"
import main  # noqa: E402

# --- parser grup z linii "Synchro:" (przykłady z komentarzy w Bazarze)
assert main._parse_release_groups("Synchro: [SubsPlease]") == ["SubsPlease"]
assert sorted(main._parse_release_groups("Synchro: SubsPlease, Erai-raws, VARYG (CR)")) == ["Erai-raws", "SubsPlease", "VARYG"]
assert main._parse_release_groups("Synchro do [Erai-raws]") == ["Erai-raws"]
assert main._parse_release_groups("Tłumaczenie: Jan\nSynchronizacja ręczna") == []
assert main._parse_release_groups("") == []
print("ok - parser grup Synchro")

# --- parsowanie HTML: opis musi zachować podział na linie
def entry(i, desc_html):
    return f'''<table class="Napisy" style="text-align:center;width:100%">
<tr class="KNap"><td>Kimetsu no Yaiba ep02</td><td>2024</td><td></td><td>Advanced SSA</td></tr>
<tr class="KNap"><td>Demon Slayer ep02</td><td><a>Jan</a></td><td></td><td></td><td>12 kB</td></tr>
<tr class="KNap"><td></td><td>0</td><td></td><td>308 razy</td></tr>
<tr class="KKom"><td class="KNap" align="left">{desc_html}</td>
<td><form method="POST" action="sciagnij.php"><input type="hidden" name="id" value="{i}"><input type="hidden" name="sh" value="h{i}"></form></td></tr></table>'''
html = entry(1, "Tłumaczenie z angielskiego<br>Synchro: [SubsPlease], Erai-raws<br>Uwagi: brak")
r = main._parse_search_results(html)[0]
assert r["release_groups"] == ["SubsPlease", "Erai-raws"], r["release_groups"]
assert "Synchro: [SubsPlease], Erai-raws" in r["description"].split("\n")
print("ok - parsowanie HTML zachowuje linie opisu i zwraca release_groups")

# --- premia za wydanie: dokładna grupa > trafienie w tekście
exact = {"description": "Synchro: [SubsPlease]", "release_groups": ["SubsPlease"], "title_org": "", "title_eng": "", "title_alt": ""}
loose = {"description": "do wydania SubsPlease z CR", "release_groups": [], "title_org": "", "title_eng": "", "title_alt": ""}
none_ = {"description": "inne", "release_groups": ["Erai-raws"], "title_org": "", "title_eng": "", "title_alt": ""}
rel = main._release_info("[SubsPlease] Kimetsu no Yaiba - 02 (1080p).mkv")
assert main.release_bonus(exact, rel) == 3 + 0   # grupa dokładnie (rozdzielczości brak w opisie)
assert main.release_bonus(loose, rel) == 2
assert main.release_bonus(none_, rel) == 0
print("ok - release_bonus: dokładna grupa (3) > trafienie w tekście (2) > brak (0)")

# --- Advanced SSA jako remis
assert main.ssa_preference({"format_type": "Advanced SSA"}) == 1
assert main.ssa_preference({"format_type": "SubRip"}) == 0
main.PREFER_ADVANCED_SSA = False
assert main.ssa_preference({"format_type": "Advanced SSA"}) == 0
print("ok - ssa_preference (włączane przez PREFER_ADVANCED_SSA)")
