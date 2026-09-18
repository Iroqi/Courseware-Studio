# 页面骨架

> 写页面时读。`references/template.html` 是最终结构范本，本文件只说明布局契约。

## 1. 页面结构

```text
shell
├─ 左栏
│  ├─ 章节头
│  ├─ stage
│  │  ├─ SVG 舞台
│  │  ├─ 画布内字幕
│  │  ├─ 播放层
│  │  └─ 门禁（可选）
│  └─ rack：播放 / 进度 / 章节刻度
└─ 右栏：参考资料
```

窄屏退成单栏，侧栏放在舞台之后。

## 2. 舞台

```css
.stage{
  position:relative;
  overflow:hidden;
}
.stage svg{
  display:block;
  width:100%;
  height:auto;
  aspect-ratio:<viewBox-width>/<viewBox-height>;
}
```

不要给舞台单独设 `max-width`，不要用 `flex:1` 或 `height:100%` 把舞台拉满高度。

舞台宽度就是左栏宽度。想整体收窄，收 `.shell`，不要单独封顶 SVG。

`viewBox` 比例应该由场景内容并集决定，不要为了“填满屏幕”制造大块空白。

## 3. 播放层

未开始时用画布内播放层提示用户开始；开始后由 `stage[data-state]` 切换到播放态。

播放层只是页面 UI：

```js
audio.play()
audio.pause()
```

播放器本身不拥有时间轴。

## 4. 字幕

字幕必须长在 SVG 里：

```html
<g id="cap">
  <line ... />
  <text id="cap-text" data-courseware-caption="1"></text>
</g>
```

要求：位置固定、字号固定、画面底部保留带，文案唯一来自 `runtime.narration[].text`。

## 5. 播放器行

播放器行紧贴舞台下面：

```text
▶  ⟲  [progress / chapter marks]  3/8 · 标题 · 时间
```

它常驻。进度条同时承担章节地图，不再在舞台上方另排一行章节导航。

门禁打开时播放器行只变灰、不可绕过门禁。

## 6. 侧栏

侧栏只放：

- 参考要点；
- 少量不阻塞播放的手感实验。

侧栏内容不跟随旁白逐句高亮，也不写进证据。

## 7. 层叠

```text
SVG 内容 0
播放层 5
门禁 9
```

字幕是 SVG 的最后一组内容，所以属于画布的一部分。

## 8. 交付

源形态：

```text
index.html
interactive_runtime.js
audio/
```

直接把这个目录交出去即可。Courseware Studio 不自带单文件 bundler；需要单文件时交给通用的 Artifact / Web 处理能力，不把它变成这个 skill 的基础设施。
