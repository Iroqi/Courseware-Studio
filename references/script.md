# 讲稿与音频

## 1. 旁白脚本

```json
{
  "title": "主题",
  "opening": "先抛一个问题。",
  "segments": [
    {
      "id": "seg-1",
      "title": "第一段",
      "tagline": "一句话提示",
      "hl": [2],
      "text": "第一句。第二句。第三句。"
    }
  ],
  "closing": "最后给一个判断标准。"
}
```

`text` 是真正被朗读的正文，同时也是最终字幕的唯一文本来源。

多人对话（问答 / 情景剧）用顶层 `speakers` + 段落 `dialogue` 代替该段的 `text`：

```json
{
  "speakers": {
    "A": {"label": "小明", "voice_id": "…", "voice_style": "…"},
    "B": {"label": "小钢", "voice_id": "…"}
  },
  "segments": [
    {
      "id": "seg-2",
      "title": "一轮问答",
      "dialogue": [
        {"speaker": "A", "text": "第一问。第二句。"},
        {"speaker": "B", "text": "这是回答。"}
      ],
      "hl": [3]
    }
  ]
}
```

每一轮**独立分句**（短句不会被并进下一位说话人）；turn 级音色取 `speakers[speaker]`，
比段级 `voice_id` / `voice_style` 更细。落成的时间轴句子条目带 `speaker`（即 `label`），
`build_timeline.py` 透传为 `runtime.narration[i].speaker`，页面可用它标"谁在说"；
`hl` 仍按**整段总句序**从 1 数起，跨轮连续计数。

## 2. 分句原则

一条作者句子 = 一个视觉步 = 一个字幕步。

脚本按终止标点分句，并对英文句点做缩写 / 小数点守卫。**不会自动把短句并到下一句**；少于 5 字只告警。

建议：

- 大多数句子 12–28 字；
- 超过 40 字优先拆句；
- 每句用 `。！？` 或等价终止标点收尾；
- `hl` 从 1 开始计数；
- 不要朗读“如图所示”“请看这里”。

## 3. TTS

```bash
python scripts/narration.py --source narration-source.json -o audio
```

输出：

```text
audio/combined.wav
audio/narration_timing.json
```

未使用 `--resume` 时，句子音频只保存在临时工作目录，结束后清理；使用 `--resume` 时，缓存默认写在输出目录同级的 `.courseware-cache/<输出目录名>/sentences/`（输出目录名为 `audio` 时即 `.courseware-cache/audio/sentences/`），也可通过 `--cache-dir` 指定。无论哪种模式，`audio/` 交付目录只包含 `combined.wav` 与 `narration_timing.json`，不要把缓存目录当成成品模板。

当前 `narration.py` 是 **MiMo TTS 适配器**；Courseware Studio 真正需要的是：

```text
音频 + 全局 sentence timing + 原文 text
```

未来可以由其它 TTS / 其它 skill 提供同样的数据，而不修改页面层。

常用开关：`--dry-run`、`--resume`、`--speed`、段级 `speed` / `voice_id` / `voice_style`、`--on-fail silence`。

语速优先级：`segments[].speed` > 全局 `--speed`；`opening_speed` / `closing_speed`（顶层键）单独覆盖开场与收尾，缺省时它们**跟随全局 `--speed`**（不钉死 1.0）。要"开场略慢"就写一个小于当前语速的值。

`--resume` 的缓存按"文本 + 音色 + 风格 + 模型 + 语速"指纹判定，改任何一项只有受影响的句子重烧。`--on-fail silence` 留下的失败静音占位带 `.failed` 标记；**下次 `--resume` 在默认 `--on-fail abort` 下会拒绝带着占位直接交付**（提示你删标记重试或显式改 `--on-fail silence`）。TTS 失败且连静音占位也没落成的句子会被整句丢弃，时间轴 `status` 记为 `degraded`（`degraded.dropped_sentence_count`）。

`--bgm` 一旦指定就必须指向存在的文件；找不到会直接失败，避免最终成品静默缺少用户要求的背景音乐。

改了语速就必须重新生成音频与时间轴（指纹含语速，`--resume` 会自动重烧受影响的句子）。

## 4. 时间轴

```bash
python scripts/build_timeline.py \
  --timing audio/narration_timing.json \
  --source narration-source.json \
  -o timeline.html
```

`--bare` 输出裸 JSON；与 `-o timeline.json` 联用时会把裸 JSON 写入该文件，诊断信息仍输出到 stderr。

结果内联为：

```html
<script type="application/json" id="lesson-timeline">…</script>
```

时间是**全局秒**。页面直接读取 `runtime.narration[i]`，不要在运行时再 fetch 外部 JSON。

## 5. TTS 配置

密钥解析优先级：CLI 参数 > 系统环境变量 > 项目级 `.env` > 用户级 `.env`：

```text
~/.config/courseware-studio/.env
```

项目级 `.env` 从讲稿/输出所在目录向上找，**走到当前工作目录即停**：命中带 `.git` 的
项目根也停（该根的 `.env` 是最后一个候选）；不会越过 cwd 去吸它的父目录、更不吸盘根
或别人目录里的 `.env`。`.env` 值支持成对引号与行内注释：
`KEY=sk-xxx # 备注` 取 `sk-xxx`，`KEY="sk-a#b"` 里的 `#` 是值的一部分；
不带引号时，只有"空白 + `#`"才起注释作用。

也可直接使用环境变量或 CLI 参数。不要读取其它 skill 的私有配置目录。

## 6. 自检

- [ ] dry-run 后句数和视觉步数对得上；
- [ ] 短句是显式改稿，不依赖脚本静默合并；
- [ ] `hl` 没有越界；
- [ ] 时间轴已内联；
- [ ] 最终音频始终是 `audio/combined.wav`，BGM / loudness 不产生第二个交付文件名；
- [ ] `synth_failed` 默认视为交付失败；只有明确要保留降级成片时才用 `--allow-degraded`；
- [ ] 运行 `check_gates.py` 检查最终字幕、时间轴、renderer、门禁与 JS。
