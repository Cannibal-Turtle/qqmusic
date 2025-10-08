# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
KuGou downloader (free tier). Gets play URL from mobile API, cover/metadata from desktop API when possible.
Usage:
  python kugou.py "<kugou url>"
"""

import os, re, sys, urllib.parse, requests, mutagen.easyid3, mutagen.id3, mutagen.mp3

CHUNK = 1024 * 256
HDR_M = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.kugou.com/",
}
HDR_D = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.kugou.com/",
}

# Save to Android Downloads
OUTPUT_DIR = "/storage/emulated/0/Download"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200]

def download_file(path: str, url: str):
    print(f"⬇️  Downloading: {path}")
    with requests.get(url, headers=HDR_D, stream=True, timeout=25) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for c in r.iter_content(chunk_size=CHUNK):
                if c:
                    f.write(c)

# ---------- URL parsing ----------
def parse_hash_album(url: str):
    """Return (hash, album_id or None)."""
    u = urllib.parse.urlparse(url)

    # 1) fragment (#hash=...&album_id=...)
    frag = urllib.parse.parse_qs(u.fragment)
    if "hash" in frag:
        return frag["hash"][0], frag.get("album_id", [None])[0]

    # 2) query (?hash=...&album_id=...)
    qs = urllib.parse.parse_qs(u.query)
    if "hash" in qs:
        return qs["hash"][0], qs.get("album_id", [None])[0]

    # 3) mixsong page: scrape HTML for both hash and album_id
    html = requests.get(url, headers=HDR_D, timeout=15).text
    m_hash = re.search(r'"hash"\s*:\s*"([A-F0-9]{32})"', html, re.I) or re.search(r'hash=([A-F0-9]{32})', html, re.I)
    if not m_hash:
        return None, None
    h = m_hash.group(1).upper()
    m_album = re.search(r'"album_id"\s*:\s*(\d+)', html)
    return h, (m_album.group(1) if m_album else None)

# ---------- APIs ----------
def get_mobile_meta(hash_id: str):
    """Free playback URL + basic meta (often singer avatar)."""
    api = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={hash_id}"
    r = requests.get(api, headers=HDR_M, timeout=15)
    r.raise_for_status()
    d = r.json()
    if not d.get("url"):
        raise RuntimeError(f"No free URL (maybe VIP or region): {d}")
    return d

def get_desktop_meta(hash_id: str, album_id: str):
    """Richer meta & album art. Requires album_id."""
    api = f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata&hash={hash_id}&album_id={album_id}"
    r = requests.get(api, headers=HDR_D, timeout=15)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and j.get("status") == 1 and "data" in j:
        return j["data"]
    raise RuntimeError(f"Desktop API unexpected: {j}")

def pick_cover_url(meta_mobile: dict, meta_desktop: dict | None):
    """Prefer real cover (album_img or union_cover). Fallback to imgUrl (often singer avatar)."""
    if meta_desktop:
        u = meta_desktop.get("album_img") or (meta_desktop.get("trans_param") or {}).get("union_cover")
        if u:
            return u.replace("{size}", "400").replace("http://", "https://")
    u = (meta_mobile.get("imgUrl") or "").replace("{size}", "400").replace("http://", "https://")
    return u

# ---------- Tagging ----------
def embed_tags(path: str, meta_mobile: dict, meta_desktop: dict | None, cover_url: str):
    # Prefer clean fields from desktop when present
    title = (meta_desktop or {}).get("song_name") or meta_mobile.get("songName") or meta_mobile.get("fileName") or "Unknown"
    artist = (meta_desktop or {}).get("author_name") or meta_mobile.get("singerName") or "Unknown"
    album  = (meta_desktop or {}).get("album_name") or meta_mobile.get("album_name") or "Unknown"

    # EasyID3 basic tags
    audio = mutagen.easyid3.EasyID3(path)
    audio["title"] = title
    audio["artist"] = artist
    audio["album"]  = album
    audio.save()

    # Cover art
    if cover_url:
        tmp = path + ".cover"
        try:
            download_file(tmp, cover_url)
            mp3 = mutagen.mp3.MP3(path)
            try:
                mp3.add_tags()
            except Exception:
                pass
            # Guess mime
            lower = cover_url.lower()
            mime = "image/jpeg"
            if lower.endswith(".png"):  mime = "image/png"
            if lower.endswith(".webp"): mime = "image/webp"
            with open(tmp, "rb") as f:
                mp3.tags.add(mutagen.id3.APIC(
                    encoding=mutagen.id3.Encoding.UTF8,
                    mime=mime,
                    type=mutagen.id3.PictureType.COVER_FRONT,
                    desc="Front cover",
                    data=f.read()
                ))
            mp3.save()
            os.remove(tmp)
            print("✅ Embedded cover art.")
        except Exception as e:
            print(f"⚠️  Cover fetch failed: {e}")

# ---------- Main ----------
def main():
    if len(sys.argv) < 2:
        print('Usage: python kugou.py "<kugou url>"')
        return
    url = sys.argv[1].strip()
    print(f"🎵 URL: {url}")

    # Parse identifiers
    h, album_id = parse_hash_album(url)
    if not h:
        print("❌ Error: Could not find hash in URL or page.")
        return
    print(f"🔍 Hash: {h}{'  |  album_id: ' + album_id if album_id else ''}")

    # Get free play URL
    try:
        m = get_mobile_meta(h)
    except Exception as e:
        print("❌ Error (mobile/free):", e)
        return

    # Try to enrich meta (for correct cover)
    d = None
    if album_id:
        try:
            d = get_desktop_meta(h, album_id)
        except Exception as e:
            print("⚠️  Desktop meta not available:", e)

    # Build filename: Artist - Title (from best meta)
    title  = (d or {}).get("song_name") or m.get("songName") or m.get("fileName") or "Unknown"
    artist = (d or {}).get("author_name") or m.get("singerName") or "Unknown"
    fname  = safe(f"{artist} - {title}.mp3")
    out    = os.path.join(OUTPUT_DIR, fname)

    # Download audio
    try:
        download_file(out, m["url"])
    except Exception as e:
        print("❌ Download error:", e)
        return

    # Tag + cover
    cover_url = pick_cover_url(m, d)
    try:
        embed_tags(out, m, d, cover_url)
    except Exception as e:
        print("⚠️  Tagging failed:", e)

    print(f"✅ Done. Saved to: {out}")

if __name__ == "__main__":
    main()
