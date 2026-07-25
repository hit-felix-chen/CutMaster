# CutMaster 日志规范

CutMaster 的运行日志用于回答三个问题：工作流正在执行什么、执行到了哪里、失败后能否定位并恢复。日志不承担模型上下文归档职责；完整 prompt、原始响应和结构化结果分别保存在 `analysis_history.json` 与 `planning_history.json`。

## 单行格式

```text
TIMESTAMP | LEVEL | COMPONENT | EVENT | key=value ... | message
```

示例：

```text
2026-07-25 14:20:31.482+08:00 | INFO     | analyser | stage.start | duration_sec=7668.400 stage=shot_detection | Full-video Shot detection started
2026-07-25 14:22:08.104+08:00 | INFO     | model | model.complete | elapsed_sec=3.412 modality=text_and_images model=qwen3-vl-plus operation=shot_annotation response_chars=1248 | Model request completed
2026-07-25 14:24:18.991+08:00 | WARNING  | planner.sequence | validation.reject | attempt=1 error_type=NoFeasiblePathError failed_slots=["slot_04"] stage=chronology_preflight | Planning attempt was infeasible; replanning with diagnostics
```

要求：

- 时间戳使用本地时区，精确到毫秒，并携带 UTC offset。
- `LEVEL`、`COMPONENT`、`EVENT` 是稳定的机器可检索字段。
- 业务字段使用 `snake_case`；字段按名称排序，保证输出稳定。
- 浮点数默认保留三位小数；布尔值使用 `true/false`；空值使用 `null`。
- `message` 是简短的人类可读摘要，不在其中重复所有结构化字段。
- 一个事件只占一行。异常堆栈可以紧跟在错误事件之后。

## Level

| Level | 用途 |
| --- | --- |
| `DEBUG` | 单个片段、边界、候选等高频决策明细 |
| `INFO` | 阶段起止、缓存与检查点、模型调用元数据 |
| `SUCCESS` | 完整工作流成功 |
| `WARNING` | 可恢复校验失败、重试、降级或约束放宽 |
| `ERROR` | 模型重试耗尽、阶段或工作流失败 |
| `CRITICAL` | 不可恢复的数据损坏或系统级错误 |

## Component

固定使用以下组件名：

```text
orchestrator
analyser
dialogue
music
planner.slot
planner.candidate
planner.sequence
planner.review
script
source_window
renderer
asr
model
```

新增模块必须归入现有组件；只有形成独立、长期稳定的运行子系统时才新增组件名。

## Event

优先使用以下事件：

```text
workflow.start
workflow.complete
workflow.fail
stage.start
stage.progress
stage.complete
stage.fail
cache.hit
cache.miss
cache.invalid
model.start
model.complete
model.retry
model.fail
checkpoint.write
checkpoint.resume
validation.reject
fallback.apply
```

耗时阶段必须成对记录 `stage.start` 与 `stage.complete`，完成事件包含 `elapsed_sec`。重试事件必须包含：

```text
operation
attempt
max_attempts
backoff_sec
error_type
```

## 模型调用

`model.start` 记录：

```text
operation
model
modality
thinking
images
prompt_chars
```

`model.complete` 额外记录 `elapsed_sec` 与 `response_chars`。日志只能保存请求规模和运行元数据，不保存完整 prompt、原始响应、图片 data URL 或 Base64 内容。

## 安全与隐私

严禁写入运行日志：

- API Key、Authorization header 或其他访问令牌；
- 图片 Base64、data URL；
- 完整模型 prompt 和原始响应；
- 原始用户指令全文；
- 可能包含凭证的完整 HTTP 请求。

日志接口会对常见 API Key、Authorization 和 Bearer token 形式进行兜底脱敏，但调用方仍应只传必要的操作元数据。错误原因最多保留 500 个字符并在写入前脱敏。

## 进度条与持久化文件

- `cutmaster.log`：使用本规范的结构化运行日志，文件级别为 `DEBUG`。
- 终端：使用同一格式，默认从 `INFO` 开始。
- `tqdm` 进度条：只输出到终端/stdout-stderr；不写入 `cutmaster.log`。
- benchmark 的 `logs/backend.log`：由 adapter 捕获进程输出，因此可以包含日志与进度条。
- `analysis_history.json`、`planning_history.json`：用于模型上下文、响应与可复现性审计，不属于运行日志。

## 代码约束

业务模块统一调用：

```python
from cutmaster.observability import log_event

log_event(
    "INFO",
    "analyser",
    "stage.complete",
    "Full-video Shot detection completed",
    stage="shot_detection",
    shots=len(shots),
    elapsed_sec=elapsed,
)
```

禁止在业务模块中直接调用或配置 Loguru。日志 sink 只由 CLI 通过 `configure_logging()` 初始化。
