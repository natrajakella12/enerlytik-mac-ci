"""
Stage Tauri-bundled resources before `cargo tauri build`.

Copies the demo DB, ChromaDB knowledge base, and HTML/asset directories
into src-tauri/resources/ so they are picked up by tauri.conf.json's
`resources` glob. Run this every time before a cargo tauri build.

After copy, scrubs any literal dev API token strings from bundled HTML/JS
files (replaces with the `{{ api_token }}` placeholder that
passport_server's renderer substitutes at request time). Source files
under the project tree are NOT modified — only the bundled copies.

Usage:
    cd tauri_bundle/enerlytik-app
    python -X utf8 prepare_resources.py
"""
import shutil
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent              # tauri_bundle/enerlytik-app
TAURI_BUNDLE = THIS.parent                          # tauri_bundle
PROJECT_ROOT = TAURI_BUNDLE.parent                  # Ev__ML
RES = THIS / "src-tauri" / "resources"

# Token strings that must never reach the bundled artifact. Each is replaced
# with the `{{ api_token }}` placeholder so passport_server's renderer
# substitutes the runtime-generated token at request time.
LITERAL_TOKENS = [
    "{{ api_token }}",  # dev fallback — compromised in handover docs
]

SCRUB_SUFFIXES = {".html", ".js", ".json", ".css"}

# Each entry: (source path, destination relative to RES)
TARGETS = [
    (TAURI_BUNDLE / "data" / "enerlytik_tauri.db",  RES / "data" / "enerlytik_tauri.db"),
    (TAURI_BUNDLE / "data" / "RAG",                  RES / "data" / "RAG"),
    (PROJECT_ROOT / "passport_v2",                   RES / "passport_v2"),
    (PROJECT_ROOT / "enerlyst" / "ui",               RES / "enerlyst" / "ui"),
    # oem_v3.html lives at the repo root in dev; bundle it next to passport_v2
    # so the passport sidecar can serve it via ENERLYTIK_OEM_V3_HTML.
    (PROJECT_ROOT / "oem_v3.html",                   RES / "passport_v2" / "oem_v3.html"),
]


def copy_file(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.stat().st_size


def copy_dir(src: Path, dst: Path) -> int:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    total = 0
    for p in dst.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def scrub_tokens(root: Path) -> int:
    """Replace literal dev tokens with the `{{ api_token }}` placeholder.
    Returns the number of files modified.
    """
    modified = 0
    for fp in root.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in SCRUB_SUFFIXES:
            continue
        try:
            txt = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = txt
        for tok in LITERAL_TOKENS:
            new = new.replace(tok, "{{ api_token }}")
        if new != txt:
            fp.write_text(new, encoding="utf-8")
            modified += 1
            print(f"    scrubbed: {fp.relative_to(root)}")
    return modified


def main() -> int:
    if RES.exists():
        shutil.rmtree(RES)
    RES.mkdir(parents=True)

    total_bytes = 0
    print(f"Staging into {RES}\n")
    for src, dst in TARGETS:
        if not src.exists():
            print(f"  MISSING source: {src}")
            return 1
        if src.is_file():
            sz = copy_file(src, dst)
        else:
            sz = copy_dir(src, dst)
        total_bytes += sz
        rel = dst.relative_to(RES)
        print(f"  {sz/1024/1024:>8.1f} MB  {rel}")

    print(f"\nScrubbing literal API tokens from bundled HTML/JS:")
    n_scrubbed = scrub_tokens(RES)
    print(f"  {n_scrubbed} file(s) scrubbed")

    print(f"\nTotal staged: {total_bytes/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
