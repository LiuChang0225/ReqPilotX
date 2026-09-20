# AI Gold readiness

> 2026-09-03 定位：保留的离线/兼容工具参考，不定义当前开发顺序或产品完成状态。
> 其中运行计数、版本与阶段结论按其历史语境阅读；仅在明确使用该工具时采用对应合同。
> 当前工作按 [目标态优先开发计划](../../../docs/需求提取/开发计划.md) 推进，客户数据和真实模型调用需单独授权。

This directory stores only public contracts and empty templates for AI evaluation.
Real customer text, evidence, labels, identifiers, reviewer identities and HMAC keys stay in controlled external storage.

Run the aggregate-only inventory from `prototype/`:

```bash
python3 scripts/ai_eval_readiness.py --db storage/app.db
python3 scripts/ai_eval_readiness.py --db storage/app.db --require-ready
```

The database inventory answers only whether enough independent human labels exist. It does not run a quality gate.
After exporting a controlled payload, validate or evaluate it from `prototype/`:

```bash
python3 scripts/ai_gold_eval.py \
  --manifest /controlled/ai-gold-manifest.json \
  --payload /controlled/ai-gold-payload.json \
  --validate-only

python3 scripts/ai_gold_eval.py \
  --manifest /controlled/ai-gold-manifest.json \
  --payload /controlled/ai-gold-payload.json \
  --mode heuristic \
  --scope all \
  --output /tmp/reqpilot-ai-gold-report.json \
  --require-pass

# 模型质量门禁必须使用 llm_primary；llm_shadow 只记录遥测，
# 产品返回中没有可独立评分的 shadow candidate。
python3 scripts/ai_gold_eval.py \
  --manifest /controlled/ai-gold-manifest.json \
  --payload /controlled/ai-gold-payload.json \
  --mode llm_primary \
  --scope all \
  --require-pass
```

`ai_gold_eval.py` invokes the same screening and assessment entry point used by the product. Decision cases force only the already human-confirmed `REQUIREMENT` routing label so the decision stage can be measured independently. The external payload is hashed byte-for-byte; its manifest must be `FROZEN`, both policies must be `APPROVED`, code/config/model/prompt revisions must match, and each case must have two independent HMAC-pseudonymized reviews. Reviewers also attest the hash of the complete structured truth—evidence IDs, missing items and sub-judgments—not only the final label. A disagreement requires an independent completed adjudication.

The emitted report is aggregate-only: it contains coverage, confusion matrices, thresholds and revision hashes, but never requirement text, evidence excerpts, customer file names, case IDs or reviewer IDs. Runtime traces are written to a temporary directory and deleted at process exit. Use an output path outside the repository when recording clean-revision gate evidence.

`--require-ready` and `--require-pass` are intentionally fail-closed. Synthetic smoke cases remain regression fixtures and never count as Gold labels. A manifest can become `FROZEN` only after the label and evaluation policies are approved, every case has two independent reviews plus adjudication where needed, the external payload hash is fixed, and code/prompt/model/config revisions are recorded.

The readiness inventory uses proposed safety minimums until the product owner approves a versioned label policy: at least 100 explicit `REQUIREMENT` labels, 50 explicit `INFO` labels, 100 decision labels with at least 20 in each decision class, 5 documents, and 2 projects. A decision counts as dual-reviewed only when the latest recorded decision from two distinct reviewers agrees with the current final decision. Merely adding an empty Gold column or accumulating conflicting edit history cannot make the gate ready.

The runner also enforces minimum quality floors. An approved policy may tighten but cannot lower them: explicit-requirement recall `1.0`, INFO specificity `0.80`, decision and compliance macro recall `0.70`, per-class decision recall `0.50`, citation precision/recall `0.80`, information-gap completeness `0.90`, sub-judgment exact match `0.75`, and zero unsafe `ACCEPT` predictions for a non-`ACCEPT` Gold case. These are release-safety floors for the current advisory workflow, not a claim that the existing unlabeled product database already satisfies them.

## External payload contract

- `manifest.schema.json` defines the aggregate freeze and revision contract.
- `payload.schema.json` defines the controlled case and review contract.
- `manifest.template.json` and `payload.template.json` contain no labels and cannot pass a gate.
- `case_id`, `project_id`, `document_id`, `reviewer_id` and `adjudicator_id` are 64-character HMAC-SHA256 pseudonyms generated outside the repository.
- Each case may carry an `analysis_label`, a `final_decision`, or both. Every decision case also carries the separately reviewed `compliance_status`, stable expected retrieval evidence IDs, HMAC-identified information-gap items, statement-bound sub-judgments and `assessment_contract_sha256`. The runner measures screening, legacy decision compatibility and the canonical assessment contract separately.
- Citation precision/recall uses only non-context `assessment.evidence_findings`; merely retrieving or returning a candidate no longer counts as citing it. Information gaps are matched by normalized Gold content, and sub-judgments are matched by statement plus status rather than by count/status multiset.
- Decision input may include controlled knowledge documents. It is consumed in memory and temporary traces only; no raw input is copied into the aggregate report.
- Citation and structured-output comparisons are reduced to counts and booleans before reporting; the aggregate report never contains evidence IDs, missing-item text or sub-judgment text.
