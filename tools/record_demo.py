"""Record a narrated walkthrough of the dashboard as an MP4.

Drives the real page in a headless browser with genuine mouse events (every
control is clicked, the globe is dragged and zoomed), captures the browser's
own screencast frames with their timestamps, and encodes them with ffmpeg.
The on-screen pointer and the caption strip are added by this script for the
recording only; they are not part of the application.

Usage:  python tools/record_demo.py [url] [out.mp4]
"""
import base64
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cdp import Browser

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent.parent / "demo" / "SomaiyaSat_demo.mp4")
FRAMES = Path(os.environ.get("FRAMES_DIR", "/tmp/somaiyasat-frames"))
W, H = 1600, 900

OVERLAY = r"""
(() => {
  const c = document.createElement('div');
  c.id = '__cur';
  c.style.cssText = 'position:fixed;z-index:99999;left:0;top:0;width:22px;height:22px;pointer-events:none;transform:translate(800px,450px)';
  c.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 2l15 8-6.5 1.8L9 18z" fill="#fff" stroke="#06080b" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  const ring = document.createElement('div');
  ring.id = '__ring';
  ring.style.cssText = 'position:fixed;z-index:99998;left:0;top:0;width:34px;height:34px;margin:-17px 0 0 -17px;border:2px solid #8fd3ff;border-radius:50%;pointer-events:none;opacity:0';
  const cap = document.createElement('div');
  cap.id = '__cap';
  cap.style.cssText = 'position:fixed;z-index:99997;left:50%;top:70px;transform:translateX(-50%);max-width:640px;padding:10px 16px;background:rgba(6,8,11,.92);border:1px solid #2c3742;border-left:3px solid #8fd3ff;border-radius:5px;color:#d7dde4;font:500 15px/1.4 Barlow,system-ui,sans-serif;text-align:left;pointer-events:none;display:none';
  document.body.append(c, ring, cap);
  addEventListener('mousemove', (e) => { c.style.transform = `translate(${e.clientX}px,${e.clientY}px)`; }, true);
  addEventListener('mousedown', (e) => {
    ring.style.transition = 'none'; ring.style.transform = `translate(${e.clientX}px,${e.clientY}px) scale(.4)`; ring.style.opacity = '1';
    requestAnimationFrame(() => { ring.style.transition = 'transform .45s ease-out, opacity .45s ease-out'; ring.style.transform = `translate(${e.clientX}px,${e.clientY}px) scale(1.2)`; ring.style.opacity = '0'; });
  }, true);
  window.__caption = (t) => { cap.textContent = t; cap.style.display = t ? 'block' : 'none'; };
})()
"""


class Recorder(Browser):
    frames = []          # (timestamp, path)
    recording = False
    mouse = (800, 450)

    def _handle(self, msg):
        if msg.get("method") == "Page.screencastFrame":
            p = msg["params"]
            if self.recording:
                path = FRAMES / f"f{len(self.frames):06d}.jpg"
                path.write_bytes(base64.b64decode(p["data"]))
                self.frames.append((p["metadata"]["timestamp"], path))
            self.msg += 1
            self.ws.send(json.dumps({"id": self.msg, "method": "Page.screencastFrameAck",
                                     "params": {"sessionId": p["sessionId"]}}))

    def send(self, method, **params):
        self.msg += 1
        mid = self.msg
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._handle(msg)

    def pump(self, seconds):
        """Let time pass while still collecting frames."""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            if self.ws.buf or select.select([self.ws.sock], [], [], min(left, 0.05))[0]:
                self._handle(json.loads(self.ws.recv()))

    # ---- pointer
    def move(self, x, y, dur=0.55):
        x0, y0 = self.mouse
        steps = max(6, int(dur * 40))
        for i in range(1, steps + 1):
            f = i / steps
            e = f * f * (3 - 2 * f)
            self.send("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x - x0) * e, y=y0 + (y - y0) * e,
                      buttons=self._buttons)
            self.pump(dur / steps)
        self.mouse = (x, y)

    _buttons = 0

    def press(self):
        self._buttons = 1
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=self.mouse[0], y=self.mouse[1], button="left", buttons=1, clickCount=1)

    def release(self):
        self._buttons = 0
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=self.mouse[0], y=self.mouse[1], button="left", buttons=0, clickCount=1)

    def rect(self, sel):
        r = self.js(f"(()=>{{const e=document.querySelector({sel!r}); e.scrollIntoView({{block:'nearest'}}); const r=e.getBoundingClientRect(); return [r.left,r.top,r.width,r.height]}})()")
        if not r:
            raise RuntimeError(f"no element for {sel}")
        return r

    def click(self, sel, fx=0.5, fy=0.5, settle=0.5):
        l, t, w, h = self.rect(sel)
        self.move(l + w * fx, t + h * fy)
        self.pump(0.15)
        self.press()
        self.pump(0.07)
        self.release()
        self.pump(settle)

    def slide(self, sel, value):
        lo, hi = self.js(f"(()=>{{const e=document.querySelector({sel!r}); return [+e.min,+e.max]}})()")
        l, t, w, h = self.rect(sel)
        thumb = 16
        self.click(sel, fx=(thumb / 2 + (value - lo) / (hi - lo) * (w - thumb)) / w)
        got = self.js(f"+document.querySelector({sel!r}).value")
        if abs(got - value) > (hi - lo) * 0.03:      # nudge with the keyboard if the click landed a step off
            key = "ArrowRight" if got < value else "ArrowLeft"
            for _ in range(int(abs(got - value))):
                self.send("Input.dispatchKeyEvent", type="keyDown", key=key, code=key, windowsVirtualKeyCode=39 if got < value else 37)
                self.send("Input.dispatchKeyEvent", type="keyUp", key=key, code=key, windowsVirtualKeyCode=39 if got < value else 37)
            self.pump(0.3)
        return self.js(f"+document.querySelector({sel!r}).value")

    def wheel(self, dy, n=6, gap=0.12):
        for _ in range(n):
            self.send("Input.dispatchMouseEvent", type="mouseWheel", x=self.mouse[0], y=self.mouse[1], deltaX=0, deltaY=dy)
            self.pump(gap)

    def caption(self, text, hold=0.0):
        self.js(f"window.__caption({text!r})")
        self.pump(hold)

    def until_slot(self, k, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            if self.js("window.__gs.lastSlot") >= k:
                return
            self.pump(0.15)
        raise TimeoutError(f"slot {k} not reached")

    def until_done(self, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            if self.js("!!(window.__gs.cur && window.__gs.cur.done)"):
                return
            self.pump(0.2)
        raise TimeoutError("pass did not finish")


def walkthrough(b):
    used = []
    note = lambda name: used.append(name)

    b.caption("SomaiyaSat ground station. The satellite is shown at its real position right now, computed from real tracking data.", 5)
    b.caption("Drag to rotate the globe. Scroll to zoom.")
    b.move(800, 380)
    b.press(); b.move(1010, 430, 1.4); b.move(700, 330, 1.6); b.release(); note("globe drag")
    b.wheel(-90); b.pump(0.6); b.wheel(90); b.pump(0.8); note("globe zoom")

    b.click('[data-view="real"]'); note("tab: real telemetry")
    b.caption("Real telemetry: frames received from the real satellite CroCube by volunteer ground stations, decoded here.", 5)
    b.move(800, 420); b.wheel(120, n=5, gap=0.25); b.pump(3.5)
    b.click('[data-view="model"]'); note("tab: router model")
    b.caption("Router model: the AI is a small decision tree. This table compares it with the fixed-priority rule on 300 unseen passes.", 6)
    b.move(800, 420); b.wheel(120, n=5, gap=0.25); b.pump(3.5)
    b.click('[data-view="pass"]'); note("tab: pass")

    # ---- pass A: default settings, then break things
    b.caption("Play next pass: replays a real upcoming pass over Mumbai at 20x speed.")
    b.click('#jump', settle=1.0); note("play next pass")
    b.until_slot(4)
    b.caption("Top tape: AI router. Bottom tape: fixed priority. Blue = housekeeping, purple = image, green = voice. A hollow box was sent but lost.")
    b.until_slot(11)
    b.click('#pause'); note("pause")
    b.caption("Paused. Every AI decision is logged with its reason and the tree rule that produced it.")
    b.move(190, 690, 0.8); b.pump(3.5)
    b.click('#pause'); note("resume")
    b.caption("Playback speed: 5x, 60x, 20x.")
    b.click('[data-speed="5"]', settle=3.0); note("speed 5x")
    b.click('[data-speed="60"]', settle=1.2); note("speed 60x")
    b.click('[data-speed="20"]'); note("speed 20x")
    b.until_slot(22)
    b.click('#f-fault'); note("break the model")
    b.caption("Break the model: the AI returns an invalid answer for 5 slots. The watchdog rejects it and a safe rule takes over (amber).")
    b.until_slot(29)
    b.click('#f-corrupt'); note("corrupt sensor frame")
    b.caption("Corrupt sensor frame: a battery reading of -40% is sent. The router API refuses it (HTTP 422) and only housekeeping is sent.")
    b.until_done()
    b.caption("Pass complete: delivered data, lost transmissions and final battery for both methods.", 7)

    # ---- pass B: hard conditions
    b.click('#jump', settle=1.5); note("back to live")
    b.caption("A harder case: start the battery at 40% and add 5 dB of link interference.")
    soc = b.slide('#c-soc', 40); note(f"battery slider -> {soc:.0f}%")
    loss = b.slide('#c-loss', 5); note(f"interference slider -> {loss:.0f} dB")
    b.pump(1.0)
    b.caption("Pick a pass from the list.")
    b.click('#passes li[data-i="5"]', settle=1.0); note("pass list item")
    b.until_slot(5)
    b.caption("On a weak battery the AI holds back to protect it. Fixed priority keeps transmitting and loses data on the weak link.")
    b.until_slot(12)
    loss = b.slide('#c-loss', 9); note(f"interference mid-pass -> {loss:.0f} dB")
    b.caption("Raising interference mid-pass: the rest of the pass is re-planned from this moment.")
    b.until_slot(18)
    b.click('#f-eclipse'); note("force eclipse")
    b.caption("Force eclipse: solar charging stops for the rest of the pass.")
    b.until_slot(24)
    b.click('#f-drain'); note("drain battery")
    b.caption("Drain battery: both batteries drop to 28%.")
    b.until_slot(30)
    b.click('#f-safe'); note("safe mode on")
    b.caption("Safe mode: the operator overrides the AI. Housekeeping only.")
    b.until_slot(35)
    b.click('#f-safe'); note("safe mode off")
    b.caption("Safe mode released: the AI router decides again.")
    b.until_done()
    b.caption("Pass complete. Compare delivered data, losses and battery for the two methods.", 7)
    b.click('#jump', settle=1.0); note("back to live (again)")
    b.caption("Back to live tracking. SomaiyaSat ground station: Cloud Computing AI use case KJS-SRS-01.", 4)
    return used


def encode(frames, out):
    lst = FRAMES / "frames.txt"
    with open(lst, "w") as f:
        for (t, p), nxt in zip(frames, frames[1:] + [(frames[-1][0] + 0.5, None)]):
            f.write(f"file '{p}'\nduration {max(nxt[0] - t, 0.001):.4f}\n")
        f.write(f"file '{frames[-1][1]}'\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)


def main():
    FRAMES.mkdir(parents=True, exist_ok=True)
    for old in FRAMES.glob("f*.jpg"):
        old.unlink()
    b = Recorder(width=W, height=H, scale=1, profile=os.environ.get("CDP_PROFILE", "/tmp/somaiyasat-cdp-profile"))
    try:
        b.send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        b.goto(URL)
        b.wait("!!window.__gs", timeout=30)
        b.js(OVERLAY)
        time.sleep(2.0)
        b.send("Page.startScreencast", format="jpeg", quality=88, maxWidth=W, maxHeight=H, everyNthFrame=1)
        b.recording = True
        t0 = time.time()
        used = walkthrough(b)
        b.recording = False
        b.send("Page.stopScreencast")
        dur = b.frames[-1][0] - b.frames[0][0]
        print(f"{len(b.frames)} frames over {dur:.1f}s ({len(b.frames) / dur:.1f} fps), wall {time.time() - t0:.0f}s")
        print("exercised:", "; ".join(used))
        errs = b.js("document.getElementById('toast').textContent")
        print("last toast:", errs)
    finally:
        b.close()
    encode(b.frames, OUT)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
