"""Render docs/two_pager.html to PDF with headless Brave/Chrome.  Usage: python tools/render_pdf.py [out.pdf]"""
import base64
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cdp import Browser

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "SomaiyaSat_Project_Summary.pdf")
b = Browser(width=1000, height=1400, scale=1, profile=os.environ.get("CDP_PROFILE", "/tmp/somaiyasat-cdp-profile"))
try:
    b.goto((ROOT / "docs" / "two_pager.html").as_uri())
    b.wait("document.fonts.status === 'loaded'", timeout=20)
    time.sleep(0.8)
    over = b.js("[...document.querySelectorAll('.page')].map(p => { const f = p.querySelector('footer').getBoundingClientRect().top; let m = 0; for (const e of p.children) if (e.tagName !== 'FOOTER') m = Math.max(m, e.getBoundingClientRect().bottom); return Math.round(f - m); }).join(',')")
    print("free px above footer per page:", over)
    r = b.send("Page.printToPDF", printBackground=True, preferCSSPageSize=True, marginTop=0, marginBottom=0, marginLeft=0, marginRight=0)
    out.write_bytes(base64.b64decode(r["data"]))
    print("wrote", out)
finally:
    b.close()
