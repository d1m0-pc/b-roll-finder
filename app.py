import concurrent.futures as cf
import glob
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")
PORT = 5055
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

app = Flask(__name__)
LOCK = threading.Lock()
YT_TOKENS = {}      # (query, orientation, dur, page) -> pageToken
ISSUED = set()      # URLs issued by the server itself (SSRF protection)

FFMPEG = shutil.which("ffmpeg")
HAS_YTDLP = importlib.util.find_spec("yt_dlp") is not None
HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None


# ----------------------------------------------------------------- protection
@app.before_request
def guard():
    if request.host not in ALLOWED_HOSTS:
        return jsonify(error="Forbidden host"), 403
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify(error="Forbidden origin"), 403


# ----------------------------------------------------------------- settings
def load_keys():
    keys = {}
    path = os.path.join(BASE, "keys.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
    return keys


def load_data():
    with LOCK:
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    d.setdefault("history", [])
    d.setdefault("favorites", {})
    d.setdefault("used", [])
    d.setdefault("folder", os.path.join(os.path.expanduser("~"), "Downloads", "b-roll"))
    return d


def save_data(d):
    with LOCK:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)


def issue(item):
    for f in item.get("files", []):
        ISSUED.add(f["url"])


# ----------------------------------------------------------------- sources
def search_pexels(key, query, page, per_page, orientation, **_):
    params = {"query": query, "page": page, "per_page": per_page}
    if orientation:
        params["orientation"] = orientation
    r = requests.get("https://api.pexels.com/videos/search",
                     headers={"Authorization": key}, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json().get("videos", [])
    out = []
    for v in raw:
        files = [{"w": f.get("width") or 0, "h": f.get("height") or 0, "url": f["link"]}
                 for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("link")]
        if not files:
            continue
        files.sort(key=lambda x: x["h"])
        out.append({
            "id": f"pexels-{v['id']}", "source": "Pexels", "kind": "stock",
            "thumb": v.get("image"), "preview": files[0]["url"],
            "duration": v.get("duration"), "w": v.get("width"), "h": v.get("height"),
            "author": (v.get("user") or {}).get("name", ""), "page": v.get("url"),
            "files": files,
        })
    return out, len(raw) >= per_page


def search_pixabay(key, query, page, per_page, orientation, **_):
    params = {"key": key, "q": query, "page": page, "per_page": max(3, per_page)}
    r = requests.get("https://pixabay.com/api/videos/", params=params, timeout=20)
    r.raise_for_status()
    raw = r.json().get("hits", [])
    out = []
    for v in raw:
        vids = v.get("videos", {})
        files = []
        for name in ("tiny", "small", "medium", "large"):
            f = vids.get(name)
            if f and f.get("url"):
                files.append({"w": f.get("width") or 0, "h": f.get("height") or 0, "url": f["url"]})
        if not files:
            continue
        files.sort(key=lambda x: x["h"])
        w, h = files[-1]["w"], files[-1]["h"]
        if orientation == "portrait" and h < w:
            continue
        if orientation == "landscape" and w < h:
            continue
        thumb = (vids.get("medium") or vids.get("small") or {}).get("thumbnail")
        out.append({
            "id": f"pixabay-{v['id']}", "source": "Pixabay", "kind": "stock",
            "thumb": thumb,
            "preview": (vids.get("tiny") or vids.get("small") or {}).get("url") or files[0]["url"],
            "duration": v.get("duration"), "w": w, "h": h,
            "author": v.get("user", ""), "page": v.get("pageURL"), "files": files,
        })
    return out, len(raw) >= max(3, per_page)


def search_coverr(key, query, page, per_page, orientation, **_):
    r = requests.get("https://api.coverr.co/videos",
                     headers={"x-api-key": key},
                     params={"query": query, "page": page - 1, "page_size": per_page, "urls": "true"},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    out = []
    for v in data.get("hits", []):
        urls = v.get("urls") or {}
        dl = urls.get("mp4_download") or urls.get("mp4")
        if not dl:
            continue
        w, h = v.get("max_width") or 0, v.get("max_height") or 0
        if orientation == "portrait" and h < w:
            continue
        if orientation == "landscape" and w < h:
            continue
        out.append({
            "id": f"coverr-{v['id']}", "source": "Coverr", "kind": "stock",
            "thumb": v.get("thumbnail") or v.get("poster"),
            "preview": urls.get("mp4_preview") or urls.get("mp4") or dl,
            "duration": round(v.get("duration") or 0), "w": w, "h": h,
            "author": v.get("title", ""), "page": f"https://coverr.co/videos/{v['id']}",
            "files": [{"w": w, "h": h, "url": dl}],
        })
    return out, (data.get("page", 0) + 1) < (data.get("pages", 0) or 0)


def parse_iso_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = [int(x or 0) for x in m.groups()]
    return d * 86400 + h * 3600 + mi * 60 + sec


def search_youtube(key, query, page, per_page, orientation, yt_dur="short", **_):
    q = query + (" #shorts" if orientation == "portrait" else "")
    params = {"key": key, "q": q, "part": "snippet", "type": "video",
              "maxResults": min(per_page, 25), "safeSearch": "moderate"}
    if yt_dur in ("short", "medium"):
        params["videoDuration"] = yt_dur
    if page > 1:
        tok = YT_TOKENS.get((query, orientation, yt_dur, page))
        if not tok:
            return [], False
        params["pageToken"] = tok
    r = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = [it for it in data.get("items", []) if (it.get("id") or {}).get("videoId")]
    if data.get("nextPageToken"):
        YT_TOKENS[(query, orientation, yt_dur, page + 1)] = data["nextPageToken"]
    durations = {}
    if items:
        r2 = requests.get("https://www.googleapis.com/youtube/v3/videos",
                          params={"key": key, "part": "contentDetails",
                                  "id": ",".join(i["id"]["videoId"] for i in items)}, timeout=20)
        r2.raise_for_status()
        for v in r2.json().get("items", []):
            durations[v["id"]] = parse_iso_duration(v["contentDetails"].get("duration"))
    out = []
    for it in items:
        vid = it["id"]["videoId"]
        sn = it.get("snippet", {})
        th = sn.get("thumbnails", {})
        thumb = (th.get("high") or th.get("medium") or th.get("default") or {}).get("url")
        out.append({
            "id": f"youtube-{vid}", "source": "YouTube", "kind": "youtube", "vid": vid,
            "thumb": thumb, "preview": None, "duration": durations.get(vid, 0),
            "w": None, "h": None, "author": sn.get("channelTitle", ""),
            "title": sn.get("title", ""),
            "page": f"https://www.youtube.com/watch?v={vid}", "files": [],
        })
    return out, bool(data.get("nextPageToken"))


SOURCES = {
    "pexels": ("PEXELS_KEY", search_pexels),
    "pixabay": ("PIXABAY_KEY", search_pixabay),
    "coverr": ("COVERR_KEY", search_coverr),
    "youtube": ("YOUTUBE_KEY", search_youtube),
}


# ----------------------------------------------------------------- routes: state
@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.route("/api/state")
def state():
    keys = load_keys()
    d = load_data()
    favs = list(d["favorites"].values())
    for it in favs:
        issue(it)
    return jsonify({
        "history": d["history"], "favorites": favs, "folder": d["folder"],
        "sources": {name: bool(keys.get(k)) for name, (k, _) in SOURCES.items()},
        "tools": {"ffmpeg": bool(FFMPEG), "ytdlp": HAS_YTDLP,
                  "claude": bool(keys.get("ANTHROPIC_KEY")) and HAS_ANTHROPIC},
    })


@app.route("/api/fav", methods=["POST"])
def fav():
    item = (request.get_json(silent=True, force=True) or {}).get("item") or {}
    iid = item.get("id")
    if not iid or not re.fullmatch(r"[a-z]+-[\w-]+", iid):
        return jsonify(error="Invalid clip"), 400
    d = load_data()
    if iid in d["favorites"]:
        del d["favorites"][iid]
        now = False
    else:
        if len(d["favorites"]) >= 500:
            return jsonify(error="Too many favorites (max 500)"), 400
        d["favorites"][iid] = item
        issue(item)
        now = True
    save_data(d)
    return jsonify({"fav": now})


@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    code = ("import tkinter as tk\nfrom tkinter import filedialog\n"
            "r=tk.Tk()\nr.withdraw()\nr.attributes('-topmost',True)\n"
            "print(filedialog.askdirectory(title='Choose a folder for B-roll clips') or '')\n")
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=300)
        path = p.stdout.strip()
    except Exception:
        path = ""
    if not path:
        return jsonify(error="Folder selection failed, type the path manually."), 400
    d = load_data()
    d["folder"] = path
    save_data(d)
    return jsonify({"folder": path})


# ----------------------------------------------------------------- search
def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.route("/api/search")
def search():
    keys = load_keys()
    query = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = min(30, max(3, int(request.args.get("per_page", 15) or 15)))
    orientation = request.args.get("orientation", "")
    yt_dur = request.args.get("yt_dur", "short")
    wanted = [s for s in request.args.get("src", "pexels,pixabay,coverr").split(",") if s in SOURCES]
    min_dur = to_float(request.args.get("min_dur"))
    max_dur = to_float(request.args.get("max_dur"))
    min_h = to_float(request.args.get("min_h"))
    sort = request.args.get("sort", "")
    if not query:
        return jsonify({"results": [], "errors": [], "has_more": False})

    jobs, errors = {}, []
    with cf.ThreadPoolExecutor() as ex:
        for name in wanted:
            keyname, fn = SOURCES[name]
            if not keys.get(keyname):
                errors.append(f"{name}: no key ({keyname}) in keys.txt")
                continue
            jobs[name] = ex.submit(fn, keys[keyname], query, page, per_page, orientation, yt_dur=yt_dur)
        results, has_more = [], False
        for name, fut in jobs.items():
            try:
                items, more = fut.result()
                results.append(items)
                has_more = has_more or more
            except Exception as e:
                errors.append(f"{name}: {e}")

    def keep(it):
        dur = it.get("duration") or 0
        if min_dur and dur and dur < min_dur:
            return False
        if max_dur and dur and dur > max_dur:
            return False
        if min_h and it["kind"] == "stock":
            best = max((min(f["w"], f["h"]) for f in it["files"]), default=0)
            if best < min_h:
                return False
        return True

    merged = []
    for i in range(max((len(r) for r in results), default=0)):
        for r in results:
            if i < len(r) and keep(r[i]):
                merged.append(r[i])
    if sort == "duration_desc":
        merged.sort(key=lambda x: -(x.get("duration") or 0))
    elif sort == "duration_asc":
        merged.sort(key=lambda x: (x.get("duration") or 0))

    d = load_data()
    used, favs = set(d["used"]), d["favorites"]
    for it in merged:
        issue(it)
        it["used"] = it["id"] in used
        it["fav"] = it["id"] in favs

    if page == 1:
        hist = [h for h in d["history"] if h.lower() != query.lower()]
        d["history"] = ([query] + hist)[:30]
        save_data(d)
    return jsonify({"results": merged, "errors": errors, "has_more": has_more})


# ----------------------------------------------------------------- download
def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s or "klip").strip("_")[:80] or "klip"


def get_stock(url, dest):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


LINK_HOSTS = ("pinterest.com", "pin.it", "tiktok.com", "instagram.com",
              "youtube.com", "youtu.be", "vimeo.com")


def link_allowed(url):
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in LINK_HOSTS)


def get_ytdlp(url, height, start, end, item_dir, ranged=True):
    """ranged=True: trim [start, end] (first 60 s by default). ranged=False: whole clip (max 10 min)."""
    if not HAS_YTDLP:
        raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
    h = int(height) if str(height).isdigit() else 1080
    fmt = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
           f"bv*[height<={h}]+ba/b[height<={h}]/b")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "-q",
           "--socket-timeout", "30", "-f", fmt, "--merge-output-format", "mp4",
           "-o", os.path.join(item_dir, "clip.%(ext)s")]
    if ranged:
        if not FFMPEG:
            raise RuntimeError("ffmpeg is not installed (needed to trim clips)")
        start = max(0, int(to_float(start) or 0))
        end = int(to_float(end) or 0)
        if end <= start:
            end = start + 60
        cmd += ["--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts"]
    else:
        cmd += ["--match-filter", "duration<=?600"]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    files = [f for f in glob.glob(os.path.join(item_dir, "*"))
             if os.path.splitext(f)[1].lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if p.returncode != 0 or not files:
        if p.returncode == 0:
            raise RuntimeError("video is longer than 10 min: enter from/to seconds")
        raise RuntimeError("yt-dlp: " + (p.stderr.strip().splitlines() or ["download failed"])[-1])
    return max(files, key=os.path.getsize)


VERTICAL_VF = ("crop=w='if(gt(a,9/16),trunc(ih*9/16/2)*2,iw)':"
               "h='if(gt(a,9/16),ih,trunc(iw*16/9/2)*2)',"
               "scale=1080:1920:flags=lanczos,setsar=1")


def to_vertical(src, dst):
    if not FFMPEG:
        raise RuntimeError("ffmpeg is not installed (needed for 9:16)")
    p = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-vf", VERTICAL_VF,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", dst],
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + (p.stderr.strip().splitlines() or ["error"])[-1])


def process_item(idx, it, tmp, vertical, numbered):
    item_dir = os.path.join(tmp, f"i{idx}")
    os.makedirs(item_dir, exist_ok=True)
    if it.get("kind") == "youtube":
        vid = str(it.get("vid", ""))
        if not re.fullmatch(r"[\w-]{11}", vid):
            raise RuntimeError("invalid YouTube ID")
        src = get_ytdlp(f"https://www.youtube.com/watch?v={vid}", it.get("h"),
                        it.get("start"), it.get("end"), item_dir)
    elif it.get("kind") == "link":
        url = str(it.get("url", "")).strip()
        if not link_allowed(url):
            raise RuntimeError("link not supported (Pinterest, TikTok, Instagram, YouTube, Vimeo)")
        ranged = bool(str(it.get("start") or "").strip() or str(it.get("end") or "").strip())
        src = get_ytdlp(url, it.get("h"), it.get("start"), it.get("end"), item_dir, ranged=ranged)
    else:
        url = it.get("url", "")
        if url not in ISSUED:
            raise RuntimeError("unknown link (search again)")
        src = os.path.join(item_dir, "raw.mp4")
        get_stock(url, src)
    base = safe_name(it.get("name"))
    final_name = (f"{idx + 1:02d}_" if numbered else "") + base + (".9x16" if vertical else "") + ".mp4"
    final = os.path.join(item_dir, "final_" + final_name)
    if vertical:
        to_vertical(src, final)
    else:
        shutil.move(src, final)
    return final_name, final


@app.route("/api/download", methods=["POST"])
def download():
    body = request.get_json(silent=True, force=True) or {}
    items = body.get("items") or []
    mode = body.get("mode", "zip")
    vertical = bool(body.get("vertical"))
    numbered = bool(body.get("numbered"))
    if not items:
        return jsonify(error="Nothing selected"), 400
    if vertical and not FFMPEG:
        return jsonify(error="9:16 needs ffmpeg (not found)."), 400
    folder = (body.get("folder") or "").strip()
    if mode == "folder":
        if not folder:
            return jsonify(error="Enter or choose a folder."), 400
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            return jsonify(error=f"Could not create folder: {e}"), 400

    tmp = tempfile.mkdtemp(prefix="broll_")
    done, failed = [], []

    def work(args):
        idx, it = args
        try:
            return idx, it, process_item(idx, it, tmp, vertical, numbered), None
        except Exception as e:
            return idx, it, None, str(e)

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for idx, it, res, err in ex.map(work, list(enumerate(items))):
            if err:
                failed.append(f"{it.get('name', it.get('id'))}: {err}")
            else:
                done.append((it, res[0], res[1]))

    if not done:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="No clip was downloaded. " + " | ".join(failed)), 500

    d = load_data()
    d["used"] = list(dict.fromkeys(d["used"] + [it["id"] for it, _, _ in done if it.get("id")]))[-5000:]
    if mode == "folder":
        d["folder"] = folder
    save_data(d)

    if mode == "folder":
        saved = []
        for _, name, path in done:
            target = os.path.join(folder, name)
            stem, ext = os.path.splitext(name)
            n = 1
            while os.path.exists(target):
                target = os.path.join(folder, f"{stem}_{n}{ext}")
                n += 1
            shutil.copyfile(path, target)
            saved.append(target)
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify({"saved": saved, "failed": failed, "folder": folder,
                        "ids": [it["id"] for it, _, _ in done if it.get("id")]})

    zip_path = os.path.join(tmp, "b-roll-clips.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for _, name, path in done:
            z.write(path, name)
    resp = send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name="b-roll-clips.zip")
    resp.headers["X-Failed"] = str(len(failed))
    resp.headers["X-Failed-Info"] = requests.utils.quote(" | ".join(failed)[:800])
    resp.headers["X-Done-Ids"] = ",".join(it["id"] for it, _, _ in done if it.get("id"))
    resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
    return resp


# ----------------------------------------------------------------- transcript -> keywords
SUGGEST_SYSTEM = """You help a podcast video editor choose B-roll footage.
You get a transcript (any language). Split it into consecutive moments where B-roll could be shown, and for each give stock-footage search phrases.
Rules:
- Return ONLY valid JSON, no prose, in this shape:
  {"segments":[{"text":"short excerpt from the transcript, max 12 words, original language","keywords":["phrase 1","phrase 2","phrase 3"]}]}
- Keywords are ENGLISH, concrete and visual, 1-3 words each (e.g. "stock market", "hacker typing", "city night"). 2-3 keywords per segment, best first.
- Skip parts that have no useful visual (greetings, filler, abstract talk).
- Keep the transcript order. At most 15 segments."""


@app.route("/api/suggest", methods=["POST"])
def suggest():
    keys = load_keys()
    if not keys.get("ANTHROPIC_KEY"):
        return jsonify(error="Add ANTHROPIC_KEY to keys.txt"), 400
    if not HAS_ANTHROPIC:
        return jsonify(error="The 'anthropic' package is not installed (pip install anthropic)"), 400
    text = ((request.get_json(silent=True, force=True) or {}).get("text") or "").strip()
    if len(text) < 20:
        return jsonify(error="Paste a transcript (at least a few sentences)."), 400
    text = text[:15000]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=keys["ANTHROPIC_KEY"])
        msg = client.messages.create(
            model=keys.get("CLAUDE_MODEL") or "claude-haiku-5-5",
            max_tokens=2500, system=SUGGEST_SYSTEM,
            messages=[{"role": "user", "content": text}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        a, b = raw.find("{"), raw.rfind("}")
        segs = json.loads(raw[a:b + 1]).get("segments", [])
    except Exception as e:
        return jsonify(error=f"Suggestions failed: {e}"), 500
    clean = []
    for s in segs[:15]:
        kws = [str(k).strip() for k in s.get("keywords", []) if str(k).strip()][:3]
        if kws:
            clean.append({"text": str(s.get("text", ""))[:140], "keywords": kws})
    if not clean:
        return jsonify(error="No B-roll moments found in this text."), 400
    return jsonify({"segments": clean})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)
