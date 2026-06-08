#!/usr/bin/env python3
"""
Playlist + EPG Updater
Scarica playlist M3U, normalizza i canali, genera EPG unificato.

Configurazione tramite variabile d'ambiente PLAYLIST_CONFIG (JSON):
  {
    "base_url": "https://...",
    "sources": [
      {"remote": "filename.m3u", "output": "OUTPUT.m3u", "tag": "TAG"},
      {"remote": "other.m3u", "output": "OTHER.m3u", "tag": "OTHER",
       "base_url": "https://altro-dominio.com",
       "exclude_groups": ["ADULTI"]},
      ...
    ],
    "merge": {
      "output": "UNIFICATA.m3u",
      "exclude_tags": ["TAG1", ...]
    },
    "epg": {
      "output": "epg.xml.gz",
      "url": "https://raw.githubusercontent.com/patlombardi81/RobinHood/main/playlists/epg.xml.gz",
      "sources": [
        {"url": "https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz", "gzip": true},
        {"url": "https://iptv-epg.org/files/epg-it.xml.gz", "gzip": true}
      ]
    }
  }

Ogni source può specificare:
  - base_url (opzionale): sovrascrive il base_url globale
  - exclude_groups (opzionale): lista di group-title da escludere (case-insensitive)

Se presente la sezione `merge`, dopo il download delle singole playlist viene
creata una playlist unificata che include tutte le fonti tranne quelle elencate
in `exclude_tags`, rimuovendo i canali duplicati (per URL stream).

Se presente la sezione `epg`, lo script scarica e fonde le sorgenti EPG
specificate, filtra per i canali presenti nelle playlist, salva il risultato
e sovrascrive l'attributo url-tvg nell'header #EXTM3U di TUTTE le playlist
con l'URL fornito in `epg.url`.

Utilizzo:
  python3 download.py [--output-dir DIR] [--skip-epg]
"""

import os
import sys
import json
import gzip
import hashlib
import re
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:
    import urllib.request
    from urllib.error import URLError, HTTPError
except ImportError:
    print("Errore: modulo urllib non disponibile")
    sys.exit(1)

# ── Configurazione ───────────────────────────────────────────────────────────


def _load_config() -> dict:
    """Carica la configurazione dalla variabile d'ambiente PLAYLIST_CONFIG."""
    raw = os.environ.get("PLAYLIST_CONFIG", "").strip()
    if not raw:
        print("❌ Errore: variabile d'ambiente PLAYLIST_CONFIG non impostata.")
        print("   Impostala con il JSON di configurazione o aggiungila come GitHub Secret.")
        sys.exit(1)
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"❌ Errore: PLAYLIST_CONFIG non è un JSON valido — {exc}")
        sys.exit(1)

    if "base_url" not in cfg or not cfg["base_url"]:
        # base_url globale opzionale se ogni source ha il proprio
        has_per_source = all(s.get("base_url") for s in cfg.get("sources", []))
        if not has_per_source:
            print("❌ Errore: PLAYLIST_CONFIG manca 'base_url' globale e non tutte le source lo specificano")
            sys.exit(1)
    if "sources" not in cfg or not cfg["sources"]:
        print("❌ Errore: PLAYLIST_CONFIG manca 'sources'")
        sys.exit(1)

    return cfg


# ── Funzioni ────────────────────────────────────────────────────────────────


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "playlist-updater/1.0"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (URLError, HTTPError) as exc:
            if attempt < 2:
                print(f"  ⚠ Tentativo {attempt + 1} fallito: {exc}")
                continue
            raise


def _extract_group(extinf: str) -> str:
    """Estrae il group-title da una riga #EXTINF."""
    if 'group-title="' in extinf:
        parts = extinf.split('group-title="')
        if len(parts) > 1:
            return parts[1].split('"')[0]
    return ""


def _exclude_groups_only(m3u: str, exclude_groups: list) -> str:
    """Rimuove i canali in exclude_groups mantenendo tutti i formati (MPD + HLS)."""
    excluded = {g.strip().lower() for g in exclude_groups}
    lines = m3u.splitlines()
    out = []
    meta = []
    skip = False

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTM3U"):
            out.append(s)
        elif s.startswith("#EXTINF"):
            skip = False
            grp = _extract_group(s).lower()
            if grp in excluded:
                skip = True
                meta = []
                continue
            meta.append(s)
        elif s.startswith("#") and not s.startswith("#EXTM3U") and not s.startswith("#EXTINF"):
            if not skip:
                meta.append(s)
        elif s.startswith("http"):
            if skip:
                meta = []
                skip = False
                continue
            out.extend(meta)
            out.append(s)
            meta = []
            skip = False
        else:
            meta = []
            skip = False

    return "\n".join(out) + "\n" if out else ""


def _normalize_kodi_props(props: list) -> list:
    """Normalizza e riordina le righe #KODIPROP di un canale."""
    lt_line = None
    lk_line = None
    others = []

    for p in props:
        p = p.strip()
        if not p:
            continue
        if "=" not in p:
            others.append(p)
            continue

        key, val = p.split("=", 1)

        if key.endswith(".license_type"):
            if val.strip() == "clearkey":
                val = "org.w3.clearkey"
            lt_line = f"{key}={val}"
            continue

        if key.endswith(".license_key"):
            if val.startswith("{"):
                try:
                    d = json.loads(val)
                    val = ";".join(f"{k}:{v}" for k, v in d.items())
                except json.JSONDecodeError:
                    pass
            lk_line = f"{key}={val}"
            continue

        others.append(p)

    out = []
    if lt_line:
        out.append(lt_line)
    if lk_line:
        out.append(lk_line)
    out.extend(others)
    return out


def _normalize_playlist(m3u: str) -> str:
    """Riordina ogni canale in KODIPROP/EXTVLCOPT -> EXTINF -> URL.
    Converte clearkey -> org.w3.clearkey e JSON license_key in kid:key.
    Mantiene EXTVLCOPT e altre metadata non-KODIPROP (es. #EXTVLCOPT)."""
    if not m3u:
        return ""

    lines = m3u.splitlines()
    out = []
    pending_meta = []  # metadata (#KODIPROP, #EXTVLCOPT, etc.) before #EXTINF
    i = 0

    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if not s:
            continue

        if s.startswith("#EXTM3U"):
            out.append(s)
            continue

        if s.startswith("#") and not s.startswith("#EXT"):
            pending_meta.append(s)
            continue

        if s.startswith("#EXTINF"):
            extinf = s
            block_meta = list(pending_meta)
            pending_meta = []
            block_urls = []

            while i < len(lines):
                t = lines[i].strip()
                i += 1
                if not t:
                    continue
                if t.startswith("#EXTINF") or t.startswith("#EXTM3U"):
                    i -= 1
                    break
                if t.startswith("#") and not t.startswith("#EXT"):
                    block_meta.append(t)
                elif t.startswith("http"):
                    block_urls.append(t)

            kodi_lines = [m for m in block_meta if m.startswith("#KODIPROP")]
            other_meta = [m for m in block_meta if not m.startswith("#KODIPROP")]

            norm_kodi = _normalize_kodi_props(kodi_lines)

            out.extend(norm_kodi)
            out.extend(other_meta)
            out.append(extinf)
            out.extend(block_urls)
            continue

    return "\n".join(out) + "\n" if out else ""


def _count(m3u: str) -> int:
    return sum(1 for l in m3u.splitlines() if l.startswith("#EXTINF"))


def _merge_playlists(
    odir: Path, sources: list, merge_cfg: dict
) -> dict:
    """Crea una playlist unificata dalle singole playlist scaricate.

    Esclude i tag elencati in merge_cfg["exclude_tags"] e rimuove i duplicati
    in base all'URL dello stream.

    Se merge_cfg["epg_url"] è specificato, sovrascrive l'attributo url-tvg
    nell'header #EXTM3U della playlist unificata.

    Ritorna un dict con le statistiche della playlist unificata.
    """
    exclude_tags = set(merge_cfg.get("exclude_tags", []))
    output_file = merge_cfg.get("output", "UNIFICATA.m3u")
    epg_url = merge_cfg.get("epg_url")

    seen_urls = set()
    merged_lines = []
    header_added = False
    dupes = 0

    for src in sources:
        tag = src["tag"]
        if tag in exclude_tags:
            continue

        fpath = odir / src["output"]
        if not fpath.exists():
            continue

        content = fpath.read_text(encoding="utf-8")
        lines = content.splitlines()

        meta = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#EXTM3U"):
                if not header_added:
                    if epg_url:
                        # Sovrascrive o aggiunge url-tvg nell'header
                        if 'url-tvg="' in s:
                            s = re.sub(r'url-tvg="[^"]*"', f'url-tvg="{epg_url}"', s)
                        else:
                            s += f' url-tvg="{epg_url}"'
                    merged_lines.append(s)
                    header_added = True
                continue
            if s.startswith("#") and not s.startswith("#EXTM3U"):
                meta.append(s)
                continue
            if s.startswith("http"):
                if s in seen_urls:
                    dupes += 1
                    meta = []
                else:
                    seen_urls.add(s)
                    merged_lines.extend(meta)
                    merged_lines.append(s)
                    meta = []
            else:
                meta = []

    stats = {"file": output_file, "duplicates_removed": dupes}

    if merged_lines:
        final = "\n".join(merged_lines) + "\n"
        dest = odir / output_file
        old_h = _hash(dest.read_bytes()) if dest.exists() else ""
        new_h = _hash(final.encode("utf-8"))

        dest.write_text(final, encoding="utf-8")

        total = sum(1 for l in merged_lines if l.startswith("#EXTINF"))
        changed = new_h != old_h
        stats["channels"] = total
        stats["hash"] = new_h
        stats["changed"] = changed

        if changed:
            print(f"   ✅ UNIFICATA — {total} canali, {dupes} duplicati rimossi")
        else:
            print(f"   ⏭  UNIFICATA invariata — {total} canali")
    else:
        stats["channels"] = 0
        print("   ⚠  UNIFICATA — nessun canale da unire")

    return stats


# ── EPG ─────────────────────────────────────────────────────────────────────


EPG_USER_AGENT = "epg-updater/1.0"


def _fetch_epg(url: str, gzipped: bool = False, timeout: int = 120) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": EPG_USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if gzipped:
                    data = gzip.decompress(data)
                return data.decode("utf-8", errors="replace")
        except (URLError, HTTPError) as exc:
            if attempt < 2:
                print(f"  \u26a0 Tentativo {attempt + 1} fallito: {exc}")
                continue
            raise


def _parse_xmltv(raw: str) -> ET.Element:
    root = ET.fromstring(raw)
    if root.tag != "tv":
        raise ValueError("Root element non è <tv>")
    return root


def _merge_xmltv(roots: list[ET.Element]) -> ET.Element:
    merged = ET.Element("tv", attrib={"source": "merged-epg-updater"})
    seen_channels = set()
    seen_programmes = set()
    for root in roots:
        for ch in root.findall("channel"):
            cid = ch.get("id")
            if cid and cid not in seen_channels:
                merged.append(ch)
                seen_channels.add(cid)
        for prog in root.findall("programme"):
            ch = prog.get("channel", "")
            start = prog.get("start", "")
            stop = prog.get("stop", "")
            title_el = prog.find("title")
            title = title_el.text if title_el is not None else ""
            key = (ch, start, stop, title)
            if key not in seen_programmes:
                merged.append(prog)
                seen_programmes.add(key)
    return merged


def _filter_epg_by_channels(root: ET.Element, wanted_ids: set) -> ET.Element:
    filtered = ET.Element("tv", attrib=root.attrib)
    seen_ids = set()
    for ch in root.findall("channel"):
        cid = ch.get("id")
        if cid and cid in wanted_ids:
            filtered.append(ch)
            seen_ids.add(cid)
    for prog in root.findall("programme"):
        if prog.get("channel", "") in seen_ids:
            filtered.append(prog)
    return filtered


def _serialize_xmltv(root: ET.Element) -> str:
    raw = ET.tostring(root, encoding="unicode", xml_declaration=True)
    raw = raw.replace(" />", "/>")
    return raw + "\n"


def _generate_epg(odir: Path, epg_cfg: dict, wanted_ids: set) -> str | None:
    """Scarica, merge e filtra EPG. Ritorna il path del file generato o None."""
    sources = epg_cfg.get("sources", [])
    output_name = epg_cfg.get("output", "epg.xml.gz")

    if not sources:
        print("   \u26a0 Nessuna sorgente EPG configurata")
        return None

    print(f"\n{'=' * 50}")
    print(f"  EPG")
    print(f"{'=' * 50}")

    roots = []
    errors = 0

    for src in sources:
        url = src["url"]
        gz = src.get("gzip", False)
        print(f"\n\U0001f4e1 {src.get('name', url)}")
        try:
            raw = _fetch_epg(url, gzipped=gz)
            root = _parse_xmltv(raw)
            channels = len(root.findall("channel"))
            programmes = len(root.findall("programme"))
            print(f"   \u2705 {channels} canali, {programmes} programmi")
            roots.append(root)
        except Exception as exc:
            errors += 1
            print(f"   \u274c Errore: {exc}")

    if not roots:
        print("\n\u274c Nessun EPG scaricato correttamente")
        return None

    print(f"\n\U0001f517 Merge di {len(roots)} sorgenti...")
    merged = _merge_xmltv(roots)
    total_ch = len(merged.findall("channel"))
    total_pr = len(merged.findall("programme"))
    print(f"   Merge: {total_ch} canali, {total_pr} programmi")

    if wanted_ids:
        matched = set()
        epg_ids = {ch.get("id") for ch in merged.findall("channel") if ch.get("id")}
        for wid in wanted_ids:
            low = wid.lower()
            found = next((cid for cid in epg_ids if cid.lower() == low), None)
            matched.add(found or wid)
        print(f"\n\U0001f50d Filtro: {len(matched)}/{len(wanted_ids)} canali nelle playlist")
        merged = _filter_epg_by_channels(merged, matched)
        print(f"   Filtrato: {len(merged.findall('channel'))} canali, {len(merged.findall('programme'))} programmi")

    output_path = odir / output_name
    serialized = _serialize_xmltv(merged)
    data = serialized.encode("utf-8")

    is_gz = str(output_path).endswith(".gz")
    if is_gz:
        data = gzip.compress(data)

    old_data = output_path.read_bytes() if output_path.exists() else b""
    changed = data != old_data
    output_path.write_bytes(data)

    size_mb = len(data) / (1024 * 1024)
    if changed:
        print(f"\n   \u2705 EPG salvato: {output_path} ({size_mb:.1f} MB)")
    else:
        print(f"\n   \u23ed EPG invariato: {output_path} ({size_mb:.1f} MB)")

    print(f"   Errori: {errors}")
    return str(output_path)


def _update_playlist_headers(odir: Path, sources: list, epg_url: str):
    """Sovrascrive url-tvg nell'header #EXTM3U di ogni playlist."""
    if not epg_url:
        return
    for src in sources:
        fpath = odir / src["output"]
        if not fpath.exists():
            continue
        content = fpath.read_text(encoding="utf-8")
        lines = content.splitlines()
        changed = False
        for i, line in enumerate(lines):
            if line.startswith("#EXTM3U"):
                if 'url-tvg="' in line:
                    new = re.sub(r'url-tvg="[^"]*"', f'url-tvg="{epg_url}"', line)
                else:
                    new = line + f' url-tvg="{epg_url}"'
                if new != line:
                    lines[i] = new
                    changed = True
                break
        if changed:
            fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"   \u2705 Header EPG aggiornato: {src['output']}")


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="Playlist + EPG Updater")
    ap.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR", "playlists"))
    ap.add_argument("--skip-epg", action="store_true", help="Salta la generazione EPG")
    args = ap.parse_args()

    cfg = _load_config()
    base_url = cfg["base_url"].rstrip("/")
    sources = cfg["sources"]

    odir = Path(args.output_dir)
    odir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"{'=' * 50}")
    print(f"  Playlist + EPG Updater — {now}")
    print(f"  Output : {odir.resolve()}")
    print(f"  Sources: {len(sources)}")
    print(f"{'=' * 50}\n")

    manifest = {"updated_at": now, "playlists": {}}
    updated = unchanged = errors = 0

    for src in sources:
        tag = src["tag"]
        fname = src["output"]
        remote = src["remote"]
        src_base = src.get("base_url", base_url).rstrip("/")
        url = f"{src_base}/{remote}"
        exclude_groups = src.get("exclude_groups")

        print(f"📡 {tag}")

        try:
            raw = _fetch(url).decode("utf-8", errors="replace")
            total = _count(raw)

            final = _exclude_groups_only(raw, exclude_groups) if exclude_groups else raw
            final = _normalize_playlist(final)
            final_n = _count(final)

            dest = odir / fname
            old_h = _hash(dest.read_bytes()) if dest.exists() else ""
            new_h = _hash(final.encode("utf-8"))

            dest.write_text(final, encoding="utf-8")

            changed = new_h != old_h
            if changed:
                updated += 1
                print(f"   ✅ AGGIORNATO — {total} totali, {final_n} salvati")
            else:
                unchanged += 1
                print(f"   ⏭  Invariato — {final_n} canali")

            manifest["playlists"][tag] = {
                "file": fname,
                "total_channels": total,
                "saved_channels": final_n,
                "hash": new_h,
                "changed": changed,
            }

        except Exception as exc:
            errors += 1
            print(f"   ❌ Errore: {exc}")
            manifest["playlists"][tag] = {"file": fname, "error": str(exc)}

    # Unificazione playlist
    merge_cfg = cfg.get("merge")
    if merge_cfg:
        print(f"\n🔗 Unificazione playlist...")
        merge_stats = _merge_playlists(odir, sources, merge_cfg)
        manifest["merge"] = merge_stats

    # EPG — generazione
    epg_cfg = cfg.get("epg")
    if epg_cfg and not args.skip_epg:
        wanted = set()
        for f in odir.glob("*.m3u"):
            for m in re.finditer(r'tvg-id="([^"]*)"', f.read_text(encoding="utf-8")):
                if m.group(1):
                    wanted.add(m.group(1))
        epg_path = _generate_epg(odir, epg_cfg, wanted)
        if epg_path:
            manifest["epg"] = {"file": Path(epg_path).name}

    # EPG — aggiornamento header di tutte le playlist
    epg_url = None
    if epg_cfg and epg_cfg.get("url"):
        epg_url = epg_cfg["url"]
    elif cfg.get("merge") and cfg["merge"].get("epg_url"):
        epg_url = cfg["merge"]["epg_url"]
    if epg_url:
        _update_playlist_headers(odir, sources, epg_url)

    # Manifest
    (odir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{'=' * 50}")
    print(f"  ✅ Aggiornati : {updated}")
    print(f"  ⏭  Invariati : {unchanged}")
    print(f"  ❌ Errori    : {errors}")
    print(f"{'=' * 50}")

    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"updated={updated}\n")
            f.write(f"errors={errors}\n")
            f.write(f"has_changes={'true' if updated > 0 else 'false'}\n")

    sys.exit(1 if errors == len(sources) else 0)


if __name__ == "__main__":
    main()
