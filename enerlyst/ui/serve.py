"""
enerlyst UI server — serves external.html and internal.html on port 8002.
Injects ENERLYST_API_TOKEN from environment at serve time.
Usage: python -X utf8 enerlyst/ui/serve.py
"""
import os
import sys
from pathlib import Path
from flask import Flask, send_from_directory, make_response

# In a PyInstaller-frozen build, __file__ points inside sys._MEIPASS where
# external.html does not exist. Honour ENERLYTIK_ENERLYST_UI_DIR (the Tauri
# shell sets this to the bundled UI resource path); fall back to the source
# dir when running from disk.
_env_dir = os.environ.get("ENERLYTIK_ENERLYST_UI_DIR")
if _env_dir and Path(_env_dir).is_dir():
    UI_DIR = Path(_env_dir).resolve()
elif getattr(sys, "frozen", False):
    # Tauri install layouts: probe Windows resources/, macOS Contents/Resources/,
    # then PyInstaller _MEIPASS as the last resort.
    _exe_dir = Path(sys.executable).parent.resolve()
    _candidates = [
        _exe_dir / "resources" / "enerlyst" / "ui",
        _exe_dir.parent / "Resources" / "enerlyst" / "ui",
        Path(getattr(sys, "_MEIPASS", "")) / "enerlyst" / "ui",
    ]
    UI_DIR = next((p for p in _candidates if p.is_dir()), _candidates[-1]).resolve()
else:
    UI_DIR = Path(__file__).parent.resolve()
PORT = int(os.getenv("ENERLYST_UI_PORT") or os.getenv("ENERLYST_PORT", "8002"))
API_TOKEN = os.getenv("ENERLYST_API_TOKEN") or os.getenv("ENERLYTIK_API_TOKEN", "")

app = Flask(__name__, static_folder=str(UI_DIR))


@app.route("/health")
def health():
    return {"status": "ok", "service": "enerlyst-ui"}, 200


@app.route("/")
@app.route("/external")
@app.route("/chat")
def serve_external():
    html_path = UI_DIR / "external.html"
    content = html_path.read_text(encoding="utf-8")
    # Inject token so the UI picks it up via window.ENERLYST_TOKEN
    token_script = f'<script>window.ENERLYST_TOKEN="{API_TOKEN}";</script>'
    content = content.replace("</head>", token_script + "\n</head>", 1)
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/internal")
def serve_internal():
    return send_from_directory(str(UI_DIR), "internal.html")


@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(UI_DIR), filename)


if __name__ == "__main__":
    print(f"enerlyst UI: http://localhost:{PORT}/external (chat)")
    print(f"enerlyst UI: http://localhost:{PORT}/internal (cockpit)")
    if API_TOKEN:
        print(f"Token: injected from ENERLYST_API_TOKEN env var")
    else:
        print(f"Warning: ENERLYST_API_TOKEN not set — auth may fail")
    app.run(host="0.0.0.0", port=PORT, debug=False)
