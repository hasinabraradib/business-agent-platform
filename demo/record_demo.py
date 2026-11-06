"""Record a product demo video from a scene script (Playwright, Chromium, 1920x1080).

    uv run --no-project --with playwright==1.63.0 --with pyyaml python demo/record_demo.py --dry-run
    uv run --no-project --with playwright==1.63.0 --with pyyaml python demo/record_demo.py
    ./demo/build_video.sh

    # Scripts that check reply languages import the app's own rules, so run them with the
    # backend's environment:
    uv run --project backend --with playwright==1.63.0 python demo/record_demo.py \
        --scenes demo/scenes-bd.yaml --out demo/out/bd --dry-run

Needs the stack (API, worker, dashboard), the demo site (demo/site/serve.py) and the demo
tenant (demo/seed.py). Each scene is recorded in its own browser context, so a scene can be
retried (up to `video.retries` times) without the failed attempt reaching the video; the scenes
are trimmed and joined by build_video.sh using demo/out/segments.json.

--dry-run plays every scene without recording and checks each reply against its expected
keywords. While recording, a failed check retries the scene, and if it still fails the run
aborts rather than keep a bad answer.
"""

# ruff: noqa: E501  (embedded JavaScript, HTML and CSS read better unbroken)
import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TENANT = "saffron-sage"  # default tenant when a script names none
ADMIN_LABEL = "Demo video recorder"


class SceneFailed(Exception):
    pass


# --- overlays: a visible cursor and a caption bar ----------------------------------------------

OVERLAY_JS = """
(() => {
  if (window.__demo) return;
  const install = () => {
    if (document.getElementById('demo-cursor')) return;
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    cursor.innerHTML = `<svg width="34" height="34" viewBox="0 0 24 24"><path d="M4 2l15 9.5-6.6 1.4L9 20z"
      fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
    Object.assign(cursor.style, {position: 'fixed', left: '960px', top: '700px', zIndex: 2147483647,
      pointerEvents: 'none', transition: 'transform 120ms ease', filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.35))'});
    const caption = document.createElement('div');
    caption.id = 'demo-caption';
    Object.assign(caption.style, {position: 'fixed', left: '50%', bottom: '56px', transform: 'translateX(-50%)',
      zIndex: 2147483646, pointerEvents: 'none', background: 'rgba(17,17,17,.78)', color: '#fff',
      font: '600 40px/1.25 -apple-system, "Segoe UI", system-ui, sans-serif', padding: '18px 36px',
      borderRadius: '20px', maxWidth: '1500px', textAlign: 'center', opacity: '0',
      transition: 'opacity 400ms ease', letterSpacing: '0.2px'});
    const main = document.createElement('div');
    const sub = document.createElement('div');
    sub.id = 'demo-caption-sub';
    Object.assign(sub.style, {font: '500 28px/1.3 -apple-system, "Segoe UI", system-ui, sans-serif',
      color: '#d7dae0', marginTop: '6px'});
    caption.append(main, sub);
    document.documentElement.append(cursor, caption);
  };
  window.__demo = {
    install,
    caption(text, sub) { install(); const c = document.getElementById('demo-caption');
      c.firstChild.textContent = text || ''; c.lastChild.textContent = sub || '';
      c.lastChild.style.display = sub ? 'block' : 'none'; c.style.opacity = text ? '1' : '0'; },
    move(x, y) { install(); const c = document.getElementById('demo-cursor');
      c.style.left = x + 'px'; c.style.top = y + 'px'; },
    press(down) { const c = document.getElementById('demo-cursor');
      if (c) c.style.transform = down ? 'scale(0.82)' : 'scale(1)'; },
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
"""


class Director:
    """Cursor, captions, typing and waiting on one page."""

    def __init__(self, page: Page, settings: dict) -> None:
        self.page = page
        self.type_delay = settings["type_delay_ms"]
        self.x, self.y = 960.0, 700.0
        self.t0 = time.monotonic()  # the page's recording starts with the page

    def mark(self) -> float:
        """Seconds since this page's recording started."""
        return time.monotonic() - self.t0

    def caption(self, text: str | None, translation: str | None = None) -> None:
        self.page.evaluate(
            "([t, s]) => window.__demo.caption(t, s)", [text or "", translation or ""]
        )

    def move_to(self, locator, duration_ms: int = 700) -> None:
        box = locator.bounding_box()
        if box is None:
            raise SceneFailed(f"element not visible: {locator}")
        tx, ty = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        steps = max(12, duration_ms // 16)
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)  # smoothstep
            x, y = self.x + (tx - self.x) * ease, self.y + (ty - self.y) * ease
            self.page.evaluate("([x, y]) => window.__demo.move(x, y)", [x, y])
            self.page.wait_for_timeout(16)
        self.x, self.y = tx, ty

    def click(self, locator) -> None:
        self.move_to(locator)
        self.page.evaluate("() => window.__demo.press(true)")
        self.page.wait_for_timeout(120)
        locator.click()
        self.page.evaluate("() => window.__demo.press(false)")

    def type(self, locator, text: str) -> None:
        self.click(locator)
        locator.press_sequentially(text, delay=self.type_delay)

    def hold(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))


# --- the widget --------------------------------------------------------------------------------


def widget(page: Page):
    return page.locator("#bap-chat-widget")


def open_widget(page: Page, director: Director | None = None) -> None:
    w = widget(page)
    w.locator("button[aria-controls='bap-panel']").wait_for(timeout=20_000)
    launcher = w.locator("button[aria-controls='bap-panel']")
    if director:
        director.click(launcher)
    else:
        launcher.click()
    w.locator("#bap-input").wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(400)


def ask(page: Page, director: Director, text: str, timeout_s: float = 60) -> dict:
    """Type a message, send it, wait for the streamed reply to finish; return the reply."""
    w = widget(page)
    replies = w.locator(".msg.assistant, .msg.staff, .msg.error")
    before = replies.count()
    director.type(w.locator("#bap-input"), text)
    director.click(w.locator("button[aria-label='Send message']"))
    # wait_for_function enforces its timeout; a polling loop of evaluate() calls once hung for
    # half an hour when the page stopped responding.
    try:
        page.wait_for_function(
            """before => {
              const root = document.querySelector('#bap-chat-widget').shadowRoot;
              const msgs = root.querySelectorAll('.msg.assistant, .msg.staff, .msg.error');
              const last = msgs[msgs.length - 1];
              const live = [...root.querySelectorAll('[aria-live=polite]')]
                .map(n => n.textContent).join(' ');
              return msgs.length > before && !last.querySelector('.typing')
                && !live.includes('typing')
                && (last.querySelector('.bubble')?.innerText || '').trim().length > 0;
            }""",
            arg=before,
            polling=250,
            timeout=timeout_s * 1000,
        )
    except PlaywrightTimeout:
        raise SceneFailed(f"no finished reply within {timeout_s:.0f} s to: {text}") from None
    page.wait_for_timeout(600)  # let the last tokens and source chips render
    return page.evaluate(
        """() => {
          const root = document.querySelector('#bap-chat-widget').shadowRoot;
          const msgs = root.querySelectorAll('.msg.assistant, .msg.staff, .msg.error');
          const last = msgs[msgs.length - 1];
          return {text: last.querySelector('.bubble')?.innerText || '',
                  sources: last.querySelectorAll('.sources .chip').length,
                  error: last.classList.contains('error')};
        }"""
    )


def check_reply(step: dict, reply: dict) -> list[str]:
    problems = []
    if reply.get("error"):
        problems.append("the widget showed an error")
    for pattern in step.get("expect", []):
        if not re.search(pattern, reply["text"], re.I):
            problems.append(f"missing /{pattern}/")
    for pattern in step.get("not_expect", []):
        if re.search(pattern, reply["text"], re.I):
            problems.append(f"contains /{pattern}/")
    if step.get("sources") and not reply["sources"]:
        problems.append("no source chips")
    want = step.get("language")
    if want and reply.get("language") != want:
        problems.append(f"replied in {reply.get('language')}, expected {want}")
    return problems


_LANGUAGE = None


def detect_language(text: str) -> str | None:
    """The app's own rule (app.chat.prompts.reply_language): bengali (Bengali script),
    banglish (Bengali in Latin letters) or english. None when the backend isn't importable."""
    global _LANGUAGE
    if _LANGUAGE is None:
        sys.path.insert(0, str(REPO / "backend"))
        try:
            from app.chat.prompts import reply_language
        except ImportError:
            _LANGUAGE = False
        else:
            _LANGUAGE = reply_language
    if not _LANGUAGE:
        return None
    # The rule was written for customer messages. The system prompt has English replies write
    # prices as "480 taka", and two of those made a correct English reply count as Banglish,
    # so the currency word is not counted when checking a reply.
    return _LANGUAGE(re.sub(r"\btakar?\b", " ", text, flags=re.I))


# --- scenes ------------------------------------------------------------------------------------


CARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#111;color:#fff;font-family:-apple-system,"Segoe UI",system-ui,sans-serif}
.c{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px;text-align:center}
h1{font-size:96px;margin:0;letter-spacing:-1.5px;max-width:1600px;line-height:1.08}
p{font-size:40px;margin:0;color:#c9ccd3}
.sig{margin-top:40px;font-size:36px;color:#c5ee4f;font-weight:600}
.bar{width:120px;height:10px;border-radius:99px;background:#c5ee4f}
h1,p,.sig,.bar{animation:in .7s ease both}p{animation-delay:.15s}.sig{animation-delay:.3s}
@keyframes in{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
</style></head><body><div class="c"><div class="bar"></div><h1></h1><p></p><div class="sig"></div></div></body></html>"""


def scene_card(page: Page, director: Director, scene: dict, cfg: dict) -> float:
    page.set_content(CARD_HTML)
    page.evaluate(
        """s => { document.querySelector('h1').textContent = s.headline || '';
                  document.querySelector('p').textContent = s.subline || '';
                  const sig = document.querySelector('.sig');
                  sig.textContent = s.signature || ''; if (!s.signature) sig.remove(); }""",
        scene,
    )
    page.wait_for_timeout(100)
    start = director.mark()
    director.hold(scene.get("hold_s", 3))
    return start


def load_site(page: Page, cfg: dict, scene: dict) -> None:
    page.goto(scene.get("site_url", cfg["site_url"]), wait_until="networkidle")
    widget(page).locator("button[aria-controls='bap-panel']").wait_for(timeout=20_000)
    page.evaluate("() => window.__demo.install()")
    if zoom := cfg["video"].get("widget_zoom"):
        enlarge_widget(page, zoom, cfg["video"].get("widget_panel_height", 450))


def enlarge_widget(page: Page, zoom: float, panel_height: int) -> None:
    """Zoom only the chat widget, so its text is readable on a phone (15 px x 1.875 = 28 px),
    and shorten the panel so the zoomed panel still fits the frame. The site, the dashboard and
    the captions keep their normal size. Recording only: the product is unchanged."""
    page.evaluate(
        """([zoom, height]) => {
          const host = document.querySelector('#bap-chat-widget');
          host.style.zoom = String(zoom);
          const style = document.createElement('style');
          style.textContent = `.panel{height:${height}px;max-height:none}`;
          host.shadowRoot.appendChild(style);
        }""",
        [zoom, panel_height],
    )


def scene_site(page: Page, director: Director, scene: dict, cfg: dict) -> float:
    load_site(page, cfg, scene)
    page.wait_for_timeout(300)
    start = director.mark()
    director.caption(scene.get("caption"))
    director.hold(0.8)
    if target := scene.get("scroll_to"):
        page.evaluate("s => document.querySelector(s).scrollIntoView({behavior: 'smooth'})", target)
        director.hold(1.2)
        page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
        director.hold(1.0)
    open_widget(page, director)
    director.hold(scene.get("hold_s", cfg["video"]["default_hold_s"]))
    return start


def scene_chat(page: Page, director: Director, scene: dict, cfg: dict, checks: list) -> float:
    load_site(page, cfg, scene)
    open_widget(page)
    start = director.mark()
    director.caption(scene.get("caption"), scene.get("translation"))
    director.hold(0.6)
    for step in scene["messages"]:
        if "translation" in step:  # a per-message translation replaces the scene's
            director.caption(scene.get("caption"), step["translation"])
        reply = ask(page, director, step["say"])
        reply["language"] = detect_language(reply["text"])
        if step.get("language") and reply["language"] is None:
            sys.exit("Language checks need the backend: run with uv run --project backend.")
        problems = check_reply(step, reply)
        checks.append({"scene": scene["id"], "say": step["say"], "reply": reply["text"],
                       "sources": reply["sources"], "language": reply["language"],
                       "problems": problems})  # fmt: skip
        if problems:
            raise SceneFailed(f"{scene['id']}: {'; '.join(problems)}\n  reply: {reply['text']}")
        director.hold(step.get("hold_s", cfg["video"]["default_hold_s"]))
    return start


def scene_dashboard(
    page: Page, director: Director, scene: dict, cfg: dict, admin_key: str
) -> float:
    base = cfg["dashboard_url"]
    # Sign in through the API (shares the context's cookies): the key never appears on screen.
    response = page.context.request.post(
        f"{base}/api/session", data={"key": admin_key}, headers={"x-bap-dashboard": "1"}
    )
    if not response.ok:
        raise SceneFailed(f"dashboard sign-in failed: HTTP {response.status}")
    page.goto(f"{base}/inbox", wait_until="networkidle")
    page.evaluate("() => window.__demo.install()")
    conversations = page.locator("main a[href^='/inbox/']")
    conversations.first.wait_for(timeout=15_000)
    start = director.mark()
    director.caption(scene.get("caption"))
    director.hold(1.0)
    if term := scene.get("search"):  # find this conversation the way an owner would
        director.type(page.get_by_label("Search conversations"), term)
        page.wait_for_timeout(900)  # the inbox search is debounced
    if scene.get("inbox_hold_s"):  # let viewers see the list of conversations
        director.hold(scene["inbox_hold_s"])
    if scene.get("open_conversation", True):
        director.click(conversations.first)
        page.wait_for_load_state("networkidle")
        page.evaluate("() => window.__demo.install()")
        director.caption(scene.get("caption"))
        director.hold(scene.get("conversation_hold_s", 1.8))
    trace = page.get_by_role("link", name="View trace").last
    if scene.get("trace", True) and scene.get("open_conversation", True) and trace.count():
        director.click(trace)
        page.wait_for_load_state("networkidle")
        page.evaluate("() => window.__demo.install()")
        director.caption(scene.get("caption"))
        director.hold(2.5)
    nav = page.get_by_role("navigation", name="Main")
    director.click(nav.get_by_role("link", name="Bookings & leads"))
    page.wait_for_load_state("networkidle")
    page.evaluate("() => window.__demo.install()")
    director.caption(scene.get("caption"))
    page.get_by_text(scene.get("bookings_text", "Maria Lopez")).first.wait_for(timeout=10_000)
    director.hold(scene.get("hold_s", 2.5))
    return start


# --- keys --------------------------------------------------------------------------------------


def reset_activity(tenant: str) -> None:
    subprocess.run(
        ["docker", "compose", "run", "--rm", "--no-deps", "-T", "migrate", "python",
         "/demo/seed.py", "--reset-activity", "--tenant", tenant],
        cwd=REPO, check=True,
    )  # fmt: skip


def create_admin_key(tenant: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "run", "--rm", "--no-deps", "-T", "migrate", "python", "-m",
         "app.cli", "create-key", "--tenant", tenant, "--kind", "admin", "--label", ADMIN_LABEL],
        cwd=REPO, capture_output=True, text=True,
    )  # fmt: skip
    match = re.search(r"bap_admin_[A-Za-z0-9_-]+", out.stdout)
    if not match:
        sys.exit(f"Could not create an admin key for {tenant}; is it seeded?")
    return match.group(0)


def revoke_admin_key(api: str, key: str) -> None:
    """Revoke this run's key, and any left by an interrupted run; this run's own key last,
    since it makes the calls. A failure here is reported, never fatal."""
    import urllib.request

    headers = {"Authorization": f"Bearer {key}"}
    try:
        request = urllib.request.Request(f"{api}/v1/api-keys", headers=headers)
        with urllib.request.urlopen(request) as response:
            keys = json.load(response)
        mine = [k for k in keys if k["label"] == ADMIN_LABEL and not k["revoked_at"]]
        own = key.removeprefix("bap_admin_")  # stored prefixes are the random part
        for item in sorted(mine, key=lambda k: own.startswith(k["prefix"])):
            delete = urllib.request.Request(
                f"{api}/v1/api-keys/{item['id']}", method="DELETE", headers=headers
            )
            urllib.request.urlopen(delete).close()
    except Exception as exc:  # cleanup must never lose a finished recording
        print(
            f"Warning: could not revoke the recorder's admin key ({exc}); revoke "
            f"'{ADMIN_LABEL}' keys in the dashboard's Settings."
        )


# --- main --------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="play and check, don't record")
    parser.add_argument("--scenes", default=str(HERE / "scenes.yaml"))
    parser.add_argument("--out", default=str(HERE / "out"), help="output folder for this video")
    parser.add_argument("--only", action="append", help="scene id; repeatable")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.scenes).read_text(encoding="utf-8"))
    video = cfg["video"]
    size = {"width": video["width"], "height": video["height"]}
    scenes = [s for s in cfg["scenes"] if not args.only or s["id"] in args.only]
    out, raw = Path(args.out), Path(args.out) / "raw"
    default_tenant = cfg.get("tenant", TENANT)
    tenants = list(dict.fromkeys(s.get("tenant", default_tenant) for s in cfg["scenes"]
                                 if s["kind"] in ("site", "chat", "dashboard")))  # fmt: skip
    if not args.dry_run and not args.only:
        for tenant in tenants:  # the video's dashboard shows only what this recording creates
            reset_activity(tenant)
    admin_keys = {
        tenant: create_admin_key(tenant)
        for tenant in dict.fromkeys(
            s.get("tenant", default_tenant) for s in scenes if s["kind"] == "dashboard"
        )
    }

    if not args.dry_run:
        shutil.rmtree(raw, ignore_errors=True)
        raw.mkdir(parents=True)
    checks: list[dict] = []
    segments: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chromium", headless=not args.headed)
            for scene in scenes:
                attempts = 1 if args.dry_run else video["retries"]
                for attempt in range(1, attempts + 1):
                    context = make_context(browser, size, None if args.dry_run else raw)
                    page = context.new_page()
                    director = Director(page, video)
                    try:
                        key = admin_keys.get(scene.get("tenant", default_tenant), "")
                        start = run_scene(page, director, scene, cfg, checks, key)
                        end = director.mark()
                        context.close()
                        path = page.video.path() if page.video else None
                        segments.append({"id": scene["id"], "file": str(path) if path else None,
                                         "start": round(start, 2), "end": round(end, 2),
                                         "thumbnail": bool(scene.get("thumbnail"))})  # fmt: skip
                        print(f"  {scene['id']}: ok ({end - start:.1f} s)", flush=True)
                        break
                    except SceneFailed as exc:
                        out.mkdir(parents=True, exist_ok=True)
                        with contextlib.suppress(PlaywrightError):  # how the page looked
                            page.screenshot(path=str(out / f"failed-{scene['id']}.png"))
                        context.close()
                        if page.video:
                            Path(page.video.path()).unlink(missing_ok=True)
                        print(f"  {scene['id']}: attempt {attempt} failed: {exc}", flush=True)
                        if args.dry_run:
                            break
                        if attempt == attempts:
                            print(f"\nAborting: scene '{scene['id']}' failed {attempts} times. "
                                  "No video was kept for it; fix the cause and record again.")  # fmt: skip
                            return 1
            browser.close()
    finally:
        for key in admin_keys.values():
            revoke_admin_key(args.api, key)

    if args.dry_run:
        failed = [c for c in checks if c["problems"]]
        print("\nReplies:")
        for c in checks:
            mark = "FAIL" if c["problems"] else "ok  "
            print(f"  {mark} {c['scene']:<12} [{c['language'] or '-'}] {c['reply'][:110]!r}")
        print(
            f"\nDry run: {len(checks) - len(failed)}/{len(checks)} replies matched their keywords."
        )
        for c in failed:
            print(
                f"  FAIL {c['scene']}: {c['say']}\n    {'; '.join(c['problems'])}\n    reply: {c['reply']}"
            )
        return 1 if failed else 0

    (out / "segments.json").write_text(json.dumps(segments, indent=2))
    total = 0.0
    print("\nScene durations:")
    for s in segments:
        length = s["end"] - s["start"]
        total += length
        print(f"  {s['id']:<14} {length:5.1f} s")
    print(f"  {'total':<14} {total:5.1f} s")
    print(f"Next: ./demo/build_video.sh --out {args.out}")
    return 0


def make_context(browser, size: dict, video_dir: Path | None) -> BrowserContext:
    options = {"viewport": size, "device_scale_factor": 1}
    if video_dir:
        options.update(record_video_dir=str(video_dir), record_video_size=size)
    context = browser.new_context(**options)
    context.add_init_script(OVERLAY_JS)
    return context


def run_scene(
    page: Page, director: Director, scene: dict, cfg: dict, checks: list, admin_key: str
) -> float:
    try:
        return _run_scene(page, director, scene, cfg, checks, admin_key)
    except PlaywrightError as exc:  # timeouts and missing elements fail the scene, not the run
        raise SceneFailed(f"{scene['id']}: {str(exc).splitlines()[0]}") from None


def _run_scene(
    page: Page, director: Director, scene: dict, cfg: dict, checks: list, admin_key: str
) -> float:
    kind = scene["kind"]
    if kind == "card":
        return scene_card(page, director, scene, cfg)
    if kind == "site":
        return scene_site(page, director, scene, cfg)
    if kind == "chat":
        return scene_chat(page, director, scene, cfg, checks)
    if kind == "dashboard":
        return scene_dashboard(page, director, scene, cfg, admin_key)
    raise SceneFailed(f"unknown scene kind {kind}")


if __name__ == "__main__":
    sys.exit(main())
