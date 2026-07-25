# CutMaster

<p align="center">
  <a href="README.md"><kbd>中文</kbd></a>
  &nbsp;|&nbsp;
  <a href="README_EN.md"><kbd>English</kbd></a>
</p>

<p align="center">
  <img src="assets/cutmaster_pipeline.png" alt="CutMaster 方法总览" width="100%">
</p>

<p align="center"><em>CutMaster：面向长视频混剪的 Agentic Workflow</em></p>

CutMaster 是一个纯后端长视频混剪 Agentic Workflow。它接收一部长视频、一条背景音乐和一条自然语言指令，生成时间轴精确到帧的音乐混剪。项目从 Mashup-Benchmark 的 NarratoAI adapter 所使用的生产流程中提取并扩展而来，现已成为独立的 Python 项目。

CutMaster 当前支持可复用的全片 Shot/Segment 视觉分析、LLM 辅助台词重组、结构化音乐分析、抽象剪辑 Slot 规划、基于结构化视频描述的多候选检索、带时序依赖的 Beam Search、版本化脚本补丁、结合视觉切点的原片窗口优化，以及确定性的 FFmpeg 渲染。最终视频会静音原片音频，只保留指定的背景音乐。

## 工作流

```text
原始视频 + 背景音乐 + 用户指令
  -> 校验输入；除非指定 --overwrite，否则保护已有输出
  -> 使用 PySceneDetect 完整提取全片 Shot 边界
  -> 复用用户提供/已有的 SRT，或使用 DashScope Fun-ASR 转写
  -> 并行重组完整台词，同时保留原始字幕锚点
  -> LLM 一次读取完整台词，按连续对话/独白返回台词序号范围
  -> 将对白范围扩展到能完整覆盖它的 Shot；间隙、片头和片尾 Shot 成为无对白 Segment
  -> 按 Segment 将全片准确切开并缓存
  -> Segment 之间并行、内部 Shot 串行；每次 VLM 用均匀采样的 5 帧标注一个 Shot
  -> 生成可跨剪辑任务复用的 video_description.json
  -> 使用 librosa 生成节拍、重音、能量曲线和音乐段落的结构化画像
  -> LLM 根据指令、音乐画像和结构化视频描述规划可落地的剪辑 Slot
  -> 全局调整 Slot 时长，将所有输出边界对齐到音乐重音
  -> LLM 从真实 Segment/Shot 描述中为每个 Slot 检索若干 Shot 边界候选
  -> 从真实候选画面生成接触图，由多模态模型验证可见内容与主体出镜
  -> 候选不足时定向补检；多轮后仍不足则带失败诊断重新规划
  -> 从真实视频计算候选片段运动强度
  -> 并行评估每对相邻 Slot 的 3×3 候选首尾画面，缓存原片硬切连续性分数
  -> 同时计算逐 Slot 独立最优路径和带时序/VLM 连续性依赖的 Beam Search 路径
  -> LLM 在既有候选池内复核并以补丁方式修改脚本
  -> 并行检测每个候选窗口内的原片切点
       - 在自适应场景检测前过滤近重复帧
       - 所有保留帧继续使用原片时间戳
       - 每个原片窗口最多向后搜索 2 秒
       - 最小化所有内部切点到最近音乐节拍的最大距离
       - 内部切点与片段边界优先保持至少 1 秒距离
  -> 按精确输出帧数渲染每个片段
  -> 拼接标准化的无声视频片段
  -> 循环并淡出背景音乐，保持原片音频静音
  -> output.mp4 + 结构化中间产物
```

### 台词重组

Fun-ASR 最初会将转写结果切分为较短的字幕条目。CutMaster 将同一说话人的相邻字幕组织为候选段落，再调用文本模型判断哪些完整段落应当合并。请求并发数由 `llm.max_concurrency` 控制。

`dialogues.json` 同时记录重组后的完整句子时间范围以及对应的每一个原始字幕锚点。`dialogue_merged.srt` 是传给剪辑脚本生成阶段的句子级字幕。台词重组是一个约束明确的边界选择任务，因此会关闭模型 thinking。

### 可复用视频素材描述

素材分析与用户剪辑指令、背景音乐和输出目录解耦。缓存目录由
`material_analysis.material_cache_dir` 指定，并按照视频文件、字幕输入、模型、分析 schema
和检测参数生成稳定目录。命中完整缓存时，不再执行 ASR、场景检测、素材切片或
Shot VLM 标注。

素材分析同时采用逐阶段检查点，而不是只有全部成功后才能复用。PySceneDetect、
字幕与台词、Segment 边界、每个 Segment 视频和每个成功的 Shot VLM 标注都会在完成后
立即持久化。后续阶段失败或进程中断时，再次启动会校验已有产物，并从第一个缺失阶段
继续；已经成功的单 Shot VLM 请求也不会重复调用。JSON 历史和检查点采用原子替换，
避免中断留下半写文件。

全片 Shot 检测、Segment 切片、逐 Shot VLM、候选抽帧与运动分析、Pairwise VLM、
源窗口优化和最终片段渲染都会输出进度条，包括完成比例、处理速度和 ETA。通过
benchmark adapter 运行时，这些进度同时进入 task 的 `logs/backend.log`。

PySceneDetect 首先使用与后续切点优化相同的 `AdaptiveDetector` 参数完整检测全片。
对白划分 LLM 读取全部句子及每句覆盖的 Shot ID，必须把每个台词序号恰好分配一次，
且不能在同一个 Shot 内建立 Segment 边界。Python 随后把每个对白组扩展到最小覆盖
Shot 区间，并将所有剩余 Shot 确定性地补成无对白 Segment。

每个 Segment 会先保存为独立 MP4。VLM 标注以 Segment 为并发单位，共用
`vlm.max_concurrency`；同一 Segment 内的 Shot 严格串行。每个请求只标注一个 Shot，
输入是该 Shot 的 5 张均匀采样帧以及完整台词全局上下文。台词只能帮助理解叙事和
名字，不能作为人物出镜、动作、地点或道具的视觉证据。`analysis_history.json`
记录模型上下文快照、完整提示词、帧标签、响应和校验结果。

### 音乐画像与抽象规划

CutMaster 使用 `librosa` 计算 RMS、onset strength、spectral centroid 和节拍，并生成统一的 `music_profile.json`。画像包括能量时间序列、普通节拍、强重音、段落边界、段落角色，以及根据段落能量建议的片段时长范围。背景音乐短于目标视频时，节拍和重音会按照最终循环方式同步扩展。

初始规划会开启模型 thinking。默认 Slot 数量为：

```text
ceil(target_duration / target_shot_length)
```

模型只描述每个 Slot 应承载的内容、叙事作用、目标情绪强度、目标运动强度、与前一片段的衔接关系和期望时长，不允许给出原片时间戳。随后通过全局动态规划同时修正全部边界，使剪辑点落在音乐重音上，并保持单调、非零的片段时长。

### 候选检索、路径选择与补丁

候选检索模型根据 Slot 描述和 `video_description.json`，为每个 Slot 返回若干原片区间。每个候选必须满足：

- 由一个或多个连续 Shot 构成；
- 起止时间严格落在首尾 Shot 边界；
- 长度足以容纳对应 Slot；
- 包含来自 Shot VLM 标注的非空内容描述；
- 只使用当前检索轮次提供的 Segment 和 Shot。

CutMaster 对候选计算结构化语义相关性、真实画面相关性、主体出镜置信度、情绪匹配、画面运动匹配、显著性和时长可行性等单片段分数。候选池完成后，它会对每对相邻 Slot 的所有候选组合并行抽取前段尾帧和后段首帧，由多模态模型预计算直接硬切的视觉连续性、情绪连续性和叙事桥接分；不生成或依赖任何转场特效。视觉模型调用并发数由 `vlm.max_concurrency` 控制，Beam Search 只读取缓存分数，不会在路径扩展时重复调用模型。它同时保留：

- 每个 Slot 单独取最高分候选得到的独立最优路径；
- 在原片时间严格单调且片段不重叠的硬约束下，将相邻连续性和音乐能量变化纳入路径分数的 Beam Search 全局路径。

选择后，复核 LLM 只能使用候选池中已有的 `candidate_id` 进行 `keep/replace` 补丁，不能直接创造新时间戳。补丁会选择可共同成立的最大子集；冲突补丁会单独拒绝并记录原因，不再导致整批回滚。`planning_history.json` 会记录请求上下文、每次模型调用、视觉输入标签、响应状态、结构化结果、脚本版本、已接纳补丁和拒绝原因。CutMaster 对完整的 API 请求、JSON 解析和语义校验事务进行指数退避重试，不会接受残缺结果。

### 视觉切点优化

对于每个选中的原片区间，CutMaster 会在该区间及其向后两秒的搜索范围内检测内部视觉切点。当前使用 PySceneDetect 的 `AdaptiveDetector`，默认参数为：

- 自适应阈值：`2.0`；
- 最小内容变化值：`15.0`；
- 最短场景长度：`0.25s`；
- 近重复帧阈值：灰度平均绝对差 `< 1.0`。

近重复帧过滤对于由较低帧率素材生成的 50/60 fps 视频十分重要。如果不做过滤，交替出现的“重复帧/新帧”会让自适应检测器将普通运动误判为大量切点。过滤只影响参与检测的帧，所有保留帧仍携带原视频中的真实时间码。

候选起点会在原片帧网格上从初始位置搜索至 `+2s`。Minimax 目标首先最小化任意内部输出切点到最近背景音乐节拍的最大距离，然后依次偏好更小的向后位移和更少的内部切点。

内部切点与片段边界的期望安全距离为 `1.0s`。如果没有可行候选窗口，CutMaster 会依次尝试 `0.75s`、`0.5s`、`0.25s`，最后尝试 `0.0s`。任何约束放宽都会记录在 `script_adapted.json` 中并输出 warning。`0.0s` 仅是保证任务完成的最后手段，不是正常优化目标。

### 帧精确渲染

每个适配后的片段都包含 `output_frame_range`。FFmpeg 会按照配置的分辨率和 FPS 精确渲染对应数量的帧，并移除原片音频。所有片段拼接后不会改变已规划的时间轴；随后背景音乐会循环、裁剪到混剪的精确时长、执行淡出并编码为 AAC。

当 `render.encoder = "auto"` 时，编码器按以下顺序选择：

1. macOS 上可用的 `h264_videotoolbox`；
2. 可用的 `h264_nvenc`；
3. 其他情况下使用 `libx264`。

## 环境要求

- Python `3.12`（`>=3.12,<3.13`）
- `uv`
- `PATH` 中可用的 `ffmpeg` 和 `ffprobe`
- 用于默认 LLM 和 Fun-ASR 配置的 DashScope API Key

项目暂不支持 Python 3.13，因为当前使用的 librosa/Numba 节拍跟踪路径在该环境中不稳定。

## 安装

```bash
uv sync
cp config.example.toml config.toml
```

`config.toml` 已被 Git 忽略。配置加载器支持直接通过 `api_key` 保存密钥，也支持通过 `api_key_env` 指定环境变量名称。

若采用环境变量，请在 `[llm]`、`[vlm]` 和 `[asr]` 中使用：

```toml
api_key_env = "DASHSCOPE_API_KEY"
```

运行 CutMaster 前导出密钥：

```bash
export DASHSCOPE_API_KEY="..."
```

## 配置

配置文件严格按照工作流执行顺序排列。没有可调参数的音乐分析阶段只保留阶段注释，
不创建空配置表。

### Stage 0a/0b：`[llm]` 与 `[vlm]`

两个配置表拥有相同字段，但完全独立。`[llm]` 用于台词重组、Segment 划分、Slot
规划、候选检索和脚本复核；`[vlm]` 用于逐 Shot 标注、候选视觉核验和 Pairwise
连续性评分。

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `model` | OpenAI-compatible 文本或视觉语言模型 | `qwen3.7-plus` |
| `base_url` | OpenAI-compatible API Base URL | DashScope compatible-mode URL |
| `api_key` / `api_key_env` | 直接密钥或环境变量名称 | 占位值 |
| `enable_thinking` | 是否向该模型的所有请求启用 thinking | `true` |
| `temperature` | 采样温度 | `0.1` |
| `max_tokens` | 最大输出 token 数 | `4000` |
| `timeout_sec` | 单次模型请求超时 | `180` |
| `max_retries` | 首次请求失败后的重试次数 | `3` |
| `max_concurrency` | 该模型服务的最大并发请求数 | `4` |

OpenAI SDK 自身的重试已关闭，由 CutMaster 负责完整的“请求/解析/校验”重试周期。因此 `max_retries = 3` 表示最多执行四次完整请求，失败后的等待时间依次为 `1s`、`2s`、`4s`。

### Stage 1：素材分析

`[material_analysis]`

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `material_cache_dir` | 与任务输出解耦的可复用视频素材分析目录 | `.cutmaster/materials` |

`[shot_detection]`

| 配置项 | 含义 | 默认值 |
| --- | --- | --- |
| `adaptive_threshold` | PySceneDetect 自适应阈值 | `2.0` |
| `adaptive_min_content_val` | 最小内容变化值 | `15.0` |
| `adaptive_min_scene_len_sec` | 最短 Shot 时长 | `0.25` |
| `duplicate_frame_threshold` | 近重复帧灰度平均绝对差阈值 | `1.0` |

这组检测参数同时用于全片素材分析和最终源窗口优化。

`[asr]`

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `backend` | ASR 后端；当前仅支持 `bailian` | `bailian` |
| `api_key` / `api_key_env` | 直接密钥或环境变量名称 | 占位值 |
| `reuse` | 复用非空 `source.srt` 和已提取的 ASR 音频 | `true` |
| `timeout_sec` | 异步 ASR 总超时 | `1800` |
| `poll_interval_sec` | ASR 任务轮询间隔 | `2` |
| `max_chars` | 初始字幕条目的期望最大字符数 | `20` |
| `max_subtitle_duration_sec` | 初始字幕条目的期望最大时长 | `3.5` |

`[shot_annotation]`

| 配置项 | 含义 | 默认值 |
| --- | --- | --- |
| `shot_sample_frames` | 单次 Shot VLM 标注的均匀采样帧数；固定为 5 | `5` |

### Stage 3：`[slot_planning]`

| 配置项 | 含义 | 默认值 |
| --- | --- | --- |
| `replan_max_rounds` | 候选或严格时序路径不可行时的最大重新规划次数 | `3` |

### Stage 4：`[candidate_retrieval]`

| 配置项 | 含义 | 示例配置默认值 |
| --- | --- | --- |
| `candidates_per_slot` | 每个 Slot 最终保留的原片候选数 | `3` |
| `retrieval_batch_size` | 单次候选检索请求包含的 Slot 数 | `5` |
| `retrieval_max_rounds` | 主体过滤后候选不足时的最大检索轮数 | `3` |
| `visual_sample_frames` | 每个候选用于视觉核验的均匀采样帧数 | `4` |
| `protagonist_visibility_threshold` | 必须出镜主体的最低归一化视觉置信度 | `0.55` |
| `protagonist_visibility_fallback_threshold` | 多轮补检后的最低归一化身份置信度 | `0.5` |
| `motion_sample_fps` | 候选运动强度的采样帧率 | `2.0` |
| `motion_workers` | 候选运动特征解码 worker 数 | `4` |

### Stage 5–7：选择、复核与源窗口优化

| 配置表 | 配置项 | 含义 | 默认值 |
| --- | --- | --- | --- |
| `[beam_search]` | `beam_width` | Beam Search 保留的路径数量 | `8` |
| `[script_review]` | `review_rounds` | 候选内脚本补丁复核轮数 | `1` |
| `[source_window_optimization]` | `search_margin_sec` | 候选窗口向后搜索范围 | `2.0` |
| `[source_window_optimization]` | `min_boundary_distance_sec` | 内部切点与片段边界的首选安全距离 | `1.0` |
| `[source_window_optimization]` | `max_workers` | 源窗口切点优化并发数 | `8` |

### Stage 8：`[render]`

| 配置项 | 含义 | 默认值 |
| --- | --- | --- |
| `width`, `height` | 输出画布 | `1920×1080` |
| `fps` | 输出帧率和时间轴网格 | `30` |
| `encoder` | FFmpeg 视频编码器或 `auto` | `auto` |
| `threads` | libx264 编码线程数 | `8` |
| `bgm_volume` | 最终背景音乐音量倍率 | `0.3` |
| `original_volume` | 原片音频音量；帧精确模式要求为 `0` | `0.0` |
| `audio_sample_rate` | 最终 AAC 采样率 | `48000` |

文本和视觉模型并发分别由 `llm.max_concurrency`、`vlm.max_concurrency` 控制，运动特征解码由
`candidate_retrieval.motion_workers` 控制，源窗口优化由
`source_window_optimization.max_workers` 控制，编码线程由 `render.threads` 控制。

## 使用方法

```bash
uv run cutmaster run \
  --video /path/to/source.mp4 \
  --audio /path/to/bgm.mp3 \
  --prompt "剪出所有决定比赛走向的进球" \
  --output-dir outputs/demo \
  --target-duration 60 \
  --target-shot-length 4 \
  --prompt-type event
```

`run` 命令支持以下参数：

| 参数 | 是否必需 | 说明 |
| --- | --- | --- |
| `--video PATH` | 是 | 长视频原片 |
| `--audio PATH` | 是 | 背景音乐 |
| `--prompt TEXT` | 是 | 混剪指令 |
| `--output-dir PATH` | 是 | 中间产物和输出目录 |
| `--config PATH` | 否 | TOML 配置；默认为 `config.toml` |
| `--subtitle PATH` | 否 | 已有 SRT；提供后跳过 Fun-ASR |
| `--target-duration SEC` | 否 | 目标输出时长；默认 `60` |
| `--target-shot-length SEC` | 否 | 期望片段时长；默认 `4` |
| `--prompt-type TYPE` | 否 | 提供给脚本生成阶段的元数据；默认 `event` |
| `--video-title TEXT` | 否 | 提供给模型的人类可读原片标题 |
| `--custom-clips N` | 否 | 覆盖自动计算的片段数量 |
| `--max-clip-duration SEC` | 否 | 时长适配阶段使用的片段硬上限 |
| `--overwrite` | 否 | 覆盖已有运行结果 |

如果 `output.mp4` 已存在，运行会直接停止，除非指定 `--overwrite`。`--overwrite`
只重建当前剪辑任务；输入签名一致的视频素材分析完整缓存和有效阶段检查点仍会复用。

## 输出文件

运行日志写入输出目录中的 `cutmaster.log`，采用稳定的
`TIMESTAMP | LEVEL | COMPONENT | EVENT | key=value ... | message` 单行格式。
模型 prompt、原始响应和图片数据不会进入运行日志；完整模型上下文只保存在分析与规划
历史文件中。事件命名、级别、安全边界及开发约束见
[日志规范](docs/logging.md)。

素材缓存目录包含：

| 路径 | 内容 |
| --- | --- |
| `shots.json` | PySceneDetect 产生的全片 Shot 边界 |
| `source.srt` / `dialogues.json` / `dialogue_merged.srt` | ASR 与完整台词重组结果 |
| `segment_boundaries.json` | 对白组、无对白间隙和 Segment/Shot 归属 |
| `segments/segment_XXXX.mp4` | 按 Shot 边界保存的独立 Segment 视频 |
| `video_description.json` | Segment、Shot、场景、人物和对白的完整结构化描述 |
| `analysis_history.json` | 素材分析阶段全部 LLM/VLM 上下文及响应 |
| `analysis_manifest.json` | 缓存输入、模型、schema 和检测参数签名 |

每个任务输出目录包含：

| 路径 | 内容 |
| --- | --- |
| `source.srt` / `dialogues.json` / `dialogue_merged.srt` | 从素材缓存复制的任务审计副本 |
| `music_profile.json` | 音乐能量、节拍、重音、段落和建议时长 |
| `edit_plan.json` | 重音对齐后的抽象剪辑 Slot，不含原片时间戳 |
| `candidate_pool.json` | 每个 Slot 的结构化视频候选、模型分数和本地运动特征 |
| `selection_diagnostics.json` | VLM Pairwise Beam Search 的候选路径、路径分数和评分数量 |
| `planning_history.json` | 主动维护的规划上下文、模型调用和脚本版本/补丁历史 |
| `script_raw.json` | 最终选中的候选路径及 Slot/候选 ID |
| `script_adapted.json` | 输出帧范围、节拍对齐、优化后的原片范围和切点诊断信息 |
| `clips/clip_XXXX.mp4` | 标准化的无声中间视频片段 |
| `montage.mp4` | 混入背景音乐前拼接得到的无声视频 |
| `output.mp4` | 带循环/淡出背景音乐、原声静音的最终视频 |
| `result.json` | 最终路径、时长、片段数、总耗时和各阶段耗时 |
| `cutmaster.log` | INFO/DEBUG 后端运行日志 |

`script_adapted.json` 中的每个片段还会记录：

- `output_timestamp` 和 `output_frame_range`；
- 优化后的原片 `timestamp`；
- 检测到的原片/输出切点时间戳；
- 优化前后切点到节拍的最大距离；
- 原片位移、边界安全距离 fallback 等级和实际生效距离。

## 包结构

- `asr.py`：音频提取、DashScope 上传、异步 Fun-ASR 轮询、说话人字幕转换和 ASR 复用。
- `dialogue.py`：候选段落构建、并行 LLM 边界选择、完整句子重组和字幕锚点保留。
- `llm.py`：OpenAI-compatible 客户端和完整 JSON 事务重试。
- `beats.py`：librosa onset envelope 和动态规划节拍跟踪。
- `music.py`：音乐能量、节拍、重音、段落结构和动态片段时长分析。
- `video_description.py`：Segment、Shot、场景、人物和对白的严格数据契约。
- `prompting/`：统一注册 analyser/planner Prompt，由同一 JSON Schema 生成响应模板、执行结构校验并管理版本 fingerprint；详见 [Prompt 中间层](docs/prompting.md)。
- `analyser.py`：全片 Shot 检测、对白 Segment 构造、素材切片、并行单-Shot VLM 标注和素材缓存。
- `workflow_context.py`：analyser 与 planner 共用的结构化产物、模型调用历史、断点和脚本版本持久化。
- `planner.py`：规划门面，统一暴露并组织四个解耦阶段。
- `slot_planner.py`：根据用户目标、音乐画像和结构化素材规划抽象 Slot。
- `candidate_retriever.py`：检索候选片段，并用真实画面完成主体与内容核验。
- `sequence_selector.py`：预计算片段间评分，执行严格时序 Beam Search。
- `script_reviewer.py`：在候选池内复核和修补已选脚本。
- `script.py`：选定候选的时长适配、输出时间轴校验和帧网格量化。
- `cuts.py`：感知重复帧的 PySceneDetect 分析，以及并行、仅向后、帧级 minimax 原片窗口优化。
- `renderer.py`：编码器选择、帧精确片段渲染、视频拼接和最终 AAC 背景音乐混合。
- `orchestrator.py`：端到端 Agentic Workflow 编排、输入校验、阶段计时和结果输出。
- `cli.py`：命令行入口。

## 当前范围与限制

- 首次素材分析需要对全片完成场景检测、准确切片和逐 Shot VLM 标注，成本较高；相同素材分析完成后会直接复用。
- 当前运动特征使用低分辨率帧差近似画面活动程度，还不是稠密光流或语义动作识别。
- 每次运行只接受一部原片和一条背景音乐。
- 原片音频会被主动静音；帧精确渲染器会拒绝 `original_volume > 0` 的配置。
- 项目不包含 UI、Web 任务队列、TTS、旁白字幕、素材搜索或 benchmark 专用运行记录。
- CutMaster 运行时不需要导入或安装 NarratoAI。

## 验证

```bash
uv run pytest
uv run python -m cutmaster --help
uv run python -m cutmaster run --help
```

## 开源归属

初始工作流源自采用 MIT 许可证的 NarratoAI 项目。详情参见 `THIRD_PARTY_NOTICES.md` 和 `LICENSE`。
