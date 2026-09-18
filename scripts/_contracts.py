#!/usr/bin/env python3
"""跨脚本共享的契约原语：数值/语速校验、默认值、严格 JSON。

各脚本通过文件（旁白脚本 / narration_timing.json / Learning Graph 等）通信，
字段缺失或类型错误会变成难以定位的报错或静默降级。把领域规则收口成少量共享
函数，避免漂移。

运行时共享规则；不承担制品审计或 self-test。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def require_finite_number(value, label, *, positive=False, nonnegative=False):
    """校验「有限数值」的唯一实现。

    label 形如 "Learning Graph concepts[0].estimated_time"，直接进错误消息。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{label} 必须是有限数字")
    if positive and value <= 0:
        raise ValueError(f"{label} 必须是正数")
    if nonnegative and value < 0:
        raise ValueError(f"{label} 必须是非负数字")
    return value


def validate_speed(speed):
    """校验语速倍率必须是 >0 的有限数值，非法时抛 ValueError。

    CLI 的 --speed 入口与 _audio.build_atempo_filter 的入口守卫共用这一份判断，
    避免多处各写一份而漂移（速度 <=0 会让 atempo 链不收敛）。
    """
    if not isinstance(speed, (int, float)) or isinstance(speed, bool):
        raise ValueError(f"speed 必须是数值（收到 {speed!r}）")
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError(f"speed 必须是大于 0 的有限数值（收到 {speed!r}）")
    return speed


# ── 跨脚本默认值（单一来源）────────────────────────────────────────
# 默认**原速**。变速是逐段可选的调味（`segments[].speed`），不是全局基调：
# 默认值一旦不是 1.0，"这段就是原速"这个最朴素的预期就没了，改回来还得重烧一遍 TTS 额度。
DEFAULT_SPEED = 1.0
# opening/closing 段的默认语速：默认与正文同速。
# 要单独做开场/收尾的节奏，记得它必须**小于** DEFAULT_SPEED 才是"略慢"——
# 写成比正文大的值，效果是开场比正文还快，与初衷正好相反。
OPENING_CLOSING_DEFAULT_SPEED = 1.0

# 静音兜底时长估算用的启发式语速（字/秒）。
DEFAULT_CHARS_PER_SEC = 4.3
# 与 narration.py 的 --gap 默认值保持一致（不一致会让估算相对实测系统性偏移）。
DEFAULT_GAP = 0.4


def estimate_sentence_seconds(sentence, chars_per_sec, speed):
    """单句预计时长（秒）：字数 / 语速 / 倍速（TTS 失败时估静音占位时长用）。"""
    return len(sentence) / chars_per_sec / max(speed, 0.01)


# ── Voice registry ───────────────────────────────────────────────
VOICE_IDS = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]


def list_voice_ids():
    return list(VOICE_IDS)
