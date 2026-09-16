#!/usr/bin/env python3
"""生成 App 图标：Codex 云朵标 + 终端提示符，黑白配色、白色为主、液态玻璃质感。

只在 macOS 上跑得完整（需要 sips 和 iconutil），其它系统也能生成 SVG/PNG。

  python3 packaging/macos/icon/make_icon.py

产物：
  appicon.svg           矢量源文件
  appicon-1024.png      位图
  ../AppIcon.icns       macOS 应用图标（可选）
"""

from __future__ import annotations

import math
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SIZE = 1024
CENTER = SIZE / 2

# 云朵标：中心圆 + 八个圆角花瓣，和 Codex 官方 icon 的形状一致
LOBE_COUNT = 8
LOBE_RADIUS = 118.0
LOBE_DISTANCE = 212.0
CORE_RADIUS = 190.0

# 终端提示符的尺寸
STROKE = 48.0


def blob_circles() -> str:
    parts = ['    <circle cx="0" cy="0" r="%.0f" />' % CORE_RADIUS]
    for index in range(LOBE_COUNT):
        angle = math.radians(360.0 / LOBE_COUNT * index + 22.5)
        cx = math.cos(angle) * LOBE_DISTANCE
        cy = math.sin(angle) * LOBE_DISTANCE
        parts.append('    <circle cx="%.1f" cy="%.1f" r="%.0f" />' % (cx, cy, LOBE_RADIUS))
    return "\n".join(parts)


def build_svg() -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}">
  <defs>
    <!-- 底板：白色为主，略带冷灰，模拟玻璃的厚度 -->
    <linearGradient id="tile" x1="0" y1="0" x2="0.35" y2="1">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="0.52" stop-color="#f4f6f8"/>
      <stop offset="1" stop-color="#e3e7ec"/>
    </linearGradient>
    <!-- 顶部的镜面高光 -->
    <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.95"/>
      <stop offset="0.55" stop-color="#ffffff" stop-opacity="0.12"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <!-- 云朵玻璃体：中间实、边缘透 -->
    <!-- 用不透明的浅灰白渐变：既能盖住内部的圆线，又保留玻璃的层次 -->
    <radialGradient id="glass" cx="0.36" cy="0.26" r="0.95">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="0.58" stop-color="#fbfcfe"/>
      <stop offset="1" stop-color="#e6ebf1"/>
    </radialGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="0.55" y2="1">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="0.45" stop-color="#ccd4de"/>
      <stop offset="1" stop-color="#8f99a6"/>
    </linearGradient>
    <!-- 注意：描边用渐变必须给 userSpaceOnUse，否则下划线这种零高度的路径会画不出来 -->
    <linearGradient id="glyph" gradientUnits="userSpaceOnUse" x1="-140" y1="-110" x2="150" y2="120">
      <stop offset="0" stop-color="#2b2f36"/>
      <stop offset="1" stop-color="#08090b"/>
    </linearGradient>
    <filter id="softShadow" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="18" stdDeviation="26" flood-color="#0b0f16" flood-opacity="0.16"/>
    </filter>
    <filter id="blobShadow" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="8" stdDeviation="14" flood-color="#0b0f16" flood-opacity="0.08"/>
    </filter>
    <clipPath id="tileClip">
      <rect x="0" y="0" width="{SIZE}" height="{SIZE}" rx="228"/>
    </clipPath>
  </defs>

  <!-- 底板：白色玻璃方砖 -->
  <g clip-path="url(#tileClip)">
    <rect x="0" y="0" width="{SIZE}" height="{SIZE}" fill="url(#tile)"/>
    <rect x="0" y="0" width="{SIZE}" height="{SIZE}" fill="url(#sheen)"/>
    <!-- 左上角的一束光，营造玻璃的高光 -->
    <ellipse cx="250" cy="180" rx="470" ry="300" fill="#ffffff" opacity="0.55" transform="rotate(-24 250 180)"/>
  </g>
  <rect x="1.5" y="1.5" width="{SIZE - 3}" height="{SIZE - 3}" rx="226" fill="none"
        stroke="#ffffff" stroke-opacity="0.85" stroke-width="3"/>

  <!-- 云朵：半透明玻璃体 -->
  <g transform="translate({CENTER} {CENTER})" filter="url(#blobShadow)">
    <!-- 先给并集描一圈外轮廓，再用填充盖住内部的圆线，得到一整朵云 -->
    <g fill="none" stroke="url(#rim)" stroke-width="9">
{blob_circles()}
    </g>
    <g fill="url(#glass)">
{blob_circles()}
    </g>
    <!-- 玻璃体内部的高光 -->
    <ellipse cx="-96" cy="-150" rx="188" ry="118" fill="#ffffff" opacity="0.62"
             transform="rotate(-28 -96 -150)"/>
  </g>

  <!-- 终端提示符 >_ -->
  <g transform="translate({CENTER} {CENTER})" fill="none" stroke="url(#glyph)"
     stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round">
    <path d="M -108 -74 L -26 6 L -108 86"/>
    <path d="M 46 96 L 152 96"/>
  </g>
</svg>
'''


def render_png(svg: pathlib.Path, target: pathlib.Path, size: int = SIZE) -> bool:
    """用无头 Chrome 把 SVG 渲染成 PNG（macOS 自带 Chrome 时可用）。"""
    chrome = None
    for candidate in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if pathlib.Path(candidate).exists():
            chrome = candidate
            break
    if not chrome:
        print("没有找到 Chrome/Chromium，跳过 PNG 渲染（SVG 已经写好）")
        return False
    html = HERE / "_preview.html"
    html.write_text(
        '<html><body style="margin:0;background:transparent">'
        '<img src="%s" width="%d" height="%d"></body></html>' % (svg, size, size))
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000",
                    "--window-size=%d,%d" % (size, size),
                    "--screenshot=%s" % target, html.as_uri()],
                   capture_output=True, timeout=120)
    html.unlink(missing_ok=True)
    return target.exists()


def build_icns(png: pathlib.Path) -> bool:
    if sys.platform != "darwin" or not shutil.which("iconutil"):
        return False
    iconset = HERE / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    sizes = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]
    for base, scale in sizes:
        pixels = base * scale
        name = "icon_%dx%d%s.png" % (base, base, "@2x" if scale == 2 else "")
        subprocess.run(["sips", "-z", str(pixels), str(pixels), str(png),
                        "--out", str(iconset / name)], capture_output=True)
    result = subprocess.run(["iconutil", "-c", "icns", str(iconset),
                             "-o", str(HERE / "AppIcon.icns")], capture_output=True)
    shutil.rmtree(iconset, ignore_errors=True)
    if result.returncode != 0:
        print("iconutil 失败：", result.stderr.decode()[:200])
        return False
    return True


def main() -> int:
    svg_path = HERE / "appicon.svg"
    svg_path.write_text(build_svg())
    print("已生成 SVG：%s" % svg_path)

    png_path = HERE / "appicon-1024.png"
    if render_png(svg_path, png_path):
        print("已生成 PNG：%s" % png_path)
    if build_icns(png_path):
        print("已生成 macOS 图标：%s" % (HERE / "AppIcon.icns"))

    # 界面左上角用的是同一份矢量，改一处两边都跟着变
    webui_logo = HERE.parents[2] / "codex_switcher" / "webui" / "static" / "logo.svg"
    if webui_logo.parent.exists():
        webui_logo.write_text(build_svg())
        print("已同步界面 logo：%s" % webui_logo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
