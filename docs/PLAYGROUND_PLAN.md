# M-Flow Playground — 设计与实施计划

> 一个融合 LLM 对话、人脸识别、指代消解的交互式 Playground 界面。  
> 让 AI 能看到与它对话的人，记住每个人说过的话，并随着对话推进持续构建记忆。

---

## 目录

- [产品愿景](#产品愿景)
- [系统架构](#系统架构)
- [数据流设计](#数据流设计)
- [前端设计](#前端设计)
- [后端 API 设计](#后端-api-设计)
- [核心模块详细设计](#核心模块详细设计)
- [边界情况处理](#边界情况处理)
- [实施阶段](#实施阶段)

---

## 交叉验证修订记录

> 以下为逐项读取 M-Flow 和 Fanjing-Face-Recognition 源码后发现的技术假设偏差及修正。

| 计划假设 | 实际代码 | 修正 |
|---------|---------|------|
| `/api/v1/responses` 支持 `messages` 数组 + `system` prompt + SSE 流式 | `ResponseRequest` 只有 `input: str`，一次性 JSON 返回，无 SSE，无 system 字段 | Playground 后端直接调 Python 内部 API（`AsyncOpenAI` 或 LiteLLM），自行构建 messages + system + stream，不走 HTTP `/api/v1/responses` |
| `/api/v1/add` 接收 JSON body + `dataset_id` | 实际为 `multipart/form-data`，`datasetId` 是表单字段 + 文件上传 | Playground 后端直接调内部 Python 函数 `m_flow.api.v1.add.add.add()`，传入 `DataInput` 对象，绕过 HTTP 表单限制 |
| coreference 有独立 HTTP 消解接口 | HTTP 仅提供配置/统计/reset；实际消解在 `/api/v1/search` 预处理或 Python 内部 `preprocess_query_with_coref_async` | Playground 后端直接调 `preprocess_query_with_coref_async(query, session_id=...)` |
| fanjing-face-recognition `/api/persons` 返回 `speaking: bool` 和 `identity_state` | 实际字段为 `mouth: str`（"speaking"/"not_speaking"/"occluded"）和 `identity: str` | 前后端适配实际字段名 |
| fanjing-face-recognition 有"新注册面孔"通知 API | 无此 API，仅能轮询 `/api/persons` | Playground 后端轮询 persons 列表，对比已知 `registered_id` 集合，检测新面孔 |
| Playground 后端通过 HTTP 调 M-Flow API 需认证 | 同进程内可直接调 Python 函数，绕过 FastAPI 认证链 | 后端编排层使用内部 Python API 调用，无需 HTTP 认证 |

**第二轮验证修订（业务逻辑深度审查）**:

| 计划假设 | 验证发现 | 修正 |
|---------|---------|------|
| 通过 `mouth==="speaking"` 判断"谁在和 AI 对话" | `mouth` 是视觉嘴部检测，用户键盘打字时嘴不动，永远不会被识别为 speaking。此判据在文字输入模式下完全失效 | 改为多层说话者归因策略（见下方新增章节） |
| 对话 episode 推送到每个参与者的数据集 | 同一段对话写入多个 dataset → 多数据集并集检索时同一内容重复命中 | 改为主写一份 + episode_id 关联表去重 |
| 未说明 fanjing-face-recognition pipeline 的启停协调 | Playground 打开时若 fanjing-face-recognition 未 `/api/start`，`/api/persons` 返回空缓存，所有视觉功能不可用 | 新增 session 创建时自动启动 fanjing-face-recognition pipeline |
| 未说明 M-Flow 如何获知 fanjing-face-recognition 的 API Key | fanjing-face-recognition 未设 `FACE_API_KEY` 环境变量时每次启动随机生成 Key，M-Flow 无法调用需认证的 fanjing-face-recognition API | 强制要求通过共享环境变量 `FACE_API_KEY` 配置同一 Key |
| 文字输入 vs 语音输入未明确 | 影响整个说话者识别逻辑的设计基础 | 明确 MVP 为文字输入，语音作为扩展，说话者归因独立于输入方式 |

**第三轮验证修订（入库+检索链路深度审查）**:

| 计划假设 | 实际代码 | 修正 |
|---------|---------|------|
| `add()` 推送后内容即可检索 | `add()` 只入库原始内容，**必须再调 `memorize()` 才能生成可检索的 Episode/向量** | flush 改为两步：add() + memorize()，memorize 异步执行不阻塞对话 |
| 通过 episode_id 元数据实现去重 | `add()` 和 `search()` 均不支持自定义元数据字段 | 改为内容差异化（全文 vs 摘要）+ `use_combined_context=True` 天然去重 |
| 将人脸上下文作为 candidate entities 注入 coref | `preprocess_query_with_coref_async` 无此参数 | 改为通过格式化文本历史隐式引导（"[张三] 消息内容"） |
| 检索结果包含相似度分数 `score` | search API 返回 LLM 答案/上下文，不暴露原始向量分数 | 修正响应格式为 `dataset_id + context` |

**关键架构调整**:
1. Playground 后端不走 HTTP 自调自的 M-Flow API，而是直接调用 Python 内部函数
2. 说话者归因从"视觉嘴部检测"改为"多层身份推断"策略

---

## 产品愿景

### 核心体验

用户坐在摄像头前与 AI 对话。AI：
1. **认识**正在说话的人（通过人脸识别）
2. **记得**之前与每个人聊过的内容（通过 M-Flow 记忆图谱）
3. **理解**"他/她/那个人"指的是谁（通过指代消解）
4. **区分**多人场景下各自的记忆（取并集检索）

### 用户故事

```
张三走到摄像头前：
  AI: "张三你好！上次你提到的论文投稿进展如何？"

李四也走过来：
  张三: "他也在做那个项目"
  AI 理解: "他" → 李四（coreference）
  AI 检索: 张三数据集 ∪ 李四数据集（多人并集）
  AI: "李四，张三说你也在做那个项目。你负责哪部分？"

新面孔出现：
  AI: "你好！我还不认识你，请问怎么称呼？"
  用户: "我叫王五"
  → fanjing-face-recognition 自动注册 → M-Flow 新建"王五"数据集 → 关联
```

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    M-Flow Frontend (Next.js)                 │
│  ┌───────────────────────────────────────────────────────┐   │
│  │                  Playground 视图                       │   │
│  │  ┌──────────────┐  ┌──────────────────────────────┐   │   │
│  │  │ 视频区域      │  │  对话区域                     │   │   │
│  │  │ MJPEG + 叠加  │  │  消息气泡流                   │   │   │
│  │  ├──────────────┤  │  流式打字效果                  │   │   │
│  │  │ 在场人员卡片  │  │  记忆状态指示器                │   │   │
│  │  │ 数据集关联    │  ├──────────────────────────────┤   │   │
│  │  │ [编辑] 按钮   │  │  输入框 + 发送                │   │   │
│  │  └──────────────┘  └──────────────────────────────┘   │   │
│  └───────────────────────────────────────────────────────┘   │
└──────────┬──────────────────────────────┬────────────────────┘
           │                              │
      HTTP/WS                        HTTP/WS
           │                              │
┌──────────▼──────────┐    ┌──────────────▼────────────────────┐
│   Fanjing-Face-Recognition          │    │        M-Flow API                 │
│   :5001              │    │        :8000                      │
│                      │    │                                   │
│ /api/persons         │◄───┤  /api/v1/playground/context       │
│ /api/stats           │    │  /api/v1/playground/chat          │
│ /api/person/rename   │    │  /api/v1/playground/flush         │
│ /video_feed          │    │  /api/v1/playground/link-face     │
│                      │    │  /api/v1/playground/persons       │
│                      │    │                                   │
│                      │    │  直接调用内部 Python API:           │
│                      │    │  - LLM: AsyncOpenAI (stream=True) │
│                      │    │  - 数据集: datasets CRUD 函数      │
│                      │    │  - 检索: search() + coref 预处理   │
│                      │    │  - 摄入: add() 内部函数            │
│                      │    │  - 指代消解: preprocess_coref()    │
└──────────────────────┘    └───────────────────────────────────┘
```

### 为什么在 M-Flow 后端做编排（而非纯前端）

| 关注点 | 前端编排 | 后端编排（选择） |
|--------|---------|----------------|
| Token 计数 | 需在前端实现 tokenizer | 服务端有精确 tokenizer |
| 指代消解 | 需暴露 coref 内部状态到前端 | 服务端直接调 Python 模块 |
| 人脸↔数据集映射 | 存 localStorage（丢失风险） | 存数据库（持久化） |
| Fanjing-Face-Recognition API Key | 需下发到浏览器（泄露风险） | 后端 server-to-server（安全） |
| 记忆推送 | 前端调 /api/v1/add（需认证） | 后端内部调用（无跨域） |

---

## 数据流设计

### 对话完整流程

```
用户输入文本
    │
    ▼
[1] POST /api/v1/playground/chat
    │   body: { message, session_id, speaker_face_id }
    │
    ▼
[2] 服务端处理:
    ├── 从 fanjing-face-recognition /api/persons 获取当前在场人员（字段: mouth, identity, registered_id）
    ├── 确定说话者（多层归因，见下方"说话者归因策略"）
    ├── 查找说话者对应的 M-Flow 数据集
    ├── 指代消解（"他" → 具名实体）
    ├── 构建上下文:
    │   ├── 系统提示: "你在和 {说话者} 对话，在场还有 {其他人}"
    │   ├── 短期记忆: 最近 N 轮对话
    │   └── 长期记忆: 从相关数据集检索（多人取并集）
    ├── 直接调用 LLM Python SDK (AsyncOpenAI, stream=True)
    ├── 追加到短期记忆缓冲
    └── 检查是否触发推送 → 若触发则异步推送到长期记忆
    │
    ▼
[3] 返回:
    { reply, speaker, persons_in_frame,
      memory_status: { buffer_tokens, buffer_turns, flushed } }
```

### 短期记忆 → 长期记忆推送

```
短期记忆缓冲区 (服务端 session 内存)
┌──────────────────────────────────────┐
│ turn 1: [张三] "帮我查下项目进度"       │
│ turn 2: [AI]  "张三，你上次提到..."     │
│ turn 3: [张三] "对，就是那个"           │
│ turn 4: [李四] "我也需要看一下"         │  ← 累计 token > 阈值
│ turn 5: [AI]  "好的，李四..."          │
└──────────────────────────────────────┘
                    │
            触发推送（token > 2000 或 turns > 10）
                    │
                    ▼
    ┌───────────────────────────────┐
    │  打包为 Episode:              │
    │  - 参与者: [张三, 李四]        │
    │  - 内容: 格式化的对话文本       │
    │  - 时间戳: 起止时间            │
    │  - 元数据: session_id 等       │
    │                               │
    │  推送两步走:                     │
    │  Step 1 — add():               │
    │  → 张三数据集: 完整对话全文      │
    │  → 李四数据集: 参与摘要          │
    │  Step 2 — memorize():           │
    │  → 对两个数据集触发记忆化        │
    │  → 生成 Episode/Facet/Entity    │
    │  → 写入向量索引和知识图谱        │
    │  （异步执行，不阻塞对话）        │
    └───────────────────────────────┘
```

### 人脸识别 ↔ M-Flow 数据集 关联

```
Fanjing-Face-Recognition                          M-Flow
┌──────────────┐                ┌──────────────────┐
│ registered_id │ ── mapping ──▶│ dataset_id        │
│ R#1 (张三)    │       ▲       │ ds_abc123 (张三)  │
│ R#2 (李四)    │       │       │ ds_def456 (李四)  │
│ R#3 (新面孔)  │       │       │ (待创建)          │
└──────────────┘       │       └──────────────────┘
                       │
              playground_face_mapping 表
              ┌──────────────────────────┐
              │ face_registered_id │ INT  │
              │ dataset_id         │ STR  │
              │ display_name       │ STR  │
              │ auto_created       │ BOOL │
              │ created_at         │ TS   │
              └──────────────────────────┘
```

**映射生命周期：**
1. **自动创建**：fanjing-face-recognition 注册新面孔 → playground 检测到新 registered_id → 自动创建 M-Flow 数据集 → 写入映射
2. **手动关联**：用户在 UI 上将已有面孔关联到已有数据集（或修改关联）
3. **重命名同步**：在 playground UI 重命名人员时，同步更新 fanjing-face-recognition display_name 和 M-Flow 数据集名称

---

## 前端设计

### 布局（高级灰色系，简约现代）

```
┌─────────────────────────────────────────────────────────────────┐
│  ◉ Playground                                    [设置] [返回]  │  ← 顶栏
├───────────────────────────┬─────────────────────────────────────┤
│                           │                                     │
│   ┌───────────────────┐   │   ┌─────────────────────────────┐   │
│   │                   │   │   │  🤖 AI                      │   │
│   │   实时视频画面     │   │   │  "张三你好！上次你提到..."   │   │
│   │   (MJPEG 流)      │   │   │                             │   │
│   │   叠加人脸框+标签  │   │   │  👤 张三                    │   │
│   │                   │   │   │  "帮我查下项目进度"          │   │
│   └───────────────────┘   │   │                             │   │
│                           │   │  🤖 AI                      │   │
│   ┌───────────────────┐   │   │  "好的，查到以下信息..."     │   │
│   │ 在场人员           │   │   │                             │   │
│   │ ┌───┐ 张三 🎤      │   │   │  💾 记忆已保存 (2.1k tok)   │   │
│   │ │ 📷│ ds:项目A     │   │   │                             │   │
│   │ │   │ [编辑关联]   │   │   └─────────────────────────────┘   │
│   │ └───┘              │   │                                     │
│   │ ┌───┐ 李四 🔇      │   │   ┌─────────────────────────────┐   │
│   │ │ 📷│ ds:项目B     │   │   │  输入消息...         [发送]  │   │
│   │ │   │ [编辑关联]   │   │   └─────────────────────────────┘   │
│   │ └───┘              │   │                                     │
│   │ ┌───┐ 未知 ⚠️      │   │   记忆缓冲: ████████░░ 1.8k/2k    │
│   │ │ 📷│ [关联数据集] │   │   对话轮次: 7/10                    │
│   │ └───┘              │   │                                     │
│   └───────────────────┘   │                                     │
│                           │                                     │
├───────────────────────────┴─────────────────────────────────────┤
│  状态: 🟢 已连接  │  FPS: 28  │  当前说话者: 张三  │  Session: a3f│
└─────────────────────────────────────────────────────────────────┘
```

### 色彩系统（高级灰）

```css
:root {
  --pg-bg:         #0a0a0a;     /* 主背景 */
  --pg-surface:    #141414;     /* 卡片/面板 */
  --pg-elevated:   #1e1e1e;     /* 悬浮/选中 */
  --pg-border:     #2a2a2a;     /* 边框 */
  --pg-text:       #e0e0e0;     /* 主文字 */
  --pg-text-dim:   #808080;     /* 次要文字 */
  --pg-accent:     #6b8afd;     /* 强调色（冷蓝） */
  --pg-ai-bubble:  #1a1d23;     /* AI 消息背景 */
  --pg-user-bubble:#1e2a1e;     /* 用户消息背景（微绿） */
  --pg-speaking:   #4da378;     /* 说话中指示器 */
  --pg-warning:    #e5a84b;     /* 警告/新面孔 */
}
```

### 组件清单

| 组件 | 位置 | 职责 |
|------|------|------|
| `PlaygroundView` | `components/playground/PlaygroundView.tsx` | 顶层布局，管理 session 状态 |
| `VideoPanel` | `components/playground/VideoPanel.tsx` | MJPEG 视频 + 人脸叠加层 |
| `PersonCard` | `components/playground/PersonCard.tsx` | 在场人员卡片（头像/名称/数据集/编辑） |
| `ChatPanel` | `components/playground/ChatPanel.tsx` | 消息列表 + 输入框 + 流式打字 |
| `ChatMessage` | `components/playground/ChatMessage.tsx` | 单条消息气泡（区分 AI/用户/系统） |
| `MemoryIndicator` | `components/playground/MemoryIndicator.tsx` | 缓冲进度条 + 推送状态 |
| `DatasetLinker` | `components/playground/DatasetLinker.tsx` | 关联/修改数据集的弹窗 |
| `PlaygroundSettings` | `components/playground/PlaygroundSettings.tsx` | 设置面板（阈值/模型/fanjing-face-recognition 地址） |

---

## 后端 API 设计

### 新增路由：`/api/v1/playground/*`

> **认证**: 所有 playground 路由使用 M-Flow 标准认证（`Depends(get_authenticated_user)`），
> 与 datasets/search 等路由一致。session 绑定到 `user_id`，不同用户看不到彼此的 session。

#### `POST /api/v1/playground/chat`

主对话接口。接收用户消息，通过 SSE 流式返回 AI 回复和上下文信息。

> **实现说明**: 不走 `/api/v1/responses` HTTP API（其不支持 messages 数组和 SSE），
> 而是直接调用 LLM Python SDK（AsyncOpenAI / LiteLLM）以 `stream=True` 模式推理，
> playground 路由自行构建 FastAPI `StreamingResponse` 实现 SSE。

```json
// Request
{
  "session_id": "pg_abc123",
  "message": "帮我查下项目进度",
  "speaker_face_id": 1       // 前端显式指定; null 时由后端按归因策略推断
}

// Response (SSE stream, 由 playground 路由自行实现)
event: token
data: {"text": "好的"}

event: token
data: {"text": "，"}

event: token
data: {"text": "张三"}

event: done
data: {
  "full_reply": "好的，张三，根据你上次提到的...",
  "speaker": {
    "face_registered_id": 1,
    "display_name": "张三",
    "dataset_id": "ds_abc123"
  },
  "persons_in_frame": [
    {"face_registered_id": 1, "name": "张三", "dataset_id": "ds_abc123", "speaking": true},
    {"face_registered_id": 2, "name": "李四", "dataset_id": "ds_def456", "speaking": false}
  ],
  "memory_status": {
    "buffer_tokens": 1847,
    "buffer_turns": 7,
    "threshold_tokens": 2000,
    "threshold_turns": 10,
    "flushed": false
  },
  "coref_resolutions": [
    {"original": "那个项目", "resolved": "张三提到的 AI 论文项目"}
  ],
  "retrieved_memories": [
    {"dataset_id": "ds_abc123", "dataset_name": "张三的记忆", "context": "张三曾在 3 天前讨论过 AI 论文投稿..."}
  ]
}
```

#### `POST /api/v1/playground/flush`

手动触发短期记忆推送到长期记忆。

```json
// Request
{ "session_id": "pg_abc123" }

// Response
{
  "ok": true,
  "episodes_created": 2,
  "datasets_affected": ["ds_abc123", "ds_def456"],
  "tokens_flushed": 1847,
  "turns_flushed": 7
}
```

#### `GET /api/v1/playground/persons`

获取当前在场人员及其数据集关联。

```json
// Response
[
  {
    "face_registered_id": 1,
    "display_name": "张三",
    "dataset_id": "ds_abc123",
    "dataset_name": "张三的记忆",
    "speaking": true,
    "identity_state": "KNOWN_STRONG",
    "auto_linked": true
  },
  {
    "face_registered_id": null,
    "display_name": "未知用户",
    "dataset_id": null,
    "speaking": false,
    "identity_state": "UNKNOWN_STRONG"
  }
]
```

#### `POST /api/v1/playground/link-face`

手动建立/修改人脸与数据集之间的关联。

```json
// Request
{
  "face_registered_id": 3,
  "dataset_id": "ds_existing_456",
  "display_name": "王五"
}

// Response
{ "ok": true, "created_new_dataset": false }
```

#### `POST /api/v1/playground/session`

创建/恢复 playground 会话。自动检测 fanjing-face-recognition 状态并按需启动 pipeline。

> **前提**: M-Flow 和 fanjing-face-recognition 必须配置相同的环境变量 `FACE_API_KEY`，
> 否则 M-Flow 无法调用 fanjing-face-recognition 需认证的 API。

```json
// Request
{ "face_recognition_url": "http://localhost:5001" }

// Response
{
  "session_id": "pg_abc123",
  "face_recognition_status": "connected",   // "connected" | "pipeline_started" | "offline"
  "coref_session_id": "coref_xyz",
  "config": {
    "flush_token_threshold": 2000,
    "flush_turn_threshold": 10,
    "llm_model": "default",
    "face_recognition_url": "http://localhost:5001"
  }
}
```

---

## 核心模块详细设计

### Module 1: 短期记忆管理器 (`PlaygroundSession`)

```python
class PlaygroundSession:
    """管理单次 playground 会话的短期记忆和状态。"""

    session_id: str
    messages: list[dict]          # [{role, content, speaker, timestamp}]
    buffer_tokens: int            # 当前缓冲区 token 数
    buffer_turns: int             # 当前缓冲区对话轮次
    flush_token_threshold: int    # 推送阈值（默认 2000）
    flush_turn_threshold: int     # 推送阈值（默认 10）
    coref_session_id: str         # 指代消解会话 ID
    face_recognition_url: str            # fanjing-face-recognition 服务地址
    face_dataset_mapping: dict    # {face_registered_id: dataset_id}
    participants: set[int]        # 当前缓冲区中出现过的 face_registered_id

    # 持久化策略: session 元数据存 SQLite（M-Flow 已有数据库），
    # 短期消息缓冲存内存（推送后清空）。
    # M-Flow 重启时: session 元数据可恢复，未推送的缓冲丢失（已推送的安全）。
    # 前端 session_id 存 URL params，重连时可恢复 session 上下文。

    def add_message(self, role, content, speaker_face_id=None):
        """追加消息，更新 token 计数，检查是否需要推送。"""

    def should_flush(self) -> bool:
        """buffer_tokens >= threshold 或 buffer_turns >= threshold"""

    async def flush_to_long_term(self):
        """打包缓冲区为 episode，推送到长期记忆。分两步:
        1. add(): 原始内容入库（生成 Data 记录）
        2. memorize(): 将 Data 转化为可检索的记忆节点（Episode/Facet/Entity + 向量）
        两步都用内部 Python API 调用。memorize 是异步流水线，可能需要数秒。"""

    def get_context_for_llm(self, retrieved_memories) -> list[dict]:
        """构建发给 LLM 的完整 messages 列表（系统提示+记忆+对话历史）。"""
```

### Module 2: 人脸上下文桥接 (`FaceContextBridge`)

```python
class FaceContextBridge:
    """桥接 fanjing-face-recognition 和 M-Flow，维护人脸↔数据集映射。"""

    async def get_persons_in_frame(self) -> list[PersonInfo]:
        """调用 fanjing-face-recognition /api/persons，返回带数据集关联的人员列表。"""

    async def identify_speaker(self, speaker_face_id: Optional[int] = None) -> Optional[PersonInfo]:
        """多层说话者归因（Module 5）：显式ID > 单人推断 > 视觉辅助 > 上下文延续。"""

    async def ensure_dataset_for_face(self, face_registered_id, name) -> str:
        """若该面孔无关联数据集，自动创建并关联，返回 dataset_id。"""

    async def link_face_to_dataset(self, face_registered_id, dataset_id, name):
        """手动关联/修改。"""

    async def get_datasets_for_persons(self, face_ids: list[int]) -> list[str]:
        """返回一组面孔对应的所有 dataset_id（用于并集检索）。"""
```

### Module 3: 记忆检索 + 指代消解 (`MemoryRetriever`)

```python
class MemoryRetriever:
    """基于在场人员检索长期记忆，并处理指代消解。"""

    async def resolve_and_retrieve(self, query, coref_session_id, dataset_ids,
                                    persons_context: list[dict] = None) -> RetrievalResult:
        """
        1. 人脸上下文注入到对话历史（非 coref API 参数）:
           在每轮对话追加到 coref session 时，将用户消息格式化为
           "[张三] 帮我查下进度" 而非匿名 "帮我查下进度"。
           coref 基于文本历史消解，看到 "[张三]" 后可将后续 "他" 消解为张三。
           （注: preprocess_query_with_coref_async 不接受 candidate entities 参数，
           只能通过文本上下文隐式引导消解方向）
        2. 调用 preprocess_query_with_coref_async(query, session_id=coref_session_id)
        3. 调用内部 search() 函数，传入 dataset_ids + use_combined_context=True
        4. 多数据集自动取并集
        5. 返回检索结果 + 消解日志
        """
```

### Module 4: LLM 系统提示构建

```python
SYSTEM_PROMPT_TEMPLATE = """你是 M-Flow Playground 的 AI 助手。

当前在场人员：
{persons_description}

当前说话者：{speaker_name}

相关记忆：
{retrieved_memories}

指导原则：
- 称呼说话者的名字
- 引用之前的对话记忆时注明来源
- 如果不认识新面孔，友好地询问对方名字
- 多人在场时注意区分对象
"""
```

### Module 5: 说话者归因策略

> **核心问题**: `mouth` 字段是视觉嘴部检测，用户键盘打字时嘴不动。
> 不能用 `mouth==="speaking"` 来判断"谁在输入这条消息"。

**多层归因策略（优先级从高到低）：**

```
[1] 前端显式指定（最可靠）
    用户在 UI 上选择"我是谁"（多人场景下必须）
    → chat 请求携带 speaker_face_id

[2] 单人自动推断
    若画面中只有 1 个 identity==="KNOWN_STRONG" 的人 → 默认为说话者
    无需用户操作

[3] 视觉辅助（仅作参考信号）
    若有 mouth==="speaking" 的人 → 提升其作为说话者的权重
    但不作为唯一判据

[4] 上下文延续
    若上一轮对话已确定说话者且该人仍在画面中 → 延续
    直到有人离开或新人出现触发重新确认

[5] 未知时提问
    多人在场且无法确定 → AI 主动问"请问是谁在说话？"
    或 UI 弹出选择器
```

**前端实现：**

```
┌──────────────────────────────────────────┐
│ 当前发言者: [张三 ▼]                      │  ← 下拉选择器（多人时显示）
│                                          │
│ ┌──────────────────────────────────────┐ │
│ │ 输入消息...                    [发送] │ │
│ └──────────────────────────────────────┘ │
└──────────────────────────────────────────┘

单人在场时: 下拉选择器隐藏，自动绑定唯一在场者
无人在场时: 显示为"匿名"，对话仍可进行但无记忆归因
```

**chat API 调整：**

```json
// Request
{
  "session_id": "pg_abc123",
  "message": "帮我查下项目进度",
  "speaker_face_id": 1           // 前端显式指定，或 null 由后端推断
}
```

---

### Module 6: 记忆推送与去重策略

> **核心问题**: 同一段对话推送到多个参与者数据集会导致检索重复。
> **额外发现**: M-Flow 的 `add()` 和 `search()` 均不支持自定义元数据字段（如 episode_id），
> 无法在 API 层面做基于 ID 的去重。

**修正策略: 内容差异化 + `use_combined_context` 天然去重**

```
对话: 张三(8轮) + 李四(2轮)

推送时（add + memorize 两步）:
  1. 张三数据集 ← 完整对话全文（含所有参与者发言，带标注）
     格式: "[张三] 帮我查下项目进度\n[AI] 好的...\n[李四] 我也需要看看\n..."

  2. 李四数据集 ← 参与摘要（非全文，内容天然不同）
     格式: "李四参与了关于项目进度的对话。讨论了...。参与者: 张三、李四。"

  3. 分别对两个数据集调 memorize()，各自生成独立的 Episode

去重机制:
  - 多人并集检索使用 use_combined_context=True
  - M-Flow 对每个数据集分别检索后合并上下文，LLM 只调一次
  - 全文和摘要内容不同 → 不会产生逐字重复
  - LLM 天然融合多源上下文 → 不需要应用层去重
```

> **为什么不用 episode_id 去重**: M-Flow 的 `add()` 不支持附带自定义元数据，
> `search()` 也没有按自定义字段过滤的能力。通过内容差异化（全文 vs 摘要）
> + `use_combined_context=True` 的 LLM 合并，在不修改 M-Flow 核心代码的前提下
> 实现等效的去重效果。

---

### Module 7: 服务编排与 Session 生命周期

**fanjing-face-recognition ↔ M-Flow Playground 协调：**

```
Playground 打开
    │
    ├── POST /api/v1/playground/session 创建会话
    │       ├── 读取环境变量 FACE_API_KEY（必须与 fanjing-face-recognition 一致）
    │       ├── 尝试连接 fanjing-face-recognition /api/stats（健康检查）
    │       ├── 若 fanjing-face-recognition 未运行 → 降级模式（纯文字对话）
    │       ├── 若 fanjing-face-recognition 已运行但 pipeline 未 start →
    │       │       自动调 POST /api/start（摄像头模式）
    │       └── 返回 session_id + face_recognition_status
    │
    ▼
对话进行中...
    │
    ▼
Playground 关闭 / session 超时
    ├── 推送剩余短期记忆缓冲
    ├── 关闭 coref session
    └── 不停止 fanjing-face-recognition pipeline（其他功能可能在用）
```

**API Key 同步方案：**

```bash
# 部署时通过共享环境变量确保一致
export FACE_API_KEY="your-shared-secret-key"

# 启动 fanjing-face-recognition
cd fanjing-face-recognition && python run_web_v2.py

# 启动 m_flow（同一机器或通过 .env 传递）
cd m_flow && uv run mflow run
```

M-Flow playground 后端通过 `os.environ["FACE_API_KEY"]` 获取 Key，用于：
- 调用 fanjing-face-recognition 需认证的 API（`/api/start`、`/api/stop`、`/api/person/rename`）
- 生成 `/video_feed` 的签名 URL

---

## 边界情况处理

### 人脸识别相关

| 场景 | 处理方式 |
|------|---------|
| **无人在摄像头前** | 对话正常进行，不注入人脸上下文，LLM 提示为"当前无可见用户" |
| **新面孔（identity==="UNKNOWN_STRONG"）** | UI 显示"未知用户 ⚠️"，LLM 提示包含"有一位新面孔"，提供 [关联数据集] 按钮。检测方式：轮询 `/api/persons`，对比 `registered_id` 集合发现新 ID |
| **正在识别中（identity==="AMBIGUOUS"）** | UI 显示"识别中..."，暂不关联数据集，不检索记忆 |
| **多人在场，谁在打字？** | 单人时自动推断；多人时 UI 显示发言者选择器，用户手动选择后发送 |
| **说话者切换** | 人员进出画面时自动更新选择器列表；用户可随时切换 |
| **无人在场时打字** | 标记为"匿名"消息，对话正常进行但不归因到任何数据集 |
| **fanjing-face-recognition 服务不可达** | 降级为纯文字对话模式，视频区显示"视觉服务离线"，功能不中断。后台每 30 秒重试连接，恢复后自动重新 `/api/start` pipeline |
| **人脸与数据集解除关联** | 保留历史数据集但不再自动检索，可手动重新关联 |
| **多用户共享摄像头** | fanjing-face-recognition 绑定单摄像头，多个 playground session 共享同一视频/人脸数据。适用于本地演示场景；远程多用户场景需每用户独立 fanjing-face-recognition 实例 |

### 记忆相关

| 场景 | 处理方式 |
|------|---------|
| **缓冲区达到阈值时正在说话** | 等当前轮次完成后再推送（不打断对话） |
| **推送失败（M-Flow 不可达）** | 保留缓冲区，UI 显示"记忆保存失败，将在恢复后重试"，下次自动重试 |
| **session 超时** | 自动推送剩余缓冲区，关闭 coref session |
| **页面刷新** | session_id 存 URL params，可恢复（缓冲区丢失但已推送的安全） |
| **极长对话（>50k tokens）** | 每次推送后清空缓冲区，滑动窗口保留最近 5 轮作为 LLM 上下文 |

### 指代消解相关

| 场景 | 处理方式 |
|------|---------|
| **"他" 无法消解** | 保持原文不替换，LLM 根据上下文理解 |
| **"他" 消解错误** | 不会影响存储（存储原文+消解结果），LLM 有短期记忆可纠正 |
| **多语言混合** | coreference 模块支持中英文，根据检测到的语言自动切换 |

### 数据集关联相关

| 场景 | 处理方式 |
|------|---------|
| **一个人脸关联多个数据集** | 不允许，一对一映射 |
| **多个人脸关联同一数据集** | 允许（如同一人不同角度注册了两个面孔） |
| **数据集被删除** | 映射自动失效，UI 显示"数据集已删除"，需重新关联 |
| **自动创建的数据集命名** | 格式：`{display_name} 的记忆`，可手动修改 |

---

## 实施阶段

### Phase 1: 基础对话 + 视频集成（MVP）

**目标**: 能在视频画面旁与 LLM 对话，看到在场人员列表。

| 任务 | 位置 | 工作量 |
|------|------|--------|
| 新增 `playground` 视图到 `useUIStore` | m_flow-frontend/lib/store/ui.ts | 小 |
| 创建 `PlaygroundView` 布局组件 | m_flow-frontend/components/playground/ | 中 |
| 创建 `VideoPanel`（嵌入 fanjing-face-recognition MJPEG） | 同上 | 小 |
| 创建 `ChatPanel` + `ChatMessage` | 同上 | 中 |
| 创建 `PersonCard`（只读显示） | 同上 | 小 |
| 后端 `POST /api/v1/playground/session` | m_flow/api/v1/playground/ | 小 |
| 后端 `POST /api/v1/playground/chat`（基础版，无记忆） | 同上 | 中 |
| 后端 `GET /api/v1/playground/persons`（透传 fanjing-face-recognition） | 同上 | 小 |

**验收标准**: 打开 Playground，左侧看到视频和人员列表，右侧可与 AI 对话。

### Phase 2: 短期记忆 + 自动推送

**目标**: 对话累积到阈值时自动推送到 M-Flow 长期记忆。

| 任务 | 位置 | 工作量 |
|------|------|--------|
| 实现 `PlaygroundSession` 短期记忆管理 | m_flow/api/v1/playground/session.py | 中 |
| 实现 token 计数（tiktoken 或近似） | 同上 | 小 |
| 实现 `flush_to_long_term`（add() + memorize() 两步，异步执行） | 同上 | 中 |
| 创建 `MemoryIndicator` 前端组件 | m_flow-frontend | 小 |
| 后端 `POST /api/v1/playground/flush`（手动推送） | 同上 | 小 |
| 对话消息中注入推送状态通知 | 前后端 | 小 |

**验收标准**: 对话到 2000 token 时自动推送，UI 显示"💾 记忆已保存"。

### Phase 3: 说话者识别 + 记忆检索

**目标**: AI 知道在和谁说话，并能从该人的数据集检索历史记忆。

| 任务 | 位置 | 工作量 |
|------|------|--------|
| 实现 `FaceContextBridge` | m_flow/api/v1/playground/face_bridge.py | 中 |
| 实现人脸↔数据集映射表（SQLite 或 M-Flow 内部） | 同上 | 中 |
| 实现自动为新面孔创建数据集 | 同上 | 中 |
| 将说话者身份注入 LLM system prompt | playground/chat 逻辑 | 小 |
| 实现 `MemoryRetriever`（从数据集检索） | m_flow/api/v1/playground/retriever.py | 中 |
| 多人在场时取并集检索 | 同上 | 小 |
| 前端 `PersonCard` 显示数据集关联状态 | m_flow-frontend | 小 |

**验收标准**: AI 能说"张三你好"而非"用户你好"；能引用之前存储的记忆。

### Phase 4: 指代消解 + 手动关联

**目标**: 自动消解代词，支持手动编辑人脸↔数据集关联。

| 任务 | 位置 | 工作量 |
|------|------|--------|
| 集成 coreference 模块到 chat 流程 | playground/chat 逻辑 | 中 |
| 创建 `DatasetLinker` 弹窗组件 | m_flow-frontend | 中 |
| 后端 `POST /api/v1/playground/link-face` | m_flow backend | 小 |
| 重命名同步（fanjing-face-recognition + M-Flow 数据集） | 后端 | 小 |
| 对话中显示指代消解结果（可折叠） | 前端 | 小 |

**验收标准**: 说"他也参与了"能正确消解为具名实体；可手动修改人脸关联。

### Phase 5: 流式输出 + 体验优化

**目标**: 流式打字效果、平滑动画、边界情况处理。

| 任务 | 位置 | 工作量 |
|------|------|--------|
| 实现 SSE 流式输出 | 后端 chat endpoint | 中 |
| 前端流式打字效果 | ChatMessage 组件 | 中 |
| fanjing-face-recognition 断线降级模式 | 前后端 | 小 |
| M-Flow 推送失败重试 | 后端 session | 小 |
| 设置面板（阈值/模型/地址配置） | 前端 | 中 |
| 响应式布局（移动端适配） | 前端 CSS | 中 |

**验收标准**: 完整的生产级体验，所有边界情况有优雅的降级。
