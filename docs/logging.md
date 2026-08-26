# CutMaster 日志规范

CutMaster 的运行日志用于回答三个问题：工作流正在执行什么、执行到了哪里、失败后能否定位并恢复。日志不归档完整 Prompt、模型上下文或原始响应，工作流状态文件同样不记录这些调用内容。

## 单行格式

```text
TIMESTAMP | LEVEL | COMPONENT | EVENT | key=value ... | message
```

示例：

```text
2026-07-25 14:20:31.482+08:00 | INFO     | analyser | stage.start | duration_sec=7668.400 stage=shot_detection | Full-video Shot detection started
2026-07-25 14:22:08.104+08:00 | INFO     | model | model.complete | elapsed_sec=3.412 modality=text_and_images model=qwen3-vl-plus operation=shot_annotation response_chars=1248 | Model request completed
2026-07-25 14:24:18.991+08:00 | WARNING  | aster.composition | validation.reject | attempt=1 error_type=NoFeasiblePathError failed_slots=["slot_04"] stage=chronology_preflight | ASTER attempt was infeasible; retrying coordination with diagnostics
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
cutmaster
application.workflow
application.materials
application.runs
application.renders
application.jobs
worker
web
analyser
dialogue
dialogue_audio
music
planners
aster.arrangement
aster.story
aster.timeline
aster.media
aster.composition
aster.revision
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
- `<data_root>/logs/jobs/<job_id>.log`：每个持久化 Job 的 canonical
  Job 日志。无论 Job 是由 Web Supervisor 在隔离 Worker 中执行，还是由
  Application 的同步 Managed Job Executor 执行，都必须写入这个相同的
  Data Root-owned 文件。Web 只按 Job ID 读取并实时 tail 该文件，不读取
  adapter 进程管道。日志窗口默认读取最后 50 条完整日志行；用户显式选择
  “读取完整日志”时，Web 获取请求时刻的完整文件快照，再从快照末尾的字节
  游标继续通过 SSE 追加，切换过程不得漏行。前端的 Level 与 Component
  筛选只影响显示，不影响日志接收和游标推进。
- `workflow.log`：由 Application 同步完整流程拥有的 canonical 聚合
  日志，按实际执行顺序包含本次 Material Analysis（如有）、ASTER
  Planning 和 Rendering Job 的日志。它作为 `workflow.log` 逻辑产物出现在
  Managed Workflow Receipt 中。Receipt 还分别暴露
  `analyser.video_job_log`、`analyser.music_job_log`（仅当本次确实运行对应
  Analysis Job 时）、`planners.job_log` 和 `renderer.job_log`；各 Job 日志仍是
  Web 按 Attempt 查看时的 authoritative source。
- 终端：使用同一格式，默认从 `INFO` 开始。
- `infrastructure/observability/progress.py` 的 `tqdm` 进度条：只在交互式
  终端输出到 stdout；非 TTY 的 Managed Worker 不输出进度刷新，也不写入
  Job 日志或 `cutmaster.log`。
- benchmark 的 `logs/backend.log`：不是运行时重定向。Benchmark Adapter
  不得捕获 CutMaster Worker 的 stdout/stderr 来生成该文件；只在同步
  Managed Workflow 成功并拿到 Receipt 后，验证 `workflow.log` 的 Data
  Root-relative 引用，再将该 CutMaster-owned 产物复制为提交副本。
- `analysis_history.json`、`planners_history.json`：仅保存轻量工作流产物和脚本版本，不包含模型调用历史。
- `planners_calls.json`：按任务和调用组织 Planners 阶段调用树，完整保存每次重试的模型回复；Prompt 仅保存标识、版本、指纹、字符数和上下文字段等元数据，不保存正文或上下文快照。
- `shot_annotations/`：按 Segment 保存可断点复用的有序 Shot 结构化标注数组。

## 代码约束

业务模块统一调用：

```python
from cutmaster.infrastructure.observability.logging import log_event

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

禁止在业务模块中直接调用或配置 Loguru。日志 sink 只在执行边界初始化：
Managed Worker 使用 `configure_console_logging()` 输出无 ANSI 的结构化
stderr，由 Supervisor 单次持久化到对应 canonical Job 日志；Application
的同步 Managed Job Executor 必须在自己的 Job 执行边界建立同等的
canonical sink，不得依赖 CLI 或 Benchmark 重定向。Domain 和 Workflow 不感知
当前入口类型。CLI 与 Benchmark 作为平级入口，都不调用另一个 adapter
来配置或捕获日志。
