# ASTER 消融实验

## 实验定义

所有实验均使用移除自动 Revision Editor 后的代码。保留相同任务、模型、
提示词版本、目标时长、素材记忆、窗口/节拍优化和渲染设置。

| 实验 | Anchor | 每组候选 | 选择方式 | 是否重新调用规划模型 |
| --- | --- | --- | --- | --- |
| 完整版 | 开启 | 默认 3 条有效 trajectory | pairwise scoring + beam search | 是，或重建同版本对照 |
| 无 Anchor | 关闭 | 同完整版 | 同完整版 | 是 |
| 单候选、无搜索 | 保留 | 首条有效完整 trajectory | 直接选择，不排序、不打 pairwise 分、不跑 beam | 历史候选复用时不需要 |

Trajectory 是整组 Slot 的一条完整方案，不是一个 Slot 的单独候选。
“首条”指候选池中保留的检索顺序，不是语义分最高的一条，也不是被校验拒绝的
第一条原始模型输出。跨组冲突时报告失败，不偷偷改选下一条。因此此实验消融的是
**多候选选择与全局搜索**，并非彻底移除检索或候选有效性验证。

## 开关

复制完整的本地配置，分别修改以下字段；不要将只有以下片段的文件当作完整配置。
含凭据的本地配置不要提交到 Git。
旧的自定义配置如含 `[planners.script_review]`，须删除这个已废弃的配置节；
仓库默认配置已更新。

无 Anchor：

```sh
cutmaster plan --no-anchor ...
```

Anchor 开关属于项目 Creative Brief 的 `anchor_enabled`，默认 `true`；Web 项目设置
可切换，CLI 和 Benchmark 使用 `--anchor` / `--no-anchor`。旧项目缺少字段时启用。
启动 Run 时快照该值；Retry/Resume/Run again 沿用原 Run，不受项目后续修改影响。
旧 TOML 中的 `planners.dialogue_anchors.enabled` 仅为兼容读取而接受，不再生效。

Story Editor 不调用 anchor 模型，清除 anchor 固定候选，并按普通 Slot 构建
规划分区/分组。只适用于新规划，不应续跑包含旧 Anchor 的历史检查点。

从头生成单候选实验时，同时设置：

```toml
[planners.candidate_retrieval]
target_trajectories_per_group = 1

[planners.beam_search]
selection_mode = "first"
```

完整版保持项目 Anchor 启用、`target_trajectories_per_group = 3`、
`selection_mode = "beam"`。首选模式不改变正常失败校验与重规划机制。

## 通过 Benchmark 启动无 Anchor 生成

在 Mashup-Benchmark 根目录，使用其现有适配器，配置路径替换为实际完整配置：

```sh
python scripts/run_cutmaster.py --all --run-id cutmaster_no_anchor --no-anchor \
  --cutmaster-config /absolute/path/to/config.toml
```

为避免 Anchor 原声带来的音频模式混杂，对照与消融应评估相同音频模式
（例如均为 BGM-only），并使用相同 Benchmark 评测接口。

## 零 API 的历史候选复用

在 CutMaster 根目录：

```sh
.venv/bin/python -m cutmaster.workflow.planners.replay_ablation \
  --benchmark-root /Users/xinfanchen/Project/Mashup-Benchmark \
  --source-run cutmaster_overlap --dry-run
```

准备脚本时去掉 `--dry-run`，传入新的、尚不存在的 `--output` 目录；可重复传
`--task-id task_009` 选择子集。源 run 不会被修改。生成每任务的
`raw_script.json`、`selection.json` 及 `preparation_summary.json`。

程序校验完整 trajectory、时间顺序、Anchor 不变和输出时间轴不变。
当前 `cutmaster_overlap` 的 40 个成功任务已通过只读预检。

**此入口只准备脚本，不生成成片，也不伪造 Benchmark 成功记录。** 正式实验
还须用与对照一致的 plan compiler、source-window optimizer 和 Renderer
编译/渲染，再经 Benchmark 接口登记及评测。不能直接把 raw script 当作已完成
beat 优化的 Render Plan。

旧 run 如经过自动 Revision Editor，不能直接当作“新版完整版”公平对照：
须重新生成同版本对照，或从相同候选池恢复 Composer 选择并走同一编译/渲染链。

## 成本及验收

### 完整离线生成入口

Benchmark 的 `scripts/run_cutmaster_ablation.py` 已接通首候选准备、正常
plan compiler / beat 窗口优化、BGM-only Renderer 和标准 run manifest。
它复用已有素材，核验素材 SHA-256，校验所有 Anchor 与输出帧时间轴不变，
并禁止 Python 网络连接。运行示例：

```sh
.venv/bin/python /Users/xinfanchen/Project/Mashup-Benchmark/scripts/run_cutmaster_ablation.py \
  --cutmaster-root /Users/xinfanchen/Project/CutMaster \
  --source-run cutmaster_overlap_beat \
  --run-id cutmaster_overlap_beat_ablation_T_E --workers 2
```

`--prepare-only` 可先编译全部任务；随后 `--resume --render-prepared` 使用已准备
且经过来源/选择校验的 RenderPlan。原始 run 保持不变；此入口不运行评测。

### 验收口径

- 新运行没有 `script_review` 请求，也没有自动 Revision Editor 里程碑。
- 关闭 Anchor 后无 Anchor 模型请求、无固定 Anchor 候选。
- 首选模式只使用首条保留 trajectory；无 pairwise API 请求、无 beam 排序。
- 无解不允许跳过任务后只统计成功样本；同时报告成功率和配对任务的质量变化。
- 离线复用的新增模型费用为零，但它复用了历史多候选检索/验证成本。
  不据此声称真实单候选流程节省了多少 API 费用；成本比较需独立在线运行。
- 成片改变后重新评测；不要复用旧成片的自动指标或 VLM 分数。
