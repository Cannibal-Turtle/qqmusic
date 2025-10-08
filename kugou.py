# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
KuGou downloader (free version only).
Usage:
  python kugou.py "<kugou url>"
"""

import os, re, sys, urllib.parse, requests, mutagen.easyid3, mutagen.id3, mutagen.mp3

CHUNK_SIZE = 1024 * 256
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.kugou.com/",
}

# ✅ Save directly to Android Music folder
OUTPUT_DIR = "/storage/emulated/0/Download/KuGou"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def windows_safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200]

def download_file(filename: str, url: str):
    print(f"⬇️  Downloading: {filename}")
    with requests.get(url, headers=HEADERS, stream=True, timeout=25) as r:
        r.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)

def get_meta(hash_id: str):
    api = f"https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={hash_id}"
    r = requests.get(api, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("url"):
        raise RuntimeError(f"❌ No free URL found. Song may be VIP only: {data}")
    return data

def extract_hash(url: str):
    u = urllib.parse.urlparse(url)
    frag_qs = urllib.parse.parse_qs(u.fragment)
    if "hash" in frag_qs:
        return frag_qs["hash"][0]
    m = re.search(r"hash=([A-F0-9]{32})", url, re.I)
    if m:
        return m.group(1)
    # Try mixsong page
    resp = requests.get(url, headers=HEADERS, timeout=10)
    m2 = re.search(r'"hash"\s*:\s*"([A-F0-9]{32})"', resp.text, re.I)
    if m2:
        return m2.group(1)
    raise RuntimeError("❌ Could not find hash in URL or page.")

def add_tags(filename, data):
    audio = mutagen.easyid3.EasyID3(filename)
    audio["title"] = data.get("fileName", "Unknown")
    audio["artist"] = data.get("singerName", "Unknown")
    audio.save()

    img_url = data.get("imgUrl")
    if img_url:
        img_url = img_url.replace("{size}", "400")
        cover_temp = filename + ".cover"
        download_file(cover_temp, img_url)
        audio = mutagen.mp3.MP3(filename)
        try:
            audio.add_tags()
        except Exception:
            pass
        mime = "image/jpeg"
        with open(cover_temp, "rb") as f:
            audio.tags.add(mutagen.id3.APIC(
                encoding=mutagen.id3.Encoding.UTF8,
                mime=mime,
                type=mutagen.id3.PictureType.COVER_FRONT,
                desc="Front cover",
                data=f.read()
            ))
        audio.save()
        os.remove(cover_temp)
        print("✅ Embedded cover art.")

def main():
    if len(sys.argv) < 2:
        print('Usage: python kugou.py "<kugou url>"')
        return
    url = sys.argv[1].strip()
    print(f"🎵 URL: {url}")
    try:
        h = extract_hash(url)
        print(f"🔍 Hash: {h}")
        data = get_meta(h)
        play_url = data["url"]
        file_name = windows_safe_name(data["fileName"]) + ".mp3"
        out_path = os.path.join(OUTPUT_DIR, file_name)
        download_file(out_path, play_url)
        add_tags(out_path, data)
        print(f"✅ Done. Saved to: {out_path}")
    except Exception as e:
        print("❌ Error:", e)

if __name__ == "__main__":
    main()
