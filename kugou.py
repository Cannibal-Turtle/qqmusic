# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
KuGou downloader (free tracks only; no paywall bypass).
- Saves to /storage/emulated/0/Download (Android/Termux)
- Picks best cover in this order:
  desktop album_img -> album_img from page JSON -> page og:image -> album page og:image -> mobile imgUrl
- Embeds cover + basic ID3
- Title tag = just the song title (from "Artist - Title")
Usage:
  python kugou.py "<kugou url>"
  python kugou.py "<kugou url>" --cover "https://example.com/cover.jpg"
"""

import os, re, sys, argparse, urllib.parse
from typing import Optional, Tuple, Dict

import requests
import mutagen.easyid3, mutagen.id3, mutagen.mp3

CHUNK_SIZE = 1024 * 256

HEADERS_DESKTOP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
    "Referer": "https://www.kugou.com/",
}
HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.kugou.com/",
}

# Save to Android "Download" folder
OUTPUT_DIR = "/storage/emulated/0/Download"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def windows_safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200]

def _normalize_img(u: str) -> str:
    if not u:
        return ""
    u = u.replace("{size}", "1000")
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
def parse_hash_album_from_url_or_page(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Returns (hash, album_id?, page_html_if_scraped)
    """
    u = urllib.parse.urlparse(url)

    # fragment (#)
    frag_qs = urllib.parse.parse_qs(u.fragment)
    if "hash" in frag_qs:
        h = frag_qs["hash"][0]
        album_id = frag_qs.get("album_id", [None])[0]
        return h, album_id, None

    # query (?)
    qs = urllib.parse.parse_qs(u.query)
    if "hash" in qs:
        h = qs["hash"][0]
        album_id = qs.get("album_id", [None])[0]
        return h, album_id, None

    # mixsong page
    if re.search(r"/(?:mixsong|kgmixsong)/([A-Za-z0-9]+)\.html", url):
        resp = requests.get(url, headers=HEADERS_DESKTOP, timeout=15)
        resp.raise_for_status()
        html = resp.text
        import html as html_lib
        html = html_lib.unescape(html)
        m_hash = re.search(r'"hash"\s*:\s*"([A-F0-9]{32})"', html, re.I) or \
                 re.search(r'hash=([A-F0-9]{32})', html, re.I)
        if not m_hash:
            raise RuntimeError("Could not find song hash in page.")
        h = m_hash.group(1).upper()
        m_album = re.search(r'"album_id"\s*:\s*(\d+)', html)
        album_id = m_album.group(1) if m_album else None
        return h, album_id, html

    raise RuntimeError("❌ Could not find hash in URL or page.")

# ---------- Metadata ----------
def get_mobile_meta(hash_id: str) -> Dict:
    api = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={hash_id}"
    r = requests.get(api, headers=HEADERS_MOBILE, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not data.get("url"):
        raise RuntimeError(f"Mobile API returned no free url: {data}")
    return data

def get_desktop_meta(hash_id: str, album_id: Optional[str]) -> Optional[Dict]:
    if not album_id:
        return None
    api = f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata&hash={hash_id}&album_id={album_id}"
    r = requests.get(api, headers=HEADERS_DESKTOP, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict) or payload.get("status") != 1 or "data" not in payload:
        raise RuntimeError(f"Desktop API unexpected: {payload}")
    return payload["data"]

def extract_album_img_from_page_json(html: str) -> Optional[str]:
    # Try common JSON keys embedded on the page
    for key in ("album_img", "union_cover"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', html, re.I)
        if m:
            return _normalize_img(m.group(1))
    return None

def fetch_og_image(page_url: str, headers: dict) -> Optional[str]:
    try:
        r = requests.get(page_url, headers=headers, timeout=12)
        r.raise_for_status()
        html = r.text
        m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
        if m:
            u = m.group(1).replace("{size}", "1000")
            if u.startswith("http://"):
                u = "https://" + u[7:]
            return u
    except Exception:
        pass
    return None

def fetch_mobile_mixsong_og_image(url: str) -> Optional[str]:
    """
    If the desktop mixsong page is crippled on mobile/Termux,
    try the mobile-share variant which often exposes the same og:image.
    We convert .../mixsong/<mid>.html to .../share/mixsong/<mid>.html
    """
    m = re.search(r"/(?:mixsong|kgmixsong)/([A-Za-z0-9]+)\.html", url)
    if not m:
        return None
    mid = m.group(1)
    mobile_share = f"https://m.kugou.com/share/mixsong/{mid}.html"
    return fetch_og_image(mobile_share, HEADERS_MOBILE)

def choose_best_cover(src_page_url: str,
                      page_html: Optional[str],
                      album_id: Optional[str],
                      mobile: Dict,
                      desktop: Optional[Dict]) -> Tuple[str, str]:
    # 0) If desktop JSON worked, prefer its album image keys
    if desktop:
        for key in ("album_img", "union_cover", "img", "imgUrl"):
            u = desktop.get(key)
            if isinstance(u, str) and u:
                u = u.replace("{size}", "1000")
                if u.startswith("http://"):
                    u = "https://" + u[7:]
                if "imge.kugou.com" in u:  # album server, not singer avatar
                    return u, f"desktop:{key}"

    # 1) Try og:image on the original (desktop) mixsong page
    u = fetch_og_image(src_page_url, HEADERS_DESKTOP)
    if u and "imge.kugou.com" in u:
        return u, "page:og:image"

    # 2) If that fails on mobile, try the *mobile share* mixsong page
    u = fetch_mobile_mixsong_og_image(src_page_url)
    if u and "imge.kugou.com" in u:
        return u, "mobile_share:og:image"

    # 3) As another fallback, try the album page og:image when album_id is known
    if album_id:
        for album_url in (
            f"https://www.kugou.com/album/{album_id}.html",
            f"https://m.kugou.com/share/album/{album_id}.html",
        ):
            u2 = fetch_og_image(album_url, HEADERS_DESKTOP)
            if u2 and "imge.kugou.com" in u2:
                return u2, "album_page:og:image"

    # 4) Absolute fallback: mobile avatar (usually singer head)
    u = mobile.get("imgUrl") or ""
    u = u.replace("{size}", "1000")
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u, "mobile:imgUrl"


def fetch_album_page_cover(album_id: str) -> Optional[str]:
    # Try the desktop album page
    for album_url in (
        f"https://www.kugou.com/album/{album_id}.html",
        f"https://m.kugou.com/share/album/{album_id}.html",
    ):
        u = fetch_og_image(album_url, HEADERS_DESKTOP)
        if u:
            return u
    return None


# ---------- Tagging ----------
def add_basic_id3_tags(filename: str, mobile_data: Dict):
    file_name = mobile_data.get("fileName", "Unknown")
    # Title tag: only the right side after "Artist - Title" if present
    title = file_name.split(" - ", 1)[-1] if " - " in file_name else file_name
    artist = mobile_data.get("singerName", "Unknown")

    audio = mutagen.easyid3.EasyID3(filename)
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

    # Resolve hash (+ optional album_id) and capture page html if scraped
    try:
        h, album_id, page_html = parse_hash_album_from_url_or_page(url)
        print(f"🔍 Hash: {h}  |  album_id: {album_id}")
    except Exception as e:
        print("❌ Error:", e)
        sys.exit(2)

    # Mobile API (provides free play URL)
    try:
        mobile = get_mobile_meta(h)
    except Exception as e:
        print("❌ Error:", e)
        sys.exit(3)

    # Desktop meta (non-fatal; richer cover if it works)
    desktop = None
    try:
        desktop = get_desktop_meta(h, album_id)
    except Exception as e:
        print(f"⚠️  Desktop meta not available: {e}")

    # Choose cover
    if args.cover:
        cover_url, cover_src = _normalize_img(args.cover), "override"
    else:
        cover_url, cover_src = choose_best_cover(url, page_html, album_id, mobile, desktop)
    print(f"🖼  Cover source: {cover_src} -> {cover_url or 'None'}")

    # Filename as "Artist - Title.mp3" (from mobile fileName), trimmed
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
    # Please respect KuGou’s Terms of Service; this only downloads tracks
    # that KuGou serves for free in your region.
    main()
