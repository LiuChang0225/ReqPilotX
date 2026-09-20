# 需求提取评测契约与私有案例

本目录仅发布 JSON Schema。真实客户案例的文件名、来源哈希、定位器、视觉路由和复核登记均属于私有评测数据，即使不包含正文也不随源码发布。

`manifest.json`、`oracles.json`、`silver.json`、`llm_silver.json`、`llm_experience.json`、`visual_routes.json` 已从同步候选移出；原文件仍保留在原始开发仓库和推送前备份中，没有改写成合成数据。历史运行结论不代表当前发布版本完成真实语料验收。

## 公开合同

- `schema.json` 保留既有 29 案例 manifest 的严格格式及跨引用约束，没有为缺失客户数据放宽它。
- `oracles.schema.json` 保留 source-bound hash、独立 oracle、范围与计数合同。
- `silver.schema.json`、`llm_silver.schema.json`、`llm_experience.schema.json` 保留非 Gold、非 serving gate 的复核证据类型。
- `../public/decision_cases.json` 是从零编写的三条合成值比较样例，只验证比较、缺失输入和未知方向处理，不声称产品质量验收。
- 测试中原生文档、表格、语义和投影的合成构造继续执行。仅依赖真实注册表的测试显式跳过，并显示所需配置。

## 私有运行

显式配置 `REQPILOTX_PRIVATE_EVAL_ROOT`，目录结构与 `prototype/eval` 一致：

```text
<controlled-eval-root>/
  extraction_cases/
    manifest.json
    oracles.json
    silver.json
    llm_silver.json
    llm_experience.json
    visual_routes.json
  irps_rag/
    manifest.json
```

未配置时只查被忽略的 `prototype/eval/private/`，不会自动寻找其他 checkout 或客户目录。Schema 始终使用当前源码版本。各 CLI 的显式路径参数仍优先，manifest 的 oracle sidecar 与该 manifest 同目录。

从 `prototype` 执行：

```bash
REQPILOTX_PRIVATE_EVAL_ROOT=/controlled/eval \
python -m scripts.extraction_case_runner \
  --corpus-root /controlled/corpus \
  --legacy-mode off --source-ir-mode native \
  --out-dir /controlled/results/run
```

文件身份、页域、hash pin、独立审查与全案例验收仍由原运行器检查；缺失证据不会降级为 PASS。源文档通过 `--corpus-root` 或 `REQPILOTX_TRAINING_CORPUS_ROOT` 指定，私有注册表与源文档是两个独立路径。

## 无私有资料的验证

```bash
cd prototype
python -m pytest -q tests/test_public_eval_contract.py tests/test_decision_eval.py \
  tests/test_extraction_case_runner_fmt.py tests/test_extraction_case_runner_pdf.py \
  tests/test_extraction_case_runner_body_sequence.py tests/test_llm_requirement_silver.py
python -m scripts.decision_eval --corpus eval/public/decision_cases.json
```

完整私有回归需显式提供真实注册表及语料。公开测试通过、私有测试跳过和真实文档验收是不同证据，不得合并报告为全部真实案例通过。

## 兼容批处理

`fidelity_profiles.json` 和 `quality_gate_profiles.json` 只保存通用 defaults；客户专用 profile 已移出候选。真实回归须传显式 `--profile-config`，保真运行器没有匹配 profile 时失败，不产生空通过。批处理公开默认是 `extraction_batches.example.json` 模板，真实批次传 `--batch-config`。

章节回归 `section_split_regression.py` 的客户专用章节号已移出默认行为。`--profile-config` 接受私有 `required_codes` 与 `guard_sections`；未指定时仅输出 `generic_structure_only` 检查，空 outline 不通过，不能把通用检查当成客户章节完整性验证。
