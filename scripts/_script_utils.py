"""全仓共享的基础设施（"只该有一份实现"的东西）。

两类，都不依赖 TTS / 网络 / 并发：

1. 文本处理：`split_sentences`（中文断句，TTS 分句复用）。
2. 落盘与进程原语：`write_json_atomic`（先写 .tmp → fsync → os.replace）、
   `setup_stdio`（Windows 重定向场景强制 UTF-8）、`guard_not_in_skill_dir`
   （产物不得落进技能目录的守卫）、`is_inside`。
"""
import json
import os
import re
import sys
import tempfile

# 技能目录（scripts/ 的上一级）：制作产物一律不得落在这里——产物写在用户项目
# 目录，混进技能目录会污染仓库、多次制作串台。放在共享模块是因为各入口都要拦
# 同一件事，各写一份必然漂移。
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 原子写 ───────────────────────────────────────────────────────
# 制作产物（narration_timing.json / index.html 等）被 Ctrl-C 或断电打断在写到
# 一半时会留下截断文件：下次读它直接崩在 json.load / 报莫名其妙的语法错——堆栈
# 都不指向"上次中断了，重跑一遍就好"。先写 .tmp 再 replace，要么完整要么不存在。
def _atomic_replace(path, write_fn):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # mkstemp 生成唯一临时名：两个进程先后写同一路径时不会共用（并踩坏）
    # 同一个 <path>.tmp。
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path, data, indent=2):
    """原子写 JSON：写 <path>.tmp → fsync → os.replace 覆盖。"""
    def _write(tmp):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
    _atomic_replace(path, _write)


def setup_stdio():
    """stdout/stderr 强制 UTF-8 输出（errors=replace），入口脚本 main() 第一行调用。

    Windows 下 stdout 被重定向/进管道时，Python 按 locale 编码写流（中文系统
    cp936）——pipeline 把用户稿件原文打进 stdout，稿件含 emoji 或任何该编码
    表示不了的字符时，print 到一半裸栈 UnicodeEncodeError，而此时 TTS 已经烧
    掉一半额度。交互控制台因 PEP 528 本就是 UTF-8 不受影响；测试环境替换过
    的假流没有 reconfigure 方法时静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def is_inside(child, parent):
    """child 是否位于 parent 目录内（realpath 归一化，软链接也能判对）。"""
    try:
        child_r = os.path.realpath(child)
        parent_r = os.path.realpath(parent)
    except OSError:
        return False
    return child_r == parent_r or child_r.startswith(parent_r + os.sep)


def guard_not_in_skill_dir(*labeled_paths, **kw):
    """产物路径落在技能目录内时 fail-fast（(标签, 路径) 成对传入）。

    -o/--output 之类是相对 CWD 解析的，而文档示例命令用的正是相对路径
    （`-o audio_output`）——从技能目录照抄就会把产物建在技能目录里，正好
    踩中"不要在技能目录内生成任何文件"的禁令。这道守卫把约定变成机械
    拦截，各写盘入口共用。
    """
    tip = kw.pop("tip", "") or ("请 cd 到你的项目目录后重跑（用脚本绝对路径调用即可），"
                                "或用 -o/--project 显式指定技能目录之外的绝对路径。")
    offenders = [(label, p) for label, p in labeled_paths if is_inside(p, SKILL_DIR)]
    if not offenders:
        return
    lines = "\n".join(f"  · {label} -> {p}" for label, p in offenders)
    raise SystemExit(
        f"[guard] 制作产物不能写在技能目录内（{SKILL_DIR}）：\n{lines}\n"
        f"产物混进技能目录会污染技能仓库，也容易在多次制作之间串台。\n{tip}")


def strip_env_comment(value):
    """剥掉 .env 值里的行内注释与成对引号（引号感知，_env 解析层唯一实现）。

    规则（对齐 dotenv 的常见行为）：
      · 值以引号开头：取到配对的闭合引号为止——"sk-a#b" 里的 # 是值的一部分，
        闭合引号之后的任意内容按注释丢弃；没有闭合引号时保守地保留引号后的
        全部正文（宁可把注释当值，也不要把密钥截断一半）。
      · 值不带引号：首个"空白 + #"（或值直接以 # 开头）处截断；
        紧贴的非空白 #（如锚点、abc#def）视为值的一部分。
    """
    s = value
    if s[:1] in ('"', "'"):
        q = s[0]
        i = 1
        while i < len(s):
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == q:
                return s[1:i]
            i += 1
        return s[1:]
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "#" and (i == 0 or s[i - 1].isspace()):
            break
        i += 1
    return s[:i].rstrip()


# 中文终止符：。！？＋中文分号＋ASCII 分号＋换行（保持稳定的断句行为）。
# ASCII 的 .!? 由下方扫描逻辑带边界守卫地补充（见 split_sentences）。
_CN_TERMINATORS = "。！？\uFF1B;\n"

# 常见英文缩写词尾：句点即使后面跟着空白也不视为句子结束。全小写比对；
# 含内部点的形式（e.g / i.e / u.s）。维护原则：漏收一个缩写只是"少切一刀"
# （句子偏长、可被显示层切行兜住）；误收一个普通词会把完整句子劈成两半。
_EN_ABBREV_TAILS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc",
    "cf", "al", "fig", "no", "inc", "ltd", "co", "corp", "col", "gen",
    "sen", "rep", "rev", "hon", "univ", "dept", "est", "approx", "ave",
    "blvd", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec", "e.g", "i.e", "a.m", "p.m", "u.s", "u.k",
}
_EN_WORD_TAIL_RE = re.compile(r"[A-Za-z.]+$")


def _is_english_sentence_end(text, i):
    """text[i] 为 ASCII 句点时判断它是否终结一个句子。

    守卫全部朝"宁可少切、不可错切"的方向设计：
    1. 句点后不是空白/换行 → 不切（小数点 3.5、域名、文件名、
       "U.S-China" 这类连字符复合词都落在这类）；
    2. 省略号（前一个字符仍是句点）→ 不切；
    3. 点前单词命中缩写表（Mr./Dr./e.g./U.S.），或点前是单个 ASCII
       字母且再往前非字母数字（人名首字母 J.、缩写链 U.S. 的最后一个
       点）→ 不切。
    """
    n = len(text)
    j = i + 1
    if i >= 1 and text[i - 1] == ".":
        return False  # 省略号中段/尾点
    prev_ch = text[i - 1] if i >= 1 else ""
    if not prev_ch.isalnum():
        return False  # "(...)" 收尾括号点等孤立符号后不切
    if j < n:
        if not text[j].isspace():
            return False  # 小数点/URL/路径：句点后不是空白
        # 单字母尾（首字母 J. / 缩写链 U.S. 的最后一个点）
        if (i >= 2 and not text[i - 2].isalnum()
                and prev_ch.isascii() and prev_ch.isalpha()):
            return False
        # 缩写词表：取句点前连续字母/点组成的最长尾串比对
        m = _EN_WORD_TAIL_RE.search(text[max(0, i - 12):i])
        if m:
            tok = m.group().lower()
            if tok in _EN_ABBREV_TAILS or tok.lstrip(".") in _EN_ABBREV_TAILS:
                return False
        return True
    # 文本末尾的句点：已排除省略号与孤立符号，视为正常句子结束
    return True


def split_sentences(text):
    """Split script text into sentences by terminal punctuation.

    中文按 。！？（含中文分号 ；、ASCII 分号 ;、换行）切分；ASCII 的 .!?
    作为补充终止符带边界守卫地参与（. 需通过 _is_english_sentence_end，
    !? 需后随空白/文末）——中英混合稿里的英文句子不再整段粘成一个"句子"
    （单次 TTS 文本过长、韵律崩坏、45 字超长告警刷屏），而小数点、缩写、
    省略号、人名首字母均不会被误切。纯中文稿的行为保持现有断句契约。
    """
    text = text.strip()
    if not text:
        return []
    parts = []
    start = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in _CN_TERMINATORS:
            i += 1
            parts.append(text[start:i])
            start = i
            continue
        if ch == ".":
            if _is_english_sentence_end(text, i):
                i += 1
                parts.append(text[start:i])
                start = i
                continue
        elif ch in "!?":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "" or nxt.isspace():
                i += 1
                parts.append(text[start:i])
                start = i
                continue
        i += 1
    if start < n:
        parts.append(text[start:])
    sentences = []
    for p in parts:
        s = p.strip()
        # 过滤游离标点；单字句是有效内容。
        if s and (len(s) > 1 or s.isalnum()):
            sentences.append(s)
    # 不自动合并短句。一个作者终止句就是一个视觉步 / 字幕步。
    # TTS 可能不喜欢过短句，但应告警，让作者主动改稿。
    short = [x for x in sentences if len(x) < 5]
    if short:
        print(f"[warn] 有 {len(short)} 句少于 5 字：不会自动合并，请按需要在讲稿中主动改写。", file=sys.stderr)
    return sentences
