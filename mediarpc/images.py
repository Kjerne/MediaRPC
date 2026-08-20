"""Image upload (Zipline/ImgBB), squaring, dedupe cache, poster resolution."""
import os
import sys
import time
import json
from io import BytesIO
from PIL import Image, ImageFilter, ImageOps

from . import rt


def load_status_icons():
    """Set permanent status icon URLs from Imgur (no upload needed)."""

    rt.STATUS_ICON_PLAY  = "https://i.imgur.com/wDhrODz.png"
    rt.STATUS_ICON_PAUSE = "https://i.imgur.com/4ZnvVao.png"
    rt.EMBY_LOGO_URL     = "https://i.imgur.com/W9Wtkdn.png"

    rt.log("Status icons loaded from permanent Imgur URLs")


def square_poster_bytes(raw):
    """Make portrait posters look good in Discord's square activity image."""
    with Image.open(BytesIO(raw)) as img:
        img = img.convert("RGB")

        bg = ImageOps.fit(img, (512, 512), method=Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(18))
        bg = Image.blend(bg, Image.new("RGB", (512, 512), (18, 18, 20)), 0.35)

        poster = ImageOps.contain(img, (360, 500), method=Image.Resampling.LANCZOS)
        x = (512 - poster.width) // 2
        y = (512 - poster.height) // 2
        bg.paste(poster, (x, y))

        out = BytesIO()
        bg.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue()


def _zipline_get_folder_id():
    """Resolve (and cache) the MediaRPC folder id, creating it if missing."""
    if rt._zipline_folder_id is not None:
        return rt._zipline_folder_id
    if not (rt.ZIPLINE_ENABLED and rt.ZIPLINE_FOLDER):
        return None
    h = {"Authorization": rt.ZIPLINE_TOKEN}
    try:
        r = rt.http.get(f"{rt.ZIPLINE_URL}/api/user/folders", headers=h, timeout=15)
        if r.status_code == 200:
            for f in r.json():
                if f.get("name") == rt.ZIPLINE_FOLDER and not f.get("parentId"):
                    rt._zipline_folder_id = f.get("id")
                    return rt._zipline_folder_id
        # Not found → create it
        c = rt.http.post(f"{rt.ZIPLINE_URL}/api/user/folders", headers=h,
                          json={"name": rt.ZIPLINE_FOLDER}, timeout=15)
        if c.status_code in (200, 201):
            rt._zipline_folder_id = c.json().get("id")
            rt.log(f"Zipline: created folder '{rt.ZIPLINE_FOLDER}'")
    except Exception as e:
        rt.log(f"Zipline folder resolve error: {e}")
    return rt._zipline_folder_id


def _zipline_upload_bytes(raw):
    if not rt.ZIPLINE_ENABLED:
        return None
    # Zipline v4 expiry: header x-zipline-deletes-at = "date=<ISO8601 UTC>".
    from datetime import datetime, timedelta, timezone
    deletes = (datetime.now(timezone.utc) + timedelta(seconds=rt._UPLOAD_EXPIRY)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    headers = {"Authorization": rt.ZIPLINE_TOKEN, "x-zipline-deletes-at": f"date={deletes}"}
    fid = _zipline_get_folder_id()
    if fid:
        headers["X-Zipline-Folder"] = str(fid)
    try:
        r = rt.http.post(f"{rt.ZIPLINE_URL}/api/upload", headers=headers,
                          files={"file": ("poster.jpg", raw, "image/jpeg")}, timeout=20)
        if r.status_code in (200, 201):
            files = (r.json() or {}).get("files") or []
            if files:
                f0 = files[0]
                return f0.get("url") if isinstance(f0, dict) else f0
        else:
            rt.log(f"Zipline upload HTTP {r.status_code}")
    except Exception as e:
        rt.log(f"Zipline upload error: {e}")
    return None


def _imgbb_upload_bytes(raw):
    if not rt.IMGBB_KEY:
        return None
    try:
        r = rt.http.post(
            "https://api.imgbb.com/1/upload",
            params={"key": rt.IMGBB_KEY, "expiration": str(rt._UPLOAD_EXPIRY)},
            files={"image": ("poster.jpg", raw, "image/jpeg")},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()["data"]["url"]
    except Exception:
        pass
    return None


def upload_image_bytes(raw):
    """Host image bytes and return a public URL. Prefers Zipline, falls back to ImgBB."""
    return _zipline_upload_bytes(raw) or _imgbb_upload_bytes(raw)


def _img_cache_path():
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    return os.path.join(base, "image_cache.json")


def _img_cache_load():
    if rt._img_cache_loaded:
        return
    rt._img_cache_loaded = True
    try:
        with open(_img_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        rt._img_cache = {k: v for k, v in data.items() if now - v[1] < rt._IMG_CACHE_TTL}
    except Exception:
        rt._img_cache = {}


def _img_cache_save(force=False):
    # Throttle disk writes: a first-watch burst adds many posters back-to-back and
    # rewriting the whole JSON each time is wasteful. Skip if we saved recently
    # (the entry stays in memory and lands on a later write). force=True bypasses.
    now = time.time()
    if not force and now - rt._img_cache_last_save < rt.IMG_CACHE_SAVE_INTERVAL:
        return
    try:
        with open(_img_cache_path(), "w", encoding="utf-8") as f:
            json.dump(rt._img_cache, f)
        rt._img_cache_last_save = now
    except Exception as e:
        rt.log(f"Image cache save failed: {e}")


def _img_cache_key(url, square):
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}{u.path}"   # drop query (tokens/versions)
    except Exception:
        base = url
    return f"{'sq' if square else 'raw'}:{base}"


def _img_cache_get(url, square):
    _img_cache_load()
    v = rt._img_cache.get(_img_cache_key(url, square))
    if v and time.time() - v[1] < rt._IMG_CACHE_TTL:
        return v[0]
    return None


def _img_cache_set(url, square, hosted):
    _img_cache_load()
    rt._img_cache[_img_cache_key(url, square)] = [hosted, time.time()]
    _img_cache_save()


def upload_image(url, request_headers=None, square=True):
    if not rt.UPLOAD_ENABLED:
        return None

    cached = _img_cache_get(url, square)
    if cached:
        return cached

    try:
        raw = rt.http.get(url, headers=request_headers or {}, timeout=10).content
        if square:
            raw = square_poster_bytes(raw)
        hosted = upload_image_bytes(raw)
        if hosted:
            _img_cache_set(url, square, hosted)
        return hosted
    except Exception as e:
        rt.log(f"Poster upload error: {e}")
    return None


def get_poster(item):
    """Get poster URL.
    Tries ImgBB upload first (works for private servers, gives a public URL).
    Falls back to the direct Emby URL (works when your server is publicly reachable)."""
    media_type = item.get("Type")

    if media_type == "Episode":
        series_id = item.get("SeriesId")
        poster_id = series_id if series_id else item.get("Id")
    else:
        poster_id = item.get("Id")

    if not poster_id:
        return None

    # Check cache with TTL. A cached None (lookup failed) expires much faster than a
    # real URL, so a transient Emby hiccup doesn't pin the fallback icon for 6 h.
    if poster_id in rt.poster_cache:
        cached_url, cached_time = rt.poster_cache[poster_id]
        ttl = rt.POSTER_CACHE_TTL if cached_url else rt.POSTER_NULL_TTL
        if time.time() - cached_time < ttl:
            rt.poster_cache.move_to_end(poster_id)
            return cached_url
        else:
            rt.log(f"Poster cache expired for {poster_id}, refreshing")

    emby_url = f"{rt.SERVER}/Items/{poster_id}/Images/Primary?maxWidth=900"

    # Try the image host first (Zipline or ImgBB)
    if rt.UPLOAD_ENABLED:
        uploaded = upload_image(emby_url, request_headers=rt.headers, square=True)
        if uploaded:
            if len(rt.poster_cache) >= rt.CACHE_MAX_SIZE:
                rt._evict_oldest(rt.poster_cache)
            rt.poster_cache[poster_id] = (uploaded, time.time())
            return uploaded
        rt.log(f"Image upload failed for {poster_id} - trying direct Emby URL")

    # Fallback: direct Emby URL - only works if HTTPS (Discord rejects plain HTTP,
    # which would silently kill the whole assets block including the small icons)
    if emby_url.startswith("https://"):
        if len(rt.poster_cache) >= rt.CACHE_MAX_SIZE:
            rt._evict_oldest(rt.poster_cache)
        rt.poster_cache[poster_id] = (emby_url, time.time())
        return emby_url

    # Local/HTTP server - return None so the caller uses the bundled "emby" asset
    if len(rt.poster_cache) >= rt.CACHE_MAX_SIZE:
        rt._evict_oldest(rt.poster_cache)
    rt.poster_cache[poster_id] = (None, time.time())
    return None
