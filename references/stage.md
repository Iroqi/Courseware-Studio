# 舞台与逐句渲染

## 1. Renderer 的职责

renderer 接收一个句序号：

```js
renderScene(step)
```

输出这一句应该看到的 SVG / DOM 状态。

主循环负责：

```text
audio.currentTime
  ↓
sentAt(t)
  ↓
如果 scene / index 变了 → RENDER[scene](index)
```

## 2. 四条纪律

### 纪律 1：不在 renderer 里计时

禁止：

```js
setInterval(...)
setTimeout(...)
requestAnimationFrame(...)
```

一次性动效也绑定到句序号，而不是墙钟时间。

### 纪律 2：句号决定步数

一幕有 6 句旁白，就按 6 个主要视觉步设计。不要凭感觉再造第 7 个中间状态。

### 纪律 3：字幕不属于 renderer

renderer 画图，时间轴写字幕。

如果图元文字与旁白长句高度相似，交付检查器会给出提示。

### 纪律 4：图形表达优先于装饰

| 结构 | 常见形式 |
|---|---|
| 顺序 | 流程图 |
| 关系 | 关系图 / 连线 |
| 系统组成 | 分区 / 架构图 |
| 对比 | 双栏 / 对照矩阵 |
| 状态 | 状态块 / 状态图 |

装饰可以有，但不要因此再造一个时间系统。

## 3. 一次性效果

```js
if (step >= 3) showToken();
if (step === 4) flashOnce();
```

阈值直接写成常量，不用由句数推导出的魔法表达式。

## 4. 字幕区

模板提供：

```html
<text id="cap-text" data-courseware-caption="1"></text>
```

每次 `sentAt(t)` 返回当前句时，把该句对象交给 `capShow()`。页面不维护第二份字幕表。

## 5. 内容底线

字幕带有固定区域。主要图形留在分隔线以上，不与字幕重叠。

真实尺寸由你的 `viewBox` 与内容并集决定，不把模板某个数字当成通用魔法数字。

## 6. 自检

- [ ] 一个句子对应一个主要视觉步；
- [ ] renderer 不计时；
- [ ] 没有第二份字幕正文；
- [ ] 图元文字是必要的短标注；
- [ ] 关键图形没有压住字幕；
- [ ] `check_gates.py` 没有报高相似度双字幕提示。
