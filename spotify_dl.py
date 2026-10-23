#!/usr/bin/env python3
"""spotify-dl: download Spotify tracks, albums and playlists as tagged MP3 files.
Includes a clean, modern CustomTkinter GUI ('prodby026b - Spotify Downloader') when run with no arguments or --gui.

Metadata : Spotify Web API via spotipy when credentials are set, otherwise a keyless
           fallback that reads Spotify's public embed pages. iTunes fills missing
           album / cover / year.
Audio    : yt-dlp. Ranked YouTube searches first, alternative queries next, then SoundCloud.
Tagging  : mutagen (ID3v2.3: title, artist, album, year, cover art).
GUI      : customtkinter (launched automatically when run without URLs or with --gui).
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests
import yt_dlp
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1, TPE2, TRCK
from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

__version__ = "1.1.0"
BRAND_TITLE = "prodby026b - Spotify Downloader"

log = logging.getLogger("spotify-dl")
console = Console()

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HTTP_TIMEOUT = 20
SPOTIFY_URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[A-Za-z-]+/)?|spotify:)(track|playlist|album)[/:]([A-Za-z0-9]+)"
)
SEARCH_PREFIX = {"youtube": "ytsearch", "soundcloud": "scsearch"}
TMP_DIRNAME = ".spotify_dl_tmp"


class MetadataError(Exception):
    """Spotify metadata could not be fetched."""


class DownloadFailed(Exception):
    """No source produced a usable audio file for a track."""


@dataclass
class Track:
    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    year: str = ""
    duration_s: Optional[int] = None
    track_no: Optional[int] = None
    cover_url: str = ""
    spotify_id: str = ""

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artists else self.title


@dataclass
class Stats:
    ok: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)  # (Track, reason)
    meta_errors: int = 0
    interrupted: bool = False


# --------------------------------------------------------------------------- text helpers

_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_SUFFIX_RE = re.compile(
    r"\s+-\s+(?:\d{4}\s+)?(?:remaster(?:ed)?|remix|live|radio edit|single version|"
    r"album version|mono|stereo|bonus|from\b).*$",
    re.I,
)
_AR_FIX = str.maketrans({"ي": "ی", "ك": "ک"})  # Arabic yeh/kaf -> Persian forms
_BAD_FS = re.compile(r'[<>:"|?*\x00-\x1f]')
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BAD_WORDS = {
    "live", "cover", "karaoke", "instrumental", "remix", "reaction", "slowed", "sped",
    "nightcore", "8d", "mashup", "tutorial", "boosted", "reverb", "acoustic",
}


def clean_title(title: str) -> str:
    """Strip '(feat. X)', '- Remastered 2011', '[Deluxe]' etc. for looser search queries."""
    cleaned = _SUFFIX_RE.sub("", _PAREN_RE.sub("", title))
    return re.sub(r"\s+", " ", cleaned).strip() or title


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.translate(_AR_FIX).casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\W+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return _norm(text).split()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def sanitize(name: str, max_len: int = 150) -> str:
    name = _BAD_FS.sub("", re.sub(r"[/\\]", "-", name)).strip()
    return name[:max_len].strip(" .") or "untitled"


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def explain_error(message: str) -> str:
    msg = _ANSI.sub("", message).strip()
    low = msg.lower()
    rules = (
        (("not a bot", "sign in to confirm"), "YouTube bot check (try --cookies-from-browser)"),
        (("http error 429", "too many requests"), "rate limited (HTTP 429)"),
        (("private video",), "private video"),
        (("in your country", "geo restrict", "geo-restrict"), "geo-blocked"),
        (("video unavailable", "not available", "has been removed", "unavailable"), "unavailable"),
        (("ffprobe", "ffmpeg not found"), "ffmpeg/ffprobe problem"),
    )
    for needles, reason in rules:
        if any(n in low for n in needles):
            return reason
    last = msg.splitlines()[-1] if msg else "unknown error"
    return shorten(re.sub(r"^ERROR:\s*", "", last), 160)


# --------------------------------------------------------------------------- config

def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader so no extra dependency is needed."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


def spotify_creds() -> Optional[tuple[str, str]]:
    cid = os.getenv("SPOTIPY_CLIENT_ID") or os.getenv("SPOTIFY_CLIENT_ID")
    secret = os.getenv("SPOTIPY_CLIENT_SECRET") or os.getenv("SPOTIFY_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


# --------------------------------------------------------------------------- Spotify metadata

def parse_spotify_url(url: str) -> tuple[str, str]:
    url = url.strip()
    if re.search(r"spotify\.link/|spotify\.app\.link/", url):
        try:
            url = requests.get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT).url
        except requests.RequestException as exc:
            raise MetadataError(f"could not resolve short link ({exc}); paste the full open.spotify.com URL")
    m = SPOTIFY_URL_RE.search(url)
    if not m:
        raise MetadataError(f"not a Spotify track/album/playlist URL: {url}")
    return m.group(1), m.group(2)


def _api_track(t: dict, album: Optional[dict] = None) -> Optional[Track]:
    if not isinstance(t, dict) or not t.get("name") or t.get("type") not in (None, "track"):
        return None
    alb = t.get("album") or album or {}
    images = alb.get("images") or []
    dur = t.get("duration_ms")
    return Track(
        title=t["name"],
        artists=[a["name"] for a in (t.get("artists") or []) if a.get("name")],
        album=alb.get("name") or "",
        year=(alb.get("release_date") or "")[:4],
        duration_s=round(dur / 1000) if dur else None,
        track_no=t.get("track_number"),
        cover_url=images[0]["url"] if images else "",
        spotify_id=t.get("id") or "",
    )


def fetch_via_api(kind: str, sid: str, login: bool) -> tuple[str, list[Track]]:
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth, SpotifyOauthError
    except ImportError as exc:
        raise MetadataError("spotipy is not installed (pip install -r requirements.txt)") from exc

    cid, secret = spotify_creds()
    if login:
        auth = SpotifyOAuth(
            client_id=cid,
            client_secret=secret,
            redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
            scope="playlist-read-private playlist-read-collaborative",
            cache_path=str(Path.home() / ".cache" / "spotify-dl-token"),
        )
    else:
        auth = SpotifyClientCredentials(client_id=cid, client_secret=secret)
    sp = spotipy.Spotify(auth_manager=auth, requests_timeout=HTTP_TIMEOUT, retries=3)

    def playlist_page(offset: int) -> dict:
        try:
            return sp._get(f"playlists/{sid}/items", limit=100, offset=offset)
        except spotipy.SpotifyException as exc:
            if exc.http_status != 404:
                raise
            return sp._get(f"playlists/{sid}/tracks", limit=100, offset=offset)

    try:
        tracks: list[Track] = []
        if kind == "track":
            tr = _api_track(sp.track(sid))
            if not tr:
                raise MetadataError("track has no usable metadata")
            return tr.title, [tr]
        if kind == "album":
            alb = sp.album(sid)
            page = alb.get("tracks") or alb.get("items") or {}
            while page:
                tracks += [t for t in (_api_track(x, album=alb) for x in page.get("items") or []) if t]
                page = sp.next(page) if page.get("next") else None
            return alb.get("name") or "album", tracks
        name = (sp.playlist(sid, fields="name") or {}).get("name") or "playlist"
        offset = 0
        while True:
            page = playlist_page(offset)
            items = page.get("items") or []
            for entry in items:
                tr = _api_track(entry.get("item") or entry.get("track"))
                if tr:
                    tracks.append(tr)
            if not items or not page.get("next"):
                break
            offset += len(items)
        return name, tracks
    except spotipy.SpotifyException as exc:
        status = getattr(exc, "http_status", "?")
        hint = ""
        if status in (401, 403):
            hint = " (playlist contents are only readable for playlists you own or collaborate on; try --login)"
        raise MetadataError(f"Spotify API returned HTTP {status}{hint}") from exc
    except (SpotifyOauthError, requests.RequestException) as exc:
        raise MetadataError(f"Spotify auth/network error: {exc}") from exc


def _find_key(obj, key: str):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _embed_cover(entity: dict) -> str:
    best_url, best_w = "", -1
    for key in ("visualIdentity", "coverArt"):
        block = entity.get(key) or {}
        for img in block.get("image") or block.get("sources") or []:
            width = img.get("maxWidth") or img.get("width") or 0
            if img.get("url") and width > best_w:
                best_url, best_w = img["url"], width
    return best_url


def fetch_via_embed(kind: str, sid: str) -> tuple[str, list[Track]]:
    """Keyless fallback: parse the JSON that Spotify embeds in its public embed page."""
    try:
        r = requests.get(
            f"https://open.spotify.com/embed/{kind}/{sid}",
            headers={"User-Agent": UA, "Accept-Language": "en"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        raise MetadataError(f"embed page request failed: {exc}") from exc
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise MetadataError("embed page has no data block (Spotify changed its markup?)")
    try:
        entity = _find_key(json.loads(m.group(1)), "entity")
    except ValueError as exc:
        raise MetadataError("embed data is not valid JSON") from exc
    if not isinstance(entity, dict):
        raise MetadataError("embed data has no entity (private or unavailable item?)")

    name = entity.get("name") or entity.get("title") or kind
    cover = _embed_cover(entity)

    def to_seconds(v) -> Optional[int]:
        if not isinstance(v, (int, float)) or v <= 0:
            return None
        return round(v / 1000) if v > 10_000 else round(v)

    if kind == "track":
        artists = [a["name"] for a in (entity.get("artists") or []) if a.get("name")]
        if not artists and entity.get("subtitle"):
            artists = [entity["subtitle"].replace("\xa0", " ").strip()]
        return name, [Track(title=name, artists=artists, duration_s=to_seconds(entity.get("duration")),
                            cover_url=cover, spotify_id=sid)]

    tracks = []
    for row in entity.get("trackList") or []:
        title = row.get("title")
        if not title:
            continue
        artist = (row.get("subtitle") or "").replace("\xa0", " ").strip()
        tracks.append(Track(
            title=title,
            artists=[artist] if artist else [],
            album=name if kind == "album" else "",
            cover_url=cover if kind == "album" else "",
            duration_s=to_seconds(row.get("duration")),
            spotify_id=(row.get("uri") or "").split(":")[-1],
        ))
    if len(tracks) >= 100:
        log.warning("Embed pages list at most ~100 tracks; this playlist may be truncated. "
                    "Use Spotify API credentials (see README) for the full list.")
    return name, tracks


_warned_no_creds = False


def fetch_metadata(kind: str, sid: str, login: bool) -> tuple[str, list[Track]]:
    global _warned_no_creds
    if spotify_creds():
        try:
            return fetch_via_api(kind, sid, login)
        except MetadataError as exc:
            log.warning("Spotify API failed: %s. Trying keyless embed fallback.", exc)
    else:
        if login:
            log.warning("--login needs SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET; ignoring it.")
        if not _warned_no_creds:
            log.info("No Spotify API credentials set: using the keyless embed reader (see README).")
            _warned_no_creds = True
    return fetch_via_embed(kind, sid)


_itunes_cache: dict[str, Optional[dict]] = {}


def enrich_from_itunes(track: Track) -> None:
    """Fill missing album / cover / year from the public iTunes Search API (no key needed)."""
    if track.album and track.cover_url and track.year:
        return
    term = f"{track.artist} {clean_title(track.title)}".strip()
    if term not in _itunes_cache:
        try:
            r = requests.get("https://itunes.apple.com/search", headers={"User-Agent": UA},
                             params={"term": term, "entity": "song", "media": "music", "limit": 5},
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            results = r.json().get("results", [])
        except (requests.RequestException, ValueError) as exc:
            log.debug("iTunes lookup failed for %s: %s", term, exc)
            results = []
        want_t, want_a = _norm(clean_title(track.title)), _norm(track.artist)
        match = None
        for res in results:
            got_t, got_a = _norm(clean_title(res.get("trackName", ""))), _norm(res.get("artistName", ""))
            if _ratio(want_t, got_t) < 0.85:
                continue
            if want_a and got_a and not (got_a in want_a or want_a in got_a or _ratio(want_a, got_a) >= 0.6):
                continue
            match = res
            break
        _itunes_cache[term] = match
    res = _itunes_cache[term]
    if not res:
        return
    track.album = track.album or res.get("collectionName") or ""
    track.year = track.year or (res.get("releaseDate") or "")[:4]
    track.track_no = track.track_no or res.get("trackNumber")
    if not track.cover_url and res.get("artworkUrl100"):
        track.cover_url = re.sub(r"\d+x\d+bb", "1000x1000bb", res["artworkUrl100"])


# --------------------------------------------------------------------------- audio search / download

class YDLLogger:
    """Route yt-dlp's console output into our debug log so it never garbles the progress bar."""

    def debug(self, msg): log.debug("yt-dlp: %s", msg)
    def info(self, msg): log.debug("yt-dlp: %s", msg)
    def warning(self, msg): log.debug("yt-dlp warning: %s", msg)
    def error(self, msg): log.debug("yt-dlp error: %s", msg)


class ProviderHealth:
    """Circuit breaker: if a provider starts blocking us, stop hammering it for a while."""

    HINTS = ("not a bot", "sign in to confirm", "http error 429", "too many requests")

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    def usable(self, provider: str) -> bool:
        return time.monotonic() >= self._until.get(provider, 0.0)

    def report(self, provider: str, message: str) -> None:
        if any(h in message.lower() for h in self.HINTS):
            if self.usable(provider):
                log.warning("%s looks blocked or rate-limited: %s. Skipping it for 2 minutes.",
                            provider, explain_error(message))
            self._until[provider] = time.monotonic() + 120


def base_ydl_opts(args) -> dict:
    ffmpeg_loc = getattr(args, "ffmpeg", None)
    if not ffmpeg_loc:
        base_path = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        for sub in [base_path, base_path / "ffmpeg", base_path / "ffmpeg" / "bin"]:
            if (sub / "ffmpeg.exe").exists():
                ffmpeg_loc = str(sub)
                break

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": YDLLogger(), "socket_timeout": 20, "retries": 3, "extractor_retries": 2,
    }
    if getattr(args, "cookies", None):
        opts["cookiefile"] = args.cookies
    if getattr(args, "cookies_from_browser", None):
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if getattr(args, "proxy", None):
        opts["proxy"] = args.proxy
    if ffmpeg_loc:
        opts["ffmpeg_location"] = ffmpeg_loc
    return opts


def build_attempts(track: Track, providers: list[str]) -> list[tuple[str, str]]:
    """Ordered (provider, query) ladder: most precise first, looser/alternative queries after."""
    artist = track.artist
    primary = track.artists[0] if track.artists else ""
    clean = clean_title(track.title)
    queries = {
        "youtube": [f"{artist} - {track.title} official audio", f"{artist} {clean} audio", f"{primary} {clean}"],
        "soundcloud": [f"{artist} {clean}", f"{primary} {clean}"],
    }
    out, seen = [], set()
    for provider in providers:
        for q in queries.get(provider, []):
            q = re.sub(r"\s+", " ", q).strip()
            if (provider, q.lower()) not in seen:
                seen.add((provider, q.lower()))
                out.append((provider, q))
    return out


def search_candidates(provider: str, query: str, args, n: int = 8) -> list[dict]:
    opts = {**base_ydl_opts(args), "extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{SEARCH_PREFIX[provider]}{n}:{query}", download=False)
    return [e for e in (info or {}).get("entries") or [] if e]


def score_candidate(c: dict, track: Track, tol: float, lenient: bool = False) -> Optional[float]:
    """Score a search hit against the Spotify track. None means 'reject'."""
    title_tokens = set(_tokens(c.get("title") or ""))
    if not title_tokens:
        return None
    channel = c.get("channel") or c.get("uploader") or ""
    want_title = set(_tokens(clean_title(track.title)))
    want_artist = set(_tokens(track.artist))
    pool = title_tokens | set(_tokens(channel))

    diff = None
    dur = c.get("duration")
    if track.duration_s and isinstance(dur, (int, float)) and dur > 0:
        diff = abs(dur - track.duration_s)
        if diff > tol:
            return None

    title_frac = len(want_title & title_tokens) / max(len(want_title), 1)
    artist_frac = len(want_artist & pool) / len(want_artist) if want_artist else 0.5
    if title_frac < 0.4:
        exact_len = diff is not None and diff <= 3
        if not (exact_len and (artist_frac > 0 or lenient)):
            return None

    score = 3 * title_frac + 2 * artist_frac
    if diff is not None:
        score += 4 * (1 - diff / tol)
    score -= 2 * len((title_tokens & _BAD_WORDS) - set(_tokens(track.title)))
    if channel.lower().endswith("- topic"):
        score += 1.5
    if "official" in title_tokens:
        score += 0.5
    return score


def rank_candidates(cands: list[dict], track: Track, tol: float, lenient: bool = False) -> list[dict]:
    scored = [(s, c) for c in cands if (s := score_candidate(c, track, tol, lenient)) is not None]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]


def download_audio(url: str, base: Path, args, ui) -> dict:
    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                ui.status("downloading", 100 * d.get("downloaded_bytes", 0) / total)
        elif d.get("status") == "finished":
            ui.status("converting to mp3", 100)

    opts = {
        **base_ydl_opts(args),
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(base).replace("%", "%%") + ".%(ext)s",
        "progress_hooks": [hook],
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": str(args.quality)}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True) or {}


# --------------------------------------------------------------------------- cover art + tagging

def fetch_cover(url: str, cache: Optional[dict] = None) -> Optional[tuple[bytes, str]]:
    if not url:
        return None
    cache = _cover_cache if cache is None else cache
    if url in cache:
        return cache[url]
    result = None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.content
        if data[:3] == b"\xff\xd8\xff":
            result = (data, "image/jpeg")
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            result = (data, "image/png")
        else:
            log.debug("cover %s is not JPEG/PNG; skipping", url)
    except requests.RequestException as exc:
        log.debug("cover download failed (%s): %s", url, exc)
    if len(cache) >= 32:
        cache.pop(next(iter(cache)))
    cache[url] = result
    return result


_cover_cache: dict[str, Optional[tuple[bytes, str]]] = {}


def pick_thumbnail(info: dict) -> str:
    """Last-resort cover: best JPEG thumbnail of the source video/track."""
    thumbs = [t for t in (info.get("thumbnails") or []) if ".jpg" in (t.get("url") or "").lower()]
    thumbs.sort(key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
    return thumbs[-1]["url"] if thumbs else ""


def tag_mp3(path: Path, track: Track, cover: Optional[tuple[bytes, str]]) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for frame in ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TDRC", "APIC"):
        tags.delall(frame)
    tags.add(TIT2(encoding=3, text=track.title))
    if track.artists:
        tags.add(TPE1(encoding=3, text=track.artists))
        tags.add(TPE2(encoding=3, text=track.artists[0]))
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))
    if track.track_no:
        tags.add(TRCK(encoding=3, text=str(track.track_no)))
    if track.year:
        tags.add(TDRC(encoding=3, text=track.year))
    if cover:
        tags.add(APIC(encoding=3, mime=cover[1], type=3, desc="Cover", data=cover[0]))
    tags.save(path, v2_version=3)


# --------------------------------------------------------------------------- UI Abstractions

class UI:
    """Two stacked rich progress displays: the current track, and the overall run."""

    def __init__(self, total: int, title: str) -> None:
        self.current = Progress(SpinnerColumn(), TextColumn("{task.description}"),
                                BarColumn(bar_width=None), TextColumn("{task.percentage:>3.0f}%"),
                                console=console, expand=True)
        self.overall = Progress(TextColumn("[bold cyan]{task.description}"), BarColumn(bar_width=None),
                                MofNCompleteColumn(), TimeElapsedColumn(), TimeRemainingColumn(),
                                console=console, expand=True)
        self._cur = self.current.add_task("", total=100)
        self._all = self.overall.add_task(escape(shorten(title, 28)), total=total)
        self._label = ""
        self._live = Live(Group(self.current, self.overall), console=console, refresh_per_second=10)

    def __enter__(self) -> "UI":
        self._live.start()
        return self

    def __exit__(self, *exc) -> None:
        self.current.update(self._cur, visible=False)
        self._live.stop()
        if not console.is_terminal:
            console.print()

    def begin(self, label: str) -> None:
        self._label = escape(shorten(label, 60))
        self.current.reset(self._cur, description=self._label, completed=0)

    def status(self, text: str, pct: Optional[float] = None) -> None:
        kw = {"description": f"{self._label} [dim]{escape(text)}[/dim]"}
        if pct is not None:
            kw["completed"] = pct
        self.current.update(self._cur, **kw)

    def advance(self) -> None:
        self.overall.advance(self._all)


class GuiUI:
    """Progress bridge for CustomTkinter GUI mode."""

    def __init__(
        self,
        total: int,
        title: str,
        on_track_begin: Callable[[str, int, int], None],
        on_track_status: Callable[[str, Optional[float]], None],
        on_overall_advance: Callable[[int, int], None],
    ) -> None:
        self.total = max(total, 1)
        self.title = title
        self.completed = 0
        self._on_begin = on_track_begin
        self._on_status = on_track_status
        self._on_advance = on_overall_advance

    def __enter__(self) -> "GuiUI":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def begin(self, label: str) -> None:
        self._on_begin(label, self.completed, self.total)

    def status(self, text: str, pct: Optional[float] = None) -> None:
        self._on_status(text, pct)

    def advance(self) -> None:
        self.completed += 1
        self._on_advance(self.completed, self.total)


# --------------------------------------------------------------------------- per-track pipeline

def process_track(track: Track, out_dir: Path, args, ui, health: ProviderHealth) -> tuple[str, str]:
    """Returns ('ok' | 'skipped', detail). Raises DownloadFailed if no source worked."""
    stem = sanitize(track.label)
    final = out_dir / f"{stem}.mp3"
    if final.exists() and not args.overwrite:
        return "skipped", "already exists"

    ui.status("fetching metadata")
    enrich_from_itunes(track)

    work = out_dir / TMP_DIRNAME
    reasons: list[str] = []
    tried: set[str] = set()
    try:
        for provider, query in build_attempts(track, args.providers):
            if not health.usable(provider):
                reasons.append(f"{provider}: skipped (blocked recently)")
                continue
            ui.status(f"searching {provider}")
            try:
                cands = search_candidates(provider, query, args)
            except yt_dlp.utils.YoutubeDLError as exc:
                health.report(provider, str(exc))
                reasons.append(f"{provider} search: {explain_error(str(exc))}")
                continue
            ranked = rank_candidates(cands, track, args.tolerance, args.lenient)
            if not ranked:
                reasons.append(f"{provider}: no close match")
                continue
            for cand in ranked[:2]:
                url = cand.get("url") or cand.get("webpage_url")
                if not url or url in tried:
                    continue
                tried.add(url)
                ui.status(f"{provider}: {shorten(cand.get('title') or '', 40)}", 0)
                work.mkdir(parents=True, exist_ok=True)
                try:
                    info = download_audio(url, work / stem, args, ui)
                except yt_dlp.utils.YoutubeDLError as exc:
                    health.report(provider, str(exc))
                    reasons.append(f"{provider}: {explain_error(str(exc))}")
                    log.debug("download failed for %s", url, exc_info=True)
                    if not health.usable(provider):
                        break
                    continue
                produced = work / f"{stem}.mp3"
                if not produced.is_file() or produced.stat().st_size < 10_000:
                    reasons.append(f"{provider}: empty output file")
                    continue

                ui.status("tagging")
                cover = fetch_cover(track.cover_url) or fetch_cover(pick_thumbnail(info))
                try:
                    tag_mp3(produced, track, cover)
                except Exception as exc:
                    log.warning("Could not write tags for %s: %s", track.label, exc)
                out_dir.mkdir(parents=True, exist_ok=True)
                os.replace(produced, final)
                return "ok", f"{provider}: {shorten(cand.get('title') or '', 60)}"
        unique = list(dict.fromkeys(reasons))
        raise DownloadFailed("; ".join(unique) or "no usable source found")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_collection(
    name: str,
    tracks: list[Track],
    out_dir: Path,
    args,
    health: ProviderHealth,
    stats: Stats,
    ui_factory: Optional[Callable[[int, str], object]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    failed_before = len(stats.failed)
    ctx = ui_factory(len(tracks), name) if ui_factory else UI(len(tracks), name)
    with ctx as ui:
        for i, track in enumerate(tracks, 1):
            if stop_event and stop_event.is_set():
                stats.interrupted = True
                break
            ui.begin(f"[{i}/{len(tracks)}] {track.label}")
            downloaded = False
            try:
                status, detail = process_track(track, out_dir, args, ui, health)
            except DownloadFailed as exc:
                status, detail = "failed", str(exc)
            except KeyboardInterrupt:
                stats.interrupted = True
                break
            except Exception as exc:
                log.debug("unexpected error", exc_info=True)
                status, detail = "failed", f"unexpected {type(exc).__name__}: {exc}"

            if status == "ok":
                stats.ok += 1
                downloaded = True
                log.info("OK    %s  [%s]", track.label, detail)
            elif status == "skipped":
                stats.skipped += 1
                log.info("SKIP  %s  (%s)", track.label, detail)
            else:
                stats.failed.append((track, detail))
                log.warning("FAIL  %s: %s", track.label, detail)
            ui.advance()
            if downloaded and args.delay > 0 and i < len(tracks):
                if stop_event and stop_event.wait(args.delay):
                    stats.interrupted = True
                    break
                elif not stop_event:
                    time.sleep(args.delay)

    new_failures = stats.failed[failed_before:]
    if new_failures:
        report = out_dir / "failed_tracks.txt"
        report.write_text(
            "\n".join(f"{t.label}\t{t.spotify_id}\t{reason}" for t, reason in new_failures) + "\n",
            encoding="utf-8",
        )
        log.info("Failed tracks listed in %s", report)


def print_tracks(name: str, tracks: list[Track]) -> None:
    table = Table(title=escape(name), show_lines=False)
    for col in ("#", "Artist", "Title", "Album", "Length"):
        table.add_column(col)
    for i, t in enumerate(tracks, 1):
        length = f"{t.duration_s // 60}:{t.duration_s % 60:02d}" if t.duration_s else "-"
        table.add_row(str(i), escape(t.artist), escape(t.title), escape(t.album or "-"), length)
    console.print(table)


def print_summary(stats: Stats, elapsed: float) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_row("[green]Downloaded[/]", str(stats.ok))
    grid.add_row("[yellow]Already existed[/]", str(stats.skipped))
    grid.add_row("[red]Failed[/]", str(len(stats.failed)))
    if stats.meta_errors:
        grid.add_row("[red]URLs with metadata errors[/]", str(stats.meta_errors))
    grid.add_row("[dim]Elapsed[/]", f"{elapsed:.0f}s")
    console.print(Panel(grid, title="Summary", expand=False))
    if stats.failed:
        table = Table(title="Failed tracks", title_style="red")
        table.add_column("Track")
        table.add_column("Reason", overflow="fold")
        for track, reason in stats.failed:
            table.add_row(escape(track.label), escape(reason))
        console.print(table)


# --------------------------------------------------------------------------- CustomTkinter GUI

class QueueLogHandler(logging.Handler):
    """Thread-safe logging handler that pushes formatted log records into a queue for the GUI."""

    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.log_queue.put(("log", record.levelname, msg))
        except Exception:
            self.handleError(record)


def launch_gui() -> int:
    """Launch the 'prodby026b - Spotify Downloader' CustomTkinter desktop GUI."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        import customtkinter as ctk
    except ImportError as exc:
        sys.stderr.write(
            f"Error: customtkinter or tkinter is not available ({exc}).\n"
            "Install it with: pip install customtkinter>=5.2.2\n"
        )
        return 2

    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("green")

    class SpotifyDownloaderGUI(ctk.CTk):
        def __init__(self) -> None:
            super().__init__()
            self.title(BRAND_TITLE)
            self.geometry("920x720")
            self.minsize(780, 600)
            self.configure(fg_color="#0B0F17")

            self.event_queue: queue.Queue = queue.Queue()
            self.stop_event = threading.Event()
            self.worker_thread: Optional[threading.Thread] = None
            self.is_running = False

            self._setup_logging()
            self._build_ui()
            self._check_environment()
            self.after(80, self._poll_queue)

        def _setup_logging(self) -> None:
            load_dotenv()
            root = logging.getLogger()
            root.setLevel(logging.DEBUG)
            root.handlers.clear()
            self.gui_handler = QueueLogHandler(self.event_queue)
            self.gui_handler.setLevel(logging.INFO)
            self.gui_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S"))
            root.addHandler(self.gui_handler)
            for noisy in ("urllib3", "spotipy", "charset_normalizer"):
                logging.getLogger(noisy).setLevel(logging.CRITICAL if noisy == "spotipy" else logging.WARNING)

        def _build_ui(self) -> None:
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(3, weight=1)

            # 1. Header Banner
            header = ctk.CTkFrame(self, fg_color="#111827", corner_radius=10, border_width=1, border_color="#1F2937")
            header.grid(row=0, column=0, padx=20, pady=(18, 10), sticky="ew")
            header.grid_columnconfigure(0, weight=1)

            title_lbl = ctk.CTkLabel(
                header,
                text="prodby026b — Spotify Downloader",
                font=ctk.CTkFont(size=20, weight="bold"),
                text_color="#F9FAFB",
            )
            title_lbl.grid(row=0, column=0, padx=18, pady=(14, 2), sticky="w")

            sub_lbl = ctk.CTkLabel(
                header,
                text="Download Spotify tracks, albums, and playlists as ID3v2.3 tagged MP3s with cover art",
                font=ctk.CTkFont(size=12),
                text_color="#9CA3AF",
            )
            sub_lbl.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="w")

            self.version_lbl = ctk.CTkLabel(
                header,
                text=f"v{__version__}",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color="#1DB954",
            )
            self.version_lbl.grid(row=0, column=1, rowspan=2, padx=18, pady=14, sticky="e")

            # 2. Input & Options Card
            controls = ctk.CTkFrame(self, fg_color="#111827", corner_radius=10, border_width=1, border_color="#1F2937")
            controls.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
            controls.grid_columnconfigure(1, weight=1)

            # Row 0: Spotify URL Input
            url_lbl = ctk.CTkLabel(controls, text="Spotify URL:", font=ctk.CTkFont(size=13, weight="bold"), text_color="#E5E7EB")
            url_lbl.grid(row=0, column=0, padx=(18, 12), pady=(16, 10), sticky="w")

            self.url_entry = ctk.CTkEntry(
                controls,
                placeholder_text="https://open.spotify.com/track/... or playlist/... or album/...",
                height=38,
                fg_color="#0B0F17",
                border_color="#374151",
                text_color="#F9FAFB",
            )
            self.url_entry.grid(row=0, column=1, columnspan=2, padx=(0, 10), pady=(16, 10), sticky="ew")

            paste_btn = ctk.CTkButton(
                controls,
                text="Paste",
                width=80,
                height=38,
                fg_color="#1F2937",
                hover_color="#374151",
                text_color="#E5E7EB",
                command=self._paste_clipboard,
            )
            paste_btn.grid(row=0, column=3, padx=(0, 18), pady=(16, 10))

            # Row 1: Output Folder
            out_lbl = ctk.CTkLabel(controls, text="Output Folder:", font=ctk.CTkFont(size=13, weight="bold"), text_color="#E5E7EB")
            out_lbl.grid(row=1, column=0, padx=(18, 12), pady=10, sticky="w")

            self.output_entry = ctk.CTkEntry(
                controls,
                height=36,
                fg_color="#0B0F17",
                border_color="#374151",
                text_color="#F9FAFB",
            )
            self.output_entry.insert(0, str(Path("downloads").resolve()))
            self.output_entry.grid(row=1, column=1, columnspan=2, padx=(0, 10), pady=10, sticky="ew")

            browse_btn = ctk.CTkButton(
                controls,
                text="Browse…",
                width=80,
                height=36,
                fg_color="#1F2937",
                hover_color="#374151",
                text_color="#E5E7EB",
                command=self._browse_folder,
            )
            browse_btn.grid(row=1, column=3, padx=(0, 18), pady=10)

            # Row 2: Quality + Audio Providers Dropdowns
            qual_lbl = ctk.CTkLabel(controls, text="MP3 Bitrate:", font=ctk.CTkFont(size=13, weight="bold"), text_color="#E5E7EB")
            qual_lbl.grid(row=2, column=0, padx=(18, 12), pady=10, sticky="w")

            opts_subframe = ctk.CTkFrame(controls, fg_color="transparent")
            opts_subframe.grid(row=2, column=1, columnspan=3, padx=(0, 18), pady=10, sticky="ew")

            self.quality_var = ctk.StringVar(value="320 kbps")
            self.quality_menu = ctk.CTkOptionMenu(
                opts_subframe,
                values=["128 kbps", "192 kbps", "256 kbps", "320 kbps"],
                variable=self.quality_var,
                width=130,
                height=34,
                fg_color="#1F2937",
                button_color="#374151",
                button_hover_color="#4B5563",
            )
            self.quality_menu.pack(side="left", padx=(0, 18))

            prov_lbl = ctk.CTkLabel(opts_subframe, text="Providers:", font=ctk.CTkFont(size=13, weight="bold"), text_color="#E5E7EB")
            prov_lbl.pack(side="left", padx=(0, 8))

            self.provider_var = ctk.StringVar(value="youtube,soundcloud")
            self.provider_menu = ctk.CTkOptionMenu(
                opts_subframe,
                values=["youtube,soundcloud", "youtube", "soundcloud", "soundcloud,youtube"],
                variable=self.provider_var,
                width=175,
                height=34,
                fg_color="#1F2937",
                button_color="#374151",
                button_hover_color="#4B5563",
            )
            self.provider_menu.pack(side="left", padx=(0, 18))

            tol_lbl = ctk.CTkLabel(opts_subframe, text="Tolerance (s):", font=ctk.CTkFont(size=13, weight="bold"), text_color="#E5E7EB")
            tol_lbl.pack(side="left", padx=(0, 8))

            self.tolerance_entry = ctk.CTkEntry(
                opts_subframe,
                width=65,
                height=34,
                fg_color="#0B0F17",
                border_color="#374151",
            )
            self.tolerance_entry.insert(0, "20")
            self.tolerance_entry.pack(side="left")

            # Row 3: Checkboxes & Action Buttons
            flags_frame = ctk.CTkFrame(controls, fg_color="transparent")
            flags_frame.grid(row=3, column=0, columnspan=4, padx=18, pady=(6, 16), sticky="ew")
            flags_frame.grid_columnconfigure(4, weight=1)

            self.overwrite_var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                flags_frame, text="Overwrite existing", variable=self.overwrite_var,
                fg_color="#1DB954", hover_color="#1ed760", text_color="#D1D5DB"
            ).grid(row=0, column=0, padx=(0, 16), sticky="w")

            self.lenient_var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                flags_frame, text="Lenient match", variable=self.lenient_var,
                fg_color="#1DB954", hover_color="#1ed760", text_color="#D1D5DB"
            ).grid(row=0, column=1, padx=(0, 16), sticky="w")

            self.dry_run_var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                flags_frame, text="Dry run (preview)", variable=self.dry_run_var,
                fg_color="#1DB954", hover_color="#1ed760", text_color="#D1D5DB"
            ).grid(row=0, column=2, padx=(0, 16), sticky="w")

            self.verbose_var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                flags_frame, text="Verbose log", variable=self.verbose_var,
                fg_color="#1DB954", hover_color="#1ed760", text_color="#D1D5DB",
                command=self._toggle_verbose,
            ).grid(row=0, column=3, padx=(0, 16), sticky="w")

            btn_box = ctk.CTkFrame(flags_frame, fg_color="transparent")
            btn_box.grid(row=0, column=5, sticky="e")

            self.stop_btn = ctk.CTkButton(
                btn_box,
                text="Stop",
                width=90,
                height=38,
                fg_color="#7F1D1D",
                hover_color="#991B1B",
                text_color="#FEE2E2",
                state="disabled",
                command=self._stop_download,
            )
            self.stop_btn.pack(side="left", padx=(0, 10))

            self.start_btn = ctk.CTkButton(
                btn_box,
                text="Start Download",
                width=155,
                height=38,
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="#1DB954",
                hover_color="#1ed760",
                text_color="#052E16",
                command=self._start_download,
            )
            self.start_btn.pack(side="left")

            # 3. Progress Section
            prog_frame = ctk.CTkFrame(self, fg_color="#111827", corner_radius=10, border_width=1, border_color="#1F2937")
            prog_frame.grid(row=2, column=0, padx=20, pady=6, sticky="ew")
            prog_frame.grid_columnconfigure(0, weight=1)

            status_row = ctk.CTkFrame(prog_frame, fg_color="transparent")
            status_row.grid(row=0, column=0, padx=18, pady=(12, 4), sticky="ew")
            status_row.grid_columnconfigure(0, weight=1)

            self.track_status_lbl = ctk.CTkLabel(
                status_row,
                text="Ready — paste a Spotify track, album, or playlist URL above.",
                font=ctk.CTkFont(size=12),
                text_color="#D1D5DB",
                anchor="w",
            )
            self.track_status_lbl.grid(row=0, column=0, sticky="ew")

            self.counter_lbl = ctk.CTkLabel(
                status_row,
                text="0 / 0 tracks",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color="#9CA3AF",
            )
            self.counter_lbl.grid(row=0, column=1, padx=(12, 0), sticky="e")

            self.track_progress = ctk.CTkProgressBar(
                prog_frame, height=8, fg_color="#1F2937", progress_color="#1DB954"
            )
            self.track_progress.set(0)
            self.track_progress.grid(row=1, column=0, padx=18, pady=(4, 14), sticky="ew")

            # 4. Scrollable Terminal Output Log
            log_frame = ctk.CTkFrame(self, fg_color="#111827", corner_radius=10, border_width=1, border_color="#1F2937")
            log_frame.grid(row=3, column=0, padx=20, pady=(8, 18), sticky="nsew")
            log_frame.grid_columnconfigure(0, weight=1)
            log_frame.grid_rowconfigure(1, weight=1)

            log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
            log_header.grid(row=0, column=0, padx=16, pady=(10, 4), sticky="ew")
            log_header.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                log_header,
                text="Terminal Output Log",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="#9CA3AF",
            ).grid(row=0, column=0, sticky="w")

            clear_btn = ctk.CTkButton(
                log_header,
                text="Clear Log",
                width=80,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color="#1F2937",
                hover_color="#374151",
                text_color="#D1D5DB",
                command=self._clear_log,
            )
            clear_btn.grid(row=0, column=1, sticky="e")

            self.log_box = ctk.CTkTextbox(
                log_frame,
                font=ctk.CTkFont(family="Consolas", size=12),
                fg_color="#070A0F",
                text_color="#E5E7EB",
                border_width=1,
                border_color="#1F2937",
                wrap="word",
            )
            self.log_box.grid(row=1, column=0, padx=16, pady=(4, 14), sticky="nsew")
            self.log_box.configure(state="disabled")

        def _check_environment(self) -> None:
            self._append_log("INFO", f"{BRAND_TITLE} initialized (v{__version__})")
            if ffmpeg_available(None):
                self._append_log("INFO", "ffmpeg and ffprobe successfully detected.")
            else:
                self._append_log("WARNING", "ffmpeg/ffprobe not found. Please place 'ffmpeg' folder in the script directory.")

        def _toggle_verbose(self) -> None:
            self.gui_handler.setLevel(logging.DEBUG if self.verbose_var.get() else logging.INFO)

        def _paste_clipboard(self) -> None:
            try:
                clip = self.clipboard_get().strip()
                if clip:
                    self.url_entry.delete(0, "end")
                    self.url_entry.insert(0, clip)
            except Exception:
                pass

        def _browse_folder(self) -> None:
            folder = filedialog.askdirectory(initialdir=self.output_entry.get() or ".")
            if folder:
                self.output_entry.delete(0, "end")
                self.output_entry.insert(0, folder)

        def _clear_log(self) -> None:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")

        def _append_log(self, level: str, message: str) -> None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        def _start_download(self) -> None:
            if self.is_running:
                return
            raw_url = self.url_entry.get().strip()
            if not raw_url:
                self._append_log("ERROR", "Please enter at least one Spotify track, album, or playlist URL.")
                return

            urls = [u.strip() for u in re.split(r"[\s,]+", raw_url) if u.strip()]
            quality = int(self.quality_var.get().split()[0])
            providers = [p.strip() for p in self.provider_var.get().split(",") if p.strip()]
            try:
                tolerance = float(self.tolerance_entry.get().strip() or "20")
            except ValueError:
                tolerance = 20.0

            args = argparse.Namespace(
                urls=urls,
                file=None,
                output=self.output_entry.get().strip() or "downloads",
                quality=quality,
                providers=providers,
                tolerance=tolerance,
                lenient=self.lenient_var.get(),
                login=False,
                cookies=None,
                cookies_from_browser=None,
                proxy=None,
                ffmpeg=None,
                overwrite=self.overwrite_var.get(),
                delay=1.0,
                dry_run=self.dry_run_var.get(),
                log_file=None,
                verbose=self.verbose_var.get(),
            )

            self.is_running = True
            self.stop_event.clear()
            self.start_btn.configure(state="disabled", text="Downloading…")
            self.stop_btn.configure(state="normal")
            self.track_progress.set(0)

            self.worker_thread = threading.Thread(target=self._run_worker, args=(args,), daemon=True)
            self.worker_thread.start()

        def _stop_download(self) -> None:
            if self.is_running:
                self.stop_event.set()
                log.warning("Cancellation requested — finishing current step...")
                self.stop_btn.configure(state="disabled")

        def _run_worker(self, args: argparse.Namespace) -> None:
            started = time.monotonic()
            stats = Stats()
            health = ProviderHealth()

            if not args.dry_run and not ffmpeg_available(args.ffmpeg):
                log.error("ffmpeg/ffprobe not found. Please ensure ffmpeg folder is next to the script.")
                self.event_queue.put(("done", stats, time.monotonic() - started))
                return

            def make_ui(total: int, title: str) -> GuiUI:
                return GuiUI(
                    total=total,
                    title=title,
                    on_track_begin=lambda label, done, tot: self.event_queue.put(("begin", label, done, tot)),
                    on_track_status=lambda text, pct: self.event_queue.put(("status", text, pct)),
                    on_overall_advance=lambda done, tot: self.event_queue.put(("advance", done, tot)),
                )

            for url in args.urls:
                if self.stop_event.is_set():
                    stats.interrupted = True
                    break
                try:
                    log.info("── Fetching Spotify metadata: %s", url)
                    kind, sid = parse_spotify_url(url)
                    name, tracks = fetch_metadata(kind, sid, args.login)
                except MetadataError as exc:
                    log.error("%s: %s", url, exc)
                    stats.meta_errors += 1
                    continue
                if not tracks:
                    log.error("%s: no tracks found", url)
                    stats.meta_errors += 1
                    continue

                log.info("Found '%s' (%s) with %d track(s)", name, kind, len(tracks))
                if args.dry_run:
                    for idx, t in enumerate(tracks, 1):
                        dur_str = f"{t.duration_s // 60}:{t.duration_s % 60:02d}" if t.duration_s else "--:--"
                        log.info("  %02d. %s [%s] (%s)", idx, t.label, t.album or "-", dur_str)
                    continue

                out_dir = Path(args.output) if kind == "track" else Path(args.output) / sanitize(name)
                run_collection(name, tracks, out_dir, args, health, stats, ui_factory=make_ui, stop_event=self.stop_event)
                if stats.interrupted:
                    log.warning("Run stopped by user. Completed tracks are saved in %s", out_dir)
                    break

            elapsed = time.monotonic() - started
            log.info(
                "Summary — Downloaded: %d | Skipped: %d | Failed: %d | Elapsed: %.1fs",
                stats.ok, stats.skipped, len(stats.failed), elapsed,
            )
            self.event_queue.put(("done", stats, elapsed))

        def _poll_queue(self) -> None:
            try:
                while True:
                    item = self.event_queue.get_nowait()
                    kind = item[0]
                    if kind == "log":
                        _, level, msg = item
                        self._append_log(level, msg)
                    elif kind == "begin":
                        _, label, done, tot = item
                        self._current_label = label
                        self.track_status_lbl.configure(text=label)
                        self.counter_lbl.configure(text=f"{done} / {tot} tracks")
                        self.track_progress.set(0)
                    elif kind == "status":
                        _, text, pct = item
                        base = getattr(self, "_current_label", "")
                        self.track_status_lbl.configure(text=f"{base} — {text}" if base else text)
                        if pct is not None:
                            self.track_progress.set(max(0.0, min(1.0, pct / 100.0)))
                    elif kind == "advance":
                        _, done, tot = item
                        self.counter_lbl.configure(text=f"{done} / {tot} tracks")
                        self.track_progress.set(1.0)
                    elif kind == "done":
                        _, stats, elapsed = item
                        self.is_running = False
                        self.start_btn.configure(state="normal", text="Start Download")
                        self.stop_btn.configure(state="disabled")
                        if stats.interrupted:
                            self.track_status_lbl.configure(text="Stopped by user.")
                        else:
                            self.track_status_lbl.configure(
                                text=f"Finished in {elapsed:.1f}s — {stats.ok} downloaded, {stats.skipped} skipped, {len(stats.failed)} failed."
                            )
            except queue.Empty:
                pass
            self.after(80, self._poll_queue)

    app = SpotifyDownloaderGUI()
    app.mainloop()
    return 0


# --------------------------------------------------------------------------- CLI

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="spotify-dl",
        description=f"{BRAND_TITLE} — Download Spotify tracks, albums and playlists as tagged MP3s (via yt-dlp).",
    )
    p.add_argument("urls", nargs="*", help="Spotify track / album / playlist URLs or URIs")
    p.add_argument("-g", "--gui", action="store_true", help="launch the CustomTkinter graphical user interface")
    p.add_argument("-f", "--file", help="text file with one Spotify URL per line")
    p.add_argument("-o", "--output", default="downloads", help="output directory (default: ./downloads)")
    p.add_argument("-q", "--quality", type=int, choices=(128, 192, 256, 320), default=192,
                   help="MP3 bitrate in kbps (default: 192)")
    p.add_argument("--providers", default="youtube,soundcloud",
                   help="comma-separated, tried in order (default: youtube,soundcloud)")
    p.add_argument("--tolerance", type=float, default=20.0,
                   help="max duration difference in seconds vs Spotify (default: 20)")
    p.add_argument("--lenient", action="store_true",
                   help="accept a result whose title does not match if its duration matches within 3 s")
    p.add_argument("--login", action="store_true",
                   help="log in to Spotify (OAuth) to read your own / collaborative playlists")
    p.add_argument("--cookies", help="Netscape cookies.txt for yt-dlp")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="let yt-dlp read cookies from a browser")
    p.add_argument("--proxy", help="proxy URL passed to yt-dlp")
    p.add_argument("--ffmpeg", help="path to ffmpeg binary or its folder")
    p.add_argument("--overwrite", action="store_true", help="re-download files that already exist")
    p.add_argument("--delay", type=float, default=1.0, help="seconds to pause between downloads")
    p.add_argument("--dry-run", action="store_true", help="only list the tracks that would be downloaded")
    p.add_argument("--log-file", help="also write a detailed debug log to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    args.providers = [x.strip().lower() for x in args.providers.split(",") if x.strip()]
    unknown = [x for x in args.providers if x not in SEARCH_PREFIX]
    if unknown or not args.providers:
        p.error(f"--providers must be a list of: {', '.join(SEARCH_PREFIX)}")
    return args


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    handler = RichHandler(console=console, show_path=False, show_time=False)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    for noisy in ("urllib3", "spotipy", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL if noisy == "spotipy" else logging.WARNING)


def ffmpeg_available(custom: Optional[str]) -> bool:
    if custom:
        p = Path(custom)
        return p.exists() and (p.is_file() or any(p.glob("ffmpeg*")))
    
    # 1. Check system PATH
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
        
    # 2. Check local folder next to script / executable
    base_path = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    for sub in [base_path, base_path / "ffmpeg", base_path / "ffmpeg" / "bin"]:
        if (sub / "ffmpeg.exe").exists() and (sub / "ffprobe.exe").exists():
            return True
            
    return False


def collect_urls(args) -> list[str]:
    urls = list(args.urls)
    if args.file:
        try:
            lines = Path(args.file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.error("Cannot read %s: %s", args.file, exc)
            lines = []
        urls += [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return list(dict.fromkeys(urls))


def main(argv=None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if not raw_argv or "-g" in raw_argv or "--gui" in raw_argv:
        return launch_gui()

    args = parse_args(raw_argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    load_dotenv()
    setup_logging(args.verbose, args.log_file)

    if not args.dry_run and not ffmpeg_available(args.ffmpeg):
        log.error("ffmpeg/ffprobe not found. Install ffmpeg (see README) or pass --ffmpeg PATH.")
        return 2

    urls = collect_urls(args)
    if not urls:
        log.error("No URLs to process.")
        return 2

    stats, health, started = Stats(), ProviderHealth(), time.monotonic()
    for url in urls:
        try:
            kind, sid = parse_spotify_url(url)
            console.rule(f"[bold]{escape(url)}")
            name, tracks = fetch_metadata(kind, sid, args.login)
        except MetadataError as exc:
            log.error("%s: %s", url, exc)
            stats.meta_errors += 1
            continue
        if not tracks:
            log.error("%s: no tracks found", url)
            stats.meta_errors += 1
            continue

        log.info("%s: %d track(s)", name, len(tracks))
        if args.dry_run:
            print_tracks(name, tracks)
            continue
        out_dir = Path(args.output) if kind == "track" else Path(args.output) / sanitize(name)
        run_collection(name, tracks, out_dir, args, health, stats)
        if stats.interrupted:
            log.warning("Interrupted: finished files are kept; re-run the same command to resume.")
            break

    if not args.dry_run:
        print_summary(stats, time.monotonic() - started)
    if stats.interrupted:
        return 130
    return 1 if (stats.failed or stats.meta_errors) else 0


if __name__ == "__main__":
    sys.exit(main())