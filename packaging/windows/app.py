"""Windows native shell with an embedded Python runtime (no frozen helper paths)."""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    import webview
    from codex_switcher.webui import server
    from codex_switcher.cli import _ensure_bridge
    # pythonw has no console; never log the local UI access token.
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')
    splash = (Path(__file__).parent / 'loading.html').read_text(encoding='utf-8')
    window = webview.create_window('ChatGPT Model Switcher', html=splash,
                                   width=1180, height=840, min_size=(880, 600))

    def launch():
        try:
            _ensure_bridge()
            url = server.existing_url()
            if not url:
                threading.Thread(target=server.run,
                                 kwargs={'open_browser': False}, daemon=True).start()
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    url = server.existing_url()
                    if url:
                        break
                    time.sleep(0.2)
            if not url:
                raise RuntimeError('Local UI did not start')
            window.load_url(url)
        except Exception:
            window.evaluate_js("document.getElementById('status').textContent='启动失败，请关闭后重试。 / Startup failed. Close and retry.'")
    webview.start(launch, gui='edgechromium')


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        from codex_switcher import configfile, secrets
        from codex_switcher.webui import server
        import webview
        assert server.STATIC_DIR.joinpath('index.html').is_file()
        assert configfile.toml_available()
        print('Windows runtime imports and static assets OK')
    else:
        try:
            main()
        except Exception:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                'Could not start. Install Microsoft Edge WebView2 Runtime and try again.\n'
                '无法启动，请安装 Microsoft Edge WebView2 Runtime 后重试。',
                'ChatGPT Model Switcher', 0x10)
            raise SystemExit(1)
