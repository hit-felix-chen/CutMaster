# CutMaster Prompt 中间层

CutMaster 的所有 LLM/VLM 任务通过 `cutmaster.prompting` 构造。业务模块不得直接维护 system prompt、user prompt、响应示例或字段范围，也不得调用旧式的通用 JSON Prompt 接口。

## 统一入口

```python
package = prompt_registry.build(
    stage=PromptStage.ANALYSER,
    task=PromptTask.SHOT_ANNOTATION,
    details=ShotAnnotationDetails(
        segment=segment,
        shot=shot,
        sampled_frame_times_sec=sampled_times,
    ),
)
```

`details` 是每个任务独立的强类型 dataclass。返回的 `PromptPackage` 包含：

- `prompt_id`、Prompt 版本和 fingerprint；
- system prompt 与组装后的 user prompt；
- `ResponseContract`；
- 需要注入的工作流上下文键；
- 文本或图文模态；
- operation 和可选输出 artifact。

模型调用统一使用：

```python
result = workflow_context.call_prompt(
    package=package,
    config=vlm_config,
    validate_business=validate_shot_annotation,
    image_data_urls=images,
    image_labels=image_labels,
)
```

## 单一响应结构源

`ResponseContract.schema` 是响应结构的唯一来源，采用 JSON Schema Draft 2020-12。它同时用于：

1. 原样嵌入 Prompt 的 `<response_contract>`；
2. 自动生成 `<response_template>`；
3. 本地结构、类型、枚举、范围和必填字段校验；
4. 计算契约 fingerprint；
5. 判断模型调用缓存是否仍可复用。

禁止另外手写一份类似 `"wide|medium|close_up"` 的响应示例。枚举必须由 Python `StrEnum` 动态生成，运行时允许值必须由本轮真实 ID 集合生成。

结构契约负责：

- `required` 和 `additionalProperties`；
- 字符串、数字、整数、布尔、数组与对象类型；
- `enum`、`const`；
- 数值上下限；
- 字符串和数组长度；
- 当前 Shot、Segment、Slot、候选和相邻候选 ID 范围。

结构校验通过后，同一请求事务内继续运行任务业务校验器，处理 JSON Schema 不适合表达的规则，例如：

- 台词 ID 无重叠、无遗漏且保持原顺序；
- Segment/Shot 连续性；
- 候选必须落在 Shot 边界且时长足够；
- 候选组合必须完整且不重复；
- Slot 与候选归属关系；
- 脚本时间顺序和补丁约束。

任一层失败都会使完整的请求、解析和校验事务重试。

## Prompt 任务

| Stage | Task | 模态 |
| --- | --- | --- |
| analyser | `dialogue_reconstruction` | LLM |
| analyser | `dialogue_segmentation` | LLM |
| analyser | `shot_annotation` | VLM |
| planner | `slot_planning` | LLM |
| planner | `candidate_retrieval` | LLM |
| planner | `candidate_visual_scoring` | VLM |
| planner | `pairwise_scoring` | VLM |
| planner | `script_review` | LLM |

定义分别位于：

```text
src/cutmaster/prompting/analyser.py
src/cutmaster/prompting/planner.py
```

注册表会拒绝重复的 stage/task，并在构造结果与请求键不一致时立即失败。

## 历史与缓存

每次模型调用记录：

```text
prompt_id
prompt_version
prompt_fingerprint
contract_version
contract_fingerprint
context_fingerprint
response_contract
```

可复用结果必须同时匹配 Prompt、契约和所注入上下文的 fingerprint。历史中保存模型原始结构结果和业务归一化结果；恢复时使用当前契约重新检查原始结构，并重新执行当前业务校验器。

因此：

- Prompt 文本改变，只失效对应调用；
- 字段范围改变，只失效对应契约；
- maintained context 改变，不会错误复用旧结果；
- 业务归一化逻辑改变，会在恢复时重新执行；
- 旧式、没有 Prompt/契约 fingerprint 的历史调用不会复用。

## 开发约束

新增模型任务必须：

1. 在 `PromptTask` 中声明稳定任务名；
2. 定义强类型 `Details`；
3. 定义 JSON Schema 响应契约；
4. 通过 `assemble_user_prompt()` 自动附加契约和响应模板；
5. 注册到 `prompt_registry`；
6. 为枚举、运行时 ID、数值边界、额外字段拒绝和 fingerprint 增加测试。

禁止：

- 在 analyser/planner 业务模块中直接写 Prompt 长字符串；
- 单独维护响应示例和 Python 枚举列表；
- 绕过 `ResponseContract` 直接解析模型 JSON；
- 根据旧版无 fingerprint 的历史响应做兼容修复。
