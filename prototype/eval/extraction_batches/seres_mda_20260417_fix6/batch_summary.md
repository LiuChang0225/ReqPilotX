# Extraction Batch Summary

- batch_id: `seres_mda_customer_docs`
- run_id: `seres_mda_20260417_fix6`
- description: `SERES MDA customer document audit pack covering Q/SK standards, CTS specs, security algorithm addenda, and RFC-style references.`
- shared_repo_root: `/Users/liuchang/Documents/New project`

## Gates
- batch-report: `PASS` | docs=12 issues=0
- regression-suite: `PASS` | quality_pass=True fidelity_pass=True section_split_pass=True quality_exit_code=0 fidelity_exit_code=0 missing_required_profiles=[]
- oil-pump-benchmark: `FAIL` | not resolved

## Issue Rollup
- total_issues: `0`
- category_counts: `{}`
- severity_counts: `{}`

## Diagnosis
- findings: `27`
- pattern_candidates: `25`
- suspected_layer_counts: `{'gate': 9, 'parser': 11, 'preview': 7}`
- diagnosis_summary: `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260417_fix6/diagnosis_summary.md`

## Section Split
- summary: `{'targets': 0, 'targets_passed': 0, 'checks_passed': 0, 'checks_total': 0, 'score_pct': 100.0, 'results': []}`

## Documents
- `seres_aes_cmac_ref` -> `AES-CMAC128算法伪代码20250410【AAA】.pdf` | quality `87.5%` | fidelity `0.0%` | section_split `True` | diagnosis `1` findings
- `seres_q_sk_j00_14` -> `Q-SK J00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf` | quality `100.0%` | fidelity `0.0%` | section_split `True` | diagnosis `4` findings
- `seres_q_sk_j02_226` -> `Q-SK J02.226-2023_CAN(FD) AUTOSAR直接网络管理需求规范.pdf` | quality `100.0%` | fidelity `0.0%` | section_split `True` | diagnosis `2` findings
- `seres_q_sk_j02_233` -> `Q-SK J02.233-2023_CANFD 通信需求规范(2MbpS)  20231024.pdf` | quality `100.0%` | fidelity `0.0%` | section_split `True` | diagnosis `2` findings
- `seres_q_sk_j02_311` -> `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf` | quality `100.0%` | fidelity `100.0%` | section_split `True` | diagnosis `4` findings
- `seres_q_sk_j02_321` -> `Q-SK J02.321-2025_CAN、CANFD、Ethernet、LIN BootLoader刷写规范.pdf` | quality `100.0%` | fidelity `0.0%` | section_split `True` | diagnosis `2` findings
- `seres_q_sk_j02_385` -> `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf` | quality `100.0%` | fidelity `0.0%` | section_split `True` | diagnosis `3` findings
- `seres_q_sk_j02_71` -> `Q-SK J02.71-2023_CAN(FD)-CAN(FD)网关需求规范.pdf` | quality `100.0%` | fidelity `0.0%` | section_split `True` | diagnosis `2` findings
- `seres_q_sk_j02_86` -> `Q-SK J02.86-2024_CAN(FD)间接网络管理需求规范.pdf` | quality `87.5%` | fidelity `0.0%` | section_split `True` | diagnosis `2` findings
- `seres_v32_k89_cts_asc` -> `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` | quality `100.0%` | fidelity `100.0%` | section_split `True` | diagnosis `2` findings
- `seres_uds_signing_v1_1` -> `赛力斯UDS刷写规范 安全签名算法要求V1.1_20250410【AAA】.pdf` | quality `100.0%` | fidelity `100.0%` | section_split `True` | diagnosis `1` findings
- `seres_uds_27_security_v2_9` -> `赛力斯UDS诊断规范 27服务安全算法要求V2.9_20250410【AAA】.pdf` | quality `87.5%` | fidelity `0.0%` | section_split `True` | diagnosis `2` findings
