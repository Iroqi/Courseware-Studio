#!/usr/bin/env python3
"""Courseware Studio — TTS 能力适配器：旁白脚本 → 音频 + 时间轴。

职责只有三件：**把文本念出来、拼成一条音轨、给出每句的起止时间。**
页面结构、配色、布局、是否播放、怎么播放都由 Agent 在自己的 HTML 里决定——
本脚本不规定页面长什么样，也不持有任何"页面应该长这样"的假设。

输入（旁白脚本，独立于 Lesson IR，不经任何上游编译）：

    普通段落：
    {
      "title": "主题",
      "opening": "开场白。",
      "segments": [
        {"id": "seg-1", "title": "小节名", "text": "这一节要念的话。",
         "hl": [2],
         "voice_id": "冰糖", "speed": 1.2}
      ]
    }

    `text` 分句后就地成为字幕与时间轴；`hl` 是结论句的句序号（从 1 数起），
    只由 build_timeline 透传给页面用作强调色。`speed` 默认 1.0（原速），逐段可覆盖。

    多人对话：顶层提供 speakers，段落上用 dialogue（每一轮单独分句，各自用各自音色）。

输出（写到 -o 目录）：

    combined.wav            整段音轨
    narration_timing.json   逐句起止时间（内联进 HTML 用；file:// 下不能 fetch）
"""
import argparse
import base64
import concurrent.futures
import hashlib
import ntpath
import os
import random
import sys
import time
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _audio import (apply_loudnorm, apply_speed, concat_audio,  # noqa: E402
                    generate_silence, get_ffmpeg, measure_duration, mix_bgm,
                    _remove_quiet)
from _contracts import (DEFAULT_CHARS_PER_SEC, DEFAULT_GAP, DEFAULT_SPEED,  # noqa: E402
                        OPENING_CLOSING_DEFAULT_SPEED, estimate_sentence_seconds,
                        list_voice_ids, require_finite_number, validate_speed)
from _env import get_key, resolve_model_config  # noqa: E402
from _script_utils import (guard_not_in_skill_dir, setup_stdio,  # noqa: E402
                           split_sentences, write_json_atomic)

# 超长句提醒阈值：写稿时一句一口气念得完最好；超过只 warn 不拦截。
LONG_SENTENCE_CHARS = 45

# 分句预览最多打印多少句（长稿件逐句打印会刷屏，淹没真正的错误信息）。
_PREVIEW_LIMIT = 10


# ===================================================================
# 一、旁白脚本 → 句子列表 + 段落分组
# ===================================================================
@dataclass
class Block:
    """一个待合成段落：sentences 是它分好的句子；turns 非空表示多人对话段落。"""
    id: str
    title: str
    tagline: str
    sentences: List[str]
    extra: Dict = field(default_factory=dict)
    turns: List[Dict] = field(default_factory=list)


def _collect_dialogue_sentences(dialogue, speakers, seg_index, seg_title):
    """把段落的 dialogue（多轮对话）拆成扁平句子 + 段内局部 turns。

    每一轮独立分句（避免"短句被并入下一轮"这类跨轮错位）；turns 记录每轮在本段内
    的句子区间 + 说话人信息（voice_id/voice_style 就近解析，下游不必再查 speakers）。
    """
    sents, turns = [], []
    for j, turn in enumerate(dialogue, 1):
        spk = turn.get("speaker")
        t_text = (turn.get("text") or "").strip()
        if not t_text:
            raise ValueError(f"第 {seg_index} 段（title={seg_title!r}）"
                             f"dialogue[{j}]（speaker={spk!r}）的 'text' 为空")
        t_sents = split_sentences(t_text)
        if not t_sents:
            raise ValueError(f"第 {seg_index} 段（title={seg_title!r}）"
                             f"dialogue[{j}]（speaker={spk!r}）分句后为空，"
                             "请检查文本是否以终止标点（。！？）结尾")
        spk_cfg = (speakers or {}).get(spk, {})
        start = len(sents)
        sents.extend(t_sents)
        turns.append({
            "start": start, "end": len(sents), "speaker": spk,
            "label": spk_cfg.get("label") or spk,
            "voice_id": spk_cfg.get("voice_id"),
            "voice_style": spk_cfg.get("voice_style"),
        })
    return sents, turns


def _collect_blocks(source, default_speed=None):
    """把结构化 source 组装成 Block 列表（opening / segments / closing）。"""
    blocks: List[Block] = []
    # opening/closing 不传显式覆盖时跟随全局 --speed（build_parts 传进来的
    # default_speed），而不是钉死 1.0：整稿调语速时开场/收尾不该掉队。
    # OPENING_CLOSING_DEFAULT_SPEED 只在没有任何全局语速时兜底。
    oc_fallback = (default_speed if default_speed is not None
                   else OPENING_CLOSING_DEFAULT_SPEED)

    def _extra(seg, fallback_speed):
        extra = {}
        speed = seg.get("speed", fallback_speed)
        if speed is not None:
            extra["speed"] = speed
        for key in ("voice_id", "voice_style"):
            if seg.get(key) is not None:
                extra[key] = seg[key]
        return extra

    opening_text = (source.get("opening") or "").strip()
    if opening_text:
        blocks.append(Block(
            id="opening",
            title=source.get("opening_title") or source.get("title") or "本期内容",
            tagline=(source.get("opening_tagline") or "").strip(),
            sentences=split_sentences(opening_text),
            extra={"speed": source.get("opening_speed", oc_fallback)},
        ))

    raw_segments = source.get("segments", [])
    if not raw_segments:
        raise ValueError("source 中 'segments' 为空，至少需要一条内容段落")

    speakers = source.get("speakers") or {}
    for i, seg in enumerate(raw_segments, 1):
        title = seg.get("title", "")
        dialogue = seg.get("dialogue")
        turns = []
        if dialogue:
            sents, turns = _collect_dialogue_sentences(dialogue, speakers, i, title)
        else:
            text = (seg.get("text") or "").strip()
            if not text:
                raise ValueError(f"第 {i} 段（title={title!r}）的 'text' 字段为空")
            sents = split_sentences(text)
            if not sents:
                raise ValueError(f"第 {i} 段（title={title!r}）分句后为空，"
                                 "请检查文本是否以终止标点（。！？）结尾")
        blocks.append(Block(
            id=str(seg.get("id") or f"seg-{i}"),
            title=title,
            tagline=seg.get("tagline", ""),
            sentences=sents,
            extra=_extra(seg, default_speed),
            turns=turns,
        ))

    closing_text = (source.get("closing") or "").strip()
    if closing_text:
        blocks.append(Block(
            id="closing",
            title=source.get("closing_title") or "小结",
            tagline=(source.get("closing_tagline") or "").strip(),
            sentences=split_sentences(closing_text),
            extra={"speed": source.get("closing_speed", oc_fallback)},
        ))
    return blocks


def build_parts(source, default_speed=None):
    """把结构化 source 转成 (sentences, segments)，供 main 使用。

    每段**独立**分句（不拼接成整篇再重分句）——结构化输入下每段是独立字符串，
    跨段短句合并结构上不可能发生。

    Returns:
        sentences: list[str]，按段落顺序排列的全部句子
        segments: 段落分组（id/title/tagline/start/end + 可选 speed/voice_*/turns）
    """
    blocks = _collect_blocks(source, default_speed)
    # 逐段 / opening / closing 的 speed 也要过同一道校验：写进 JSON 的值是手敲的，
    # 类型或取值非法（0 / 负数 / "1.2"）会在 synth_sentence 里炸出 TypeError，
    # 而那一行在 try 之外。在这里拦下，报错直接指到出错的段落。
    for blk in blocks:
        sp = blk.extra.get("speed")
        if sp is None:
            continue
        try:
            blk.extra["speed"] = validate_speed(sp)
        except ValueError as e:
            raise ValueError(f"段落 {blk.id}（title={blk.title!r}）的 speed 非法：{e}")
    sentences = [s for blk in blocks for s in blk.sentences]

    segments, cursor = [], 0
    for blk in blocks:
        start, end = cursor, cursor + len(blk.sentences)
        cursor = end
        seg = {"id": blk.id, "title": blk.title, "tagline": blk.tagline,
               "start": start, "end": end}
        seg.update(blk.extra)
        if blk.turns:
            # 段内局部区间 → 全局区间，供按句覆盖音色 + 记录说话人标签。
            seg["turns"] = [
                {"start": start + t["start"], "end": start + t["end"],
                 "speaker": t["speaker"], "label": t["label"],
                 **({"voice_id": t["voice_id"]} if t["voice_id"] else {}),
                 **({"voice_style": t["voice_style"]} if t["voice_style"] else {})}
                for t in blk.turns
            ]
        segments.append(seg)

    for idx, s in enumerate(sentences, 1):
        if len(s) > LONG_SENTENCE_CHARS:
            print(f"[warn] 第 {idx} 句长达 {len(s)} 字（>{LONG_SENTENCE_CHARS}），"
                  f"念出来偏喘不过气：{s[:24]}…建议写稿时在逗号处拆成两句",
                  file=sys.stderr)
    return sentences, segments


# ===================================================================
# 二、MiMo TTS 单句合成
# ===================================================================
class BadAudioResponseError(Exception):
    """TTS 响应里没有音频（chat.completions 返回了纯文本）。

    几乎总是 --base-url/--model 指向了不支持 audio 参数的网关或模型，重试 N 次
    结果完全一样。必须整句放弃并让调用方尽早终止。
    """


def _is_non_retryable(exc):
    """重试也不会好的确定性失败：400/401/403/404/422 与"响应不含音频"。"""
    if isinstance(exc, BadAudioResponseError):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in (400, 401, 403, 404, 422)


def synth_sentence(client, text, voice_id, voice_style, out_path,
                   ffmpeg_path=None, speed=1.0, max_retries=3,
                   sentence_label="", model="mimo-v2.5-tts", api_timeout=30):
    """合成一句话到 out_path，并按 speed 做确定性变速。返回 (ok, speed_applied)。

    ok：音频是否成功落盘；speed_applied：atempo 是否落上（ok=True 而
    speed_applied=False 时音频有效但仍是原速）。

    MiMo TTS 走 chat completions 格式：user 角色放音色风格描述（可选），
    assistant 角色放要念的文本，audio 参数指定格式与音色，音频以 base64 WAV
    形式返回在 choices[0].message.audio.data。
    """
    messages = []
    if voice_style:
        messages.append({"role": "user", "content": voice_style})
    messages.append({"role": "assistant", "content": text})

    audio_params = {"format": "wav"}
    if voice_id:
        audio_params["voice"] = voice_id

    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model=model, messages=messages, audio=audio_params,
                timeout=api_timeout,
            )
            # 显式检查响应结构而不是直接下钻 .data：端点/模型配错时 message.audio
            # 为 None，AttributeError 不带 status_code 会被归为可重试，白烧额度。
            audio_obj = getattr(completion.choices[0].message, "audio", None)
            audio_data = getattr(audio_obj, "data", None) if audio_obj else None
            if not audio_data:
                raise BadAudioResponseError(
                    "TTS 响应不含音频（chat.completions 返回了纯文本）——"
                    "检查 --model/--base-url 是否指向支持 audio 参数的 TTS 模型"
                    "（默认 mimo-v2.5-tts），不要指向普通对话模型")
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(audio_data))
            break
        except Exception as e:  # noqa: BLE001 — 下面按异常类型分流
            label = sentence_label or (text[:30] + "...")
            if _is_non_retryable(e):
                print(f"    [{label}][fatal] {e}（确定性失败，不重试）", flush=True)
                return False, False
            print(f"    [{label}][retry {attempt+1}/{max_retries}] {e}", flush=True)
            if attempt < max_retries - 1:
                # 线性退避 + 抖动：多 worker 同步休眠同步唤醒会一起撞限流窗口。
                time.sleep(2 * (attempt + 1) + random.uniform(0.0, 1.0))
    else:
        return False, False

    # speed≈1 时根本不需要 atempo，音频就是所请求的语速：先判这条，
    # 否则"ffmpeg 缺失 + 原速"会被记成变速未落上，指纹永远不写、每轮白重烧。
    if abs(speed - 1.0) <= 0.01:
        return True, True
    if not ffmpeg_path:
        print(f"    [{sentence_label or text[:30] + '...'}][speed-skip] "
              f"ffmpeg 不可用，跳过变速（音频保持原速）", file=sys.stderr, flush=True)
        return True, False
    # 变速失败保留原速音频即可（时长由实测决定，时间轴仍然准确）；重调 TTS 只会白烧额度。
    try:
        return True, apply_speed(ffmpeg_path, out_path, speed)
    except Exception as e:  # noqa: BLE001
        print(f"    [{sentence_label or text[:30] + '...'}][speed-skip] "
              f"atempo 变速失败，保留原始语速: {e}", flush=True)
        return True, False


def _sentence_hash(text, voice_id, voice_style, model, speed):
    """一句话 TTS 输入的指纹（文本 + 音色 + 风格 + 模型 + 语速），resume 用。

    缓存文件名只有句序号（s005.wav），不含内容——改了第 5 句文案后带 --resume
    重跑会复用旧音频，配音与字幕从此错位。指纹不匹配即视为缓存失效、重新合成。
    """
    payload = "\x1f".join([text, voice_id or "", voice_style or "",
                           model or "", f"{float(speed):.4f}"])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _write_sidecar(path, content):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        print(f"  [sidecar][warn] 无法写入 {os.path.basename(path)}（{e}），"
              f"下次 --resume 会重新合成该句", file=sys.stderr)


def _drop_sentence_cache(out_path):
    """清掉某句的音频与 sidecar（重新合成前调用）。"""
    for suffix in ("", ".sha", ".failed"):
        _remove_quiet(out_path + suffix)


def _resume_decision(out_path, ffmpeg_path, text, voice_id, voice_style, model, speed):
    """--resume 时判断某句能不能跳过。返回 (action, duration)。

        "skip"        缓存可用（输入指纹一致且时长有效）
        "skip_failed" 上次已判定 TTS 失败并降级为静音——输入指纹未变则不再重试
        "regen"       没缓存 / 指纹不符 / 时长无效 → 重新合成
    """
    if os.path.exists(out_path + ".failed"):
        # 失败占位也要核对指纹：改了这句文案/音色/语速后，旧的"失败"结论
        # 对新输入不成立，必须 regen 重试，而不是永远 skip_failed 锁死静音。
        try:
            with open(out_path + ".sha", encoding="utf-8") as f:
                cached_failed_sha = f.read().strip()
        except (OSError, UnicodeDecodeError):
            cached_failed_sha = ""
        if cached_failed_sha != _sentence_hash(text, voice_id, voice_style,
                                               model, speed):
            return "regen", 0.0
        dur = measure_duration(ffmpeg_path, out_path) if ffmpeg_path else 0.0
        return ("skip_failed", dur) if dur and dur > 0 else ("regen", 0.0)

    if not os.path.exists(out_path):
        return "regen", 0.0
    try:
        with open(out_path + ".sha", encoding="utf-8") as f:
            cached = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return "regen", 0.0  # 无指纹（或读不了）→ 无法确认内容是否已变，重合成
    if cached != _sentence_hash(text, voice_id, voice_style, model, speed):
        return "regen", 0.0
    dur = measure_duration(ffmpeg_path, out_path) if ffmpeg_path else 0.0
    return ("skip", dur) if dur and dur > 0 else ("regen", 0.0)


# ===================================================================
# 三、CLI
# ===================================================================
def _build_parser():
    parser = argparse.ArgumentParser(description="courseware-studio TTS 能力（旁白 → 音频 + 时间轴）")
    parser.add_argument("--source", default=None,
                        help="旁白脚本 JSON（{title, segments:[{id,title,text}]}）。"
                             "逐段独立分句，直接产出带段落分组的时间轴。"
                             "Agent 手写这一份脚本即可，无需任何上游编译。")
    parser.add_argument("-o", "--output", default=None, help="输出目录")
    parser.add_argument("--api-key", default=None,
                        help="MiMo TTS API key（默认读 .env 的 MIMO_API_KEY）")
    parser.add_argument("--voice-id", default="冰糖", choices=list_voice_ids(),
                        help="音色（默认 冰糖）")
    parser.add_argument("--voice-style",
                        default="专业新闻播报，语速适中，语气沉稳自信，中英文表达流畅自然",
                        help="音色风格描述")
    parser.add_argument("--gap", type=float, default=DEFAULT_GAP,
                        help="句间静音秒数（默认取 _contracts.DEFAULT_GAP）")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED,
                        help="语速倍率（ffmpeg atempo，1.0=原速=默认，1.5=快一半）")
    parser.add_argument("--loudness", type=float, default=None,
                        help="响度归一化目标（LUFS，如 -16）。默认不做归一化")
    parser.add_argument("--resume", action="store_true",
                        help="复用 cache-dir 中输入未变的句子音频")
    parser.add_argument("--cache-dir", default=None,
                        help="--resume 的句子缓存目录（默认在输出目录同级的 .courseware-cache/ 下）")
    parser.add_argument("--bgm", default=None, help="背景音乐文件（mp3/wav/ogg）")
    parser.add_argument("--bgm-volume", type=float, default=0.15,
                        help="BGM 相对人声音量（0.0-1.0，默认 0.15）")
    parser.add_argument("--model", default=None,
                        help="TTS 模型（默认 MIMO_TTS_MODEL 或 'mimo-v2.5-tts'）")
    parser.add_argument("--base-url", default=None,
                        help="MiMo API base URL（默认 MIMO_BASE_URL 或官方地址）")
    parser.add_argument("--api-timeout", type=float, default=30.0,
                        help="单次 TTS 调用超时（默认 30s）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只分句 + 预览，不调 TTS、不写音频")
    parser.add_argument("--workers", type=int, default=4, help="并行 TTS 调用数（默认 4）")
    parser.add_argument("--on-fail", choices=["abort", "silence"], default="abort",
                        help="单句反复失败后：abort（默认）阻断管线；silence 该句降级为"
                             "静音占位（时长按字数/语速估算），保留在时间轴与字幕位置，"
                             "时间轴对应句子带 \"synth_failed\": true")
    return parser


def _validate_args(parser, args):
    try:
        validate_speed(args.speed)
    except ValueError as e:
        parser.error(str(e))
    # 有限数值校验只写一份（_contracts.require_finite_number），CLI 侧只负责补标签；
    # 各入口手写 math.isfinite 必然与它漂移。None = 该开关没给，跳过。
    for label, val, kw in (("--gap（句间静音秒数）", args.gap, {"nonnegative": True}),
                           ("--loudness（LUFS）", args.loudness, {}),
                           ("--bgm-volume（0.0-1.0）", args.bgm_volume, {})):
        if val is None:
            continue
        try:
            require_finite_number(val, label, **kw)
        except ValueError as e:
            parser.error(str(e))
    if args.workers < 1:
        parser.error(f"--workers 至少为 1（收到 {args.workers}）")
    if args.cache_dir and not args.resume:
        parser.error("--cache-dir 只能与 --resume 一起使用")
    # 用户显式要求 BGM 时，缺失文件不能静默改变最终制品。
    if args.bgm and not os.path.exists(args.bgm):
        parser.error(f"--bgm 文件不存在：{args.bgm}")


def _load_script_source(path):
    """读取旁白脚本 JSON，规整成内部结构。

    唯一格式：`{title, segments:[{id,title,text,...}]}`，可选顶层 opening/closing/
    opening_title/closing_title/opening_tagline/closing_tagline/opening_speed/
    closing_speed/speakers。不做隐式兼容——格式不对就报错，不猜。
    """
    import json

    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as e:
        raise ValueError(f"无法读取旁白脚本: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise ValueError("旁白脚本必须是 {title, segments:[...]}")

    segs = []
    seen_ids = set()
    for index, seg in enumerate(data["segments"], 1):
        if not isinstance(seg, dict):
            raise ValueError(f"segments[{index}] 必须是对象，不能静默跳过")
        dialogue = seg.get("dialogue")          # 多人对话：保留下来交给 _collect_blocks 展开
        text = (seg.get("text") or "").strip()
        if not text and not dialogue:
            raise ValueError(f"segments[{index}] 既没有 text 也没有 dialogue，不能是空段落")
        if dialogue is not None and not isinstance(dialogue, list):
            raise ValueError(f"segments[{index}].dialogue 必须是数组")
        sid = str(seg.get("id") or f"seg-{len(segs) + 1}").strip()
        if not sid:
            raise ValueError(f"segments[{index}].id 不能为空")
        if sid in seen_ids:
            raise ValueError(f"segments[{index}].id 重复：{sid}")
        seen_ids.add(sid)
        out = {"id": sid,
               "title": seg.get("title") or sid,
               "text": text}
        for k in ("voice_id", "voice_style", "speed", "tagline"):
            if seg.get(k) is not None:
                out[k] = seg[k]
        if dialogue is not None:
            out["dialogue"] = dialogue
        segs.append(out)
    if not segs:
        raise ValueError("旁白脚本没有可朗读的段落内容（segments[].text 全为空）")

    result = {"title": data.get("title") or data.get("opening_title") or "",
              "segments": segs}
    for k in ("opening", "closing", "opening_title", "closing_title",
              "opening_tagline", "closing_tagline",
              "opening_speed", "closing_speed", "speakers"):
        if data.get(k) is not None:
            result[k] = data[k]
    return result


# ===================================================================
# 四、主管线
# ===================================================================
def _audio_ref(path):
    """把音轨路径压成制品可携带的相对引用（basename）。"""
    return ntpath.basename(str(path).replace("/", "\\")) if path else None


def _finalize_audio(args, ffmpeg_path, sentence_data, source_data, seg_config,
                    silence_fallback_count, total_sentences, cached_count):
    """拼接 → 可选 BGM/响度 → 写 narration_timing.json。"""
    print(f"\n[concat] {len(sentence_data)} clips (gap {args.gap}s)...", flush=True)
    combined_path = os.path.join(args.output, "combined.wav")
    # 最终交付契约永远只有 audio/combined.wav。BGM / loudness 都在隐藏临时文件上处理，
    # 成功后原子替换；这样模板、manifest 和实际音频永远不会因后处理变成不同文件名。
    for stale in ("combined_bgm.wav", "combined_loud.wav"):
        try:
            os.remove(os.path.join(args.output, stale))
        except FileNotFoundError:
            pass
    if not concat_audio(ffmpeg_path, [s["file"] for s in sentence_data], args.gap,
                        combined_path):
        print("[error] 音频拼接失败", file=sys.stderr)
        sys.exit(1)

    total_dur = measure_duration(ffmpeg_path, combined_path)
    if not total_dur or total_dur <= 0:
        # 0 时长会产出"合法但废掉"的时间轴（渲染出无声空片），在这里拦下。
        print("[error] combined.wav 时长测量失败（0.0s）——检查磁盘空间与 ffmpeg 可用性",
              file=sys.stderr)
        sys.exit(1)
    print(f"[done] 总时长 {total_dur:.2f}s", flush=True)

    # 每句起始时间：拼接实测时长 + 句间 gap 累加（时间轴的唯一来源）。
    cumulative = 0.0
    for i, sd in enumerate(sentence_data):
        sd["start_time"] = round(cumulative, 3)
        cumulative += sd["duration"]
        if i < len(sentence_data) - 1:
            cumulative += args.gap

    # 时间轴按各句实测时长累加，而 combined.wav 是 concat -c copy 流拷贝出来的。
    # 两者对不上说明句间采样格式不一致、流拷贝悄悄改了时长——逐句时间会整体漂移。
    if abs(total_dur - cumulative) > max(0.5, 0.02 * cumulative):
        print(f"[warn] combined.wav 实测 {total_dur:.2f}s 与时间轴累计 {cumulative:.2f}s "
              f"相差较大——通常是各句音频采样率/声道数不一致，concat -c copy 流拷贝下"
              f"会造成逐句时间轴漂移，请确认 TTS 输出格式一致", file=sys.stderr)

    if args.bgm and os.path.exists(args.bgm):
        # bgm_volume 直接拼进 ffmpeg 滤镜串：clamp 越界值，防注入与削波（NaN/Inf
        # 已在 argparse 阶段拦下，早于 TTS，不烧额度）。
        if args.bgm_volume < 0 or args.bgm_volume > 1:
            clamped = max(0.0, min(1.0, args.bgm_volume))
            print(f"[warn] --bgm-volume {args.bgm_volume} 超出 [0,1]，已钳制到 {clamped}",
                  file=sys.stderr)
            args.bgm_volume = clamped
        print(f"[bgm] 混入 {args.bgm}（音量 {args.bgm_volume}）...", flush=True)
        fd, mixed_path = tempfile.mkstemp(prefix=".combined-bgm-", suffix=".wav", dir=args.output)
        os.close(fd)
        try:
            if mix_bgm(ffmpeg_path, combined_path, args.bgm, args.bgm_volume, mixed_path):
                os.replace(mixed_path, combined_path)
                # 混音换了文件（amix duration=first，句子相对时序不变，但整长可能
                # 差一点）——复测最终交付文件，而不是中间文件。
                total_dur = measure_duration(ffmpeg_path, combined_path) or total_dur
            else:
                print("  [warn] BGM 混音失败，使用纯人声", flush=True)
        finally:
            try:
                os.remove(mixed_path)
            except FileNotFoundError:
                pass

    if args.loudness is not None:
        fd, loud_path = tempfile.mkstemp(prefix=".combined-loud-", suffix=".wav", dir=args.output)
        os.close(fd)
        try:
            if apply_loudnorm(ffmpeg_path, combined_path, loud_path, args.loudness):
                os.replace(loud_path, combined_path)
                total_dur = measure_duration(ffmpeg_path, combined_path) or total_dur
                print(f"  [loudness] 已归一化到 {args.loudness} LUFS", flush=True)
            else:
                print("  [warn] 响度归一化失败，使用未归一化音频", flush=True)
        finally:
            try:
                os.remove(loud_path)
            except FileNotFoundError:
                pass

    # 时间轴：一句 = 一条 {start, duration, text}；一段 = 一个 scene。
    sentences_out = []
    for s in sentence_data:
        entry = {"start": float(s["start_time"]), "duration": float(s["duration"]),
                 "text": s["text"]}
        if s.get("speaker"):
            entry["speaker"] = s["speaker"]
        if s.get("synth_failed"):
            entry["synth_failed"] = True
        sentences_out.append(entry)

    scenes = []
    # 按句子的原始 index 切场，不按列表位置：整句被丢弃（静音兜底也失败）时
    # 位置会整体前移，用 enumerate 会把后面的句子静默切进错误段落。
    for seg in seg_config or []:
        start_idx, end_idx = seg["start"], seg["end"]   # 0-based / exclusive
        seg_sentences = [e for e, src_s in zip(sentences_out, sentence_data)
                         if start_idx <= src_s["index"] < end_idx]
        if not seg_sentences:
            # 该段所有句子都没产出音频（TTS 连续失败 + --on-fail abort，或段落本身
            # 被上游丢空）。报出来，而不是写一份下游读不懂的空场景。
            print(f"\n[warn] 段落 {seg.get('id')} 没有任何可用音频，已从时间轴剔除"
                  f"（常见原因：这几段 TTS 连续失败且 --on-fail abort）",
                  file=sys.stderr, flush=True)
            continue
        start = seg_sentences[0]["start"]
        end = max(e["start"] + e["duration"] for e in seg_sentences)
        sid = str(seg.get("id"))
        scenes.append({
            "step_id": sid,          # 与 interactive_runtime.js 的场景键一致
            "scene_id": sid,
            "title": seg.get("title", ""),
            "start": round(start, 3),
            "duration": round(max(0.0, end - start), 3),
            "end": round(end, 3),
            "sentences": seg_sentences,
        })

    # 整句丢弃（TTS 失败且静音兜底也没落成）同样是降级：这几句在成片里既没
    # 配音也没字幕，status 不能因为"没有占位"就报 ok。
    dropped_count = max(0, total_sentences - len(sentence_data))
    timing = {
        "schema_version": 1,
        "status": "degraded" if (silence_fallback_count or dropped_count) else "ok",
        "title": source_data.get("title") or "",
        "total_duration": round(total_dur, 3),
        "gap": args.gap,
        "voice_id": args.voice_id,
        # 只保留文件名：消费方按约定在同一目录下查找。
        "audio": _audio_ref(combined_path),
        "scenes": scenes,
        "degraded": {"tts_silence_fallback_count": silence_fallback_count,
                     "dropped_sentence_count": dropped_count},
    }
    # 原子写：narration_timing.json 是页面内联时间轴的唯一数据源，写到一半被
    # Ctrl-C 打断会留下截断 JSON——要么完整要么不存在。
    manifest_path = os.path.join(args.output, "narration_timing.json")
    write_json_atomic(manifest_path, timing, indent=2)

    print(f"\n[manifest] {manifest_path}", flush=True)
    ok_count = len(sentence_data) - silence_fallback_count
    degraded_note = f"，{silence_fallback_count} 句静音占位" if silence_fallback_count else ""
    print(f"[stats] {ok_count}/{total_sentences} 句成功{degraded_note}"
          f"（{cached_count} 句复用缓存）", flush=True)
    print(f"[duration] {total_dur:.2f}s", flush=True)


def _spread_segment_overrides(seg_config, args, sentences,
                              sentence_speeds, sentence_voices, speaker_labels):
    """按段落与 turns 展开每句的语速 / 音色 / 说话人标签。"""
    for seg in seg_config:
        start_idx = seg.get("start", 0)
        end_idx = seg.get("end", len(sentences))
        seg_speed = seg.get("speed")
        if seg_speed is not None:
            for si in range(start_idx, min(end_idx, len(sentences))):
                sentence_speeds[si] = seg_speed
        seg_voice_id, seg_voice_style = seg.get("voice_id"), seg.get("voice_style")
        if seg_voice_id or seg_voice_style:
            for si in range(start_idx, min(end_idx, len(sentences))):
                sentence_voices[si] = (
                    seg_voice_id or args.voice_id,
                    seg_voice_style if seg_voice_style is not None else args.voice_style,
                )
        # turns 是比段落更细的子区间：同一段里 A/B 交替发言，各自用各自音色。
        for turn in seg.get("turns", []):
            t_start = turn.get("start", start_idx)
            t_end = turn.get("end", end_idx)
            t_voice_id, t_voice_style = turn.get("voice_id"), turn.get("voice_style")
            t_label = turn.get("label") or turn.get("speaker")
            for si in range(t_start, min(t_end, len(sentences))):
                if t_voice_id or t_voice_style:
                    base_id, base_style = sentence_voices.get(
                        si, (args.voice_id, args.voice_style))
                    sentence_voices[si] = (
                        t_voice_id or base_id,
                        t_voice_style if t_voice_style is not None else base_style,
                    )
                if t_label:
                    speaker_labels[si] = t_label


def _make_sentence_entry(task, duration, speaker_labels, synth_failed=False):
    sd = {"index": task["index"], "text": task["text_tts"], "file": task["out_path"],
          "duration": round(duration, 3)}
    if task["index"] in speaker_labels:
        sd["speaker"] = speaker_labels[task["index"]]
    if synth_failed:
        sd["synth_failed"] = True
    return sd


def _synthesize_pending(args, client, ffmpeg_path, model, pending_tasks,
                        sentence_speaker_labels):
    """并行合成待处理句子。返回 (new_results, failed_indices)。"""
    new_results, failed = [], []
    total = len(pending_tasks)
    if not total:
        return new_results, failed

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_task = {
            executor.submit(synth_sentence, client, t["text_tts"], t["voice_id"],
                            t["voice_style"], t["out_path"], ffmpeg_path,
                            t["speed"], 3, t["label"], model, args.api_timeout): t
            for t in pending_tasks
        }
        done = 0
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            ok, speed_applied = future.result()
            done += 1
            label, out_path = task["label"], task["out_path"]
            preview = task["text_tts"][:30]

            if ok and os.path.exists(out_path):
                dur = measure_duration(ffmpeg_path, out_path)
                if dur > 0:
                    # 这句这次真成功了：**先**清掉上一轮留下的失败标记（否则下轮会
                    # 被误判为"已失败的静音占位"而跳过），**再**写指纹 —— 两步之间
                    # 被打断时，宁可留下"没指纹"（下轮重新合成），也不要留下
                    # "有指纹 + 有失败标记"。
                    _remove_quiet(out_path + ".failed")
                    if speed_applied:
                        # 指纹 sidecar：resume 时用它判断这句是不是同一份输入。
                        _write_sidecar(out_path + ".sha", _sentence_hash(
                            task["text_tts"], task["voice_id"], task["voice_style"],
                            model, task["speed"]))
                    else:
                        # atempo 没落上：文件是原速音频，绝不能留请求语速的指纹——
                        # 下轮 --resume 会按指纹 skip，错误语速永久投毒缓存。
                        # 删掉指纹，让它每轮都重合成直到变速成功。
                        _remove_quiet(out_path + ".sha")
                        print(f"    [{label}][warn] 该句仍为原速（atempo 未落上），"
                              f"下次 --resume 会重试", file=sys.stderr)
                    new_results.append(_make_sentence_entry(
                        task, dur, sentence_speaker_labels))
                    print(f"[TTS {done}/{total}] {label} {preview} -> {dur:.2f}s", flush=True)
                    continue

            if args.on_fail == "silence":
                # 降级：该句反复失败（如触发内容审核）时不丢弃，改为静音占位；
                # 时长只能按字数/语速估算（没有真实语速可测），估算已除过 speed。
                fallback_dur = max(estimate_sentence_seconds(
                    task["text_tts"], DEFAULT_CHARS_PER_SEC, task["speed"]), 0.3)
                try:
                    generate_silence(ffmpeg_path, fallback_dur, out_path)
                    # .failed 标记 + 指纹：下次 --resume 认得出这句是"已失败的静音
                    # 占位"，既不重试也不丢标记。**先写失败标记再写指纹**——
                    # 两步之间被打断时，宁可"标记了失败"（下轮保守重来），
                    # 也不要"有指纹却没标记"（静音被当成成功配音，status 变 ok）。
                    _write_sidecar(out_path + ".failed", "")
                    _write_sidecar(out_path + ".sha", _sentence_hash(
                        task["text_tts"], task["voice_id"], task["voice_style"],
                        model, task["speed"]))
                    new_results.append(_make_sentence_entry(
                        task, fallback_dur, sentence_speaker_labels, synth_failed=True))
                    print(f"[TTS {done}/{total}] {label} {preview} "
                          f"[失败 → 静音兜底 ~{fallback_dur:.2f}s，建议事后补录]", flush=True)
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"[TTS {done}/{total}] {label} {preview} "
                          f"[失败，且静音兜底也失败: {e}]", flush=True)

            failed.append(task["index"])
            print(f"[TTS {done}/{total}] {label} {preview} [失败]", flush=True)
    return new_results, failed


def main():
    setup_stdio()
    parser = _build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    if not args.output and not args.dry_run:
        parser.error("缺少 -o/--output（--dry-run 不需要）")
    if not args.source:
        parser.error("缺少 --source")
    # 产物路径守卫：--dry-run 承诺不写文件，不需要拦
    if not args.dry_run:
        guard_not_in_skill_dir(("-o/--output", os.path.abspath(args.output)))
        if args.cache_dir:
            guard_not_in_skill_dir(("--cache-dir", os.path.abspath(args.cache_dir)))

    api_key = get_key("MIMO_API_KEY", args.api_key, source_path=args.source)
    if not api_key and not args.dry_run:
        print("[error] 没有 API key。用 --api-key 或设置 .env 的 MIMO_API_KEY",
              file=sys.stderr)
        sys.exit(1)
    model, base_url = resolve_model_config(
        args.model, args.base_url, "MIMO_TTS_MODEL", "mimo-v2.5-tts")

    try:
        source_data = _load_script_source(args.source)
        sentences, seg_config = build_parts(source_data, default_speed=args.speed)
    except ValueError as e:
        print(f"[error] 旁白脚本无效：{e}", file=sys.stderr)
        sys.exit(1)
    if not sentences:
        print("[error] 旁白脚本分句为空", file=sys.stderr)
        sys.exit(1)

    print(f"[script] {sum(len(s) for s in sentences)} chars", flush=True)
    print(f"[split] {len(sentences)} sentences", flush=True)
    for i, s in enumerate(sentences[:_PREVIEW_LIMIT]):
        print(f"  {i+1}. {s[:35] + '...' if len(s) > 35 else s}", flush=True)
    if len(sentences) > _PREVIEW_LIMIT:
        print(f"  ...（其余 {len(sentences) - _PREVIEW_LIMIT} 句已省略）", flush=True)

    if args.dry_run:
        print("\n[dry-run] 分句与段落识别完成：未调 TTS、未写音频。", flush=True)
        return

    # -o 指到已存在的同名文件（手滑把文件路径当目录传）时提前拦下
    if os.path.isfile(args.output):
        print(f"[error] 输出路径 {args.output} 是一个已存在的文件，--output 需要目录路径",
              file=sys.stderr)
        sys.exit(1)
    os.makedirs(args.output, exist_ok=True)
    # 成品目录只放 combined.wav 与 narration_timing.json。可复用缓存移到同级的
    # 隐藏工作目录；不开 --resume 时用 TemporaryDirectory，不会污染交付目录。
    temp_cache = None
    if args.resume:
        cache_root = args.cache_dir or os.path.join(
            os.path.dirname(os.path.abspath(args.output)), ".courseware-cache",
            os.path.basename(os.path.abspath(args.output)))
        sentences_dir = os.path.join(cache_root, "sentences")
        print(f"[cache] {sentences_dir}", flush=True)
    else:
        temp_cache = tempfile.TemporaryDirectory(
            prefix=".courseware-tts-",
            dir=os.path.dirname(os.path.abspath(args.output)))
        sentences_dir = temp_cache.name
    os.makedirs(sentences_dir, exist_ok=True)

    ffmpeg_path = get_ffmpeg()
    if not (ffmpeg_path and os.path.isfile(ffmpeg_path)):
        # get_ffmpeg 在"系统没有 + imageio 也没装"时返回字面量 "ffmpeg"：
        # 不管它，后面 concat_audio 会抛 FileNotFoundError，用户吃一段跟 ffmpeg
        # 毫无关系的裸栈。在这里 fail-fast，直接说清要装什么。
        print("[error] 没找到可用的 ffmpeg（拼接 / 变速 / BGM 都依赖它）。\n"
              "        装一个 ffmpeg 再跑；只想看分句结果可以加 --dry-run。",
              file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    # max_retries=0：SDK 内部默认还会静默重试 2 次，叠加本模块自己的 3 次应用层
    # 重试 = 单句最多 6 次请求。重试策略统一收口到 synth_sentence。
    client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
    print(f"[api] model={model} base_url={base_url}", flush=True)

    sentence_speeds, sentence_voices, sentence_speaker_labels = {}, {}, {}
    _spread_segment_overrides(seg_config, args, sentences, sentence_speeds,
                              sentence_voices, sentence_speaker_labels)

    sentence_data, pending_tasks, cached_count = [], [], 0
    cached_failed_labels = []
    for i, sent_text in enumerate(sentences):
        out_path = os.path.join(sentences_dir, f"s{i+1:03d}.wav")
        task = {"index": i, "text_tts": sent_text, "out_path": out_path,
                "label": f"s{i+1:03d}/{len(sentences):03d}",
                "speed": sentence_speeds.get(i, args.speed),
                "voice_id": sentence_voices.get(i, (args.voice_id, args.voice_style))[0],
                "voice_style": sentence_voices.get(i, (args.voice_id, args.voice_style))[1]}

        if args.resume and os.path.exists(out_path):
            action, dur = _resume_decision(out_path, ffmpeg_path, sent_text,
                                           task["voice_id"], task["voice_style"],
                                           model, task["speed"])
            if action == "skip_failed" and args.on_fail == "abort":
                # abort 模式的契约是"交付里没有静音占位"。缓存里的失败标记
                # 若不拦下，--resume 会带着占位一路跑到 exit 0。在这里攒名单，
                # 循环后立刻退出——在任何一次 TTS 调用之前，不白烧额度。
                cached_failed_labels.append(task["label"])
                continue
            if action != "regen":
                sentence_data.append(_make_sentence_entry(
                    task, dur, sentence_speaker_labels,
                    synth_failed=(action == "skip_failed")))
                cached_count += 1
                print(f"  [{task['label']}][skip] {dur:.2f}s (cached)", flush=True)
                continue
            print(f"  [{task['label']}] 稿件/音色/语速已变或缓存不完整，重新合成", flush=True)
            _drop_sentence_cache(out_path)

        pending_tasks.append(task)

    if cached_failed_labels:
        print(f"\n[error] --resume 命中 {len(cached_failed_labels)} 个「失败静音占位」句："
              f"{', '.join(cached_failed_labels)}；--on-fail abort（默认）不接受无声占位。"
              "删掉缓存目录里这些句的 .failed 标记后重跑（将重试这几句 TTS），"
              "或显式改用 --on-fail silence 保留占位。", file=sys.stderr, flush=True)
        sys.exit(1)

    pending_count = len(pending_tasks)
    if pending_count:
        mean_chars = sum(len(t["text_tts"]) for t in pending_tasks) / pending_count
        est = mean_chars / DEFAULT_CHARS_PER_SEC * pending_count * 1.2 / max(args.workers, 1)
        print(f"[est] 待合成 {pending_count} 句，约 {est:.0f}s"
              f"（≤{args.workers} 并发，句均 {mean_chars:.1f} 字）", flush=True)

    new_results, failed = _synthesize_pending(
        args, client, ffmpeg_path, model, pending_tasks, sentence_speaker_labels)

    sentence_data.extend(new_results)
    sentence_data.sort(key=lambda s: s["index"])

    if not sentence_data:
        print("[error] 所有句子都失败了", file=sys.stderr)
        sys.exit(1)
    if failed and args.on_fail == "abort":
        print(f"\n[error] {len(failed)} 句 TTS 失败：{[f + 1 for f in failed]}。"
              "默认 --on-fail abort 阻断管线，避免静默丢失内容；"
              "如需保留时间轴并显式进入 degraded 状态，请改用 --on-fail silence。",
              file=sys.stderr, flush=True)
        sys.exit(1)
    if failed:
        # 走到这里说明 --on-fail silence 下连静音兜底也失败了：这些句**不在**
        # 时间轴上（不是占位），文案会整句消失，必须显式报出。
        print(f"\n[warn] {len(failed)} 句 TTS 失败且静音占位也没落成，已从时间轴整句丢弃："
              f"{[f + 1 for f in failed]}（成片这几处无配音也无字幕，交付时间轴为 degraded）",
              flush=True)

    silence_fallback_count = sum(1 for s in sentence_data if s.get("synth_failed"))
    if silence_fallback_count:
        print(f"\n[warn] {silence_fallback_count} 句无有效配音（静音占位，含缓存复用），"
              f"成片对应位置为静音；可核对 narration_timing.json 中 "
              f"\"synth_failed\": true 的句子并考虑补录", flush=True)

    _finalize_audio(args, ffmpeg_path, sentence_data, source_data, seg_config,
                    silence_fallback_count, len(sentences), cached_count)
    if temp_cache is not None:
        temp_cache.cleanup()


if __name__ == "__main__":
    main()
