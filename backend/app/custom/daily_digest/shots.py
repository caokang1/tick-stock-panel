"""给面板的几个页面截长图：在后台开一个不显示窗口的浏览器，逐页打开、量好高度、截下来。

只访问本机的面板，不联网。浏览器用的是这台电脑上已有的 Chrome/Chromium；找不到就不截，由调用方退回到文字版。
"""

from __future__ import annotations

import base64
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WIDTH = 1440
HEIGHT = 900
MAX_HEIGHT = 12000  # 再长的页面也只截这么高，免得图片大得离谱
SCALE = 1.5  # 截得比屏幕细一些，手机上放大看还清楚
SETTLE = 2.5  # 内容不再变化以后，再等这么久让图表画完
LOAD_TIMEOUT = 45.0  # 一页最多等这么久
QUALITY = 82

# (标题, 路径, 打开后要点一下的页签文字)
PAGES: list[tuple[str, str, str | None]] = [
    ("看板", "/", None),
    ("连板梯队", "/limit-ladder", None),
    ("概念板块", "/concept-analysis", None),
    ("行业板块", "/industry-analysis", None),
    ("市场环境", "/regime", None),
    ("每日复盘", "/daily-review", None),
    ("每日复盘 · 当天全文", "/daily-review", "每日复盘·归档"),
]

# 量出页面内容区（<main>）在屏幕上的位置，以及要把窗口再拉高多少才能不出滚动条：
# 有的页面是 <main> 自己滚，有的是它里面的一大块在滚，取其中差得最多的那个
MEASURE = """(() => {
  const m = document.querySelector('main') || document.documentElement
  const r = m.getBoundingClientRect()
  let extra = m.scrollHeight - m.clientHeight
  for (const e of m.querySelectorAll('*')) {
    if (e.clientWidth < r.width * 0.6 || e.clientHeight < 200) continue
    if (!/(auto|scroll)/.test(getComputedStyle(e).overflowY)) continue
    extra = Math.max(extra, e.scrollHeight - e.clientHeight)
  }
  return JSON.stringify({x: r.left, y: r.top, w: r.width, h: r.height, extra: extra})
})()"""

# 页面加载到哪一步了：正文有多少字、还有没有转圈
STATE = """(() => {
  const m = document.querySelector('main')
  if (!m) return ''
  return m.innerText.length + ':' + m.scrollHeight + ':' + document.querySelectorAll('main .animate-spin').length
})()"""

CLICK = """((text) => {
  const b = [...document.querySelectorAll('button,[role=tab]')].find(x => x.textContent.trim().startsWith(text))
  if (b) b.click()
  return !!b
})(%s)"""


def find_browser(configured: str = "") -> str | None:
    candidates = [configured, os.environ.get("CHROME_PATH", "")]
    candidates += [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "chrome")]
    candidates += sorted(glob.glob(str(Path.home() / ".cache/puppeteer/chrome/*/chrome-linux64/chrome")), reverse=True)
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


WINDOWS_FONTS = Path("/mnt/c/Windows/Fonts")
CHINESE_FONTS = ("msyh.ttc", "msyhbd.ttc", "simhei.ttf")


def font_env(work: Path, source: Path = WINDOWS_FONTS) -> dict[str, str]:
    """让后台浏览器认得中文字体，返回要加给它的环境变量。

    面板跑在 Windows 里的 Linux 子系统时，Linux 这边通常没有中文字体，截出来的字全是方框。
    这里只给这一个浏览器进程指一份字体设置：系统原有的字体，加上 Windows 自带的微软雅黑、黑体。
    不改系统设置，也不复制字体文件。Windows 字体不在（普通 Linux 电脑）就什么都不做。
    """
    found = [source / name for name in CHINESE_FONTS if (source / name).is_file()]
    if not found:
        return {}
    fonts = work / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    for font in found:
        link = fonts / font.name
        if not link.exists():
            link.symlink_to(font)
    conf = work / "fonts.conf"
    conf.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        f"  <dir>{fonts}</dir>\n  <cachedir>{work / 'font-cache'}</cachedir>\n</fontconfig>\n",
        encoding="utf-8",
    )
    return {"FONTCONFIG_FILE": str(conf)}


class _Tab:
    """一个浏览器页签，用浏览器自带的调试接口控制。"""

    def __init__(self, socket: Any) -> None:
        self._socket, self._id, self._session = socket, 0, None
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self._session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self._id += 1
        message: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if self._session:
            message["sessionId"] = self._session
        self._socket.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:
            reply = json.loads(self._socket.recv(timeout=max(0.1, deadline - time.monotonic())))
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result") or {}

    def evaluate(self, expression: str) -> Any:
        return self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})["result"].get("value")

    def resize(self, height: int) -> None:
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": WIDTH, "height": height, "deviceScaleFactor": SCALE, "mobile": False})

    def wait_ready(self) -> None:
        """等到正文出来、不再转圈、连续几次看都没有变化。到时间还没稳定就照现在的样子截。"""
        deadline = time.monotonic() + LOAD_TIMEOUT
        last, same = None, 0
        while time.monotonic() < deadline:
            time.sleep(1.0)
            state = self.evaluate(STATE) or ""
            parts = state.split(":")
            ready = len(parts) == 3 and int(parts[0]) > 30 and parts[2] == "0"
            same = same + 1 if ready and state == last else 0
            last = state
            if same >= 2:
                break
        time.sleep(SETTLE)

    def shoot(self, url: str, click: str | None) -> bytes:
        self.resize(HEIGHT)
        self.call("Page.navigate", {"url": url})
        self.wait_ready()
        if click:
            if not self.evaluate(CLICK % json.dumps(click)):
                raise RuntimeError(f"页面上没有找到“{click}”页签")
            self.wait_ready()
        box: dict[str, float] = {}
        height = HEIGHT
        for _ in range(4):  # 拉高窗口后内容可能又变长，多量几次
            box = json.loads(self.evaluate(MEASURE))
            extra = box["extra"]
            if extra <= 2:
                break
            height = int(min(height + extra, MAX_HEIGHT))
            self.resize(height)
            time.sleep(1.5)
            if height >= MAX_HEIGHT:
                box = json.loads(self.evaluate(MEASURE))
                break
        clip = {"x": box["x"], "y": box["y"], "width": box["w"], "height": min(box["h"], MAX_HEIGHT), "scale": 1}
        data = self.call("Page.captureScreenshot", {"format": "jpeg", "quality": QUALITY, "clip": clip}, timeout=60.0)
        return base64.b64decode(data["data"])


def capture(base_url: str, browser: str, pages: list[tuple[str, str, str | None]] | None = None,
            work: Path | None = None) -> list[dict[str, Any]]:
    """逐页截图，返回 [{"title", "image"(JPEG 字节) 或 "error"}]。浏览器起不来时抛 RuntimeError。"""
    from websockets.sync.client import connect

    profile = tempfile.mkdtemp(prefix="digest-chrome-")
    process = subprocess.Popen(
        [browser, "--headless=new", "--no-sandbox", "--no-proxy-server", "--hide-scrollbars", "--disable-gpu",
         "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        env={**os.environ, **(font_env(work) if work else {})},
    )
    try:
        endpoint = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            line = process.stderr.readline() if process.stderr else ""
            found = re.search(r"DevTools listening on (ws://\S+)", line)
            if found:
                endpoint = found.group(1)
                break
        if not endpoint:
            raise RuntimeError("后台浏览器没有启动起来")
        out: list[dict[str, Any]] = []
        with connect(endpoint, max_size=None, open_timeout=10, proxy=None) as socket:
            tab = _Tab(socket)
            for title, path, click in pages or PAGES:
                try:
                    out.append({"title": title, "image": tab.shoot(base_url.rstrip("/") + path, click)})
                except Exception as e:  # noqa: BLE001 — 一页截不了不影响其余
                    logger.warning("每日汇总：“%s”没有截下来：%s", title, e)
                    out.append({"title": title, "error": str(e)})
        return out
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)
