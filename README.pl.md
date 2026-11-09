## 🌍 Język
- 🇬🇧 [English](README.md)
- 🇵🇱 Polski

# AnimeSub.info – Stremio Addon

Addon do Stremio pobierający polskie napisy do anime z [animesub.info](http://animesub.info).

Przepisany na Python z [addonu JS](https://huggingface.co/spaces/anemicpathbling/stremio-animesub) i sklonowanego na [GitHub](https://github.com/piotrek1488/animesub-stremio) korzystającego ze Stremio Addon SDK (Node.js).

## Jak to działa

1. Stremio wysyła ID filmu/serialu (IMDB lub Kitsu)
2. Addon odpytuje Cinemeta (lub Kitsu API) żeby uzyskać tytuł anime — bez klucza API
3. Szuka tytułu na animesub.info z kilkoma strategiami (tytuł + odcinek, sam tytuł, po angielsku i po oryginalnym)
4. Filtruje wyniki — odrzuca inne serie o podobnej nazwie, openingi, endingi i spin-offy
5. Gdy użytkownik wybierze napisy, addon pobiera ZIP dwuetapowo (sesja + świeży hash), rozpakowuje (wybierając plik z właściwym odcinkiem), konwertuje tylko formaty, których Stremio nie odtwarza (MicroDVD/TMPlayer → SRT) i serwuje plik

## Ważne informacje dla użytkowników

**Po aktualizacji addonu** — jeśli zmieniła się wersja aplikacji i coś przestało działać, usuń addon ze Stremio lub Nuvio i dodaj go ponownie za pomocą adresu manifestu. Pozwoli to odświeżyć konfigurację zainstalowanego addonu i uniknąć korzystania przez klienta ze starych danych poprzedniej wersji.

**Synchronizacja napisów** — addon znajduje napisy pasujące do anime i numeru odcinka, ale mogą one być przygotowane dla innego wydania wideo, na przykład TV, Blu-ray, WEB-DL lub innego release'u grupy fansubowej. Jeśli napisy wyświetlają się za wcześnie albo za późno, przesuń je ręcznie za pomocą ustawienia opóźnienia napisów w Stremio lub Nuvio.

## Struktura projektu

```
├── main.py            # Cały addon (FastAPI)
├── ASicon.jpg         # Ikona addonu
├── requirements.txt   # Zależności Python
├── start.bat          # Uruchomienie dwuklikiem na Windowsie (lokalnie)
├── deploy.sh          # Skrypt pierwszego deployu na Oracle Cloud
├── restart.sh         # Restart po aktualizacji kodu
├── render.yaml        # Konfiguracja dla Render (alternatywa)
├── README.md
└── README.pl.md
```

## Szybki start (lokalnie)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

Addon wystartuje na `http://localhost:8080/manifest.json`.

### Windows (jeden klik)

Kliknij dwukrotnie `start.bat`. Przy pierwszym uruchomieniu skrypt tworzy środowisko wirtualne i instaluje zależności, potem startuje serwer i otwiera stronę dodatku. Okno musi zostać otwarte podczas oglądania.

## Szybki start (docker)
Możesz uruchomić ten projekt jako kontener Docker, korzystając z gotowego obrazu. Jest to najszybszy sposób na wdrożenie addonu bez konieczności konfigurowania lokalnego środowiska Pythona.

Uruchomienie kontenera:
```bash
docker run -d \
  --name animesub-stremio-addon \
  -p 8080:8080 \
  -e AS_BASE_URL=http://localhost:8080 \
  docker.io/ludvickpro/animesub-stremio-addon:latest
```

## Deploy na Oracle Cloud (zalecany, darmowy 24/7)

### Wymagania
- Konto Oracle Cloud ([rejestracja](https://cloud.oracle.com/sign-up), wymaga karty do weryfikacji)
- Instancja VM.Standard.E2.1.Micro (Always Free) lub VM.Standard.A1.Flex (ARM, Always Free)
- Darmowa subdomena z [DuckDNS](https://www.duckdns.org) (potrzebna do HTTPS)

### Przygotowanie Oracle Cloud

1. Stwórz instancję VM z Ubuntu 22.04/24.04, pobierz klucz SSH
2. W Security List (Networking → VCN → Public Subnet → Default Security List) dodaj reguły Ingress dla portów **80**, **443** i **8080** (Source CIDR: `0.0.0.0/0`, TCP)
3. Na DuckDNS stwórz subdomenę i wpisz publiczne IP serwera

### Instalacja

```bash
ssh -i klucz.key ubuntu@TWOJE_IP
sudo apt install -y sudo apt install -y python3 python3-pip python3-venv git
git clone https://github.com/piotrek1488/animesub-stremio.git ~/projects/animesub-stremio
cd ~/projects/animesub-stremio
```
lub
```bash
ssh -i klucz.key ubuntu@TWOJE_IP
sudo apt install -y sudo apt install -y python3 python3-pip python3-venv git gh
gh auth login
gh repo clone piotrek1488/animesub-stremio ~/projects/animesub-stremio
cd ~/projects/animesub-stremio
```

Otwórz `deploy.sh`, sprawdź zmienne na górze (domena, port, ścieżki), potem:

```bash
chmod +x deploy.sh restart.sh
./deploy.sh
```

Skrypt automatycznie zainstaluje Python, zależności, skonfiguruje systemd, Caddy (HTTPS), firewall i cron joba zapobiegającego wyłączeniu VM.

Na końcu dostaniesz link do wklejenia w Stremio:
```
https://twoja-subdomena.duckdns.org/manifest.json
```

### Aktualizacja kodu

```bash
cd ~/projects/animesub-stremio
git fetch origin
git rebase origin/main
./restart.sh
```

### Po co Pay As You Go?

Po rejestracji w Oracle od razu przejdź na Pay As You Go (Billing → Upgrade). Nadal korzystasz z darmowych zasobów (rachunek = 0 zł), ale Oracle nie zamknie konta za nieaktywność. Bez tego mogą wyłączyć instancję po 30 dniach bezczynności.

## Deploy alternatywny

### Koyeb (darmowy, nie usypia)
Deploy z GitHuba, darmowy plan obejmuje jedną usługę. Ustaw zmienną `AS_BASE_URL` na URL deploymentu.

### Hugging Face Spaces (darmowy, usypia)
Działa, ale usypia po czasie bezczynności. Można obejść pingując UptimeRobotem. Addon auto-wykrywa zmienne `AS_SPACE_HOST`/`AS_SPACE_ID`.

### Render
Plik `render.yaml` jest gotowy. Darmowy plan usypia po 15 min. Ustaw zmienną `AS_BASE_URL`.

## Zmienne środowiskowe

| Zmienna    | Wymagana | Opis |
|------------|----------|------|
| `AS_BASE_URL` | Tak*     | Pełny URL deploymentu (z https://). Auto-wykrywany na HF Spaces i lokalnie. |
| `AS_PORT`     | Nie      | Port serwera (domyślnie 8080) |
| `SUBTITLE_OFFSET_MS` | Nie | Stałe przesunięcie czasu wszystkich napisów w ms (ujemne = wcześniej). Domyślnie `0` |
| `SEARCH_BUDGET_S` | Nie | Łączny limit czasu szukania (domyślnie `9`). Po nim addon zwraca to, co znalazł |
| `SEARCH_BATCH` | Nie | Ile zapytań do animesub idzie równolegle (domyślnie `3`) |
| `MAX_STRATEGIES` | Nie | Maks. liczba zapytań na odcinek (domyślnie `80`) |
| `ALLOW_UNCERTAIN_SEASON` | Nie | `1` = pokazuj też napisy z dobrym numerem odcinka, ale nieznanym sezonem (z oznaczeniem `[sezon niepewny]`). Domyślnie `0` |
| `LOG_LEVEL` | Nie | `INFO` (domyślnie) lub `DEBUG` – `DEBUG` loguje, dlaczego dany wynik został odrzucony |

## Przydatne komendy (Oracle Cloud)

```bash
sudo systemctl status animesub      # status usługi
sudo systemctl restart animesub     # restart
sudo journalctl -u animesub -f      # logi na żywo
sudo systemctl status caddy         # status Caddy (HTTPS)
```

## Szczegóły techniczne

**Kodowanie strony** — animesub.info używa ISO-8859-2. Addon dekoduje poprawnie i serwuje napisy w UTF-8.

**System zabezpieczeń** — animesub.info wiąże hash pobierania z sesją. Addon najpierw wchodzi na stronę wyszukiwania (łapie ciasteczka + świeży hash), potem pobiera plik z tą samą sesją.

**Obsługa formatów** — SRT, ASS/SSA i VTT są serwowane bez zmian i zawsze na początku listy. Tylko MicroDVD i TMPlayer (których Stremio nie odtworzy) są konwertowane do SRT. Każdy plik jest przekodowywany do UTF-8. Pamiętaj, że ASS nie wyświetla się na każdym kliencie (np. starsze aplikacje mobilne lub Smart TV).

**Filtrowanie wyników** — addon sprawdza czy tytuł napisu odpowiada szukanemu anime i odrzuca inne serie o podobnej nazwie, openingi, endingi i spin-offy.

**Stremio URL format** — Stremio dodaje do URL nazwę pliku (`/subtitles/series/tt123:1:1/filename=....json`). Addon obsługuje oba formaty.

**Cache** — wyniki wyszukiwania trzymane w pamięci przez 30 minut. Wyniki zapytań zakończonych błędem nie trafiają do cache, a puste wyniki są pamiętane tylko 60 sekund.

## Lista zmian – co zostało naprawione i ulepszone

### Zły odcinek / sezon
- **Paczki ZIP z wieloma odcinkami** — wcześniej brany był pierwszy plik z archiwum, niezależnie od odcinka. Teraz plik wybierany jest po numerze odcinka w nazwie (`- 05`, `E05`, `S01E05`, `ep05v2`; CRC, rozdzielczość i kodeki są ignorowane). Gdy żaden plik nie pasuje, addon zwraca 404 zamiast napisów z innego odcinka.
- **Dopasowanie sezonu** — zapytania o S1 nie przyjmują już napisów `2nd Season` / `Final Season`, a zapytania o S2+ nie przyjmują zwykłego `ep 05` z sezonu 1. Napisy bez sezonu są akceptowane tylko, gdy numer zgadza się z numerem ciągłym (absolutnym) liczonym z Cinemeta.
- **Parser odcinków** — granice słów (koniec z `ep` w środku `Step 3`), zakresy odcinków (`ep01-12`), cyfry rzymskie (`II`, `III`), `Season N`, `Nth Season`, `Final Season`.
- **Podobne serie** — dłuższy tytuł, który tylko zaczyna się od szukanego (inna seria, remix, kontynuacja z własną nazwą), nie pasuje już do wyszukiwania. Po tytule może wystąpić tylko znacznik odcinka/sezonu (`ep`, `Season`, `Arc`, `II`, ...).
- **Kitsu** — sezon jest odczytywany z tytułu (`... Season 2`).
- **Dopasowanie do wydania** — nazwa pliku wideo przesyłana przez Stremio służy do podniesienia napisów, w których opisie jest ta sama grupa lub rozdzielczość (`[pasuje do wydania]`).

### Więcej znalezionych napisów
- **Numeracja ciągła** — długie serie są szukane także po numerze absolutnym odcinka (np. `ep202`, `ep1015`).
- **Warianty zapytań** — ze spacją i bez (`ep 5`, `ep5`), z zerem i bez, z myślnikami i dwukropkami i bez.
- **Części z nazwami zamiast sezonów** — konfigurowalna tabela (`ARC_SERIES`) tłumaczy nazwy części/łuków (także w wersji zlatynizowanej i angielskiej) na numery sezonów.
- **Sezony będące osobnymi seriami** — gdy Cinemeta/TVDB prowadzi całą osobną serię jako jeden sezon innego serialu, zapytanie jest przekierowywane na właściwy tytuł (`SERIES_REDIRECTS`).
- **Alternatywne tytuły** — dla filmów i seriali używane są tytuły z Kitsu (np. nazwy zlatynizowane lub lokalne), gdy główny tytuł nic nie znajdzie.
- **Filmy anime** — osobny tryb filmu: słowa `movie`, `film`, `gekijouban` w tytule nie powodują już odrzucenia.

### Formaty i czas
- SRT, ASS/SSA i VTT są zawsze na początku listy.
- Tylko formaty, których Stremio nie odtworzy (MicroDVD, TMPlayer), są konwertowane do SRT; reszta zostaje w oryginalnym formacie.
- Opcjonalne stałe przesunięcie czasu (`SUBTITLE_OFFSET_MS`), bez zmiany formatu.

### Niezawodność
- Jedno ponowienie przy błędzie sieci / HTTP 5xx; nieudane zapytania nie trafiają do cache (pojedyncza awaria potrafiła blokować tytuł na 30 minut).
- Zapytania idą równolegle paczkami i mają łączny limit czasu, żeby Stremio nie przerywało przy wolnym szukaniu.
- `LOG_LEVEL=DEBUG` pokazuje, dlaczego dany wynik został odrzucony.

### Znane ograniczenia
- Addon nie potrafi sam zsynchronizować napisów z wideo – czas napisów może się różnić między wydaniami.
- Tabele części/sezonów są wpisane ręcznie dla wymienionych serii; inne serie z nazwanymi częściami mogą wymagać wpisu w `ARC_SERIES` lub `SERIES_REDIRECTS`.

## Informacja o AI

Ten addon powstał przy wsparciu narzędzi AI.

AI było wykorzystywane do generowania kodu, refaktoryzacji oraz implementacji części funkcjonalności.  
Ostateczna logika, struktura oraz testy zostały zweryfikowane i poprawione ręcznie.
