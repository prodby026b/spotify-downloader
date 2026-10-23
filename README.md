# prodby026b - Spotify Downloader (`spotify-dl`)

Desktop GUI (`customtkinter`) and command-line tool that turns Spotify tracks, albums, and playlists into tagged MP3 files.

Spotify is only used for **metadata** (title, artists, album, duration, cover). The audio is matched and
downloaded with `yt-dlp` from YouTube, with alternative queries and SoundCloud as fallbacks.

- **Modern CustomTkinter GUI**: Launch `python spotify_dl.py` (or `python spotify_dl.py --gui`) for the dark-themed `prodby026b - Spotify Downloader` desktop interface with a Spotify URL input, quality & provider selectors, output folder browser, live progress bar, and scrollable terminal output log.
- **Resilient Pipeline**: Failed or blocked tracks are logged, skipped, and listed at the end. The run never aborts.
- **Full ID3v2.3 Tagging**: Title, artist, album, year, track number, and high-res cover art are embedded with `mutagen`.
- **Smart Candidate Scoring**: Candidates are ranked by title, artist, and duration so live versions, covers, and wrong-length uploads are rejected.
- **Resume Support**: Re-running the same command or GUI session skips existing files and only retries missing tracks.

## Setup

Requires Python 3.9+ and `ffmpeg`.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### ffmpeg (required for MP3 conversion)

`ffmpeg` and `ffprobe` must be on your `PATH`.

| OS | Command |
|---|---|
| Debian / Ubuntu | `sudo apt install ffmpeg python3-tk` |
| macOS (Homebrew) | `brew install ffmpeg python-tk` |
| Windows | `winget install Gyan.FFmpeg` (or `choco install ffmpeg`), then open a new terminal |

Check with `ffmpeg -version`. If it lives somewhere else, pass `--ffmpeg /path/to/ffmpeg`.

## Launching the CustomTkinter GUI

Run the script without arguments (or pass `--gui` / `-g`):

```bash
python spotify_dl.py
# or
python spotify_dl.py --gui
```

### GUI Controls
- **Spotify URL**: Paste one or more Spotify track, album, or playlist links (`open.spotify.com/...` or `spotify:...`).
- **Output Folder**: Defaults to `./downloads`. Click **Browse…** to select any local folder.
- **MP3 Bitrate**: Choose between `128 kbps`, `192 kbps`, `256 kbps`, or `320 kbps`.
- **Providers**: Select `youtube,soundcloud`, `youtube`, `soundcloud`, or `soundcloud,youtube`.
- **Tolerance (s)**: Maximum allowed duration difference in seconds compared to Spotify metadata.
- **Flags**: Toggle **Overwrite existing**, **Lenient match**, **Dry run (preview)**, or **Verbose log**.
- **Start Download / Stop**: Runs the downloader on a background worker thread while streaming real-time progress and logs to the scrollable terminal output box.

## Command-Line Usage

```bash
# one track
python spotify_dl.py "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"

# a playlist or album into ./music/<playlist name>/
python spotify_dl.py "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M" -o music

# several URLs from a file (one per line, # for comments), 320 kbps
python spotify_dl.py -f urls.txt -q 320

# preview what would be downloaded
python spotify_dl.py "<url>" --dry-run
```
