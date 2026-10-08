"""Drive the dashboard in headless Brave and capture screenshots (used for the report)."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cdp import Browser

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent.parent / "screenshots")
OUT.mkdir(parents=True, exist_ok=True)


def main():
    b = Browser(width=1512, height=920, scale=1, profile=os.environ.get('CDP_PROFILE', '/tmp/somaiyasat-cdp-profile'))
    try:
        b.goto(URL)
        b.wait("!!window.__gs", timeout=30)
        time.sleep(2.5)
        b.shot(OUT / "01_live.png")
        b.js("(()=>{const ps=[...document.querySelectorAll('#passes li')];document.querySelector('[data-speed=\"60\"]').click();window.__gs.playPass(1);})()")
        b.wait("window.__gs.lastSlot >= 22", timeout=60)
        b.js("document.getElementById('pause').click()")
        time.sleep(0.6)
        b.shot(OUT / "02_pass_mid.png")
        b.js("document.getElementById('f-fault').click()")
        time.sleep(0.5)
        b.js("document.getElementById('pause').click()")
        b.wait("window.__gs.lastSlot >= 26", timeout=60)
        b.js("document.getElementById('pause').click()")
        time.sleep(0.4)
        b.shot(OUT / "03_watchdog.png")
        b.js("document.getElementById('pause').click()")
        b.wait("window.__gs.cur && window.__gs.cur.done", timeout=60)
        time.sleep(0.5)
        b.shot(OUT / "04_pass_done.png")
        b.js("document.querySelector('[data-view=real]').click()")
        time.sleep(0.4)
        b.shot(OUT / "05_real.png")
        b.js("document.querySelector('[data-view=model]').click()")
        time.sleep(0.4)
        b.shot(OUT / "06_model.png")
        print("errors:", b.js("JSON.stringify(window.__errs||[])"))
    finally:
        b.close()


if __name__ == "__main__":
    main()
