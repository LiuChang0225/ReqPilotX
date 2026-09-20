#!/usr/bin/env python3
"""
Multi-Source Knowledge Conflict & Parameter Divergence Detector
--------------------------------------------------------------
Scans wiki pages and normalized source markdowns to extract numerical
parameters, detects cross-document divergence, and reports conflict gates.
"""

import os
import sys
import re
import json
from pathlib import Path

def detect_parameter_conflicts(wiki_dir: str, output_report: str):
    wiki_path = Path(wiki_dir)

    # Define monitored parameter patterns
    parameter_patterns = {
        "operating_temperature_max": {
            "name": "最高工作温度",
            "regex": r"(?:工作温度|环境温度|operating\s*temp|T_amb).*?([0-9]{2,3})\s*(?:℃|°C)",
            "unit": "°C",
            "expected_nominal": 150
        },
        "operating_voltage_nominal": {
            "name": "标称供电电压",
            "regex": r"(?:供电电压|工作电压|supply\s*voltage|V_cc|V_supply).*?([0-9]+(?:\.[0-9]+)?)\s*V",
            "unit": "V",
            "expected_nominal": 5.0
        },
        "airgap_nominal": {
            "name": "标称工作气隙",
            "regex": r"(?:标称气隙|工作气隙|nominal\s*airgap|air\s*gap).*?([0-9]+(?:\.[0-9]+)?)\s*mm",
            "unit": "mm",
            "expected_nominal": 1.0
        },
        "operating_speed_max": {
            "name": "最高工作转速",
            "regex": r"(?:最高转速|工作转速|max(?:imum)?\s*speed).*?([0-9]{4,6})\s*(?:rpm|RPM)",
            "unit": "rpm",
            "expected_nominal": 20000
        },
        "magnetic_immunity_dc": {
            "name": "抗直流杂散磁场",
            "regex": r"(?:抗直流磁场|磁场抗扰|stray\s*field).*?([0-9]{3,5})\s*(?:A/m)",
            "unit": "A/m",
            "expected_nominal": 4000
        }
    }

    extracted = {k: [] for k in parameter_patterns}

    for root, _, files in os.walk(wiki_path):
        for f in files:
            if f.endswith(".md") and f not in ["WIKI_RULES.md", "log.md"]:
                fp = Path(root) / f
                rel_p = str(fp.relative_to(wiki_path))
                with open(fp, "r", encoding="utf-8") as fobj:
                    lines = fobj.readlines()

                for line_idx, line in enumerate(lines, 1):
                    for p_key, p_meta in parameter_patterns.items():
                        matches = re.finditer(p_meta["regex"], line, re.IGNORECASE)
                        for m in matches:
                            val_str = m.group(1)
                            try:
                                val_num = float(val_str)
                                extracted[p_key].append({
                                    "doc": rel_p,
                                    "line_no": line_idx,
                                    "val": val_num,
                                    "unit": p_meta["unit"],
                                    "context": line.strip()[:100]
                                })
                            except ValueError:
                                pass

    # Analyze divergence/conflicts
    conflicts = []
    normal_parameters = []

    for p_key, records in extracted.items():
        meta = parameter_patterns[p_key]
        if not records:
            continue

        unique_vals = sorted(list(set(r["val"] for r in records)))
        has_conflict = False
        divergence_reason = ""

        # Evaluate if values diverge significantly from nominal
        if len(unique_vals) > 1:
            # Special domain checks
            if p_key == "operating_temperature_max":
                # 150 vs 140/160
                if any(v < 145 or v > 155 for v in unique_vals):
                    has_conflict = True
                    divergence_reason = f"存在温度边界分歧：发现文档出现不同上限值 {unique_vals}℃。通常 150℃ 为产品基线，而部分特定测试谱标注为 140℃ 或短时 160℃。"
            elif p_key == "operating_voltage_nominal":
                if any(abs(v - 5.0) > 0.5 for v in unique_vals):
                    has_conflict = True
                    divergence_reason = f"供电电压数值跨度较大 {unique_vals}V。需注意区分 5V 产品原生稳压与外部 12V 汽车总线要求。"
            elif p_key == "airgap_nominal":
                if any(v < 0.8 or v > 1.2 for v in unique_vals):
                    has_conflict = True
                    divergence_reason = f"气隙数值存在跨度 {unique_vals}mm。包含标称值 1.0mm 与极限边界 0.3mm/1.8mm，需明确界定。"
            else:
                has_conflict = True
                divergence_reason = f"文档中存在多种数值声明: {unique_vals} {meta['unit']}。"

        item_data = {
            "parameter_key": p_key,
            "parameter_name": meta["name"],
            "unit": meta["unit"],
            "nominal_baseline": meta["expected_nominal"],
            "detected_values": unique_vals,
            "occurrences_count": len(records),
            "citations": records[:5], # top citations
            "has_conflict": has_conflict,
            "divergence_analysis": divergence_reason if has_conflict else "各文档声明一致，无冲突。"
        }

        if has_conflict:
            conflicts.append(item_data)
        else:
            normal_parameters.append(item_data)

    report = {
        "status": "COMPLETED",
        "conflict_summary": {
            "total_monitored_parameters": len(parameter_patterns),
            "conflicts_detected": len(conflicts),
            "consistent_parameters": len(normal_parameters)
        },
        "conflicts": conflicts,
        "consistent": normal_parameters
    }

    out_p = Path(output_report)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n========================================================")
    print(f"✓ Knowledge Conflict Gate Scan Complete!")
    print(f"  Monitored Parameters: {len(parameter_patterns)}")
    print(f"  Divergence/Conflicts Flagged: {len(conflicts)}")
    print(f"  Saved Conflict Report to: {out_p}")
    print(f"========================================================\n")
    return report

def main():
    wiki_dir = "/Users/liuchang/Documents/New project/docs/wiki/irps_baseline"
    output_report = "/Users/liuchang/Documents/New project/docs/wiki/irps_baseline/conflicts_report.json"
    detect_parameter_conflicts(wiki_dir, output_report)

if __name__ == "__main__":
    main()
