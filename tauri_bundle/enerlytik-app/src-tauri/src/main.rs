#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use rand::Rng;
use tauri::{AppHandle, Manager, State, WindowEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

struct SidecarChildren(Mutex<Vec<CommandChild>>);

fn random_hex_token() -> String {
    let mut rng = rand::thread_rng();
    (0..32)
        .map(|_| format!("{:x}", rng.gen_range(0u8..16)))
        .collect()
}

fn wait_for_port(port: u16, timeout_secs: u64) -> bool {
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);
    let addr: std::net::SocketAddr = format!("127.0.0.1:{}", port)
        .parse()
        .expect("valid socket addr");
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok() {
            return true;
        }
        thread::sleep(Duration::from_millis(400));
    }
    false
}

fn startup(app: AppHandle) {
    // Resource directory resolved at runtime. On Windows MSI install this
    // is the executable's parent directory (e.g. C:\Program Files\enerlytik\)
    // and Tauri places bundled resources inside a "resources" subfolder.
    let resource_dir = match app.path().resource_dir() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[enerlytik] resource_dir error: {}", e);
            return;
        }
    };

    // Tauri preserves the glob path under resource_dir: on Windows MSI we
    // see <install>/resources/..., on macOS the .app sometimes flattens to
    // <App>.app/Contents/Resources/... directly. Probe to pick the right
    // base so the same env vars work on both.
    let res_root = {
        let nested = resource_dir.join("resources");
        if nested.join("data").join("enerlytik_tauri.db").is_file() {
            nested
        } else {
            resource_dir.clone()
        }
    };
    let db_path = res_root.join("data").join("enerlytik_tauri.db");
    let passport_dir = res_root.join("passport_v2");
    let passport_html = passport_dir.join("enerlytik_v4.html");
    // oem_v3.html is staged into passport_v2 by prepare_resources.py so the
    // sidecar finds it via the shared resolver.
    let oem_v3_html = passport_dir.join("oem_v3.html");
    let enerlyst_ui_dir = res_root.join("enerlyst").join("ui");
    let rag_dir = res_root.join("data").join("RAG");
    // ChromaDB persistent store lives at <rag_dir>/kb/.
    let kb_path = rag_dir.join("kb");
    let output_dir = resource_dir.clone();

    // Generate fresh ENERLYTIK_API_TOKEN per launch — never bundled, never logged.
    let token = random_hex_token();

    eprintln!("[enerlytik] resource_dir = {:?}", resource_dir);
    eprintln!("[enerlytik] res_root     = {:?}", res_root);
    eprintln!("[enerlytik] db_path      = {:?}", db_path);
    eprintln!("[enerlytik] passport_html= {:?}", passport_html);
    eprintln!("[enerlytik] kb_path      = {:?}", kb_path);
    eprintln!("[enerlytik] token length = {}", token.len());

    // Process-level env so child sidecars inherit.
    std::env::set_var("ENERLYTIK_DB_PATH", db_path.as_os_str());
    std::env::set_var("ENERLYTIK_DEMO_MODE", "true");
    std::env::set_var("ENERLYTIK_API_TOKEN", &token);
    std::env::set_var("RAG_API_TOKEN", &token);
    std::env::set_var("ENERLYTIK_PASSPORT_DIR", passport_dir.as_os_str());
    std::env::set_var("ENERLYTIK_PASSPORT_HTML", passport_html.as_os_str());
    std::env::set_var("ENERLYTIK_OEM_V3_HTML", oem_v3_html.as_os_str());
    std::env::set_var("ENERLYTIK_ENERLYST_UI_DIR", enerlyst_ui_dir.as_os_str());
    std::env::set_var("ENERLYTIK_RAG_DIR", rag_dir.as_os_str());
    std::env::set_var("ENERLYTIK_KB_PATH", kb_path.as_os_str());
    std::env::set_var("ENERLYTIK_OUTPUT_DIR", output_dir.as_os_str());
    std::env::set_var("DB_API_PORT", "3001");
    std::env::set_var("RAG_API_PORT", "8001");
    std::env::set_var("WEB_PORT", "5001");
    std::env::set_var("ENERLYST_PORT", "8002");

    // Groq keys are NEVER hardcoded. They flow in via the parent process'
    // environment — the CI workflow injects them from repo secrets, and a
    // local installer can set GROQ_KEY_1..3 in the user's OS env before
    // launching enerlytik.exe. If absent, /enerlyst/health reports
    // groq_keys=0 and LLM responses fall back to "AI offline" — see
    // enerlyst/.env.example for the contract.
    for key in ["GROQ_KEY_1", "GROQ_KEY_2", "GROQ_KEY_3", "GROQ_API_KEY"] {
        if std::env::var(key).is_err() {
            eprintln!("[enerlytik] {} not set — LLM features disabled", key);
        }
    }

    let sidecars = ["db_api", "rag_api", "passport", "enerlyst"];
    let mut children: Vec<CommandChild> = Vec::new();

    for name in &sidecars {
        match app.shell().sidecar(*name) {
            Ok(cmd) => match cmd.spawn() {
                Ok((mut rx, child)) => {
                    eprintln!("[enerlytik] spawned {}", name);
                    // Drain the event channel so the sidecar's stdout/stderr pipe
                    // does not fill and block.
                    tauri::async_runtime::spawn(async move {
                        while let Some(_event) = rx.recv().await {}
                    });
                    children.push(child);
                }
                Err(e) => eprintln!("[enerlytik] spawn error {}: {}", name, e),
            },
            Err(e) => eprintln!("[enerlytik] sidecar lookup error {}: {}", name, e),
        }
    }

    {
        let state: State<SidecarChildren> = app.state();
        let mut lock = state.0.lock().expect("children lock poisoned");
        *lock = children;
    }

    // Wait for each sidecar's port. Servers come up in parallel; the per-port
    // budget is generous to absorb cold-start overhead on slow disks.
    let ready_db = wait_for_port(3001, 25);
    let ready_rag = wait_for_port(8001, 25);
    let ready_pass = wait_for_port(5001, 25);
    let ready_eyst = wait_for_port(8002, 15);
    eprintln!(
        "[enerlytik] ports ready: db={} rag={} pass={} enerlyst={}",
        ready_db, ready_rag, ready_pass, ready_eyst
    );

    // Navigate the (still-hidden) main window to the platform, then reveal.
    if let Some(main_win) = app.get_webview_window("main") {
        let _ = main_win.eval(
            "window.location.replace('http://localhost:5001/enerlytik-v4');",
        );
        let _ = main_win.show();
        let _ = main_win.set_focus();
    }
    if let Some(splash) = app.get_webview_window("splashscreen") {
        let _ = splash.close();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(SidecarChildren(Mutex::new(Vec::new())))
        .setup(|app| {
            let handle = app.handle().clone();
            // Run startup off the main thread so the splashscreen renders
            // immediately while sidecars boot.
            thread::spawn(move || {
                startup(handle);
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { .. } = event {
                if window.label() == "main" {
                    let app = window.app_handle();
                    // Drain the children list inside a tight scope so the
                    // MutexGuard drops before `State` does. Then kill outside
                    // the lock to avoid holding it across slow OS calls.
                    let to_kill: Vec<CommandChild> = {
                        let state: State<SidecarChildren> = app.state();
                        let mut guard = match state.0.lock() {
                            Ok(g) => g,
                            Err(p) => p.into_inner(),
                        };
                        guard.drain(..).collect()
                    };
                    for child in to_kill {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running enerlytik");
}
