from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path
from typing import Optional
import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TCON, TPOS, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
import musicbrainzngs
import base64
import os
import mimetypes
import httpx
import re
import shutil

app = FastAPI(title="Taggerr API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MUSIC_DIR      = Path(os.environ.get("MUSIC_DIR", "/music"))
ART_DIR        = Path(os.environ.get("ART_DIR", "/art"))
ACOUSTID_KEY   = os.environ.get("ACOUSTID_API_KEY", "")
DISCOGS_TOKEN  = os.environ.get("DISCOGS_TOKEN", "")
BEETS_URL          = os.environ.get("BEETS_URL", "http://beets:8337")
RENAME_TEMPLATE    = os.environ.get("RENAME_TEMPLATE", "{artist}/{album}/{track} - {title}")
# Prefijo que beets usa internamente para los paths.
# Si beets monta /windows/D/.../Nuevos:/music y taggerr monta /windows/D/...:/music

musicbrainzngs.set_useragent("Taggerr", "1.0", "https://github.com/taggerr")



AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}

# ── Models ────────────────────────────────────────────────────────────────────

class TrackMeta(BaseModel):
    path: str
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    albumartist: Optional[str] = None
    year: Optional[str] = None
    track: Optional[str] = None
    disc: Optional[str] = None
    genre: Optional[str] = None
    lyrics: Optional[str] = None
    cover_b64: Optional[str] = None
    cover_mime: Optional[str] = None

class SaveRequest(BaseModel):
    path: str
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    albumartist: Optional[str] = None
    year: Optional[str] = None
    track: Optional[str] = None
    disc: Optional[str] = None
    genre: Optional[str] = None
    lyrics: Optional[str] = None
    cover_b64: Optional[str] = None
    cover_mime: Optional[str] = None

class RenameRequest(BaseModel):
    path: str

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_path(rel: str) -> Path:
    p = (MUSIC_DIR / rel).resolve()
    if not str(p).startswith(str(MUSIC_DIR.resolve())):
        raise HTTPException(403, "Acceso denegado")
    return p

def read_meta(filepath: Path) -> dict:
    suffix = filepath.suffix.lower()
    meta = {
        "path": str(filepath.relative_to(MUSIC_DIR)),
        "title": None, "artist": None, "album": None,
        "albumartist": None, "year": None, "track": None,
        "disc": None, "genre": None, "lyrics": None,
        "cover_b64": None, "cover_mime": None,
    }

    try:
        if suffix == ".mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                return meta
            meta["title"]       = str(tags["TIT2"]) if "TIT2" in tags else None
            meta["artist"]      = str(tags["TPE1"]) if "TPE1" in tags else None
            meta["album"]       = str(tags["TALB"]) if "TALB" in tags else None
            meta["albumartist"] = str(tags["TPE2"]) if "TPE2" in tags else None
            meta["year"]        = str(tags["TDRC"]) if "TDRC" in tags else None
            meta["track"]       = str(tags["TRCK"]) if "TRCK" in tags else None
            meta["disc"]        = str(tags["TPOS"]) if "TPOS" in tags else None
            meta["genre"]       = str(tags["TCON"]) if "TCON" in tags else None
            for k, v in tags.items():
                if k.startswith("USLT"):
                    meta["lyrics"] = v.text
                    break
            for k, v in tags.items():
                if k.startswith("APIC"):
                    meta["cover_b64"]  = base64.b64encode(v.data).decode()
                    meta["cover_mime"] = v.mime
                    break

        elif suffix == ".flac":
            audio = FLAC(filepath)
            meta["title"]       = audio.get("title",       [None])[0]
            meta["artist"]      = audio.get("artist",      [None])[0]
            meta["album"]       = audio.get("album",       [None])[0]
            meta["albumartist"] = audio.get("albumartist", [None])[0]
            meta["year"]        = audio.get("date",        [None])[0]
            meta["track"]       = audio.get("tracknumber", [None])[0]
            meta["disc"]        = audio.get("discnumber",  [None])[0]
            meta["genre"]       = audio.get("genre",       [None])[0]
            meta["lyrics"]      = audio.get("lyrics",      [None])[0]
            if audio.pictures:
                p = audio.pictures[0]
                meta["cover_b64"]  = base64.b64encode(p.data).decode()
                meta["cover_mime"] = p.mime

        elif suffix == ".m4a":
            audio = MP4(filepath)
            meta["title"]       = audio.tags.get("\xa9nam", [None])[0]
            meta["artist"]      = audio.tags.get("\xa9ART", [None])[0]
            meta["album"]       = audio.tags.get("\xa9alb", [None])[0]
            meta["albumartist"] = audio.tags.get("aART",    [None])[0]
            meta["year"]        = audio.tags.get("\xa9day", [None])[0]
            trkn = audio.tags.get("trkn")
            if trkn:
                meta["track"] = str(trkn[0][0])
            meta["genre"] = audio.tags.get("\xa9gen", [None])[0]
            covr = audio.tags.get("covr")
            if covr:
                meta["cover_b64"]  = base64.b64encode(bytes(covr[0])).decode()
                meta["cover_mime"] = "image/jpeg"

        elif suffix in (".ogg", ".opus"):
            audio = OggVorbis(filepath)
            meta["title"]       = audio.get("title",       [None])[0]
            meta["artist"]      = audio.get("artist",      [None])[0]
            meta["album"]       = audio.get("album",       [None])[0]
            meta["albumartist"] = audio.get("albumartist", [None])[0]
            meta["year"]        = audio.get("date",        [None])[0]
            meta["track"]       = audio.get("tracknumber", [None])[0]
            meta["genre"]       = audio.get("genre",       [None])[0]
            meta["lyrics"]      = audio.get("lyrics",      [None])[0]

    except Exception as e:
        meta["_error"] = str(e)

    return meta


def write_meta(filepath: Path, data: SaveRequest):
    suffix = filepath.suffix.lower()
    cover_data = base64.b64decode(data.cover_b64) if data.cover_b64 else None
    cover_mime = data.cover_mime or "image/jpeg"

    if suffix == ".mp3":
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()
        if data.title:       tags["TIT2"] = TIT2(encoding=3, text=data.title)
        if data.artist:      tags["TPE1"] = TPE1(encoding=3, text=data.artist)
        if data.album:       tags["TALB"] = TALB(encoding=3, text=data.album)
        if data.albumartist: tags["TPE2"] = mutagen.id3.TPE2(encoding=3, text=data.albumartist)
        if data.year:        tags["TDRC"] = TDRC(encoding=3, text=data.year)
        if data.track:       tags["TRCK"] = TRCK(encoding=3, text=data.track)
        if data.disc:        tags["TPOS"] = TPOS(encoding=3, text=data.disc)
        if data.genre:       tags["TCON"] = TCON(encoding=3, text=data.genre)
        if data.lyrics is not None:
            tags.delall("USLT")
            from mutagen.id3 import USLT
            tags["USLT::eng"] = USLT(encoding=3, lang="eng", desc="", text=data.lyrics)
        if cover_data:
            tags.delall("APIC")
            tags["APIC:"] = APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover_data)
        tags.save(filepath)

    elif suffix == ".flac":
        audio = FLAC(filepath)
        if data.title:       audio["title"]       = data.title
        if data.artist:      audio["artist"]      = data.artist
        if data.album:       audio["album"]       = data.album
        if data.albumartist: audio["albumartist"] = data.albumartist
        if data.year:        audio["date"]        = data.year
        if data.track:       audio["tracknumber"] = data.track
        if data.disc:        audio["discnumber"]  = data.disc
        if data.genre:       audio["genre"]       = data.genre
        if data.lyrics is not None: audio["lyrics"]  = data.lyrics
        if cover_data:
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = cover_mime
            pic.data = cover_data
            audio.add_picture(pic)
        audio.save()

    elif suffix == ".m4a":
        audio = MP4(filepath)
        if data.title:       audio.tags["\xa9nam"] = [data.title]
        if data.artist:      audio.tags["\xa9ART"] = [data.artist]
        if data.album:       audio.tags["\xa9alb"] = [data.album]
        if data.albumartist: audio.tags["aART"]    = [data.albumartist]
        if data.year:        audio.tags["\xa9day"] = [data.year]
        if data.track:       audio.tags["trkn"]    = [(int(data.track.split("/")[0]), 0)]
        if data.genre:       audio.tags["\xa9gen"] = [data.genre]
        if cover_data:
            from mutagen.mp4 import MP4Cover
            fmt = MP4Cover.FORMAT_PNG if "png" in cover_mime else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
        audio.save()

    elif suffix in (".ogg", ".opus"):
        audio = OggVorbis(filepath)
        if data.title:       audio["title"]       = data.title
        if data.artist:      audio["artist"]      = data.artist
        if data.album:       audio["album"]       = data.album
        if data.albumartist: audio["albumartist"] = data.albumartist
        if data.year:        audio["date"]        = data.year
        if data.track:       audio["tracknumber"] = data.track
        if data.genre:       audio["genre"]       = data.genre
        if data.lyrics is not None: audio["lyrics"] = data.lyrics
        audio.save()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/browse")
def browse(path: str = "", search: str = ""):
    base = safe_path(path)
    if not base.is_dir():
        raise HTTPException(404, "Carpeta no encontrada")

    dirs, files = [], []
    for item in sorted(base.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            dirs.append({"name": item.name, "path": str(item.relative_to(MUSIC_DIR))})
        elif item.suffix.lower() in AUDIO_EXTENSIONS:
            if not search or search.lower() in item.name.lower():
                files.append({"name": item.name, "path": str(item.relative_to(MUSIC_DIR))})

    return {"dirs": dirs, "files": files, "current": path}


@app.get("/api/meta")
def get_meta(path: str):
    fp = safe_path(path)
    if not fp.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    return read_meta(fp)



@app.post("/api/save")
def save_meta(req: SaveRequest):
    fp = safe_path(req.path)
    if not fp.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    write_meta(fp, req)
    return {"ok": True}
@app.get("/api/search/musicbrainz")
def search_mb(title: str = "", artist: str = "", album: str = "", limit: int = 8):
    try:
        query_parts = []
        if title:  query_parts.append(f'recording:"{title}"')
        if artist: query_parts.append(f'artist:"{artist}"')
        if album:  query_parts.append(f'release:"{album}"')
        query = " AND ".join(query_parts) if query_parts else title or artist or album
        result = musicbrainzngs.search_recordings(query=query, limit=limit)
        recordings = []
        for r in result.get("recording-list", []):
            release = (r.get("release-list") or [{}])[0]
            recordings.append({
                "title":  r.get("title", ""),
                "artist": (r.get("artist-credit-phrase") or
                           (r.get("artist-credit") or [{}])[0].get("artist", {}).get("name", "")),
                "album":  release.get("title", ""),
                "year":   (release.get("date") or "")[:4],
                "track":  release.get("medium-list", [{}])[0].get("track-list", [{}])[0].get("number", ""),
                "mbid":   r.get("id", ""),
            })
        return {"results": recordings}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/art/local")
def list_local_art(path: str = ""):
    """Lista archivos de imagen en ART_DIR o junto a la canción"""
    images = []
    song_dir = safe_path(path).parent if path else MUSIC_DIR
    for d in [song_dir, ART_DIR]:
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    images.append({"name": f.name, "path": str(f)})
    return {"images": images}


@app.get("/api/art/file")
def get_art_file(path: str):
    p = Path(path).resolve()
    allowed = [MUSIC_DIR.resolve(), ART_DIR.resolve()]
    if not any(str(p).startswith(str(a)) for a in allowed):
        raise HTTPException(403, "Acceso denegado")
    if not p.is_file():
        raise HTTPException(404)
    mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    return Response(content=p.read_bytes(), media_type=mime)


# ── AcoustID ──────────────────────────────────────────────────────────────────

@app.get("/api/search/acoustid")
async def search_acoustid(path: str):
    """Identifica la canción por huella de audio (AcoustID + MusicBrainz)"""
    if not ACOUSTID_KEY:
        raise HTTPException(503, "ACOUSTID_API_KEY no configurada")

    fp = safe_path(path)
    if not fp.is_file():
        raise HTTPException(404, "Archivo no encontrado")

    try:
        # Generar huella con fpcalc directamente (más confiable que pyacoustid)
        import subprocess, json as _json
        proc = subprocess.run(
            ["fpcalc", "-json", str(fp)],
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or "fpcalc falló sin mensaje"
            raise HTTPException(500, f"fpcalc error: {err}")

        fpcalc_out = _json.loads(proc.stdout)
        duration    = int(fpcalc_out.get("duration", 0))
        fingerprint = fpcalc_out.get("fingerprint", "")

        if not fingerprint:
            raise HTTPException(500, "No se pudo generar la huella de audio")

        # Consultar AcoustID API — debe ser POST con form data (fingerprint es muy larga para GET)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("https://api.acoustid.org/v2/lookup", data={
                "client":      ACOUSTID_KEY,
                "duration":    duration,
                "fingerprint": fingerprint,
                "meta":        "recordings releasegroups",
            })
            if resp.status_code == 400:
                raise HTTPException(400, f"AcoustID 400: {resp.text}")
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "ok":
            raise HTTPException(500, f"AcoustID error: {data.get('error', {}).get('message', 'desconocido')}")

        matches = []
        for result in (data.get("results") or [])[:8]:
            score = round(result.get("score", 0) * 100)
            if score < 40:
                continue
            for rec in (result.get("recordings") or [{}])[:2]:
                title  = rec.get("title", "")
                artist = ""
                artists = rec.get("artists") or []
                if artists:
                    artist = ", ".join(a.get("name", "") for a in artists)
                album = ""
                year  = ""
                rgs = rec.get("releasegroups") or []
                if rgs:
                    rg = rgs[0]
                    album = rg.get("title", "")
                    releases = rg.get("releases") or []
                    if releases:
                        year = (releases[0].get("date") or "")[:4]

                matches.append({
                    "score":  score,
                    "title":  title,
                    "artist": artist,
                    "album":  album,
                    "year":   year,
                    "track":  "",
                    "genre":  "",
                    "mbid":   rec.get("id", ""),
                })

        return {"results": matches[:8]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Discogs ───────────────────────────────────────────────────────────────────

@app.get("/api/search/discogs")
async def search_discogs(title: str = "", artist: str = "", album: str = "", limit: int = 8):
    """Busca en Discogs usando la REST API directamente (evita bugs del cliente)"""
    if not DISCOGS_TOKEN:
        raise HTTPException(503, "DISCOGS_TOKEN no configurado")

    query = " ".join(filter(None, [title, artist, album]))
    params: dict = {"q": query, "type": "release", "per_page": limit, "page": 1}
    if artist: params["artist"] = artist
    if album:  params["release_title"] = album

    headers = {
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
        "User-Agent": "Taggerr/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(
                "https://api.discogs.com/database/search",
                params=params, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()

        matches = []
        for r in (data.get("results") or [])[:limit]:
            try:
                rel_title  = r.get("title", "")
                rel_artist = ""
                if " - " in rel_title:
                    parts      = rel_title.split(" - ", 1)
                    rel_artist = parts[0].strip()
                    rel_title  = parts[1].strip()

                year   = str(r.get("year", "")) if r.get("year") else ""
                genres = r.get("genre") or []
                styles = r.get("style") or []
                genre  = ", ".join((genres + styles)[:2])

                cover_url = r.get("cover_image") or r.get("thumb") or ""

                matches.append({
                    "title":      title or rel_title,
                    "artist":     rel_artist,
                    "album":      rel_title,
                    "year":       year,
                    "track":      "",
                    "genre":      genre,
                    "cover_url":  cover_url,
                    "discogs_id": str(r.get("id", "")),
                })
            except Exception:
                continue

        return {"results": matches}

    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Discogs API error: {e.response.text}")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/discogs/cover")
async def fetch_discogs_cover(url: str):
    """Proxy para bajar la carátula de Discogs (evita CORS)"""
    if not DISCOGS_TOKEN:
        raise HTTPException(503, "DISCOGS_TOKEN no configurado")
    if "discogs.com" not in url and "discogs-cdn" not in url and "dgcdn" not in url:
        raise HTTPException(403, "URL no permitida")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={
                "User-Agent":    "Taggerr/1.0",
                "Authorization": f"Discogs token={DISCOGS_TOKEN}",
            })
            mime = resp.headers.get("content-type", "image/jpeg")
            return Response(content=resp.content, media_type=mime)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Beets ────────────────────────────────────────────────────────────────────

@app.get("/api/beets/status")
async def beets_status():
    """Verifica si beets está disponible."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{BEETS_URL}/stats")
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "items": data.get("items", 0)}
    except Exception:
        pass
    return {"ok": False}


# ── Audio streaming ──────────────────────────────────────────────────────────

@app.get("/api/stream")
def stream_audio(path: str, request: Request):
    from fastapi.responses import StreamingResponse
    fp = safe_path(path)
    if not fp.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    mime = mimetypes.guess_type(str(fp))[0] or "audio/mpeg"
    file_size = fp.stat().st_size
    range_header = request.headers.get("range")

    def iter_file(start=0, end=None):
        chunk = 65536
        with open(fp, "rb") as f:
            f.seek(start)
            remaining = (end - start + 1) if end else None
            while True:
                read_size = min(chunk, remaining) if remaining else chunk
                data = f.read(read_size)
                if not data:
                    break
                yield data
                if remaining:
                    remaining -= len(data)
                    if remaining <= 0:
                        break

    if range_header:
        try:
            range_val = range_header.replace("bytes=", "")
            start_str, end_str = range_val.split("-")
            start = int(start_str)
            end   = int(end_str) if end_str else file_size - 1
            length = end - start + 1
            headers = {
                "Content-Range":  f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges":  "bytes",
                "Content-Length": str(length),
            }
            return StreamingResponse(iter_file(start, end), status_code=206,
                                     media_type=mime, headers=headers)
        except Exception:
            pass

    return StreamingResponse(iter_file(), media_type=mime, headers={
        "Content-Length": str(file_size),
        "Accept-Ranges":  "bytes",
    })


# ── Rename ────────────────────────────────────────────────────────────────────

def sanitize(s: str) -> str:
    """Elimina caracteres inválidos para nombres de archivo/carpeta."""
    s = s.strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s)
    s = re.sub(r'_+', '_', s)
    return s or "Desconocido"

@app.post("/api/rename")
def rename_file(req: RenameRequest):
    fp = safe_path(req.path)
    if not fp.is_file():
        raise HTTPException(404, "Archivo no encontrado")

    meta = read_meta(fp)

    title  = sanitize(meta.get("title")  or fp.stem)
    artist = sanitize(meta.get("artist") or "Desconocido")
    album  = sanitize(meta.get("album")  or "Desconocido")
    year   = sanitize(meta.get("year")   or "")
    track  = meta.get("track") or ""

    # "3/12" → "03"
    if track:
        track = track.split("/")[0].strip().zfill(2)
    else:
        track = "00"

    new_rel = RENAME_TEMPLATE.format(
        title=title, artist=artist, album=album, year=year, track=track,
    ) + fp.suffix.lower()

    new_fp = MUSIC_DIR / new_rel

    # Manejar colisiones
    if new_fp.exists() and new_fp.resolve() != fp.resolve():
        stem   = new_fp.stem
        suffix = new_fp.suffix
        parent = new_fp.parent
        counter = 1
        while new_fp.exists():
            new_fp = parent / f"{stem} ({counter}){suffix}"
            counter += 1

    new_fp.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(fp), str(new_fp))

    new_path = str(new_fp.relative_to(MUSIC_DIR))
    new_dir  = str(new_fp.parent.relative_to(MUSIC_DIR))
    return {"ok": True, "new_path": new_path, "new_dir": new_dir}


# ── Batch ─────────────────────────────────────────────────────────────────────

class BatchSaveItem(BaseModel):
    path: str
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    track: Optional[str] = None

class BatchSaveRequest(BaseModel):
    items: list[BatchSaveItem]

@app.get("/api/batch/files")
def batch_files(path: str = ""):
    """Lista recursiva de archivos de audio en una carpeta."""
    base = safe_path(path)
    if not base.is_dir():
        raise HTTPException(404, "Carpeta no encontrada")
    results = []
    for fp in sorted(base.rglob("*")):
        if fp.suffix.lower() in AUDIO_EXTENSIONS and fp.is_file():
            try:
                m = read_meta(fp)
            except Exception:
                m = {}
            results.append({
                "path":   str(fp.relative_to(MUSIC_DIR)),
                "title":  m.get("title") or "",
                "artist": m.get("artist") or "",
                "album":  m.get("album") or "",
                "track":  m.get("track") or "",
            })
    return {"files": results}

@app.post("/api/batch/save")
def batch_save(req: BatchSaveRequest):
    """Guarda artista, álbum, track y título en múltiples archivos."""
    saved, errors = 0, []
    for item in req.items:
        try:
            fp = safe_path(item.path)
            if not fp.is_file():
                errors.append(item.path)
                continue
            # Reutilizar write_meta con solo los 4 campos batch
            partial = SaveRequest(
                path=item.path,
                title=item.title,
                artist=item.artist,
                album=item.album,
                track=item.track,
            )
            write_meta(fp, partial)
            saved += 1
        except Exception as e:
            errors.append(f"{item.path}: {e}")
    return {"ok": True, "saved": saved, "errors": errors}

@app.get("/api/batch/search/musicbrainz")
def batch_search_mb(title: str = "", artist: str = ""):
    """Búsqueda rápida MB para batch — devuelve el mejor resultado."""
    try:
        query_parts = []
        if title:  query_parts.append(f'recording:"{title}"')
        if artist: query_parts.append(f'artist:"{artist}"')
        if not query_parts:
            return {"result": None}
        query = " AND ".join(query_parts)
        result = musicbrainzngs.search_recordings(query=query, limit=1)
        recs = result.get("recording-list", [])
        if not recs:
            return {"result": None}
        r = recs[0]
        release = (r.get("release-list") or [{}])[0]
        track_list = (release.get("medium-list") or [{}])[0]
        track_info = (track_list.get("track-list") or [{}])[0]
        return {"result": {
            "title":  r.get("title", ""),
            "artist": (r.get("artist-credit-phrase") or
                       (r.get("artist-credit") or [{}])[0].get("artist", {}).get("name", "")),
            "album":  release.get("title", ""),
            "track":  track_info.get("number", ""),
            "year":   release.get("date", "")[:4] if release.get("date") else "",
        }}
    except Exception:
        return {"result": None}

@app.get("/api/batch/search/discogs")
async def batch_search_discogs(title: str = "", artist: str = ""):
    """Búsqueda rápida Discogs para batch — devuelve el mejor resultado."""
    try:
        params = {"type": "release", "per_page": 1}
        if title:  params["track"] = title
        if artist: params["artist"] = artist
        if not title and not artist:
            return {"result": None}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.discogs.com/database/search",
                params=params,
                headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}",
                         "User-Agent": "Taggerr/1.0"},
            )
        results = resp.json().get("results", [])
        if not results:
            return {"result": None}
        r = results[0]
        title_parts = r.get("title", "").split(" - ", 1)
        return {"result": {
            "title":  title,
            "artist": title_parts[0].strip() if len(title_parts) > 1 else "",
            "album":  title_parts[1].strip() if len(title_parts) > 1 else r.get("title", ""),
            "track":  "",
            "year":   str(r.get("year", "")),
        }}
    except Exception:
        return {"result": None}


# ── Static frontend ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")

