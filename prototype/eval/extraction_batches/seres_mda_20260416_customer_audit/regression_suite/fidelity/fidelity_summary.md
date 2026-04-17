# Fidelity Benchmark

- PDF dir: `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260416_customer_audit/staged_docs`
- Target docs: `3`
- Checks: `66/69`
- Score: `95.65%`
- Failed docs: `1`
- Overall pass: `False`

## Document Scores
- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` (seres_v32_k89_cts_asc): `86.96%`, checks `20/23`
- `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf` (seres_q_sk_j02_311): `100.0%`, checks `23/23`
- `赛力斯UDS刷写规范 安全签名算法要求V1.1_20250410【AAA】.pdf` (seres_uds_signing_v1_1): `100.0%`, checks `23/23`

## Top Issues
- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` | `required_code_missing` | `6` | required scoped code missing
- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` | `required_title_missing_code` | `6` | title check missing code; need contains '法规需求'
- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` | `required_requirement_text_mismatch` | `4.1.2` | requirement text does not contain 'ASC_F 需实时反馈当前油压'

## Missing Profiles
- `esigned_emc_88033918`
- `esigned_et_88033919`
- `pds_de_active_damper_v1_0`
- `pds_de_oil_pump_v1_1`
- `q_jly_core`
- `qc_t_1067_monitor`
- `sor_baic_core`

## Skipped Docs
- `AES-CMAC128算法伪代码20250410【AAA】.pdf`
- `Q-SK J00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf`
- `Q-SK J02.226-2023_CAN(FD) AUTOSAR直接网络管理需求规范.pdf`
- `Q-SK J02.233-2023_CANFD 通信需求规范(2MbpS)  20231024.pdf`
- `Q-SK J02.321-2025_CAN、CANFD、Ethernet、LIN BootLoader刷写规范.pdf`
- `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf`
- `Q-SK J02.71-2023_CAN(FD)-CAN(FD)网关需求规范.pdf`
- `Q-SK J02.86-2024_CAN(FD)间接网络管理需求规范.pdf`
- `赛力斯UDS诊断规范 27服务安全算法要求V2.9_20250410【AAA】.pdf`
