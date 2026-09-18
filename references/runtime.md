# 运行时契约

> `scripts/interactive_runtime.js` 是**交互内核**，不是播放器。

## 1. 它负责什么

它只负责：

- 给交互元素接事件；
- 处理 pointer / tap / drag；
- 根据 `data-interaction` 判断答案；
- 正确时写 `data-locked="1"`；
- 反复接线时保持幂等。

它不负责：

- `audio.currentTime`；
- 时间轴解析；
- 场景切换；
- 字幕；
- 进度条；
- 掌握度 / 学习记录。

## 2. 四种交互

### `choice`

```html
<div data-interaction data-interaction-type="choice">
  <button data-choice-id="a">选项 A</button>
  <button data-choice-id="b">选项 B</button>
  <div class="interaction-feedback" hidden></div>
</div>
```

配置：

```json
{
  "options": [
    {"id":"a","label":"错误答案","correct":false,"feedback":"再想一步。"},
    {"id":"b","label":"正确答案","correct":true,"feedback":"对。"}
  ]
}
```

### `hotspot`

可点击元素带 `data-hotspot-id="node-1"`。配置与 `choice` 相同，也是 `options[]`。

### `sequence`

```html
<ul class="sequence-list">
  <li class="sequence-item" data-sequence-id="a">…</li>
</ul>
<button data-sequence-submit>提交顺序</button>
```

配置：

```json
{"correct_order":["a","b","c"]}
```

支持拖拽；触屏用 runtime 的点选兜底。

### `bucket`

```html
<li class="bucket-item" data-bucket-item="a">…</li>
<div data-drop data-bucket-id="left"><ul data-drop-slot></ul></div>
<button data-bucket-submit>提交归类</button>
```

配置：

```json
{
  "answer":{"a":"left","b":"right"},
  "feedback":{"a":"解释为什么放错。"}
}
```

既支持拖拽，也支持“点条目 → 点筐”。

## 3. 放行契约

正确答案必须最终走：

```js
finish(el, msg, {correct:true});
```

runtime 随后写：

```js
el.dataset.locked = '1';
```

页面应使用 `MutationObserver` 或等价的事件式观察来放行，**不要轮询 `data-locked`**。

错误答案：显示反馈、保持未锁定、继续按钮仍不可用。

## 4. 动态交互

页面动态生成交互块后调用：

```js
window.coursewareStudioWire();
```

入口幂等：runtime 用节点级 `data-bound` / `data-gesture` 标记保证重复接线不叠加监听、不重复绑定。动态 gate shell 初始可以只有空的 `data-interaction` 占位；真正配置写入后，runtime 会严格校验题型契约（校验失败会先把其余块接完，再统一抛错给 QA 捕获）。

## 5. 配置契约

`choice` / `hotspot`：配置与 DOM 的 id 集合必须完全一致，且恰好一个 `correct:true`。

`sequence`：`correct_order` 必须存在、id 唯一，并完整覆盖全部 `.sequence-item`。

`bucket`：`answer` 必须完整覆盖全部 `.bucket-item`，并且值只能引用现有 `data-bucket-id`。

契约不满足时 runtime 直接抛错，让浏览器 QA 捕获，而不是静默把错误题目当成“永远答不对”。

## 6. 接入原则

把 `scripts/interactive_runtime.js` 复制到课件输出目录，与 `index.html` 同级。`references/` 不放副本。

页面自己拥有：

```js
RENDER
sentAt
syncGate
tick
```

runtime 不应重新实现其中任何一层。

## 7. QA 契约（`check_gates.py` 浏览器冒烟依赖）

探针不驱动真实播放，而是桩掉 `currentTime` 后 dispatch `timeupdate`，因此页面必须给 QA 留下这些稳定的观察点：

| 钩子 | 用途 |
|---|---|
| `#main-audio` | 唯一时钟源；`timeupdate` 驱动 `tick()` |
| `#lesson-timeline`（`<script type="application/json">`） | 逐句字幕与场景区间的比对基准 |
| `#cap-text[data-courseware-caption]` | 字幕正文节点；QA 读它的 `textContent` 与祖先链可见性 |
| `#gate` | 门禁浮层容器（`hidden` 属性 = 开关） |
| `#gate-host`（`dataset.stepId`） | 当前门禁归属的场景 id，QA 用它对上时间轴 |
| `#gate-next` | 答对后出现的「继续」按钮 |
| `#gate-go` | 放行按钮，QA 用它关闭门禁 |
| `#pregate` | preGate 过渡层；QA 视「gate 或 pregate 任一未 hidden」为门禁活动中，据此等待过渡结束 |

改动这些 id 或语义等于改 QA 契约：探针会把「配了门禁却从未弹出」「门禁在句中标位置打开」等判为失败。静态侧还要求 `RENDER = {sceneId: renderer}` 与 `var GATES = [{scene:'…'}]` 保持可解析的字面量形态（推送式拼装逃得过静态解析，但逃不过上面的活动驱动检测）。
