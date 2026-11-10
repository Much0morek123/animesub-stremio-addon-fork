## 🌍 Language
- 🇬🇧 English (default)
- 🇵🇱 [Polski](README.pl.md)

# AnimeSub.info – Stremio Addon

A Stremio addon that fetches Polish subtitles for anime from [animesub.info](http://animesub.info).

Rewritten in Python from a [JS addon](https://huggingface.co/spaces/anemicpathbling/stremio-animesub) and cloned on [GitHub](https://github.com/piotrek1488/animesub-stremio), using the Stremio Addon SDK (Node.js).

## How it works

1. Stremio sends a movie/series ID (IMDB or Kitsu)
2. The addon queries Cinemeta (or the Kitsu API) to retrieve the anime title — no API key required
3. It searches for the title on animesub.info using several strategies (title + episode, title only, English and original titles)
4. It filters results — removes other series with a similar name, openings, endings, and spin-offs
5. When the user selects subtitles, the addon downloads a ZIP in two steps (session + fresh hash), extracts it (picking the file for the requested episode), converts only the formats Stremio cannot play (MicroDVD/TMPlayer → SRT) and serves the file

## Important notes for users

**After an addon update** — if the application version has changed and something no longer works, remove the addon from Stremio or Nuvio and add it again using the manifest URL. This refreshes the installed addon configuration and prevents the client from continuing to use stale data from the previous version.

**Subtitle timing** — the addon finds subtitles that match the anime and episode, but the timing may have been prepared for a different video release (for example TV, Blu-ray, WEB-DL, or another fansub release). If the subtitles are ahead of or behind the video, adjust the subtitle delay manually in Stremio or Nuvio.

## Project structure

```
├── main.py            # Entire addon (FastAPI)
├── ASicon.jpg         # Addon icon
├── requirements.txt   # Python dependencies
├── deploy.sh          # Initial deployment script for Oracle Cloud
├── restart.sh         # Restart after updating the code
├── render.yaml        # Configuration for Render (alternative)
├── README.md
└── README.pl.md
```

## Quick start (local)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

The addon will start at `http://localhost:8080/manifest.json`.

## Quick start (docker)
You can run this project as a Docker container using the pre-built image. This is the fastest way to deploy the addon without the need to set up a local Python environment.

Run the container:
```bash
docker run -d \
  --name animesub-stremio-addon \
  -p 8080:8080 \
  -e AS_BASE_URL=http://localhost:8080 \
  docker.io/ludvickpro/animesub-stremio-addon:latest
```

## Deployment on Oracle Cloud (recommended, free 24/7)

### Wymagania
- Oracle Cloud account ([sign up](https://cloud.oracle.com/sign-up), requires a card for verification)
- VM instance: VM.Standard.E2.1.Micro (Always Free) or VM.Standard.A1.Flex (ARM, Always Free)
- Free subdomain from [DuckDNS](https://www.duckdns.org) (required for HTTPS)

### Preparing Oracle Cloud

1. Create a VM instance with Ubuntu 22.04/24.04 and download the SSH key
2. In Security List (Networking → VCN → Public Subnet → Default Security List), add Ingress rules for ports 80, 443, and 8080 (Source CIDR: 0.0.0.0/0, TCP)
3. Create a subdomain on DuckDNS and point it to your server’s public IP

### Installation

```bash
ssh -i id_rsa.key ubuntu@YOUR_IP
sudo apt install -y sudo apt install -y python3 python3-pip python3-venv git
git clone https://github.com/piotrek1488/animesub-stremio.git ~/projects/animesub-stremio
cd ~/projects/animesub-stremio
```
or
```bash
ssh -i id_rsa.key ubuntu@YOUR_IP
sudo apt install -y sudo apt install -y python3 python3-pip python3-venv git gh
gh auth login
gh repo clone piotrek1488/animesub-stremio ~/projects/animesub-stremio
cd ~/projects/animesub-stremio
```

Open `deploy.sh`, check the variables at the top (domain, port, paths), then:

```bash
chmod +x deploy.sh restart.sh
./deploy.sh
```

The script will automatically install Python, dependencies, configure systemd, Caddy (HTTPS), firewall, and a cron job to prevent the VM from shutting down.

At the end, you’ll get a link to paste into Stremio:
```
https://your-subdomain.duckdns.org/manifest.json
```

### Updating the code

```bash
cd ~/projects/animesub-stremio
git fetch origin
git rebase origin/main
./restart.sh
```

### Why Pay As You Go?

After registering with Oracle, switch to Pay As You Go (Billing → Upgrade). You still use free resources (bill = $0), but Oracle won’t suspend your account due to inactivity. Without this, your instance may be stopped after 30 days of inactivity.

## Alternative deployment

### Koyeb (free, no sleeping)
Deploy from GitHub. The free plan includes one service. Set the `AS_BASE_URL` variable to your deployment URL.

### Hugging Face Spaces (free, sleeps)
Works, but goes to sleep after inactivity. This can be bypassed by pinging it with UptimeRobot. The addon auto-detects `AS_SPACE_HOST`/`AS_SPACE_ID` variables.

### Render
The `render.yaml` file is ready. The free plan sleeps after 15 minutes. Set the `AS_BASE_URL` variable.

## Environment variables

| Variable   | Required | Description |
|------------|----------|-------------|
| `AS_BASE_URL` | Yes*     | Full deployment URL (with https://). Auto-detected on HF Spaces and locally |
| `AS_PORT`     | No       | Server port (default: 8080) |
| `SUBTITLE_OFFSET_MS` | No | Constant shift of all subtitle timings in ms (negative = earlier). Default `0` |
| `SEARCH_BUDGET_S` | No | Total time limit for searching (default `9`). After it the addon returns what it has found |
| `SEARCH_BATCH` | No | How many animesub requests run in parallel (default `3`) |
| `MAX_STRATEGIES` | No | Max. number of search queries per episode (default `80`) |
| `ALLOW_UNCERTAIN_SEASON` | No | `1` = also show subtitles with the right episode number but unknown season (marked `[sezon niepewny]`). Default `0` |
| `PREFER_ADVANCED_SSA` | No | `1` = on a tie, rank Advanced SSA subtitles higher (as Bazarr does). Default `0`, because not every client renders ASS |
| `LOG_LEVEL` | No | `INFO` (default) or `DEBUG` – `DEBUG` logs why each result was rejected |

## Useful commands (Oracle Cloud)

```bash
sudo systemctl status animesub      # service status
sudo systemctl restart animesub     # restart
sudo journalctl -u animesub -f      # live logs
sudo systemctl status caddy         # Caddy (HTTPS) status
```

## Technical details

**Page encoding** — animesub.info uses ISO-8859-2. The addon decodes it correctly and serves subtitles in UTF-8.

**Security system** — animesub.info binds the download hash to a session. The addon first opens the search page (gets cookies + fresh hash), then downloads the file within the same session.

**Format handling** — SRT, ASS/SSA and VTT are served unchanged and are always listed first. Only MicroDVD and TMPlayer (which Stremio cannot play) are converted to SRT. Every file is re-encoded to UTF-8. Note that ASS is not displayed by every client (e.g. some older mobile or Smart TV apps).

**Result filtering** — the addon checks whether the subtitle title matches the requested anime and removes other series with a similar name, openings, endings, and spin-offs.

**Stremio URL format** — Stremio adds a filename to the URL (`/subtitles/series/tt123:1:1/filename=....json`). The addon supports both formats.

**Cache** — search results are stored in memory for 30 minutes. Results from failed requests are never cached and empty results are cached for only 60 seconds.

## Changelog – what was fixed and improved

### Wrong episode / season
- **Multi-episode ZIP archives** — previously the first file in the archive was used regardless of the episode. Now the file is chosen by the episode number in its name (`- 05`, `E05`, `S01E05`, `ep05v2`; CRC, resolution and codec tags are ignored). If no file matches, the addon returns 404 instead of a wrong episode.
- **Season matching** — S1 requests no longer accept `2nd Season` / `Final Season` subtitles, and S2+ requests no longer accept plain `ep 05` from season 1. Subtitles without a season are accepted only if the number matches the continuous (absolute) episode number computed from Cinemeta.
- **Episode parser** — word boundaries (no more matching `ep` inside `Step 3`), episode ranges (`ep01-12`), Roman numerals (`II`, `III`), `Season N`, `Nth Season`, `Final Season`.
- **Similar series** — a longer title that only starts with the searched title (a different series, a remix, a sequel with its own name) no longer matches. Only an episode/season marker may follow the title (`ep`, `Season`, `Arc`, `II`, ...).
- **Kitsu** — the season is read from the title (`... Season 2`).
- **Release matching** — the video file name sent by Stremio is used to rank subtitles whose description mentions the same release group/resolution (`[pasuje do wydania]`). The group list from the description's `Synchro:` line (ported from Bazarr's animesub.info provider) gives an exact match a stronger bonus than a plain text hit.

### Finding more subtitles
- **Continuous numbering** — long series are searched with the absolute episode number too (e.g. `ep202`, `ep1015`).
- **Query variants** — with/without a space (`ep 5`, `ep5`), with/without zero padding, with/without hyphens and colons.
- **Named parts instead of seasons** — a configurable table (`ARC_SERIES`) maps part/arc names (including romanised and English variants) to season numbers.
- **Seasons that are separate series** — when Cinemeta/TVDB lists a whole separate series as one season of another show, the request is redirected to the matching title (`SERIES_REDIRECTS`).
- **Alternative titles** — for movies and series titles from Kitsu (e.g. romanised or localised names) are used when the main title finds nothing.
- **Anime movies** — separate movie mode: `movie`, `film`, `gekijouban` in a title are no longer rejected.

### Formats and timing
- SRT, ASS/SSA and VTT are always listed first.
- Only formats Stremio cannot play (MicroDVD, TMPlayer) are converted to SRT; everything else keeps its original format.
- Optional constant time shift (`SUBTITLE_OFFSET_MS`), applied without changing the format.

### Reliability
- One retry on network errors / HTTP 5xx; failed requests are not cached (a single hiccup used to block a title for 30 minutes).
- Requests run in parallel batches with an overall time limit, so Stremio does not time out on slow searches.
- `LOG_LEVEL=DEBUG` shows why a result was rejected.

### Known limitations
- The addon cannot synchronise subtitles with the video itself – timing can still differ between releases.
- Arc/season tables are written by hand for the series listed above; other series with named parts may need an entry in `ARC_SERIES` or `SERIES_REDIRECTS`.

## AI Notice

This addon was developed with the assistance of AI tools.

AI was used to help with code generation, refactoring, and implementation details.  
The final logic, structure, and testing were verified and adjusted manually.
