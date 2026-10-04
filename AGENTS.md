# Aura Hub — AI Agent Architecture & Operation Manual

## 1. Project Overview & Purpose
**Aura Hub** is an extensible Telegram bot built with `python-telegram-bot` (v20+ async `ApplicationBuilder`) designed to serve as a comprehensive management hub and automated audio ingestion pipeline for a self-hosted **Navidrome** music server.

### Key Capabilities:
- **Audio Download Pipelines:** Downloads albums, singles, and playlists from YouTube, YouTube Music, and Spotify using `yt-dlp` and `spotdl`.
- **Intelligent Metadata Tagging:** Multi-stage enrichment using MusicBrainz REST API, AcoustID audio fingerprinting (via Chromaprint `fpcalc`), Cover Art Archive, and Genius.
- **Transliteration & Phonetic Alignment:** Franco-Arabic translation (e.g. `3` -> `ع`, `7` -> `ح`) and bilingual artist splitting (e.g. `"Mohamed Mounir  محمد منير"` -> separate query tokens).
- **Duration-Based Track Matching:** Mutagen audio length comparison ($\pm 4$s tolerance) prevents reversed or shuffled playlist numbering.
- **Synced Karaoke Lyrics:** Fetches exact and fallback `.lrc` synchronized lyrics from LRCLIB and writes companion files for Navidrome/Symfonium.
- **Navidrome / Subsonic Integration:** Direct Subsonic REST client to ping the server and trigger instant library scans (`/rest/startScan`, `/rest/ping`, `/rest/getScanStatus`).
- **Interactive Telegram UI:** Multi-stage progress indicators (`[1/4]` -> `[4/4]`), high-resolution photo card summaries, paginated album folder browsers, and confirmation dialogs.

---

## 2. Directory Structure & File Map

```text
aura_hub/
├── config.py                 # Central configuration: tokens, paths, API constants, Subsonic settings
├── main.py                   # Minimal entry point (ApplicationBuilder, post_init, router registration)
├── requirements.txt          # Python runtime dependencies
├── .env.example              # Environment variables template
├── .gitignore                # Git exclusions (pycache, .env, venvs, test artifacts)
├── AGENTS.md                 # This instruction and architecture document for AI agents
├── utils/
│   ├── __init__.py           # Package marker
│   ├── helpers.py            # Transliteration, Franco-Arabic mappings, string cleaners, Genius URL parser
│   └── keyboards.py          # Dynamic paginated folder keyboard builder, confirmation dialogs
├── services/
│   ├── __init__.py           # Package marker
│   ├── metadata.py           # MusicBrainz REST client, Cover Art Archive fetcher, AcoustID matcher
│   ├── lyrics.py             # LRCLIB exact & fallback synced lyrics engine (.lrc)
│   ├── tagger.py             # Mutagen ID3 engine, duration/phonetic track alignment, loose cover.jpg writer
│   ├── downloader.py         # yt-dlp & spotdl execution pipelines with dynamic progress updates
│   ├── navidrome.py          # Subsonic/Navidrome REST client (/rest/startScan, /rest/ping, etc.)
│   ├── system.py             # Storage metrics, tool availability diagnostics, safe folder deletion
│   └── web.py                # FastAPI WebApp backend with HMAC-SHA256 signature verification
├── static/
│   └── index.html            # Telegram Mini App responsive single-page dashboard
├── handlers/
│   ├── __init__.py           # Package marker
│   ├── common.py             # /start, /help, /hub, /status, user authentication checks
│   ├── download.py           # Direct URL auto-catcher, /download, /genius, /search (interactive buttons)
│   ├── library.py            # /retag (interactive album browser), /delete (with confirmation dialog)
│   ├── navidrome.py          # /rescan (triggers instant Navidrome Subsonic library scan), /scanstatus
│   ├── nowplaying.py         # /nowplaying, /np active stream monitor with interactive refresh
│   ├── request.py            # /request queue, admin approval/rejection cards, ingestion pipeline
│   ├── system.py             # /storage, /disk metrics
│   └── users.py              # /users, /adduser, password reset, and account deletion
└── tests/
    ├── test_aura_hub.py      # Unit tests for helpers, keyboards, system services, and Navidrome client
    ├── test_handlers.py      # Integration tests verifying router mounting and application building
    └── test_webapp.py        # Tests for FastAPI endpoints, HMAC validation, and dashboard API
```

---

## 3. Architecture & Core Design Principles

### 3.1 Strict Separation of Concerns
1. **Handlers (`handlers/`):** Pure Telegram interface layer. Responsible for parsing updates, managing conversation state, and calling services. **No direct business logic or raw API requests belong here.**
2. **Services (`services/`):** Framework-agnostic business logic, external API integrations, subprocess executions, file tagging, and Subsonic client calls. Can be invoked from CLI, webhooks, or test suites independently of Telegram.
3. **Utilities (`utils/`):** Pure string processors, transliterators, regex parsers, and Telegram UI keyboard builders.
4. **Configuration (`config.py`):** Single source of truth. Reads from environment variables with sensible defaults.

### 3.2 Modular Extensibility (The Router Pattern)
Every module in `handlers/` exports a `router` list containing `python-telegram-bot` handler objects:
```python
router = [
    CommandHandler("custom", custom_handler),
    CallbackQueryHandler(custom_callback, pattern=r"^custom_"),
]
```
To add a new feature:
1. Add business logic to `services/` (if external API or file operations are involved).
2. Create `handlers/<new_feature>.py` defining handlers and exporting `router = [...]`.
3. Import `new_feature.router` in `main.py` and append it to `routers` inside `build_application()`.

### 3.3 Asynchronous Execution & Non-Blocking Event Loop
Subprocess calls (`yt-dlp`, `spotdl`), network I/O (`urllib`), and heavy mutagen file operations are CPU or blocking I/O bound.
- Always run these using `loop.run_in_executor(executor, func, *args)` via the shared `ThreadPoolExecutor` in `services/downloader.py`.
- Thread-safe UI updates back to Telegram must use:
  ```python
  asyncio.run_coroutine_threadsafe(status_msg.edit_text(text, parse_mode="Markdown"), loop)
  ```

### 3.4 Cross-Platform Path & Binary Handling
- **Development Environment:** Windows.
- **Target Deployment:** Linux Mint / Debian.
- Always use `pathlib.Path` or `os.path.join()`. Never hardcode `\` or `/`.
- Binary lookups (`shutil.which("yt-dlp")`, `shutil.which("spotdl")`, `shutil.which("fpcalc")`) must fail gracefully if the binary is absent in dev or test environments.
- OS-specific process commands or systemd interactions must be guarded with `if platform.system() != "Windows":`.

---

## 4. Domain Logic & Internal Pipelines

### 4.1 Transliteration & Artist Cleaning (`utils/helpers.py`)
- **`extract_clean_artists(raw_artist)`:** Extracts Arabic (`[\u0600-\u06FF]+`) and Latin (`[a-zA-Z0-9]+`) fragments from combined strings like `"Mohamed Mounir  محمد منير"`. Returns `["Mohamed Mounir", "محمد منير"]` to prevent Lucene parse errors when querying MusicBrainz.
- **`franco_to_arabic(text)`:** Translates Franco-Arabic words (`fi` -> `في`, `el` -> `ال`, `banat` -> `بنات`, `aaks` -> `عكس`) and numeric digits (`3` -> `ع`, `7` -> `ح`, `2` -> `أ`, `5` -> `خ`, `6` -> `ط`, `8` -> `غ`).
- **`sanitize_filename(name)`:** Strips forbidden filesystem characters (`\ / : * ? " < > |`) and collapses whitespace for safe folder creation across Linux and Windows.

### 4.2 Multi-Stage Metadata & Fingerprinting (`services/metadata.py`)
- **Stage 1 (Exact Match):** Combines bilingual artist candidates with Franco-Arabic album variations against MusicBrainz release queries (`release:"{alb}" AND artist:"{art}"`).
- **Stage 2 (AcoustID):** If text queries return no hits and a sample MP3 exists, executes Chromaprint (`fpcalc`) via AcoustID to identify the recording and release MBID.
- **Stage 3 (Cover Art Archive):** Fetches high-resolution album jackets from `https://coverartarchive.org/release/{mbid}/front-500` (falling back to `/front`).
- **Stage 4 (Genius Fallback):** Queries Genius for song lyrics (`USLT`), producers (`IPLS`, `TXXX:PRODUCER`), and composers/writers (`TCOM`).

### 4.3 Duration-Tolerant Track Alignment (`services/tagger.py`)
When tagging multi-track albums, tracks are matched using `find_best_track_match()`:
- Computes local audio length using mutagen: `MP3(local_path).info.length`.
- Compares duration against MusicBrainz track duration with tiered scoring:
  - $\le 2.5$s difference: $+80$ score bonus.
  - $\le 5.0$s difference: $+45$ score bonus.
  - $\le 9.0$s difference: $+20$ score bonus.
  - $> 9.0$s difference: $-25$ score penalty.
- Token overlap score: $+40$ per matching word.
- Candidate accepted if score $\ge 35$. Falls back to file sequence index if available, or first unassigned track.
- **Navidrome Loose Cover:** Saves `cover.jpg` inside the folder alongside embedding the `APIC` JPEG frame into every MP3.

### 4.4 Synced Lyrics Engine (`services/lyrics.py`)
- Queries `https://lrclib.net/api/get` (exact match) and `https://lrclib.net/api/search` (search fallback).
- Writes a `.lrc` file with the exact same base name as the `.mp3`.

### 4.5 Subsonic / Navidrome Client (`services/navidrome.py`)
- Communicates with Navidrome's Subsonic REST API endpoint (`http://localhost:4533/rest/`).
- Generates standard MD5 salt+token authentication:
  - Salt: Random 12-character hex string.
  - Token: `md5(password + salt)`.
  - Protocol version: `1.16.1`.
- Endpoints wrapped:
  - System: `ping`, `startScan`, `getScanStatus`.
  - User Management: `getUsers`, `getUser`, `createUser`, `updateUser`, `deleteUser`.
- If credentials are not set in `config.py`, methods return a graceful dictionary `{"ok": False, "message": "..."}` instead of raising unhandled exceptions.

### 4.6 User Account Administration (`handlers/users.py`)
- **`/users` Command:** Displays all registered Subsonic accounts with roles (`[👑 Admin]`, `[🎧 Stream]`, `[📥 Download]`).
- **Interactive Actions:** Per-user card with buttons to edit user details (rename username or update password with custom text / auto-generation) or trigger deletion with two-step confirmation safeguards.
- **Add User Flow:** Interactive `ConversationHandler` triggered via `/adduser` or inline button.
- **Security:** Strict admin enforcement via `@auth_required` decorator and built-in protection against deleting the primary configured server admin.

### 4.7 File Deletion Security (`services/system.py`)
- `delete_album_folder(rel_path)` validates that `(BASE_DOWNLOAD_DIR / rel_path).resolve()` is strictly within `BASE_DOWNLOAD_DIR` using `Path.relative_to()`. This completely prevents directory traversal exploits.
- Automatically deletes empty parent artist directories if no other albums remain.

### 4.8 Music Request & Ingestion Queue (`handlers/request.py`)
- **Dual-Tier Access Control:**
  - `ADMIN_USER_IDS`: Granted full server management, media deletion, user account management, and request approval/rejection.
  - `ALLOWED_USER_IDS`: Granted general bot access, YouTube search, and the ability to submit music requests.
- **Smart Link Catcher:** When an admin pastes a Spotify or YouTube URL into chat, it immediately triggers the direct download pipeline. When a standard allowed user pastes a URL, it automatically submits it as a request to the ingestion queue.
- **In-Memory Request Registry:** Stores pending items with unique hex IDs, requester credentials, and target search query or URL.
- **Admin Approval Card:** Dispatches an interactive card with `[✅ Approve & Ingest]` and `[❌ Reject]` buttons to all configured administrators.
- **Non-Blocking Background Pipeline:** Upon approval, resolves query via YouTube search if needed, streams audio, matches tags via MusicBrainz/AcoustID, fetches synced `.lrc` lyrics via LRCLIB, initiates an instant Navidrome Subsonic scan, and delivers completion notifications (with cover art) to both the approving admin and the requester.
- **Rejection Notification:** On rejection, marks the request as rejected, updates admin cards across chats, and notifies the requester.

### 4.9 Real-Time Playback Monitor (`handlers/nowplaying.py`)
- **Subsonic `getNowPlaying` Polling:** Calls `/rest/getNowPlaying` to extract active sessions across users and players.
- **Rich Stream Metadata:** Displays listener username, player/client name (Symfonium, web, desktop), track title, artist, album, stream bitrate, audio format, and elapsed minutes.
- **Dynamic In-Place Refresh:** Attached `[🔄 Refresh]` inline button re-polls the Navidrome Subsonic endpoint and updates the Telegram card in place without cluttering chat history.
- **Idle Server State:** When no active sessions are detected, renders a clean status card indicating an idle server.

### 4.10 Telegram Mini App & Web Dashboard (`services/web.py` & `static/index.html`)
- **Architecture:** Embedded FastAPI web server running via uvicorn in a dedicated background daemon thread, binding locally to `127.0.0.1:8000` behind a Caddy reverse proxy with DuckDNS.
- **Subpath & Reverse Proxy Support:** Accommodates Caddy reverse proxy under `/hub*`. Dual-mounts API routes under both `/api` and `/hub/api`, and serves dashboard on `/`, `/webapp`, `/hub`, `/hub/`, and `/hub/webapp`.
- **HMAC-SHA256 Cryptographic Auth:** Validates `Telegram.WebApp.initData` sent in `Authorization` headers using the secret key derived from `TELEGRAM_BOT_TOKEN`. Strictly restricts API access to authenticated `ADMIN_USER_IDS`.
- **REST Endpoints:**
  - `GET /`, `GET /webapp`, `GET /hub`, `GET /hub/webapp`: Serves the responsive single-page application dashboard.
  - `GET /api/users`, `GET /hub/api/users`: Fetches Navidrome user list and roles.
  - `POST /api/users/create`, `POST /hub/api/users/create`: Creates a new Navidrome account.
  - `POST /api/users/delete`, `POST /hub/api/users/delete`: Deletes a Navidrome account (with protection against deleting the primary admin).
  - `POST /api/users/reset-password`, `POST /hub/api/users/reset-password`: Resets a user's password to a secure random string.
  - `GET /api/nowplaying`, `GET /hub/api/nowplaying`: Real-time streaming sessions (`{"ok": True, "streams": [...]}`).
  - `POST /api/rescan`, `POST /hub/api/rescan`: Triggers immediate Navidrome library scan.
  - `GET /api/system`, `GET /hub/api/system`: Storage metrics, indexed MP3/LRC counts, and tool diagnostics.
- **Frontend Dashboard:** Built with vanilla HTML/CSS/JS with Google Fonts (Outfit & Inter), Telegram theme CSS variables, responsive tabs, modals, and `Telegram.WebApp.HapticFeedback`. Uses dynamic subpath resolution (`getApiUrl`) and content-type checking before JSON parsing to prevent non-JSON parse errors.
- **Bot Integration:** Configures the Telegram chat menu button (`MenuButtonWebApp`) pointing to `WEBAPP_EXTERNAL_URL`, with `/hub` command fallback.

---

## 5. Telegram Bot Command Reference

| Command | Handler File | Access Tier | Description |
| :--- | :--- | :--- | :--- |
| `/start` | `handlers/common.py` | All Users | Welcome card and feature overview |
| `/help` | `handlers/common.py` | All Users | Syntax guide and examples |
| `/hub` | `handlers/common.py` | All Users | Open the interactive Telegram Mini App dashboard |
| `/status` | `handlers/common.py` | All Users | Health check for Bot, external binaries, and Navidrome ping |
| `/nowplaying`, `/np` | `handlers/nowplaying.py` | All Users | Real-time active playback session monitor with in-place refresh |
| `/request <link or query>` | `handlers/request.py` | All Users | Queue a track, album, or URL for admin review |
| `/search <query>` | `handlers/download.py` | All Users | Interactive YouTube search (downloads for admins, queues for users) |
| `/download <url>` | `handlers/download.py` | Admins | Direct download and tagging (routes users to `/request`) |
| `/genius <url> \| <g_url>` | `handlers/download.py` | Admins | Explicit Genius URL pairing |
| `/retag` | `handlers/library.py` | Admins | Interactive paginated album browser to refresh ID3 tags and `.lrc` |
| `/delete`, `/remove` | `handlers/library.py` | Admins | Interactive album browser with confirmation dialog for folder removal |
| `/rescan` | `handlers/navidrome.py` | Admins | Triggers immediate Navidrome Subsonic library scan |
| `/scanstatus` | `handlers/navidrome.py` | All Users | Checks active scan progress and track counts |
| `/users` | `handlers/users.py` | Admins | Lists Navidrome accounts with manage/edit/delete buttons |
| `/adduser` | `handlers/users.py` | Admins | Interactive conversation flow to create a new Subsonic account |
| `/requests` | `handlers/request.py` | Admins | View pending and recent items in the ingestion queue |
| `/storage`, `/disk` | `handlers/system.py` | All Users | Displays disk partition metrics and indexed MP3/LRC counts |

*Direct Link Auto-Catcher:* Pasting any raw YouTube or Spotify link directly into chat downloads immediately for administrators, or queues an ingestion request for standard allowed users.

---

## 6. Development, Testing & Verification

### Running the Test Suite
The project includes unit and integration tests under `tests/`:
```bash
# Using standard Python unittest
python -m unittest discover tests

# Using uv (recommended for isolated ephemeral environments)
uv run --with-requirements requirements.txt python -m unittest discover tests
```

### Environment Variables
Environment variables can be supplied in a `.env` file or exported in the host shell:
```bash
TELEGRAM_BOT_TOKEN="your_bot_token"
ADMIN_USER_IDS="1497076788"
ALLOWED_USER_IDS="1497076788,987654321"
GENIUS_ACCESS_TOKEN="your_genius_token"
ACOUSTID_API_KEY="your_acoustid_key"
BASE_DOWNLOAD_DIR="~/Music"
NAVIDROME_URL="http://localhost:4533"
NAVIDROME_USER="admin"
NAVIDROME_PASS="secret_password"
PAGE_SIZE="6"
WEBAPP_HOST="127.0.0.1"
WEBAPP_PORT="8000"
WEBAPP_EXTERNAL_URL="https://your-domain.duckdns.org"
```

---

## 7. Guidelines for Future AI Agents Modifying this Codebase

1. **Do not put business logic into `handlers/`:** Handlers must only parse input, call methods in `services/`, and format Telegram responses.
2. **Never break transliteration and artist splitting:** Any changes to `utils/helpers.py` must preserve tests in `tests/test_aura_hub.py`.
3. **Preserve ID3v2.3 compatibility:** Use `audio.save(file_path, v2_version=3)` in `services/tagger.py` so embedded artwork and tags remain compatible with automotive head units, Android players, and Navidrome.
4. **Preserve loose `cover.jpg` creation:** Always ensure `cover.jpg` is written inside the album folder in addition to embedding `APIC` tags.
5. **Always gate OS process calls:** If you introduce OS-specific service or systemctl calls, check `platform.system() != "Windows"`.
6. **Always run unit tests before reporting completion:** Ensure `python -m unittest discover tests` passes with 0 failures.
