#!/usr/bin/env python3
"""Courseware Studio — 把实测时间轴压成页面能直接读的 `<script id="lesson-timeline">`。

职责只有一件：**把 narration.py 产出的时间轴改写成页面可直接内联的形状**，
避免手抄几十个数字出错（手抄是本流水线里最容易出错、也最难发现的一步）。

输入：
    --timing  audio/narration_timing.json   ← 唯一的时间来源（实测值）
    --source  narration-source.json         ← 可选：带出每段的 title / tagline

输出（-o，不给则打到 stdout）：
    <script type="application/json" id="lesson-timeline">{"scenes":[…]}</script>

形状：
    scenes[i] = {"step_id", "content":{"title","tagline"},
                 "runtime":{"start","duration","end",
                            "narration":[{start,duration,text,hl?,speaker?,synth_failed?}]}}

`narration[i].text` 是逐句口播原文，同时也是**画布字幕的唯一来源**——页面不留第二份文案，
所以字幕与旁白在结构上不可能对不上。`--source` 给的 `hl`（结论句序号，从 1 数起）只加一个
`hl:true`，让那一句在字幕带里用主色。

旁白时间是**全局秒**（不做场景内换算）——页面按全局时间切片，直接比 t >= start。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _script_utils import setup_stdio, guard_not_in_skill_dir  # noqa: E402
from _contracts import require_finite_number  # noqa: E402


def _load(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"[error] 无法读取{what}: {e}")


def _content_map(source):
    """从旁白脚本抽出 step_id → {title, tagline, hl}（章节头、目录、字幕强调色要用）。"""
    if not source:
        return {}
    out = {}
    if source.get("opening"):
        out["opening"] = {"title": source.get("opening_title") or source.get("title") or "开场",
                          "tagline": source.get("opening_tagline") or ""}
    # 与 narration.py 的 _load_script_source 用同一套编号：先滤掉空段落再数，
    # 否则讲稿里混着一个空段时，这里的 seg-N 会和音频那边错开一位，
    # 结果是该段的 title / tagline / hl 全部静默丢失。
    segs = [s for s in (source.get("segments") or [])
            if isinstance(s, dict) and ((s.get("text") or "").strip() or s.get("dialogue"))]
    for i, seg in enumerate(segs, 1):
        sid = str(seg.get("id") or f"seg-{i}")
        out[sid] = {"title": seg.get("title") or sid,
                    "tagline": seg.get("tagline") or "",
                    "hl": seg.get("hl")}
    if source.get("closing"):
        out["closing"] = {"title": source.get("closing_title") or "小结",
                          "tagline": source.get("closing_tagline") or ""}
    return out


def _hl_indices(raw, sid, n):
    """`hl` = 这一段里**结论句的句序号，从 1 数起**（作者写"第 3 句是结论"最自然）。

    越界 / 非数字**只告警，不报错**：改稿后句序会变，而一个颜色标错不该挡住整条流水线——
    它只影响那一句字幕的强调色，与文案、时长、画面步数都无关。
    """
    out = set()
    if raw is None:
        return out
    if not isinstance(raw, list):
        print(f"[warn] {sid} 的 hl 必须是数组（收到 {raw!r}），已忽略", file=sys.stderr)
        return out
    for v in raw:
        try:
            i = int(v)
        except (TypeError, ValueError):
            print(f"[warn] {sid} 的 hl 里 {v!r} 不是句序号，已忽略", file=sys.stderr)
            continue
        if 1 <= i <= n:
            out.add(i)
        else:
            print(f"[warn] {sid} 的 hl 指向第 {i} 句，但这一段只有 {n} 句（已忽略）",
                  file=sys.stderr)
    return out


def build(timing, content):
    raw = timing.get("scenes") or []
    if not isinstance(raw, list) or not raw:
        raise SystemExit("[error] 时间轴里没有任何场景——检查 narration.py 是否成功产出音频")

    scenes = []
    seen_ids = set()
    prev_start = None
    prev_end = None
    for index, sc in enumerate(raw, 1):
        if not isinstance(sc, dict):
            raise SystemExit(f"[error] scenes[{index}] 必须是对象")
        sid = str(sc.get("step_id") or sc.get("scene_id") or "").strip()
        if not sid:
            raise SystemExit(f"[error] scenes[{index}] 缺少 step_id")
        if sid in seen_ids:
            raise SystemExit(f"[error] 重复 step_id：{sid}")
        seen_ids.add(sid)

        sentences = sc.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            raise SystemExit(f"[error] 场景 {sid} 没有句子")
        try:
            start = float(require_finite_number(sc.get("start"), f"{sid}.start", nonnegative=True))
            duration = float(require_finite_number(sc.get("duration"), f"{sid}.duration", positive=True))
            end = float(require_finite_number(sc.get("end"), f"{sid}.end", nonnegative=True))
        except ValueError as exc:
            raise SystemExit(f"[error] {exc}")
        if abs((start + duration) - end) > 0.12:
            raise SystemExit(f"[error] {sid}: start + duration 与 end 相差 {abs(start + duration - end):.3f}s")
        if prev_start is not None and start < prev_start - 0.02:
            raise SystemExit(f"[error] {sid}: 场景起点没有按时间递增")
        if prev_end is not None and start < prev_end - 0.02:
            raise SystemExit(f"[error] {sid}: 场景与上一场重叠 {prev_end - start:.3f}s")

        meta = content.get(sid) or {}
        hl = _hl_indices(meta.get("hl"), sid, len(sentences))
        narration = []
        last_sent_end = None
        for ni, sent in enumerate(sentences, 1):
            if not isinstance(sent, dict):
                raise SystemExit(f"[error] {sid}#{ni}: 旁白条目必须是对象")
            text = str(sent.get("text") or "").strip()
            if not text:
                raise SystemExit(f"[error] {sid}#{ni}: text 为空")
            try:
                s_start = float(require_finite_number(sent.get("start"), f"{sid}#{ni}.start", nonnegative=True))
                s_dur = float(require_finite_number(sent.get("duration"), f"{sid}#{ni}.duration", positive=True))
            except ValueError as exc:
                raise SystemExit(f"[error] {exc}")
            s_end = s_start + s_dur
            if s_start < start - 0.12 or s_end > end + 0.12:
                raise SystemExit(f"[error] {sid}#{ni}: 旁白区间超出场景 [{start:.3f}, {end:.3f}]")
            if last_sent_end is not None and s_start < last_sent_end - 0.02:
                raise SystemExit(f"[error] {sid}#{ni}: 与上一句旁白重叠 {last_sent_end - s_start:.3f}s")
            last_sent_end = s_end
            item = {"start": round(s_start, 3), "duration": round(s_dur, 3), "text": text}
            if sent.get("speaker"):
                item["speaker"] = str(sent["speaker"])
            if sent.get("synth_failed"):
                item["synth_failed"] = True
            if ni in hl:
                item["hl"] = True
            narration.append(item)

        sentences_end = last_sent_end
        if sentences_end is not None and abs(sentences_end - end) > 0.25:
            raise SystemExit(f"[error] {sid}: 最后一句旁白结束点与 scene.end 相差 {abs(sentences_end-end):.3f}s")

        scenes.append({
            "step_id": sid,
            "content": {"title": meta.get("title") or sc.get("title") or sid,
                        "tagline": meta.get("tagline") or ""},
            "runtime": {
                "start": round(start, 3),
                "duration": round(duration, 3),
                "end": round(end, 3),
                "narration": narration,
            },
        })
        prev_start, prev_end = start, end

    return scenes


def report(scenes, total, stream=sys.stdout):
    """输出构建诊断；--bare 时走 stderr，保证 stdout 是纯 JSON。"""
    print(f"[scenes] {len(scenes)} 个场景，总时长 {total:.2f}s", file=stream)
    print(f"  {'id':<10} {'start':>8} {'end':>8} {'句数':>4}  标题", file=stream)
    for sc in scenes:
        r = sc["runtime"]
        print(f"  {sc['step_id']:<10} {r['start']:>8.3f} {r['end']:>8.3f} "
              f"{len(r['narration']):>4}  {sc['content']['title']}", file=stream)
    print("  ↑ 「句数」= 对应渲染器的分支数，逐句对一遍（对不上后半段画面会静止）", file=stream)
    marks = []
    for sc in scenes:
        k = sum(1 for s in sc["runtime"]["narration"] if s.get("hl"))
        if k:
            marks.append(f"{sc['step_id']}×{k}")
    print("  字幕强调句（hl）：" + ("、".join(marks) if marks else "无"), file=stream)
    for a, b in zip(scenes, scenes[1:]):
        gap = b["runtime"]["start"] - a["runtime"]["end"]
        if gap < -0.001:
            print(f"  [warn] {a['step_id']} 与 {b['step_id']} 时间重叠 {gap:+.3f}s", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description="把 narration_timing.json 压成可内联的时间轴脚本块")
    p.add_argument("--timing", required=True, help="narration.py 产出的 narration_timing.json")
    p.add_argument("--source", default=None, help="可选：旁白脚本，用来带出 title / tagline")
    p.add_argument("-o", "--output", default=None, help="输出文件（不给则打到 stdout）")
    p.add_argument("--bare", action="store_true",
                   help="只输出裸 JSON，不套 <script> 标签")
    args = p.parse_args()
    setup_stdio()   # Windows 重定向下 stdout 非 UTF-8：下面要打中文章节标题
    # 产物守卫：-o 是相对 CWD 解析的，从技能目录照抄示例命令会把时间轴块
    # 直接写进技能仓库（其余写盘入口都有同一道拦截）。
    if args.output:
        guard_not_in_skill_dir(("-o/--output", os.path.abspath(args.output)),
                               tip="请用 -o/--output 指定技能目录之外的绝对路径，"
                                   "或去掉 -o 直接把结果打到 stdout。")

    timing = _load(args.timing, "时间轴")
    source = _load(args.source, "旁白脚本") if args.source else None
    scenes = build(timing, _content_map(source))

    payload = json.dumps({"scenes": scenes}, ensure_ascii=False, separators=(",", ":"))
    # 页面里是 <script type="application/json">：正文若含 "</" 会提前闭合标签
    payload = payload.replace("</", "<\\/")
    text = payload if args.bare else \
        f'<script type="application/json" id="lesson-timeline">{payload}</script>\n'

    total = float(timing.get("total_duration") or scenes[-1]["runtime"]["end"])
    # --bare 是机器接口：stdout 必须只有 JSON。诊断信息统一走 stderr。
    if args.bare:
        report(scenes, total, stream=sys.stderr)
        if args.output:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            print(f"[out] {args.output}  ({len(text)} chars)", file=sys.stderr)
        else:
            print(text)
        return

    report(scenes, total)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"[out] {args.output}  ({len(text)} chars)")
    else:
        print(text)


if __name__ == "__main__":
    main()
