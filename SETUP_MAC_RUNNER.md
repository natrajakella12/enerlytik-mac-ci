# Self-hosted Mac runner — setup guide

Use this for the **MacBook Air 13" 2020 (Intel Core i3, Sonoma 14.x)**.
Total time: ~20 min setup + first build ~2-3.5 hours unattended.

---

## On the MacBook Air

### 1. Clone this repo (once)

```bash
mkdir -p ~/work && cd ~/work
git clone https://github.com/natrajakella12/enerlytik-mac-ci.git
cd enerlytik-mac-ci
```

If git asks for credentials, use a Personal Access Token with `repo` scope
(generate at https://github.com/settings/tokens?type=beta).

### 2. Install build prerequisites (one-shot)

```bash
chmod +x setup_mac_runner.sh
./setup_mac_runner.sh
```

Installs (idempotently): Xcode CLT, Homebrew, Python 3.12, Rust + the
`x86_64-apple-darwin` target, Tauri CLI v2. ~15-20 min the first time.

### 3. Register the GitHub Actions runner

Open in browser:
**https://github.com/natrajakella12/enerlytik-mac-ci/settings/actions/runners**

Click **"New self-hosted runner"** → **macOS** → **x64**.
GitHub gives you a 4-command snippet — copy-paste it into Terminal on
the Mac. The snippet's `--token` value is one-time and expires in 1 hour.

The last command is:
```bash
./run.sh
```

Leave that Terminal window open. While `./run.sh` is running, the Mac
shows up in GitHub as an available runner. Press **Ctrl+C** to stop.

### 4. (Optional) Run the daemon as a service so it auto-starts

```bash
cd ~/work/actions-runner
sudo ./svc.sh install
sudo ./svc.sh start
```

Stop it any time with `sudo ./svc.sh stop`. Uninstall with
`sudo ./svc.sh uninstall`.

---

## On GitHub (one-time, in browser)

### 5. Add Groq API keys as repo secrets

**https://github.com/natrajakella12/enerlytik-mac-ci/settings/secrets/actions**
→ **New repository secret** for each:

| Secret name | Value |
|---|---|
| `GROQ_KEY_1` | the first Groq key from `enerlyst/.env` |
| `GROQ_KEY_2` | the second |
| `GROQ_KEY_3` | the third |

Without these, the build still succeeds but the runtime DMG reports
`groq_keys: 0` and AI responses fall back to "AI offline".

### 6. Make `dev-v2.0` the default branch (so the workflow shows up)

**https://github.com/natrajakella12/enerlytik-mac-ci/settings/branches**
→ **Default branch** → switch from `main` to `dev-v2.0` → **Update**.

(Alternative: push `dev-v2.0` to `main` from your Windows machine.)

---

## Trigger the build

**https://github.com/natrajakella12/enerlytik-mac-ci/actions** → click
**tauri-mac-build** in the left sidebar → **Run workflow** → branch
`dev-v2.0` → **Run workflow** button.

The job picks up on your Mac within ~30 seconds and starts building.
Watch progress in the run page; logs stream live.

---

## Realistic build timing on the Air

| Phase | First build (cold) | Subsequent builds (cached) |
|---|---|---|
| Stage resources + sidecar prep | 2 min | 1 min |
| db_api PyInstaller | 5-8 min | 2 min |
| passport PyInstaller | 3-5 min | 1 min |
| enerlyst PyInstaller | 2-3 min | 1 min |
| **rag_api PyInstaller** (chromadb + torch) | **45-90 min** | 5-10 min |
| Cargo Tauri compile | 30-45 min | 1 min |
| WiX/DMG bundle | 10 min | 5 min |
| **Total** | **~2-3.5 hours** | **~30-45 min** |

You can close the laptop lid (System Settings → Battery → "Prevent
sleep on power adapter") and come back later. The runner uses moderate
CPU during builds and 0% when idle.

---

## Get the DMG

When the build completes:
1. Workflow run page → **Artifacts** section at the bottom
2. Download `enerlytik-mac-intel-dmg.zip` (~600 MB)
3. Unzip → `enerlytik_3.0.0_x64.dmg`
4. Double-click → drag enerlytik.app to Applications
5. First launch: **right-click → Open** (Sonoma Gatekeeper warns
   because the DMG is unsigned; from the second launch it works
   normally)

If Gatekeeper hard-blocks:
```bash
xattr -dr com.apple.quarantine /Applications/enerlytik.app
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Waiting for a runner" forever | runner offline | Restart `./run.sh` on the Mac |
| Build fails at "PyInstaller not found" | Python deps not installed | The workflow installs them via pip; check the runner's Python is on PATH |
| Build fails at "rustc not found" | rustup not sourced in the runner shell | Edit `~/work/actions-runner/.env` and add `source $HOME/.cargo/env` (or restart the runner from a fresh shell after `setup_mac_runner.sh`) |
| `actions/setup-python@v5` downloads Python every run | self-hosted runners don't cache GitHub-hosted toolchains | Set `python-version: '3.12'` to use the system Python (already installed by setup script). Workflow already configured for this. |
| App quits immediately on first launch | rag_api's 400 MB onefile binary is extracting to /tmp | Wait 60-90 sec; subsequent launches reuse the cache |

---

## Stopping the runner safely

If you need to take the Mac offline:
```bash
# In the Terminal running ./run.sh
Ctrl+C    # graceful stop, finishes any current job

# If installed as a service:
sudo ./svc.sh stop
```

GitHub will queue any incoming jobs until you bring it back up.
