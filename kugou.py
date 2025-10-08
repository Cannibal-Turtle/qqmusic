# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
KuGou downloader (free tracks only; no paywall bypass).
- Saves to /storage/emulated/0/Download on Android/Termux
- Chooses best cover: desktop API -> page og:image -> mobile imgUrl
- Embeds cover + basic ID3 tags

Usage:
  python kugou.py "<kugou url>"
  python kugou.py "<kugou url>" --cover "https://example.com/cover.jpg"
"""

import os, re, sys, argparse, urllib.parse
from typing import Optional, Tuple, Dict

import requests
import mutagen.easyid3, mutagen.id3, mutagen.mp3

# ---------- Config ----------
CHUNK_SIZE = 1024 * 256

HEADERS_DESKTOP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
    "Referer": "https://www.kugou.com/",
}
HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.kugou.com/",
}

# Save directly to Android "Download" folder
OUTPUT_DIR = "/storage/emulated/0/Download"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------- Helpers ----------
def windows_safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200]

def _normalize_img(u: str) -> str:
    if not u:
        return ""
    u = u.replace("{size}", "400")
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u

def download_file(filename: str, url: str, headers: dict):
    print(f"⬇️  Downloading: {filename}")
    with requests.get(url, headers=headers, stream=True, timeout=25) as r:
        r.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)

def ensure_id3_container(path: str):
    try:
        mp3 = mutagen.mp3.MP3(path)
        try:
            mp3.add_tags()
        except Exception:
            pass
        mp3.save()
    except Exception:
        pass

# ---------- URL / Hash ----------
def parse_hash_album_from_url_or_page(url: str) -> Tuple[str, Optional[str]]:
    """
    Returns (hash, album_id?) by checking URL fragment/query first,
    then scraping mixsong page if needed.
    """
    u = urllib.parse.urlparse(url)

    # fragment (after #)
    frag_qs = urllib.parse.parse_qs(u.fragment)
    if "hash" in frag_qs:
        h = frag_qs["hash"][0]
        album_id = frag_qs.get("album_id", [None])[0]
        return h, album_id

    # query (after ?)
    qs = urllib.parse.parse_qs(u.query)
    if "hash" in qs:
        h = qs["hash"][0]
        album_id = qs.get("album_id", [None])[0]
        return h, album_id

    # mixsong page scrape
    if re.search(r"/mixsong/([A-Za-z0-9]+)\.html", url):
        resp = requests.get(url, headers=HEADERS_DESKTOP, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # hash
        m_hash = re.search(r'"hash"\s*:\s*"([A-F0-9]{32})"', html, re.I) or \
                 re.search(r'hash=([A-F0-9]{32})', html, re.I)
        if not m_hash:
            raise RuntimeError("Could not find song hash in page.")
        h = m_hash.group(1).upper()

        # album_id (optional)
        m_album = re.search(r'"album_id"\s*:\s*(\d+)', html)
        album_id = m_album.group(1) if m_album else None
        return h, album_id

    raise RuntimeError("❌ Could not find hash in URL or page.")

# ---------- Metadata ----------
def get_mobile_meta(hash_id: str) -> Dict:
    """
    Mobile API (free tracks only):
    https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=<hash>
    Returns keys like: url, fileName, singerName, imgUrl
    """
    api = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={hash_id}"
    r = requests.get(api, headers=HEADERS_MOBILE, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not data.get("url"):
        raise RuntimeError(f"Mobile API returned no free url: {data}")
    return data

def get_desktop_meta(hash_id: str, album_id: Optional[str]) -> Optional[Dict]:
    """
    Desktop API sometimes contains better cover fields (album_img/union_cover),
    but it often blocks with status 0/err_code 20010.
    """
    if not album_id:
        return None
    api = f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata&hash={hash_id}&album_id={album_id}"
    r = requests.get(api, headers=HEADERS_DESKTOP, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict) or payload.get("status") != 1 or "data" not in payload:
        raise RuntimeError(f"Desktop API unexpected: {payload}")
    return payload["data"]

def fetch_og_image(page_url: str, headers: dict) -> Optional[str]:
    """
    Try to read <meta property="og:image" content="..."> from the mixsong page.
    """
    try:
        resp = requests.get(page_url, headers=headers, timeout=12)
        resp.raise_for_status()
        html = resp.text
        m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
        if m:
            return _normalize_img(m.group(1))
    except Exception:
        pass
    return None

def choose_best_cover(src_page_url: str, mobile: Dict, desktop: Optional[Dict]) -> Tuple[str, str]:
    """
    Order: desktop cover -> page og:image -> mobile imgUrl
    Returns: (cover_url, source_label)
    """
    # 1) desktop data (best when available)
    if desktop:
        for key in ("album_img", "union_cover", "img"):
            u = _normalize_img(desktop.get(key, ""))
            if u:
                return u, f"desktop:{key}"

    # 2) page og:image
    page_img = fetch_og_image(src_page_url, HEADERS_DESKTOP)
    if page_img:
        return page_img, "page:og:image"

    # 3) mobile avatar (usually artist pic)
    u = _normalize_img(mobile.get("imgUrl", ""))
    return u, "mobile:imgUrl"

# ---------- Tagging ----------
def add_basic_id3_tags(filename: str, mobile_data: Dict):
    audio = mutagen.easyid3.EasyID3(filename)
    title = mobile_data.get("fileName", "Unknown")
    artist = mobile_data.get("singerName", "Unknown")
    audio["title"] = title
    audio["artist"] = artist
    audio.save()

def embed_cover(filename: str, cover_url: str, headers: dict):
    if not cover_url:
        print("No cover URL available to embed.")
        return
    tmp = filename + ".cover"
    download_file(tmp, cover_url, headers)
    audio = mutagen.mp3.MP3(filename)
    try:
        audio.add_tags()
    except Exception:
        pass
    mime = "image/jpeg"
    lower = cover_url.lower()
    if lower.endswith(".png"):
        mime = "image/png"
    elif lower.endswith(".webp"):
        mime = "image/webp"
    with open(tmp, "rb") as f:
        audio.tags.add(
            mutagen.id3.APIC(
                encoding=mutagen.id3.Encoding.UTF8,
                mime=mime,
                type=mutagen.id3.PictureType.COVER_FRONT,
                desc="Front cover",
                data=f.read(),
            )
        )
    os.remove(tmp)
    audio.save()
    print("✅ Embedded cover art.")

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="KuGou downloader (free only).")
    ap.add_argument("url", help="KuGou song URL (mixsong or hash form)")
    ap.add_argument("--cover", help="Override cover image URL", default=None)
    args = ap.parse_args()

    url = args.url.strip()
    print(f"🎵 URL: {url}")

    # Resolve hash (+ optional album_id)
    try:
        h, album_id = parse_hash_album_from_url_or_page(url)
        print(f"🔍 Hash: {h}  |  album_id: {album_id}")
    except Exception as e:
        print("❌ Error:", e)
        sys.exit(2)

    # Query mobile API (for free playback URL)
    try:
        mobile = get_mobile_meta(h)
    except Exception as e:
        print("❌ Error:", e)
        sys.exit(3)

    # Try desktop meta just for richer cover (non-fatal)
    desktop = None
    try:
        desktop = get_desktop_meta(h, album_id)
    except Exception as e:
        print(f"⚠️  Desktop meta not available: {e}")

    # Pick cover
    if args.cover:
        cover_url, cover_src = _normalize_img(args.cover), "override"
    else:
        cover_url, cover_src = choose_best_cover(url, mobile, desktop)
    print(f"🖼  Cover source: {cover_src} -> {cover_url or 'None'}")

    # Build filename (strip to avoid leading spaces)
    base_name = windows_safe_name(mobile.get("fileName", "Unknown")).strip() + ".mp3"
    out_path = os.path.join(OUTPUT_DIR, base_name)

    # Download audio
    play_url = mobile.get("url")
    if not play_url:
        print("❌ No playable URL in mobile API (track may be VIP only).")
        sys.exit(4)

    try:
        download_file(out_path, play_url, HEADERS_MOBILE)
    except Exception as e:
        print("❌ Download error:", e)
        sys.exit(5)

    # Tagging
    if out_path.lower().endswith(".mp3"):
        ensure_id3_container(out_path)
        try:
            add_basic_id3_tags(out_path, mobile)
        except Exception as e:
            print(f"⚠️  Tagging (basic) failed: {e}")
        try:
            if cover_url:
                embed_cover(out_path, cover_url, HEADERS_DESKTOP)
        except Exception as e:
            print(f"⚠️  Tagging (cover) failed: {e}")

    print(f"✅ Done. Saved to: {out_path}")

if __name__ == "__main__":
    # Please respect KuGou’s Terms of Service and only download tracks that are
    # offered for free playback in your region. This script does not bypass paywalls.
    main()
