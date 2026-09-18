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

`audio/sentences/` 及其中的 `.sha` / `.failed` 文件是 `--resume` 使用的 TTS 工作缓存，**不属于最终课件交付物**。交付时只复制 `audio/combined.wav` 和 `audio/narration_timing.json`；不要把整个 `audio/` 目录当成成品模板。

当前 `narration.py` 是 **MiMo TTS 适配器**；Courseware Studio 真正需要的是：

```text
音频 + 全局 sentence timing + 原文 text
```

未来可以由其它 TTS / 其它 skill 提供同样的数据，而不修改页面层。

常用开关：`--dry-run`、`--resume`、`--speed`、段级 `speed` / `voice_id` / `voice_style`、`--on-fail silence`。

`--bgm` 一旦指定就必须指向存在的文件；找不到会直接失败，避免最终成品静默缺少用户要求的背景音乐。

改了语速就必须重新生成音频与时间轴。

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

默认用户级配置：

```text
~/.config/courseware-studio/.env
```

也可直接使用环境变量或 CLI 参数。不要读取其它 skill 的私有配置目录。

## 6. 自检

- [ ] dry-run 后句数和视觉步数对得上；
- [ ] 短句是显式改稿，不依赖脚本静默合并；
- [ ] `hl` 没有越界；
- [ ] 时间轴已内联；
- [ ] 最终音频始终是 `audio/combined.wav`，BGM / loudness 不产生第二个交付文件名；
- [ ] `synth_failed` 默认视为交付失败；只有明确要保留降级成片时才用 `--allow-degraded`；
- [ ] 运行 `check_gates.py` 检查最终字幕、时间轴、renderer、门禁与 JS。
