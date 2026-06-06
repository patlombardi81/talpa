import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "list.m3u")
HEAD_FILE = os.path.join(OUTPUT_DIR, "list.m3u.head")

LIST_URL = os.environ.get("LIST_URL", "").strip()

ADULT_GROUPS = {
    "adulti",
    "adult",
    "adults",
    "xxx",
    "porn",
    "pornxxx",
    "hentai",
    "18+",
    "erotic",
    "erotici",
    "playboy",
    "brazzers",
    "penthouse",
    "redlight",
    "hardcore",
    "nsfw",
    "onlyfans",
}

ADULT_KEYWORDS = re.compile(
    r"\b(porn|xxx|hentai|erotic[ie]?|playboy|brazzers|penthouse|"
    r"hardcore|onlyfans|nsfw)\b",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def download(url):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=ctx, timeout=120) as r:
        return r.read().decode("utf-8", errors="replace")


def split_entries(content):
    """Split M3U content into blocks, each starting with #EXTINF (or header)."""
    parts = re.split(r"(?=^#EXTINF)", content, flags=re.MULTILINE)
    header = []
    entries = []
    for p in parts:
        if p.lstrip().startswith("#EXTINF"):
            entries.append(p)
        else:
            header.append(p)
    return "".join(header), entries


def is_adult(entry):
    m = re.search(r'group-title="([^"]+)"', entry)
    if m and m.group(1).strip().lower() in ADULT_GROUPS:
        return True
    m = re.search(r"^#EXTINF[^,]*,(.*)$", entry, flags=re.MULTILINE)
    if m and ADULT_KEYWORDS.search(m.group(1)):
        return True
    return False


def filter_adult(header, entries):
    kept = [e for e in entries if not is_adult(e)]
    return header, kept, len(entries) - len(kept)


def convert_clearkey(text):
    return text.replace(
        "inputstream.adaptive.license_type=clearkey",
        "inputstream.adaptive.license_type=org.w3.clearkey",
    )


def main():
    if not LIST_URL:
        log("Errore: variabile d'ambiente LIST_URL non impostata.")
        log("Impostala con:  $env:LIST_URL='https://...'; python script.py")
        sys.exit(1)

    log(f"Download da: {LIST_URL}")
    try:
        content = download(LIST_URL)
    except Exception as e:
        log(f"Download fallito: {e}")
        sys.exit(2)
    log(f"Scaricati {len(content)} caratteri")

    header, entries = split_entries(content)
    total = len(entries)
    log(f"Entry #EXTINF trovate: {total}")

    header, kept, removed = filter_adult(header, entries)
    if removed:
        log(f"Entry ADULT rimosse: {removed}")
    else:
        log("Nessuna entry ADULT trovata")

    out_text = header + "".join(kept)

    before_ck = out_text.count("inputstream.adaptive.license_type=clearkey")
    out_text = convert_clearkey(out_text)
    after_ck = out_text.count("inputstream.adaptive.license_type=clearkey")
    log(f"license_type convertite: {before_ck - after_ck}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(out_text)

    log(f"File scritto: {OUTPUT_FILE}")
    log(f"Dimensione output: {os.path.getsize(OUTPUT_FILE)} byte")
    log(f"Entry finali: {len(kept)}")


if __name__ == "__main__":
    main()
