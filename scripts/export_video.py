#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Courseware Studio 视频导出（可选交付）。

课件画面完全由 audio.currentTime 驱动、一条旁白句子 = 一个稳定视觉步，
所以线性视频不需要真实录屏：逐句在句末前一瞬定格截帧，按逐句时长拼接，
再混入旁白音轨，字幕与语音天然逐句对齐。

依赖：本机 Chrome/Edge（headless 截图）+ ffmpeg/ffprobe。
前提：页面遵循标准骨架（.stage 内 SVG 舞台、#lesson-timeline 内联时间轴、
#main-audio 指向交付音频）。门禁是交互证据通道，线性导出会自然跳过：
每帧取在句末之前，不会踩到 at:'end' 锚点。

用法：
    python scripts/export_video.py <页面目录或 index.html>
    python scripts/export_video.py <页面目录> -o out.mp4 --crf 18 --keep
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _script_utils import setup_stdio  # noqa: E402
from check_gates import _find_chrome  # noqa: E402

STAGE_PAD = 10          # 标准骨架 .stage 的 padding；窗口按它留边
EPS = 0.06              # 定格点相对句末的提前量，避开 at:'end' 门禁锚点

SNIPPET = """
<script>
(function(){
  var t = parseFloat(new URLSearchParams(location.search).get('shot'));
  if (isNaN(t)) return;
  var st = document.createElement('style');
  st.textContent =
    '*,*::before,*::after{transition:none!important;animation:none!important}' +
    'body{margin:0;background:#181c25}' +
    '.shell{display:block;padding:0;max-width:none;gap:0}' +
    '.col-side,.chapter,.rack{display:none!important}' +
    '.stage{border:0!important;border-radius:0;padding:%(pad)dpx}';
  document.head.appendChild(st);
  var audio = document.getElementById('main-audio');
  function seek(){
    if (!audio.duration){ setTimeout(seek, 150); return; }
    audio.pause();
    audio.currentTime = Math.max(0.01, Math.min(t, audio.duration - 0.05));
    document.getElementById('stage').dataset.state = 'paused';
    audio.dispatchEvent(new Event('timeupdate'));
  }
  window.addEventListener('load', seek);
})();
</script>
</body>"""


def _fail(msg: str) -> SystemExit:
    return SystemExit(f'[error] {msg}')


def _timeline_sentences(src: str) -> list[tuple[float, float]]:
    m = re.search(r'<script[^>]*id="lesson-timeline"[^>]*>(.*?)</script>', src, re.S)
    if not m:
        raise _fail('找不到 #lesson-timeline：导出要求时间轴已内联进页面')
    scenes = json.loads(m.group(1)).get('scenes') or []
    out = []
    for sc in scenes:
        for n in sc.get('runtime', {}).get('narration') or []:
            out.append((float(n['start']), float(n['duration'])))
    if not out:
        raise _fail('时间轴里没有任何旁白句子')
    return out


def _audio_src(src: str) -> str:
    m = re.search(r'<audio[^>]*id=["\']main-audio["\'][^>]*src=["\']([^"\']+)["\']', src)
    if not m:
        raise _fail('找不到 #main-audio 的 src，请用 --audio 显式指定旁白音频')
    return m.group(1)


def _view_box(src: str) -> tuple[float, float]:
    m = re.search(r'<svg[^>]*viewBox=["\']0\s+0\s+([\d.]+)\s+([\d.]+)["\']', src)
    if m:
        return float(m.group(1)), float(m.group(2))
    print('[note] 未解析到舞台 viewBox，按 1000x460 兜底')
    return 1000.0, 460.0


def _media_dur(path: Path) -> float:
    out = subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(path)], text=True).strip()
    return float(out)


def main() -> int:
    ap = argparse.ArgumentParser(description='课件 → 线性 MP4（逐句定格 + 按时长拼接 + 旁白混音）')
    ap.add_argument('page', help='课件目录或 index.html')
    ap.add_argument('-o', '--output', help='默认 <课件目录名>.mp4，写在课件目录同级')
    ap.add_argument('--audio', help='旁白音频，默认取页面 #main-audio 的 src')
    ap.add_argument('--chrome', help='Chrome/Edge 可执行文件路径，默认自动探测')
    ap.add_argument('--width', type=int, default=1000, help='舞台 SVG Capture 宽度像素（默认 1000）')
    ap.add_argument('--scale', type=int, default=2, help='设备缩放系数，2 即输出约 2x 分辨率')
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--crf', type=int, default=20, help='x264 质量，越小越清晰（默认 20）')
    ap.add_argument('--keep', action='store_true', help='保留逐帧 PNG 与截图页副本，供排查')
    args = ap.parse_args()

    page = Path(args.page)
    page_dir = page if page.is_dir() else page.parent
    html_path = page_dir / 'index.html'
    if not html_path.exists():
        raise _fail(f'找不到页面：{html_path}')
    src = io.open(html_path, encoding='utf-8').read()

    chrome = args.chrome or _find_chrome()
    if not chrome:
        raise _fail('未找到 Chrome/Edge，无法逐句截图（可用 --chrome 指定路径）')
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise _fail(f'缺少 {tool}，无法编码视频')

    audio = Path(args.audio) if args.audio else page_dir / _audio_src(src)
    if not audio.exists():
        raise _fail(f'旁白音频不存在：{audio}')
    wav_dur = _media_dur(audio)

    sentences = _timeline_sentences(src)
    vw, vh = _view_box(src)
    win_w = int(args.width + STAGE_PAD * 2)
    win_h = int(round(args.width * vh / vw + STAGE_PAD * 2))

    out = Path(args.output) if args.output else page_dir.parent / f'{page_dir.name}.mp4'
    tmp = Path(tempfile.mkdtemp(prefix='courseware-video-'))
    frames = tmp / 'frames'
    frames.mkdir()
    shot_html = page_dir / '_shot.html'
    rc = 0
    try:
        shot_html.write_text(src.replace('</body>', SNIPPET % {'pad': STAGE_PAD}, 1),
                             encoding='utf-8')
        lines = []
        for i, (start, dur) in enumerate(sentences):
            png = frames / f'shot_{i:03d}.png'
            t_cap = start + dur - EPS
            url = shot_html.resolve().as_uri() + f'?shot={t_cap:.3f}'
            r = subprocess.run([
                chrome, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
                '--hide-scrollbars', f'--force-device-scale-factor={args.scale}',
                f'--window-size={win_w},{win_h}', '--virtual-time-budget=4000',
                f'--screenshot={png}', url], capture_output=True, timeout=120)
            if r.returncode != 0 or not png.exists():
                err = (r.stderr or b'').decode('utf-8', 'ignore')[-400:]
                raise _fail(f'第 {i} 帧截图失败 rc={r.returncode} {err}')
            nxt = sentences[i + 1][0] if i + 1 < len(sentences) else wav_dur
            show = max(nxt - start, 0.5)
            lines.append(f"file '{png.as_posix()}'\nduration {show:.3f}")
            print(f'[frame {i + 1:2d}/{len(sentences)}] t={t_cap:7.2f} show={show:6.3f}s',
                  flush=True)
        lines.append(f"file '{(frames / f'shot_{len(sentences) - 1:03d}.png').as_posix()}'")
        listf = tmp / 'list.txt'
        listf.write_text('\n'.join(lines), encoding='utf-8')

        r = subprocess.run([
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', str(listf),
            '-i', str(audio),
            '-c:v', 'libx264', '-crf', str(args.crf), '-preset', 'medium',
            '-r', str(args.fps), '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
            '-shortest', str(out)], capture_output=True, timeout=1800)
        if r.returncode != 0:
            err = (r.stderr or b'').decode('utf-8', 'ignore')[-1200:]
            raise _fail(f'ffmpeg 编码失败：{err}')
        print(f'[done] {out}  {_media_dur(out):.1f}s  '
              f'{out.stat().st_size / 1e6:.1f} MB  '
              f'{win_w * args.scale}x{win_h * args.scale}')
    finally:
        shot_html.unlink(missing_ok=True)
        if args.keep:
            print(f'[keep] 帧与截图页保留在 {tmp}')
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return rc


if __name__ == '__main__':
    setup_stdio()
    sys.exit(main())
