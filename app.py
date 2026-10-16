#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MediaSaver Web — เว็บโหลด mp3 / mp4 / อื่นๆ จากลิงก์
Flask + yt-dlp + ffmpeg  (รันเป็นโปรเซสเดียว งานเก็บในหน่วยความจำ)

ตัวแปรสภาพแวดล้อม (ตั้งใน Railway/Render/Docker):
  ACCESS_PASSWORD     รหัสผ่านเข้าใช้งาน (แนะนำมาก ถ้าเปิดสาธารณะ)
  MAX_CONCURRENT      งานที่โหลดพร้อมกัน            (ค่าเริ่มต้น 2)
  FILE_TTL_MIN        ลบไฟล์หลังเสร็จกี่นาที          (ค่าเริ่มต้น 30)
  MAX_PLAYLIST        จำนวนคลิปสูงสุดต่อ playlist    (ค่าเริ่มต้น 50)
  MAX_DURATION_MIN    ความยาวคลิปสูงสุด (นาที, 0=ไม่จำกัด) (ค่าเริ่มต้น 180)
  MAX_FILESIZE_MB     ขนาดไฟล์สูงสุด (MB, 0=ไม่จำกัด)  (ค่าเริ่มต้น 1024)
  COOKIES_B64         cookies.txt แบบ base64 (ช่วยเรื่อง YouTube บล็อกบอท)
  COOKIES_FILE        path ของ cookies.txt (ทางเลือกแทน COOKIES_B64)
  DATA_DIR            โฟลเดอร์เก็บไฟล์ชั่วคราว
  ALLOW_PRIVATE_URLS  ตั้ง 1 เพื่อทดสอบกับลิงก์ localhost (ปกติห้าม)
"""

import base64
import hmac
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
import zipfile
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory
from yt_dlp.utils import download_range_func, match_filter_func

# ───────────────────────── ตั้งค่า ─────────────────────────
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(tempfile.gettempdir()) / "mediasaver")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
MAX_CONCURRENT = max(1, env_int("MAX_CONCURRENT", 2))
FILE_TTL_MIN = max(1, env_int("FILE_TTL_MIN", 30))
MAX_PLAYLIST = max(1, env_int("MAX_PLAYLIST", 50))
MAX_DURATION_MIN = max(0, env_int("MAX_DURATION_MIN", 180))
MAX_FILESIZE_MB = max(0, env_int("MAX_FILESIZE_MB", 1024))
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS") == "1"
MAX_ACTIVE_JOBS = 20

AUDIO_FORMATS = ["mp3", "m4a", "flac", "wav", "opus"]
VIDEO_FORMATS = ["mp4", "webm", "mkv"]
VIDEO_QUALITIES = ["360", "480", "720", "1080", "best"]
AUDIO_BITRATES = ["128", "192", "256", "320"]
LOSSLESS = {"flac", "wav"}
NO_THUMB_EMBED = {"wav", "webm"}
MEDIA_EXT = {".mp3", ".m4a", ".flac", ".wav", ".opus", ".mp4", ".webm", ".mkv"}

app = Flask(__name__, static_folder=None)

# ───────────────────────── cookies ─────────────────────────
COOKIES_PATH = None


def setup_cookies() -> None:
    global COOKIES_PATH
    target = DATA_DIR / "cookies.txt"
    raw = os.environ.get("COOKIES_B64", "").strip()
    src = os.environ.get("COOKIES_FILE", "").strip()
    try:
        if raw:
            target.write_bytes(base64.b64decode(raw))
        elif src and Path(src).is_file():
            shutil.copyfile(src, target)
        else:
            return
        target.chmod(0o600)
        COOKIES_PATH = str(target)
    except Exception as e:  # noqa: BLE001
        print(f"[cookies] โหลดไม่สำเร็จ: {e}")


setup_cookies()

# ───────────────────────── ความปลอดภัย / จำกัดการใช้ ─────────────────────────
HITS = defaultdict(deque)
HITS_LOCK = threading.Lock()


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")


def rate_ok(bucket: str, limit: int, window: int = 60) -> bool:
    now = time.time()
    with HITS_LOCK:
        q = HITS[bucket]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def authed() -> bool:
    if not ACCESS_PASSWORD:
        return True
    key = request.headers.get("X-Access-Key") or request.args.get("key", "")
    return hmac.compare_digest(key.encode(), ACCESS_PASSWORD.encode())


@app.before_request
def guard():
    p = request.path
    if not p.startswith("/api/") or p in ("/api/config", "/api/health"):
        return None
    if not authed():
        if not rate_ok("auth:" + client_ip(), 10):
            return jsonify(ok=False, error="ลองรหัสผ่านผิดหลายครั้ง รอสักครู่แล้วลองใหม่"), 429
        return jsonify(ok=False, auth=True, error="รหัสผ่านไม่ถูกต้อง"), 401
    if request.method == "POST" and not rate_ok("post:" + client_ip(), 30):
        return jsonify(ok=False, error="ส่งคำขอถี่เกินไป รอสักครู่"), 429
    return None


def check_url(url: str) -> None:
    """ตรวจลิงก์ + กันไม่ให้ใช้เซิร์ฟเวอร์ยิงเข้าเครือข่ายภายใน (SSRF)"""
    if not url or len(url) > 2000:
        raise ValueError("ลิงก์ไม่ถูกต้อง")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise ValueError("หาเว็บของลิงก์นี้ไม่พบ")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ValueError("ไม่อนุญาตลิงก์ไปยังเครือข่ายภายใน")


# ───────────────────────── ตัวช่วย yt-dlp ─────────────────────────
def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(s))


class JobLogger:
    def __init__(self):
        self.errors, self.notes = [], []

    def debug(self, m):
        pass

    def info(self, m):
        m = strip_ansi(m)
        if "does not pass filter" in m or "File is larger than" in m:
            self.notes.append(m)

    def warning(self, m):
        pass

    def error(self, m):
        self.errors.append(strip_ansi(m))


def friendly_error(msg: str) -> str:
    m = msg.lower()
    if "not a bot" in m or "sign in to confirm" in m:
        return "YouTube บล็อกเซิร์ฟเวอร์นี้ ต้องใส่ cookies (ดูวิธีใน README)"
    if "private video" in m:
        return "วิดีโอนี้เป็นส่วนตัว โหลดไม่ได้"
    if "unsupported url" in m:
        return "ไม่รองรับลิงก์นี้"
    if "unavailable" in m or "removed" in m or "not available" in m:
        return "คลิปนี้ไม่พร้อมใช้งาน (ถูกลบ ถูกจำกัดประเทศ หรือจำกัดอายุ)"
    if "login" in m or "log in" in m or "cookies" in m:
        return "ลิงก์นี้ต้องล็อกอินก่อนถึงจะโหลดได้"
    if "429" in m or "too many requests" in m:
        return "เว็บต้นทางจำกัดการเข้าถึงชั่วคราว ลองใหม่ภายหลัง"
    if "ffmpeg" in m:
        return "แปลงไฟล์ไม่สำเร็จ (ffmpeg ผิดพลาด)"
    return msg[:300]


def parse_seconds(s: str):
    s = s.strip()
    if not s:
        return None
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total


def parse_trim(text):
    """'1:30-2:45' → (90, 165) | '1:30-' → (90, inf) | '-2:45' → (0, 165)"""
    if not text:
        return None
    start_s, _, end_s = str(text).partition("-")
    start = parse_seconds(start_s) or 0.0
    end = parse_seconds(end_s)
    if end is not None and end <= start:
        raise ValueError("เวลาสิ้นสุดต้องมากกว่าเวลาเริ่ม")
    if end is None and start == 0:
        return None
    return (start, end if end is not None else float("inf"))


def has_playlist(url: str) -> bool:
    return "list=" in url or "/playlist" in url


def is_mix(url: str) -> bool:
    return bool(re.search(r"[?&]list=RD", url)) or "start_radio" in url


def video_selector(fmt: str, q: str) -> str:
    h = "" if q == "best" else f"[height<={q}]"
    if fmt == "mp4":
        return (f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/bv*{h}[ext=mp4]+ba[ext=m4a]/"
                f"b{h}[ext=mp4]/bv*{h}+ba/b{h}/b")
    if fmt == "webm":
        return f"bv*{h}[ext=webm]+ba[ext=webm]/bv*{h}+ba/b{h}/b"
    return f"bv*{h}+ba/b{h}/b"


def impersonate_target():
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str("chrome")
    except Exception:
        return None


def base_opts(log=None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    if log is not None:
        opts["logger"] = log
    target = impersonate_target()
    if target is not None:
        opts["impersonate"] = target
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    return opts


# ───────────────────────── งานโหลด ─────────────────────────
JOBS = {}
LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")[:100] or "media"


def make_hooks(job: dict):
    seen = {}

    def on_progress(d):
        info = d.get("info_dict") or {}
        n_streams = len(info.get("requested_formats") or []) or 1
        pi = info.get("playlist_index") or 1
        n_items = min(info.get("n_entries") or info.get("playlist_count") or 1, MAX_PLAYLIST)
        fn = d.get("filename") or ""
        lst = seen.setdefault(pi, [])
        if fn not in lst:
            lst.append(fn)
        si = lst.index(fn)
        if d.get("status") == "finished":
            frac = 1.0
        else:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0.0
        pct = (((pi - 1) + (si + min(frac, 1.0)) / n_streams) / max(n_items, 1)) * 90
        with LOCK:
            job["status"] = "downloading"
            job["progress"] = max(job["progress"], round(min(pct, 90), 1))
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
            if info.get("playlist_title") and not job.get("playlist_title"):
                job["playlist_title"] = info["playlist_title"]
            if n_items > 1:
                job["items"] = {"index": pi, "total": n_items}

    def on_post(d):
        if d.get("status") == "started":
            with LOCK:
                job["status"] = "processing"
                job["progress"] = max(job["progress"], 92)
                job["speed"] = None
                job["eta"] = None

    return on_progress, on_post


def build_opts(job, job_dir: Path, url: str, cfg: dict, saved: list, log) -> dict:
    fmt = cfg["format"]
    is_audio = fmt in AUDIO_FORMATS
    playlist = cfg["playlist"]

    if playlist:
        tmpl = job_dir / "%(playlist_title|Playlist).80s" / "%(playlist_index)02d - %(title).100s.%(ext)s"
    else:
        tmpl = job_dir / "%(title).120s.%(ext)s"

    opts = base_opts(log)
    on_progress, on_post = make_hooks(job)
    opts.update({
        "outtmpl": str(tmpl),
        "noplaylist": not playlist,
        "ignoreerrors": True,
        "windowsfilenames": True,
        "concurrent_fragment_downloads": 4,
        "overwrites": False,
        "post_hooks": [saved.append],
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_post],
    })
    if playlist:
        opts["playlistend"] = MAX_PLAYLIST
    if MAX_FILESIZE_MB:
        opts["max_filesize"] = MAX_FILESIZE_MB * 1024 * 1024
    flt = "!is_live"
    if MAX_DURATION_MIN:
        flt += f" & duration <=? {MAX_DURATION_MIN * 60}"
    opts["match_filter"] = match_filter_func(flt)

    embed = fmt not in NO_THUMB_EMBED
    pps = []
    if embed:
        opts["writethumbnail"] = True
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})

    if is_audio:
        opts["format"] = "bestaudio/best"
        ex = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt not in LOSSLESS:
            ex["preferredquality"] = cfg["bitrate"]
        pps.append(ex)
    else:
        opts["format"] = video_selector(fmt, cfg["quality"])
        opts["merge_output_format"] = fmt

    pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    if embed:
        pps.append({"key": "EmbedThumbnail"})
    opts["postprocessors"] = pps

    if cfg["trim"]:
        opts["download_ranges"] = download_range_func(None, [cfg["trim"]])
        opts["force_keyframes_at_cuts"] = True
    return opts


def fail(job: dict, msg: str) -> None:
    with LOCK:
        job["status"] = "error"
        job["error"] = msg
        job["finished"] = time.time()


def run_job(job: dict, url: str, cfg: dict) -> None:
    job_dir = DATA_DIR / job["id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    saved, log = [], JobLogger()
    with LOCK:
        job["status"] = "downloading"
    try:
        opts = build_opts(job, job_dir, url, cfg, saved, log)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001
        log.errors.append(strip_ansi(e))

    files = [Path(f) for f in dict.fromkeys(saved) if Path(f).is_file()]
    if not files:
        files = sorted(p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXT)
    if not files:
        if log.errors:
            return fail(job, friendly_error(log.errors[-1]))
        if log.notes:
            return fail(job, "คลิปนี้ไม่ผ่านเงื่อนไข (ยาวเกิน %d นาที, ไลฟ์สด หรือไฟล์ใหญ่เกินกำหนด)" % MAX_DURATION_MIN)
        return fail(job, "ไม่พบไฟล์ที่โหลดได้")

    if len(files) == 1:
        final = files[0]
    else:
        name = safe_name(job.get("playlist_title") or "playlist") + ".zip"
        final = job_dir / name
        with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                zf.write(f, arcname=f.relative_to(job_dir).as_posix().split("/")[-1])
        for f in files:
            f.unlink(missing_ok=True)

    with LOCK:
        job["file"] = str(final)
        job["filename"] = final.name
        job["size"] = final.stat().st_size
        job["count"] = len(files)
        job["progress"] = 100
        job["status"] = "done"
        job["speed"] = job["eta"] = None
        job["finished"] = time.time()


def public_job(job: dict) -> dict:
    keys = ("id", "status", "progress", "speed", "eta", "error", "filename", "size", "count", "items")
    out = {k: job.get(k) for k in keys}
    out["ok"] = True
    return out


def cleanup_loop() -> None:
    while True:
        time.sleep(60)
        cutoff = time.time() - FILE_TTL_MIN * 60
        stale = []
        with LOCK:
            for jid, j in JOBS.items():
                ref = j.get("finished") or (j["created"] + 3 * 3600)
                if ref < cutoff or (not j.get("finished") and j["created"] < time.time() - 3 * 3600):
                    stale.append(jid)
            for jid in stale:
                JOBS.pop(jid, None)
        for jid in stale:
            shutil.rmtree(DATA_DIR / jid, ignore_errors=True)
        # โฟลเดอร์ค้างจากการรีสตาร์ท
        for d in DATA_DIR.iterdir():
            if d.is_dir() and d.name not in JOBS and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)


threading.Thread(target=cleanup_loop, daemon=True).start()

# ───────────────────────── API ─────────────────────────


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.get("/api/config")
def config():
    return jsonify(
        ok=True,
        auth_required=bool(ACCESS_PASSWORD),
        video_formats=VIDEO_FORMATS,
        audio_formats=AUDIO_FORMATS,
        qualities=VIDEO_QUALITIES,
        bitrates=AUDIO_BITRATES,
        lossless=sorted(LOSSLESS),
        ttl_min=FILE_TTL_MIN,
        max_playlist=MAX_PLAYLIST,
        max_duration_min=MAX_DURATION_MIN,
        has_cookies=bool(COOKIES_PATH),
        ytdlp=yt_dlp.version.__version__,
    )


@app.get("/api/me")
def me():
    return jsonify(ok=True)


@app.post("/api/analyze")
def analyze():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    try:
        check_url(url)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    log = JobLogger()
    opts = base_opts(log)
    opts.update({"skip_download": True, "noplaylist": True, "extract_flat": "in_playlist"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=friendly_error(strip_ansi(e))), 422
    if not info:
        msg = friendly_error(log.errors[-1]) if log.errors else "อ่านข้อมูลลิงก์นี้ไม่ได้"
        return jsonify(ok=False, error=msg), 422

    is_list = info.get("_type") == "playlist"
    entries = [e for e in (info.get("entries") or []) if e] if is_list else []
    heights = sorted({f.get("height") for f in (info.get("formats") or [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")})
    thumb = info.get("thumbnail") or (entries[0].get("thumbnails", [{}])[-1].get("url") if entries and entries[0].get("thumbnails") else None)
    count = len(entries) if is_list else 1
    duration = info.get("duration")
    return jsonify(
        ok=True,
        kind="playlist" if is_list else "video",
        title=info.get("title") or "ไม่มีชื่อ",
        uploader=info.get("uploader") or info.get("channel") or "",
        thumbnail=thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
        duration=duration,
        heights=heights,
        count=count,
        with_playlist=(not is_list) and has_playlist(url),
        mix=is_mix(url),
        too_long=bool(MAX_DURATION_MIN and duration and duration > MAX_DURATION_MIN * 60),
        live=bool(info.get("is_live")),
    )


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    try:
        check_url(url)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    fmt = data.get("format", "mp4")
    quality = str(data.get("quality", "720"))
    bitrate = str(data.get("bitrate", "192"))
    if fmt not in VIDEO_FORMATS + AUDIO_FORMATS:
        return jsonify(ok=False, error="ฟอร์แมตไม่ถูกต้อง"), 400
    if quality not in VIDEO_QUALITIES or bitrate not in AUDIO_BITRATES:
        return jsonify(ok=False, error="คุณภาพไม่ถูกต้อง"), 400
    try:
        trim = parse_trim(data.get("trim"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e) if "เวลา" in str(e) else "รูปแบบเวลาไม่ถูกต้อง (เช่น 1:30-2:45)"), 400

    with LOCK:
        active = sum(1 for j in JOBS.values() if j["status"] in ("queued", "downloading", "processing"))
        if active >= MAX_ACTIVE_JOBS:
            return jsonify(ok=False, error="ตอนนี้มีงานเยอะ รอสักครู่แล้วลองใหม่"), 429
        job = {"id": uuid.uuid4().hex, "status": "queued", "progress": 0.0, "created": time.time()}
        JOBS[job["id"]] = job

    cfg = {"format": fmt, "quality": quality, "bitrate": bitrate, "trim": trim,
           "playlist": bool(data.get("playlist"))}
    EXECUTOR.submit(run_job, job, url, cfg)
    return jsonify(ok=True, id=job["id"])


@app.get("/api/jobs/<jid>")
def job_status(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job:
            return jsonify(ok=False, error="ไม่พบงานนี้ (อาจหมดอายุแล้ว)"), 404
        return jsonify(public_job(job))


@app.get("/api/jobs/<jid>/file")
def job_file(jid):
    with LOCK:
        job = JOBS.get(jid)
        path = job.get("file") if job and job["status"] == "done" else None
        name = job.get("filename") if job else None
    if not path or not Path(path).is_file():
        return jsonify(ok=False, error="ไม่พบไฟล์ (อาจหมดอายุแล้ว ให้โหลดใหม่)"), 404
    return send_file(path, as_attachment=True, download_name=name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
