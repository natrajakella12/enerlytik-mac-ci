# enerlytik — Mac DMG build guide

Target: **macOS Sonoma (14.x) on Intel x86_64** (e.g. MacBook Air 13" 2020,
Core i3, 8 GB).

The Mac DMG is built by a GitHub Actions workflow on Intel macOS-13 runners.
You don't need a Mac to trigger the build, but you do need a Mac to install
and run the resulting DMG.

---

## How to trigger the build

### Option A — push a tag (automatic)

```bash
git tag tauri-mac-ci-ready
git push origin tauri-mac-ci-ready
```

The workflow watches for tags matching `release-*` and `hotfix-*` and runs
automatically. To use a different tag prefix, edit
`.github/workflows/tauri-mac.yml` `on.push.tags`.

### Option B — manual run from the GitHub UI

1. Open https://github.com/natrajakella12/OEM-DEMO-MAC
2. Click **Actions** → **tauri-mac-build** → **Run workflow**
3. Choose the ref (default: `hotfix-001-path-fixes`) → **Run**

### Option C — manual run from CLI

```bash
gh workflow run tauri-mac-build --ref hotfix-001-path-fixes
gh run watch
```

---

## What the workflow does (top to bottom)

1. **Checkout** the chosen ref
2. **Install Python 3.12** + the runtime deps the 4 sidecars need
   (chromadb, torch, sentence-transformers, fastapi, flask, etc.)
3. **Install Rust** + the `x86_64-apple-darwin` target
4. **Cache** Cargo + pip so re-runs after the first build are much faster
5. **Stage resources** — runs `prepare_resources.py` to copy the demo DB,
   ChromaDB, and HTML into `src-tauri/resources/`
6. **Build 4 PyInstaller sidecars** (db_api, passport, enerlyst, rag_api)
   into `dist/` using the `*-x86_64-apple-darwin.spec` files at the repo root
7. **Move sidecars** into `src-tauri/sidecars/` and `chmod +x`
8. **Cargo Tauri build** — `cargo tauri build --target x86_64-apple-darwin
   --bundles dmg`
9. **Upload artifacts**: `enerlytik-mac-intel-dmg` (the DMG) +
   `enerlytik-mac-intel-app` (the .app zipped)

Expected total time on a cold cache: **60–90 minutes** (rag_api PyInstaller
is the long pole, ~25–45 min). Re-runs with cache hit: ~15–25 min.

---

## After the workflow completes

1. Open the workflow run page → **Artifacts** section
2. Download `enerlytik-mac-intel-dmg.zip` (~600 MB)
3. Unzip → you'll get `enerlytik_3.0.0_x64.dmg`
4. Copy to `D:\Release 3\04_TAURI_MAC\` (Step 9)
5. Transfer to the target Mac (USB / network / iCloud)

---

## Installing on the Mac (one-time)

Because this DMG is **unsigned** (no Apple Developer cert), Sonoma's
Gatekeeper will warn on first launch.

```
1. Double-click the .dmg → drag enerlytik.app to /Applications
2. First launch: right-click on enerlytik.app → Open
3. Sonoma will say "macOS cannot verify the developer" → click Open
4. From the second launch onward, double-click works normally
```

If Gatekeeper hard-blocks (rare on Sonoma 14.x for self-signed apps):

```bash
xattr -dr com.apple.quarantine /Applications/enerlytik.app
```

---

## What's inside the .app

```
enerlytik.app/
├─ Contents/
│  ├─ MacOS/
│  │  ├─ enerlytik-app         ← Tauri shell (Rust)
│  │  ├─ db_api-x86_64-apple-darwin
│  │  ├─ passport-x86_64-apple-darwin
│  │  ├─ rag_api-x86_64-apple-darwin
│  │  └─ enerlyst-x86_64-apple-darwin
│  ├─ Resources/
│  │  ├─ resources/
│  │  │  ├─ data/
│  │  │  │  ├─ enerlytik_tauri.db
│  │  │  │  └─ RAG/kb/         ← ChromaDB persistent store
│  │  │  ├─ passport_v2/...
│  │  │  └─ enerlyst/ui/...
│  │  └─ icon.icns
│  └─ Info.plist
```

Both Python (`passport_server.py`, `config_rag.py`, `serve.py`) and the
Rust shell auto-detect this layout via the same env-var pattern that ships
the Windows MSI. No additional config needed.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| App quits immediately on first launch | First-run PyInstaller extraction times out (rag_api is 400+ MB) | Wait 60–90 sec; subsequent launches reuse the cache. The splashscreen covers this. |
| "Cannot be opened — developer cannot be verified" | Gatekeeper, expected for unsigned DMG | Right-click → Open the first time |
| All sidecars start but `/enerlyst/health` shows `collections: 0` | ChromaDB path resolution missed | Check `Console.app` for `[enerlytik]` log lines emitted by the Rust shell — they print the resolved `kb_path`. The path should end in `Resources/.../RAG/kb`. |
| No Groq response | Groq keys not embedded | Confirm `main.rs` has the 3 `std::env::set_var("GROQ_KEY_*", ...)` lines. The Mac build uses the same `main.rs` as Windows. |

---

## Required GitHub secrets (optional)

The workflow declares these as env vars but does **not** strictly need them
because the Groq keys are embedded directly in `main.rs`. They're declared
for future-proofing (in case keys are ever externalised).

| Secret | Purpose |
|---|---|
| `GROQ_KEY_1`, `GROQ_KEY_2`, `GROQ_KEY_3` | Round-robin Groq keys, optional |

If you want to add them: GitHub repo → Settings → Secrets and variables →
Actions → New repository secret.

---

## Limitations

1. **Intel only** for now. Apple Silicon (M1/M2/M3) Macs would need
   `aarch64-apple-darwin` target and a separate run on `macos-14`. Easy to
   add a second job in the same workflow when needed.
2. **Unsigned**. Notarisation requires Apple Developer Program ($99/yr) +
   secrets for the cert + 2FA app password. Add later if external
   distribution is required.
3. **No code-sign on the embedded Python sidecars.** macOS Gatekeeper may
   warn the first time each sidecar runs. The Tauri shell launches them
   with `chmod +x` already applied, so they work.
