#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Courseware Studio 交付检查器。

它保留两类真正有价值的校验：

1. 静态检查：时间轴、逐句旁白、字幕挂点、场景边界、重复字幕风险。
2. 浏览器冒烟：真实 DOM 中逐句核对字幕，并自动走一遍门禁错误/正确路径，
   同时捕获 JS error / unhandled rejection。

它不是 SelfTest，也不维护第二套播放引擎。页面自己的 audio.currentTime / tick()
仍是唯一时间源；本脚本只观察页面并主动推进 currentTime。

用法：
    python scripts/check_gates.py <页面目录或 index.html>
    python scripts/check_gates.py <页面目录或 index.html> --no-browser
    python scripts/check_gates.py <页面目录或 index.html> --keep
"""

from __future__ import annotations

import argparse
import difflib
import html
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


_CAP_LEN_MIN = 10
_CAP_SIM_MIN = 0.75
_CAP_PUNCT = set("，。！？、：；“”‘’「」『』·→—…（）() .,!?;:\"'…")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cap_norm(text: str) -> str:
    for c in _CAP_PUNCT:
        text = text.replace(c, "")
    return text.strip()


def _cap_extract_bodies(src: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for m in re.finditer(r"function\s+(render\w+)\s*\(\s*step\s*\)\s*\{", src):
        name = m.group(1)
        i = m.end() - 1
        depth = 0
        j = i
        while j < len(src):
            c = src[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    bodies[name] = src[m.end() : j]
                    break
            j += 1
    return bodies


def _cap_literals(body: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"(?<!function )(txt|badge)\s*\(", body):
        i = m.end() - 1
        depth = 0
        j = i
        while j < len(body):
            c = body[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    call = body[m.end() : j]
                    strs = re.findall(r"['\"]([^'\"]*)['\"]", call)
                    if m.group(1) == "txt":
                        if strs:
                            out.append(strs[-1])
                    else:
                        txts = [s for s in strs if not s.startswith("#")]
                        if txts:
                            out.append(txts[-1])
                    break
            j += 1
    return [x.strip() for x in out if x.strip()]


def _timeline_from_html(src: str) -> tuple[dict[str, Any] | None, str | None]:
    m = re.search(
        r'<script\b[^>]*\bid=["\']lesson-timeline["\'][^>]*>(.*?)</script>',
        src,
        re.S | re.I,
    )
    if not m:
        return None, '找不到 #lesson-timeline'
    raw = html.unescape(m.group(1)).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f'#lesson-timeline 不是合法 JSON：{exc}'
    if not isinstance(data, dict):
        return None, '#lesson-timeline 顶层必须是对象'
    if not isinstance(data.get('scenes'), list) or not data['scenes']:
        return None, '#lesson-timeline.scenes 必须是非空数组'
    return data, None


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _render_map(src: str) -> dict[str, str]:
    """读取模板约定的 RENDER = {sceneId: renderFn} 映射。"""
    m = re.search(r"(?:var|let|const)\s+RENDER\s*=\s*\{(.*?)\}\s*;", src, re.S)
    if not m:
        return {}
    return {k: v for k, v in re.findall(r"[\"']([^\"']+)[\"']\s*:\s*(render\w+|function\s*\()", m.group(1))}


def _gate_scene_counts(src: str) -> dict[str, int]:
    m = re.search(r"(?:var|let|const)\s+GATES\s*=\s*\[(.*?)\];", src, re.S)
    if not m:
        return {}
    counts: dict[str, int] = {}
    for sid in re.findall(r"scene\s*:\s*[\"']([^\"']+)[\"']", m.group(1)):
        counts[sid] = counts.get(sid, 0) + 1
    return counts


def static_check(src: str, *, allow_degraded: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {"scenes": 0, "sentences": 0}

    timeline, timeline_err = _timeline_from_html(src)
    if timeline_err:
        errors.append(timeline_err)
        return {"ok": False, "errors": errors, "warnings": warnings, "stats": stats, "timeline": None}

    scenes = timeline["scenes"]
    stats["scenes"] = len(scenes)
    if not re.search(r'<audio\b[^>]*\bid=["\']main-audio["\']', src, re.I):
        errors.append('找不到 #main-audio')
    if not re.search(r'(?:id=["\']cap-text["\'][^>]*data-courseware-caption|data-courseware-caption[^>]*id=["\']cap-text["\'])', src, re.I):
        errors.append('找不到带 data-courseware-caption 的 #cap-text')
    audio_tag = re.search(r'<audio\b[^>]*\bid=["\']main-audio["\'][^>]*>', src, re.I)
    if not audio_tag:
        audio_match = None
    else:
        audio_match = re.search(r'\bsrc=["\']([^"\']+)["\']', audio_tag.group(0), re.I)
    if not audio_match:
        errors.append('#main-audio 缺少 src')
    elif audio_match.group(1).replace('\\', '/') != 'audio/combined.wav':
        errors.append('最终音频必须精确引用 audio/combined.wav；不要引用外部 URL、父目录或其它同名文件')

    has_synth_failed = bool(re.search(r'"synth_failed"\s*:\s*true', src))
    if has_synth_failed and not allow_degraded:
        errors.append('时间轴包含 synth_failed=true；默认不接受降级成片，修复 TTS 失败或显式使用 --allow-degraded')

    seen_ids: set[str] = set()
    prev_start: float | None = None
    prev_end: float | None = None
    all_sentences: list[tuple[str, int, dict[str, Any]]] = []

    for si, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            errors.append(f'场景 {si + 1} 不是对象')
            continue
        sid = str(scene.get('step_id', '')).strip()
        runtime = scene.get('runtime') or {}
        if not sid:
            errors.append(f'场景 {si + 1} 缺少 step_id')
        elif sid in seen_ids:
            errors.append(f'重复 step_id：{sid}')
        seen_ids.add(sid)

        start, duration, end = runtime.get('start'), runtime.get('duration'), runtime.get('end')
        if not all(_finite_number(v) for v in (start, duration, end)):
            errors.append(f'{sid or si + 1}: runtime.start/duration/end 必须是有限数字')
            continue
        start, duration, end = float(start), float(duration), float(end)
        if duration <= 0:
            errors.append(f'{sid}: runtime.duration 必须 > 0')
        if end < start:
            errors.append(f'{sid}: runtime.end 小于 start')
        if abs((start + duration) - end) > 0.12:
            warnings.append(f'{sid}: start + duration 与 end 相差 {abs(start + duration - end):.3f}s')
        if prev_start is not None and start < prev_start:
            errors.append(f'{sid}: 场景起点没有按时间递增')
        if prev_end is not None and start < prev_end - 0.02:
            errors.append(f'{sid}: 场景与上一场重叠 {prev_end - start:.3f}s')
        prev_start, prev_end = start, end

        narration = runtime.get('narration') or []
        if not isinstance(narration, list) or not narration:
            errors.append(f'{sid}: 缺少 narration')
            continue
        last_sent_end: float | None = None
        for ni, sentence in enumerate(narration):
            if not isinstance(sentence, dict):
                errors.append(f'{sid}#{ni}: 旁白条目不是对象')
                continue
            text = str(sentence.get('text', '')).strip()
            s_start, s_dur = sentence.get('start'), sentence.get('duration')
            if not text:
                errors.append(f'{sid}#{ni}: text 为空')
            if not (_finite_number(s_start) and _finite_number(s_dur)):
                errors.append(f'{sid}#{ni}: start/duration 必须是有限数字')
                continue
            s_start, s_dur = float(s_start), float(s_dur)
            s_end = s_start + s_dur
            if s_dur <= 0:
                errors.append(f'{sid}#{ni}: duration 必须 > 0')
            if s_start < start - 0.12 or s_end > end + 0.12:
                errors.append(f'{sid}#{ni}: 旁白区间超出场景 [{start:.3f}, {end:.3f}]')
            if last_sent_end is not None and s_start < last_sent_end - 0.02:
                errors.append(f'{sid}#{ni}: 与上一句旁白重叠 {last_sent_end - s_start:.3f}s')
            last_sent_end = s_end
            stats["sentences"] += 1
            all_sentences.append((sid, ni, {**sentence, "start": s_start, "duration": s_dur, "end": s_end}))

        if narration and abs((float(narration[-1].get('start', end)) + float(narration[-1].get('duration', 0))) - end) > 0.25:
            warnings.append(f'{sid}: 最后一句旁白结束点与 scene.end 相差较大')

    # 每个 scene 必须有 renderer；缺失时页面会出现“音频/字幕继续、画面静止”的真失败。
    render_map = _render_map(src)
    bodies = _cap_extract_bodies(src)
    for scene in scenes:
        sid = str(scene.get('step_id', '')).strip()
        fn = render_map.get(sid)
        if not fn:
            errors.append(f'{sid}: RENDER 中缺少对应 renderer')
        elif fn != 'function(' and fn not in bodies:
            errors.append(f'{sid}: renderer {fn} 没有找到 function 实现')

    # 一个 scene 最多挂一个 gate；当前 runtime 的 gate-host 是单槽位，不允许静默覆盖。
    gate_counts = _gate_scene_counts(src)
    scene_set = {str(scene.get('step_id', '')).strip() for scene in scenes}
    for sid, count in gate_counts.items():
        if count > 1:
            errors.append(f'{sid}: 同一 scene 配了 {count} 个 gate，但 runtime 只支持一个')
        if sid not in scene_set:
            errors.append(f'{sid}: GATES 引用了时间轴不存在的 scene')

    # Renderer literals vs sentence text: warning only.
    # Template convention: function names usually contain the scene key elsewhere.
    for scene_id, _, sentence in all_sentences:
        render_fn = None
        mm = re.search(r'["\']' + re.escape(scene_id) + r'["\']\s*:\s*(render\w+)', src)
        if mm:
            render_fn = mm.group(1)
        body = bodies.get(render_fn or '')
        if not body:
            continue
        cap = _cap_norm(str(sentence.get('text', '')))
        if len(cap) < _CAP_LEN_MIN:
            continue
        for literal in _cap_literals(body):
            lit = _cap_norm(literal)
            if len(lit) < _CAP_LEN_MIN:
                continue
            ratio = difflib.SequenceMatcher(None, cap, lit).ratio()
            if ratio >= _CAP_SIM_MIN:
                warnings.append(f'{scene_id}: 画面文字与旁白高度相似（{ratio:.0%}）：“{literal}”')
                break

    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats, "timeline": timeline}


PROBE_STUB = r'''
<script>
(() => {
  let t = 0;
  window.__coursewareCheckSetTime = v => { t = Number(v) || 0; };
  Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {
    configurable: true,
    get(){ return t; },
    set(v){ t = Number(v) || 0; }
  });
  HTMLMediaElement.prototype.play = function(){ return Promise.resolve(); };
  HTMLMediaElement.prototype.pause = function(){};
  try {
    Object.defineProperty(document, 'visibilityState', {configurable:true, get(){return 'visible';}});
    Object.defineProperty(document, 'hidden', {configurable:true, get(){return false;}});
  } catch(e) {}
})();
</script>
'''


PROBE_DRIVER = r'''
<pre id="courseware-check-report">running</pre>
<script>
(() => {
  const report = {ok:false, errors:[], failures:[], warnings:[], gates:[], captionChecks:0};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const q = s => document.querySelector(s);
  const qa = s => Array.from(document.querySelectorAll(s));
  const esc = v => String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const out = q('#courseware-check-report');
  const timeline = JSON.parse(q('#lesson-timeline').textContent);
  const scenes = timeline.scenes || [];
  const audio = q('#main-audio');
  const gate = q('#gate');
  let prevErrorHandler = window.onerror;
  window.onerror = (m,s,l,c) => { report.errors.push(`JSERR ${m} @${l}:${c}`); };
  window.addEventListener('unhandledrejection', e => {
    report.errors.push('UNHANDLED ' + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
  });

  function fire(t){
    window.__coursewareCheckSetTime(t);
    audio.dispatchEvent(new Event('timeupdate'));
  }
  function currentCard(){
    const n = gate.querySelector('[data-interaction]:not([hidden])');
    if (!n) return null;
    return {el:n, kind:n.dataset.interactionType || 'choice'};
  }
  function lockOf(card){ return card && card.el.dataset.locked === '1'; }
  function feedback(card){
    const n = card && card.el.querySelector('.interaction-feedback');
    return n ? n.textContent.trim() : '';
  }
  function tap(el){ if(el) el.click(); }
  function gestureTap(el){
    if(!el) return;
    const r=el.getBoundingClientRect(), x=r.left+r.width/2, y=r.top+r.height/2;
    if(window.PointerEvent){
      el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,buttons:1,pointerId:19}));
      document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,buttons:0,pointerId:19}));
    }else{
      el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0}));
      document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0}));
    }
  }
  function cfg(card){ try{return JSON.parse(card.el.dataset.interaction || '{}');}catch(e){return {};} }

  function attempt(card, correct){
    if (!card) return false;
    const kind = card.kind, c = cfg(card);
    if (kind === 'choice' || kind === 'hotspot'){
      const opts = c.options || c.choices || [];
      const pick = opts.find(o => correct ? o.correct === true : o.correct !== true);
      if (!pick) return false;
      const sel = kind === 'hotspot' ? `[data-hotspot-id="${CSS.escape(String(pick.id))}"]` : `[data-choice-id="${CSS.escape(String(pick.id))}"]`;
      const node = card.el.querySelector(sel);
      if (!node) return false;
      tap(node); return true;
    }
    if (kind === 'sequence'){
      const list = card.el.querySelector('.sequence-list');
      const order = c.correct_order || c.answer || [];
      if (!list || !order.length) return false;
      const ids = correct ? order.slice() : order.slice().reverse();
      const map = {};
      Array.from(list.children).forEach(li => { map[li.dataset.sequenceId] = li; });
      ids.forEach(id => { if(map[id]) list.appendChild(map[id]); });
      const submit = card.el.querySelector('[data-sequence-submit]');
      if (!submit) return false;
      tap(submit); return true;
    }
    if (kind === 'bucket'){
      const answer = c.answer || {}, ids = Object.keys(answer);
      if (!ids.length) return false;
      const tray = document.querySelector('.bucket-tray');
      if (!tray) return false;
      // 没有任何可用的错误目标时，不能把正确答案伪装成“错答”提交。
      const allBuckets = qa('[data-drop][data-bucket-id]').map(n => String(n.dataset.bucketId));
      const wrongBucket = allBuckets.find(id => id !== String(answer[ids[0]]));
      if (!correct && !wrongBucket) return false;
      ids.forEach(id => { const item=document.querySelector(`.bucket-item[data-bucket-item="${CSS.escape(id)}"]`); if(item) tray.appendChild(item); });
      ids.forEach((id, i) => {
        let bucket = String(answer[id]);
        if (!correct && i === 0) bucket = wrongBucket;
        const item = document.querySelector(`.bucket-item[data-bucket-item="${CSS.escape(id)}"]`);
        const box = document.querySelector(`.bucket-drop[data-bucket-id="${CSS.escape(bucket)}"]`);
        if(item && box){ gestureTap(item); tap(box); }
      });
      const submit = card.el.querySelector('[data-bucket-submit]');
      if (!submit) return false;
      tap(submit); return true;
    }
    return false;
  }

  async function waitGateReveal(){
    for(let i=0;i<20;i++){
      if(!q('#pregate').hidden) await sleep(80);
      if(!q('#pregate').hidden){ await sleep(120); continue; }
      if(!gate.hidden) return true;
      await sleep(60);
    }
    return !gate.hidden;
  }

  async function testGate(time, sentence){
    const card = currentCard();
    if (!card) return false;
    const sceneId = q('#gate-host').dataset.stepId || '';
    if (!sceneId) report.failures.push('门禁缺少 gate-host.dataset.stepId');
    if (sentence){
      const frac = (time - sentence.start) / sentence.duration;
      if (frac > 0.08 && frac < 0.92) report.failures.push(`门禁落在句中间：${sceneId} ${Math.round(frac*100)}%`);
    }
    const before = gate.hidden;
    const kind = card.kind;
    // sequence / bucket 的 QA 不能只直接改 DOM：这会绕过 runtime 的真实手势接线。
    // runtime 在每个可拖拽条目上写 data-gesture="1"，以此确认动态生成的题目已重新接线。
    if (kind === 'sequence' || kind === 'bucket'){
      const items = qa(kind === 'sequence' ? '.sequence-item' : '.bucket-item')
        .filter(n => card.el.contains(n));
      if (!items.length) report.failures.push(`${kind} 缺少可交互条目`);
      items.forEach((item, i) => {
        if (item.dataset.gesture !== '1') report.failures.push(`${kind} 条目未接入手势：${i + 1}`);
      });
    }
    const wrongDid = attempt(card, false);
    if (wrongDid){
      await sleep(100);
      if (lockOf(card)) report.failures.push(`错答后仍锁定：${kind}`);
      if (!q('#gate-next').hidden) report.failures.push(`错答后出现继续按钮：${kind}`);
    } else {
      report.warnings.push(`门禁 ${kind} 未能构造错误路径，仅检查正确路径`);
    }
    await sleep(100);
    const rightDid = attempt(card, true);
    if (!rightDid){ report.failures.push(`无法构造正确路径：${kind}`); return true; }
    await sleep(120);
    if (!lockOf(card)) report.failures.push(`答对后未锁定：${kind}`);
    if (q('#gate-next').hidden) report.failures.push(`答对后没有继续按钮：${kind}`);
    report.gates.push({scene:sceneId, kind, time, wrongTested:wrongDid, correctTested:rightDid, wasHidden:before});
    return true;
  }

  async function run(){
    if(!audio || !q('#lesson-timeline') || !q('#cap-text')){
      report.failures.push('缺少 audio / timeline / caption 节点');
    }
    const caption = q('#cap-text');

    // 先把所有门禁走完。这样后面的字幕逐句核对不会被门禁的 preGate 回退干扰。
    const candidates = [];
    scenes.forEach(scene => {
      candidates.push(Number(scene.runtime.start));
      const ns = scene.runtime.narration || [];
      if(ns.length) candidates.push(Number(ns[ns.length-1].start) + Number(ns[ns.length-1].duration));
    });
    const seenGateScenes = new Set();
    for(const time of candidates){
      fire(time);
      await sleep(15);
      if(gate.hidden) continue;
      if(!await waitGateReveal()) continue;
      const sid=q('#gate-host').dataset.stepId || '';
      if(seenGateScenes.has(sid)) continue;
      const scene=scenes.find(x=>x.step_id===sid);
      const nlist=scene ? scene.runtime.narration || [] : [];
      let probeSentence=null;
      for(const n of nlist){ if(time >= n.start && time < n.start+n.duration){probeSentence=n;break;} }
      await testGate(time, probeSentence);
      seenGateScenes.add(sid);
      const go=q('#gate-go'); if(go && !go.disabled) go.click();
      await sleep(260);
    }

    // 字幕核对：每句取中点，要求画布字幕逐字等于时间轴原文。
    for(const scene of scenes){
      const ns = scene.runtime && scene.runtime.narration || [];
      for(let i=0;i<ns.length;i++){
        const n = ns[i];
        const mid = n.start + Math.min(Math.max(n.duration * 0.5, 0.05), Math.max(n.duration - 0.05, 0.05));
        fire(mid);
        await sleep(0);
        const got = (caption.textContent || '').trim();
        if(got !== String(n.text || '').trim()){
          report.failures.push(`字幕不一致：${scene.step_id}#${i} 期望“${n.text}” 实际“${got}”`);
        }
        report.captionChecks += 1;
      }
    }

    // 场景之间的短空档不能把上一句字幕抹掉，否则换场时会闪白。
    for(let i=0;i<scenes.length-1;i++){
      const a=scenes[i], b=scenes[i+1];
      const ns=a.runtime.narration || [];
      if(!ns.length) continue;
      const end=Number(ns[ns.length-1].start)+Number(ns[ns.length-1].duration);
      const gap=Number(b.runtime.start)-end;
      if(gap > 0.08){
        const probe=end+Math.min(0.05, gap/2);
        fire(probe);
        await sleep(0);
        const got=(caption.textContent||'').trim();
        const expected=String(ns[ns.length-1].text||'').trim();
        if(got !== expected) report.failures.push(`换场空档字幕被清掉：${a.step_id}`);
      }
    }

    report.ok = report.errors.length===0 && report.failures.length===0;
    out.textContent = JSON.stringify(report);
    document.title = `courseware-check ${report.ok ? 'PASS' : 'FAIL'}`;
  }
  run().catch(e => {
    report.errors.push('probe exception: ' + (e && e.stack ? e.stack : String(e)));
    report.ok = false;
    out.textContent = JSON.stringify(report);
    document.title = 'courseware-check FAIL';
  });
})();
</script>
'''


def _find_chrome() -> str | None:
    cands = [
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def _build_probe(page: Path, out: Path, runtime_src: Path) -> None:
    src = _read(page)
    if 'id="lesson-timeline"' not in src:
        raise SystemExit('[error] 找不到 #lesson-timeline')
    runtime_ref = re.search(r'<script\b[^>]*src=["\']([^"\']*interactive_runtime\.js)["\'][^>]*></script>', src, re.I)
    if runtime_ref:
        rel = runtime_ref.group(1)
        candidate = (page.parent / rel).resolve()
        if not candidate.exists():
            raise FileNotFoundError(f'页面引用的 runtime 不存在：{candidate}')
        runtime = _read(candidate)
        tag = runtime_ref.group(0)
        # probe 永远 inline 页面实际使用的 runtime，避免临时目录改变相对 src 后悄悄变成 404。
        src = src.replace(tag, '<script>\n' + runtime + '\n</script>', 1)
    src = re.sub(r'(<script\b[^>]*\bid=["\']lesson-timeline["\'][^>]*>)', PROBE_STUB + r'\n\1', src, count=1, flags=re.I)
    if '</body>' in src:
        src = src.replace('</body>', PROBE_DRIVER + '</body>', 1)
    else:
        src += PROBE_DRIVER
    out.write_text(src, encoding='utf-8')



def _run_chrome(cmd: list[str], stdout_path: Path, stderr_path: Path, timeout: float) -> int:
    """启动浏览器并提供硬超时；超时后杀整组进程，避免留下 zygote/renderer。"""
    with stdout_path.open('w', encoding='utf-8', errors='replace') as fo, stderr_path.open('w', encoding='utf-8', errors='replace') as fe:
        kwargs = {}
        if os.name == 'posix':
            kwargs['start_new_session'] = True
        proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, **kwargs)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name == 'posix':
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            raise


def _browser_preflight(chrome: str) -> bool:
    """确认本机 headless Chrome 真能完成一个最小 dump-dom。"""
    root = Path(tempfile.mkdtemp(prefix='courseware-browser-preflight-'))
    try:
        page = root / 'probe.html'
        out = root / 'dom.html'
        err = root / 'chrome.log'
        page.write_text('<!doctype html><html><body>courseware-preflight-ok</body></html>', encoding='utf-8')
        cmd = [chrome, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
               '--no-default-browser-check', '--disable-dev-shm-usage', '--allow-file-access-from-files',
               f'--user-data-dir={root / "profile"}', '--virtual-time-budget=500', '--dump-dom',
               'file:///' + str(page).replace('\\','/')]
        try:
            rc = _run_chrome(cmd, out, err, timeout=5)
        except subprocess.TimeoutExpired:
            return False
        if rc != 0 or not out.exists():
            return False
        return 'courseware-preflight-ok' in out.read_text(encoding='utf-8', errors='replace')
    finally:
        shutil.rmtree(root, ignore_errors=True)

def browser_check(page: Path, runtime_src: Path, keep: bool = False) -> dict[str, Any] | None:
    chrome = _find_chrome()
    if not chrome:
        return None
    if not _browser_preflight(chrome):
        return None
    temp_root = Path(tempfile.mkdtemp(prefix='courseware-check-'))
    probe = temp_root / 'probe.html'
    dom = temp_root / 'dom.html'
    log = temp_root / 'chrome.log'
    profile = temp_root / 'profile'
    try:
        _build_probe(page, probe, runtime_src)
    except (OSError, UnicodeError) as exc:
        if not keep:
            shutil.rmtree(temp_root, ignore_errors=True)
        return {"ok": False, "errors": [f"无法构造浏览器检查页：{exc}"], "failures": [], "warnings": [], "stats": {}}
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
        '--no-default-browser-check', '--disable-dev-shm-usage', '--allow-file-access-from-files',
        '--disable-background-timer-throttling', '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding', f'--user-data-dir={profile}',
        '--run-all-compositor-stages-before-draw', '--virtual-time-budget=15000', '--dump-dom',
        'file:///' + str(probe).replace('\\', '/'),
    ]
    try:
        rc = _run_chrome(cmd, dom, log, timeout=30)
        raw = dom.read_text(encoding='utf-8', errors='replace') if dom.exists() else ''
        m = re.search(r'<pre id="courseware-check-report">(.*?)</pre>', raw, re.S)
        if not m:
            tail = log.read_text(encoding='utf-8', errors='replace')[-1000:] if log.exists() else ''
            return {"ok": False, "errors":["浏览器没有返回检查报告"], "failures":[], "warnings":[tail], "stats":{}}
        report_text = html.unescape(m.group(1))
        try:
            report = json.loads(report_text)
        except json.JSONDecodeError:
            return {"ok":False,"errors":["浏览器返回的检查报告不是 JSON"],"failures":[],"warnings":[report_text[:1000]],"stats":{}}
        report.setdefault('browser_rc', rc)
        return report
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": ["浏览器冒烟检查超过 30s，已中止"], "failures": [], "warnings": [], "stats": {}}
    finally:
        if not keep:
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            print(f'[note] 已保留 probe：{probe}')


def main() -> int:
    ap = argparse.ArgumentParser(description='Courseware Studio 交付检查：字幕 + 时间轴 + 门禁 + JS')
    ap.add_argument('page', help='页面目录（含 index.html）或某个 .html 路径')
    ap.add_argument('--no-browser', action='store_true', help='只做静态检查，不启动 Chrome/Edge')
    ap.add_argument('--keep', action='store_true', help='保留浏览器冒烟副本')
    ap.add_argument('--require-browser', action='store_true', help='浏览器冒烟未执行时返回失败（适合 CI）')
    ap.add_argument('--allow-degraded', action='store_true', help='允许时间轴含 synth_failed=true（不建议用于最终交付）')
    args = ap.parse_args()

    page = Path(args.page)
    if page.is_dir():
        page = page / 'index.html'
    page = page.resolve()
    if not page.exists():
        print(f'[error] 找不到页面：{page}')
        return 2

    src = _read(page)
    # 本地课件的主音频也必须真实存在；否则字幕/时间轴再正确，交付仍然是无声页面。
    audio_tag = re.search(r'<audio\b[^>]*\bid=["\']main-audio["\'][^>]*>', src, re.I)
    if audio_tag:
        audio_ref = re.search(r'\bsrc=["\']([^"\']+)["\']', audio_tag.group(0), re.I)
        if audio_ref:
            ref = audio_ref.group(1)
            if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', ref) and not ref.startswith('data:'):
                audio_path = (page.parent / ref).resolve()
                if not audio_path.exists():
                    print(f'[error] main-audio 文件不存在：{audio_path}')
                    return 1
    static = static_check(src, allow_degraded=args.allow_degraded)
    print(f"[static] scenes={static['stats'].get('scenes',0)} sentences={static['stats'].get('sentences',0)}")
    for w in static['warnings']:
        print('[warn] ' + w)
    for e in static['errors']:
        print('[error] ' + e)
    if static['errors']:
        return 1

    if args.no_browser:
        print('[ok] 静态检查通过（已跳过浏览器字幕/门禁冒烟）')
        return 0

    runtime_src = Path(__file__).with_name('interactive_runtime.js')
    report = browser_check(page, runtime_src, keep=args.keep)
    if report is None:
        print('[note] 浏览器冒烟未执行：未找到可用 Chrome/Edge，或 headless 预检未通过。静态检查已通过。')
        return 1 if args.require_browser else 0
    for e in report.get('errors', []):
        print('[error] ' + str(e))
    for f in report.get('failures', []):
        print('[fail] ' + str(f))
    for w in report.get('warnings', []):
        if w:
            print('[warn] ' + str(w))
    print(f"[browser] captions={report.get('captionChecks',0)} gates={len(report.get('gates',[]))}")
    if report.get('ok'):
        print('[ok] 字幕 / 时间轴 / 门禁 / JS 浏览器冒烟通过')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
