"""Project authored review artifacts onto the existing React Matrix contract.

No extraction, classification inference, production database or compliance verdicts.
"""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import math
from urllib.parse import unquote

try:
    from .source_order import section_positions
except ImportError:  # Direct local server entry point.
    from source_order import section_positions


CLASS_LABELS = {
    "PRODUCT_SYSTEM.FUNCTIONAL": "功能",
    "PRODUCT_SYSTEM.QUALITY": "质量",
    "PRODUCT_SYSTEM.CONSTRAINT": "约束",
    "PROJECT": "项目要求",
    "PROCESS": "过程要求",
}


def identity(store):
    if hasattr(store, "native_identity"):
        return store.native_identity
    case_id = store.bundle["ui"]["caseId"]
    return f"review-{case_id}", store.bundle["source"]["sha256"][:20], store.source_revision[:20]


def prefix(store):
    project, document, _ = identity(store)
    return f"/api/projects/{project}/sor-documents/{document}"


def source_crop(store, page, bbox):
    """Render an authorized original PDF region for the existing image viewer."""
    import fitz
    fact = next((p for p in store.bundle["pages"] if p["number"] == page), None)
    if (not fact or len(bbox) != 4 or not all(math.isfinite(x) for x in bbox)
            or not 0 <= bbox[0] < bbox[2] <= fact["width"]
            or not 0 <= bbox[1] < bbox[3] <= fact["height"]):
        raise ValueError("Invalid source crop")
    scale = min(4, 2400 / max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    with fitz.open(store.run_dir / "source.pdf") as document:
        return document[page-1].get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(bbox), alpha=False).tobytes("png")


def _source_text(bundle, ranges):
    """Read exact source slices; display never accepts an Agent-written quote."""
    parts = []
    for span in ranges:
        line = _span_fact(bundle, span)["text"]
        start, end = span["start"], span["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(line):
            raise ValueError("Invalid original source span")
        parts.append(line[start:end])
    return "\n".join(parts)


def _span_fact(bundle, span):
    if "lineId" in span:
        return bundle["lines"][span["lineId"]]
    for table in bundle.get("reviewTables", []):
        for segment in table["segments"]:
            if segment["id"] == span.get("tableId"):
                cell = next((cell for cell in segment["cells"] if cell["id"] == span["cellId"]), None)
                if cell is not None:
                    return cell
    raise ValueError("Unknown source cell")


def _source_excerpts(bundle, target):
    return [{"id": excerpt["id"], "role": excerpt.get("role", "DIRECT"),
             "text": _source_text(bundle, excerpt["ranges"]),
             "pages": sorted({_span_fact(bundle, span)["page"] for span in excerpt["ranges"]})}
            for excerpt in target.get("sourceExcerpts", [])]


def _base_node(node_id, parent, order, title, page, role="STRUCTURE"):
    return {
        "node_id": node_id, "parent_id": parent, "child_ids": [], "depth": 0,
        "source_order": order, "display_role": role, "source_kind": "STRUCTURE", "source_number": "",
        "display_title": title, "display_text": "", "label": title, "summary": "",
        "content_blocks": [], "table_refs": [], "figure_refs": [], "source_refs": [],
        "page_start": page, "page_end": page, "structure_node_id": None,
        "requirement_entry_id": None, "requirement_id": None, "requirement_code": "",
        "requirement_text": "", "table_record_id": None, "logical_table_ids": [],
        "matrix_visibility": "VISIBLE", "disposition": "RETAIN", "disposition_reason": "",
        "analysis_eligibility": "NOT_ELIGIBLE", "analysis_status": "NOT_APPLICABLE",
        "analysis_result_state": "NONE", "ai_assessment": None, "manual_decision": None,
        "compliance_status": "", "save_revision": 0, "updated_at": "",
        "classification_paths": [], "topic_tags": [],
    }


def matrix(store):
    if hasattr(store, "native_matrix"):
        return store.native_matrix()
    bundle = store.bundle
    state = store.state(False)
    project, document, requirement_set = identity(store)
    nodes = []
    sections = {section["id"]: section for section in bundle["outline"]}
    section_orders = section_positions(bundle)
    table_sections = {table["sectionId"] for table in bundle.get("reviewTables", [])}
    # A trailing note/table cannot replace its chapter: doing so makes earlier
    # subclauses children of later source content and reverses the reading order.
    collapsed = set()
    for section_id in sections:
        own_items = [item for item in bundle['items'] if item['sectionId'] == section_id]
        child_orders = [section_orders[child['id']] for child in sections.values()
                        if child.get('parentId') == section_id]
        if (section_id not in table_sections and len(own_items) == 1
                and (not child_orders or own_items[0]['reviewMetadata'].get('sourceOrder', section_orders[section_id])
                     <= min(child_orders))):
            collapsed.add(section_id)
    section_aliases = {item["sectionId"]: item["id"] for item in bundle["items"] if item["sectionId"] in collapsed}
    for section in bundle["outline"]:
        if section["id"] in collapsed:
            continue
        parent = section.get("parentId")
        node = _base_node(section["id"], section_aliases.get(parent, parent), section_orders[section["id"]],
                          section["title"] or section.get("label", ""), section["page"])
        node["source_number"] = section.get("label", "")
        nodes.append(node)
    for ordinal, item in enumerate(bundle["items"]):
        for target_index, target in enumerate([item, *item.get("derived", [])]):
            derived = target_index > 0
            meta = deepcopy(target["reviewMetadata"])
            review = state["targets"].get(target["id"], {})
            text = review.get("patch", {}).get("text", target["text"])
            refs = target["sourceRefs"]
            pages = sorted({ref["page"] for ref in refs})
            presentation = target.get("sourcePresentation", {}) if not derived else {}
            if derived:
                title = meta["title"]
            elif presentation.get("titleRanges"):
                title = _source_text(bundle, presentation["titleRanges"])
            elif presentation.get("titleRole") == "TABLE_LABEL":
                title = "原文表格"
            elif presentation.get("titleRole") == "AGENT_ASSIGNED":
                title = presentation["title"]
            elif item["sectionId"] in collapsed and sections[item["sectionId"]].get("title"):
                title = sections[item["sectionId"]]["title"]
            else:
                title = next(iter(target["text"].splitlines()), "原文内容")
            number = presentation.get("number", meta["number"])
            parent = item["id"] if derived else (sections[item["sectionId"]].get("parentId") if item["sectionId"] in collapsed else item["sectionId"])
            if not derived:
                parent = section_aliases.get(parent, parent)
            node = _base_node(target["id"], parent,
                              meta.get("sourceOrder", 100 + ordinal * 10) + target_index, title, pages[0],
                              "ATOMIC_REQUIREMENT" if derived else "CLAUSE")
            node.update(source_number=number, source_kind="DERIVED_REQUIREMENT" if derived else "SOURCE_ITEM",
                        page_end=pages[-1], display_text=text, summary=text, requirement_text=text,
                        requirement_code=number, source_refs=refs,
                        source_title_role=presentation.get("titleRole", ""),
                        source_number_role=presentation.get("numberRole", "SOURCE"),
                        source_excerpts=_source_excerpts(bundle, target),
                        classification_paths=meta["classifications"], topic_tags=meta["topics"],
                        save_revision=state["revision"])
            node["structure_node_id"] = item["sectionId"] if not derived else None
            node["content_blocks"] = deepcopy(target.get("displayBlocks", [{"id": target["id"], "type": "TEXT", "text": text}]))
            if target.get("displayBlocks"):
                node["display_text"] = ""
                node["requirement_text"] = ""
                node["summary"] = ""
                node["display_role"] = "TABLE"
            if derived:
                node["requirement_id"] = target["id"]
            else:
                node["requirement_entry_id"] = target["id"]
            eligible = (meta["requirementness"] == "REQUIREMENT"
                        and meta.get("analysisEligibility", "ELIGIBLE") == "ELIGIBLE"
                        and (derived or not item.get("derived")))
            if eligible:
                node.update(disposition="ANALYZE", analysis_eligibility="ELIGIBLE", analysis_status="NOT_STARTED")
            node["candidate_review"] = {
                **meta, "status": review.get("status", "unreviewed"),
                "notes": review.get("notes", []), "lastEventId": review.get("lastEventId"),
                "sourceRevision": store.source_revision, "reviewRevision": state["revision"],
            }
            nodes.append(node)
    index = {node["node_id"]: node for node in nodes}
    review_tables = []
    for table in bundle.get("reviewTables", []):
        table_id, section_id = table["id"], table["sectionId"]
        owner = index[section_id]
        owner.update(display_role="TABLE", content_blocks=deepcopy(table["segments"]),
                     logical_table_ids=[table_id], source_refs=deepcopy(table["sourceRefs"]),
                     page_start=table["segments"][0]["page"], page_end=table["segments"][-1]["page"])
        record_ids = {record['record_id'] for record in table['records']}
        source_containers = [item for item in bundle['items']
                             if table_id in item.get('matrixTables', [])
                             and item['id'] not in record_ids
                             and item['sectionId'] in {section_id, sections[section_id].get('parentId')}]
        if len(source_containers) == 1:
            # Preserve an explicit whole-table/shared-context source association;
            # otherwise the table rows can precede their own source container.
            owner['parent_id'] = source_containers[0]['id']
        node_ids = [section_id]
        for record in table["records"]:
            source = next(i for i in bundle["items"] if i["id"] == record["record_id"])
            for target in [source, *source.get("derived", [])]:
                node = index[target["id"]]
                node.update(table_record_id=source["id"], logical_table_ids=[table_id], content_blocks=[])
                if target is source:
                    node["display_role"] = "TABLE_RECORD"
                node_ids.append(node["node_id"])
        review_tables.append({"logical_table_id":table_id, "projection_id":table_id,
            "projection_mode":"SOURCE_TABLE_RECORDS", "record_count":len(table["records"]),
            "physical_table_ids":[s["id"] for s in table["segments"]], "node_ids":node_ids,
            "evidence_binding_ids":[], "structured_segments":deepcopy(table["segments"]),
            "projection":{"records":deepcopy(table["records"])}})
    for node in nodes:
        if node["parent_id"] in index:
            index[node["parent_id"]]["child_ids"].append(node["node_id"])
    for node in nodes:
        node["child_ids"].sort(key=lambda child_id: index[child_id]["source_order"])
    roots = [node["node_id"] for node in nodes if node["parent_id"] not in index]
    roots.sort(key=lambda node_id: index[node_id]["source_order"])
    def set_depth(node_id, depth):
        index[node_id]["depth"] = depth
        for child in index[node_id]["child_ids"]:
            set_depth(child, depth + 1)
    for node_id in roots:
        set_depth(node_id, 0)
    count = len(nodes)
    eligible = sum(node["analysis_eligibility"] == "ELIGIBLE" for node in nodes)
    tables = review_tables[:]
    for node in nodes:
        if node["node_id"] in table_sections:
            continue
        for block in node["content_blocks"]:
            if block.get("type") != "TABLE":
                continue
            node["logical_table_ids"].append(block["id"])
            tables.append({"logical_table_id": block["id"], "projection_id": block["id"],
                           "projection_mode": block.get("projection_mode", "SOURCE_REFERENCE_LIST"),
                           "record_count": block["row_count"] - len(block.get("header_rows", [])),
                           "physical_table_ids": [block["id"]], "node_ids": [node["node_id"]],
                           "evidence_binding_ids": [], "structured_segments": [block]})
    return {
        "schema_version": "requirement-matrix-snapshot/v1", "state": "READY",
        "project_id": project, "document_id": document, "requirement_set_id": requirement_set,
        "requirement_set_status": "DRAFT", "tree_sha256": store.source_revision,
        "root_node_ids": roots, "nodes": nodes, "logical_tables": tables,
        "counts": {"total_nodes": count, "visible_nodes": count, "system_non_business_nodes": 0,
                   "human_excluded_nodes": 0, "analyze_nodes": eligible, "retained_nodes": count-eligible,
                   "excluded_nodes": 0, "analysis_eligible_nodes": eligible, "assessed_nodes": 0,
                   "decided_nodes": 0, "logical_tables": len(tables)},
        "candidate_review": {"caseId": bundle["ui"]["caseId"], "sourceCount": len(bundle["items"]),
                             "derivedCount": sum(len(item.get("derived", [])) for item in bundle["items"]),
                             "topics": bundle["ui"]["topics"], "initialNodeId": bundle["ui"]["initialNodeId"],
                             "scope": bundle["ui"]["scope"], "reviewRevision": state["revision"]},
    }


def bootstrap(store):
    if hasattr(store, "native_bootstrap"):
        return store.native_bootstrap()
    project, document, requirement_set = identity(store)
    return {
        "caseId": store.bundle["ui"]["caseId"],
        "project": {"id": project, "name": "客户原文核对", "customer_name": "本地案例"},
        "document": {"id": document, "project_id": project, "file_name": store.bundle["source"]["name"],
                     "source_sha256": store.bundle["source"]["sha256"],
                     "page_count": store.bundle["source"]["pageCount"], "status": "DRAFT"},
        "requirementSets": {"active_requirement_set_id": requirement_set,
                            "requirement_sets": [{"id": requirement_set, "status": "DRAFT", "version": store.bundle["ui"].get("reviewVersion", 1),
                                                  "version_label": store.bundle["ui"].get("versionLabel", "原文核对候选"), "source_kind": "AGENT_CANDIDATE"}]},
        "scope": store.bundle["ui"]["scope"], "sourceRevision": store.source_revision,
    }


def api_payload(store, path):
    if path == "/api/candidate-review":
        return bootstrap(store)
    base = prefix(store)
    _, _, requirement_set = identity(store)
    matrix_base = f"{base}/requirement-sets/{requirement_set}/matrix"
    if path == matrix_base:
        return matrix(store)
    if path.startswith(matrix_base + "/tables/"):
        table_id = unquote(path[len(matrix_base + "/tables/"):])
        snapshot = matrix(store)
        table = next((table for table in snapshot["logical_tables"] if table["logical_table_id"] == table_id), None)
        if table:
            return {"schema_version": "requirement-matrix-logical-table-detail/v1", "logical_table": table,
                    "nodes": [node for node in snapshot["nodes"] if node["node_id"] in table["node_ids"]]}
        return None
    if path.startswith(matrix_base + "/nodes/") and path.endswith("/detail"):
        node_id = unquote(path[len(matrix_base + "/nodes/"):-len("/detail")])
        snapshot = matrix(store)
        index = {node["node_id"]: node for node in snapshot["nodes"]}
        if node_id not in index:
            return None
        node = index[node_id]
        ancestors = [node]
        while ancestors[0]["parent_id"] in index:
            ancestors.insert(0, index[ancestors[0]["parent_id"]])
        parent_clause = next((n for n in reversed(ancestors[:-1]) if n.get("candidate_review")), None)
        return {"schema_version": "requirement-matrix-node-detail/v1",
                "project_id": snapshot["project_id"], "document_id": snapshot["document_id"],
                "requirement_set_id": requirement_set, "node": node, "path": ancestors,
                "parent_clause": parent_clause if node["source_kind"] == "DERIVED_REQUIREMENT" else None,
                "tree_node": node, "requirement": None, "assessment": None,
                "logical_tables": [table for table in snapshot["logical_tables"] if node["node_id"] in table["node_ids"]]}
    page_prefix = base + "/extraction/source-pages/"
    if path.startswith(page_prefix):
        page = int(path[len(page_prefix):])
        fact = next((p for p in store.bundle["pages"] if p["number"] == page), None)
        if not fact:
            return None
        return {"source_sha256": store.bundle["source"]["sha256"],
            "source_identity": {"source_sha256": store.bundle["source"]["sha256"]}, "page": {
            "page_number": page, "width": fact["width"], "height": fact["height"],
            "bounds": [0, 0, fact["width"], fact["height"]], "blocks": [], "tables": [],
        }}
    focus_prefix = base + "/extraction/geometry-focus/"
    if path.startswith(focus_prefix):
        node_id = unquote(path[len(focus_prefix):])
        target = store.targets.get(node_id)
        refs = target[1].get("sourceRefs", []) if target else next(
            (table["sourceRefs"] for table in store.bundle.get("reviewTables", []) if table["sectionId"] == node_id), [])
        return {"regions": [{"id": f"source-{i}-{j}", "page_number": ref["page"], "bbox": rect,
                              "role": "TEXT", "precision": ref.get("precision", "line")}
                             for i, ref in enumerate(refs) for j, rect in enumerate(ref.get("rects", []))]}
    return None


def export_xlsx(store, payload):
    """Export the UI's exact matched identities, without interpreting filters twice."""
    if hasattr(store, "native_export"):
        return store.native_export(payload)
    import xlsxwriter
    if not isinstance(payload, dict):
        raise ValueError("导出请求格式无效。")
    with store.lock:
        snapshot = matrix(store)
        if payload.get("sourceRevision") != store.source_revision or type(payload.get("expectedRevision")) is not int or payload.get("expectedRevision") != store.state(False)["revision"]:
            raise ValueError("核对版本已变化，请刷新后重新导出。")
        requested = payload.get("nodeIds")
        index = {node["node_id"]: node for node in snapshot["nodes"] if node.get("candidate_review")}
        if not isinstance(requested, list) or not all(isinstance(node_id, str) for node_id in requested) or len(requested) != len(set(requested)) or any(node_id not in index for node_id in requested):
            raise ValueError("导出条目范围无效。")
        rows = [index[node_id] for node_id in requested]
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    sheet = workbook.add_worksheet("原文核对清单")
    header = workbook.add_format({"bold": True, "bg_color": "#E4F1E8", "text_wrap": True, "valign": "top"})
    body = workbook.add_format({"text_wrap": True, "valign": "top"})
    headers = ["条目编号", "父项编号", "原文/派生", "完整内容", "需求大类", "系统需求类型", "主题标签", "内容角色", "来源页", "核对状态", "反馈", "原文标题/摘录或派生标题", "派生依据原文"]
    all_nodes = {node["node_id"]: node for node in snapshot["nodes"]}
    sheet.write_row(0, 0, headers, header)
    for row_index, node in enumerate(rows, 1):
        meta = node["candidate_review"]
        paths = node["classification_paths"]
        domains = list(dict.fromkeys("产品/系统" if p.startswith("PRODUCT_SYSTEM.") else CLASS_LABELS[p] for p in paths))
        kinds = [CLASS_LABELS[p] for p in paths if p.startswith("PRODUCT_SYSTEM.")]
        source = store.targets[node["node_id"]][1]
        content = node["display_text"] or source["text"]
        values = [node["source_number"], all_nodes.get(node["parent_id"], {}).get("source_number", ""),
                  "派生" if node["source_kind"] == "DERIVED_REQUIREMENT" else "原文", content,
                  "；".join(domains), "；".join(kinds), "；".join(store.bundle["ui"]["topics"][tag] for tag in node["topic_tags"]),
                  meta.get("contentRole", ""), "、".join(str(ref["page"]) for ref in source["sourceRefs"]),
                  meta["status"], "\n".join(filter(None, [meta.get("ownerFeedback", ""), *[note["text"] for note in meta["notes"]]])),
                  ("[Agent 拟名/编号] " if node["source_title_role"] == "AGENT_ASSIGNED" else "") + node["display_title"],
                  "\n\n".join(excerpt["text"] for excerpt in node["source_excerpts"])]
        sheet.write_row(row_index, 0, values, body)
        lines = sum(max(1, (len(line)+44)//45) for line in content.splitlines())
        sheet.set_row(row_index, min(409, max(44, lines * 18)), None, {"level": min(node["depth"], 7)})
    sheet.freeze_panes(1, 3)
    sheet.autofilter(0, 0, len(rows), len(headers)-1)
    sheet.set_column(0, 2, 15)
    sheet.set_column(3, 3, 82)
    sheet.set_column(4, 10, 20)
    sheet.set_column(11, 12, 60)
    tables = [(node, block) for node in rows
              for block in store.targets[node["node_id"]][1].get("displayBlocks", [])
              if block.get("type") == "TABLE"]
    selected_table_ids = {table_id for node in rows for table_id in node["logical_table_ids"]}
    for table in store.bundle.get("reviewTables", []):
        if table["id"] in selected_table_ids:
            tables.extend((all_nodes[table["sectionId"]], segment) for segment in table["segments"])
    references = [(node, block) for node, block in tables
                  if block.get("projection_mode", "SOURCE_REFERENCE_LIST") == "SOURCE_REFERENCE_LIST"]
    if references:
        detail = workbook.add_worksheet("参考依据")
        detail.write_row(0, 0, ["所属原文", "标准编号", "原文名称"], header)
        detail.freeze_panes(1, 1)
        detail.set_column(0, 1, 22)
        detail.set_column(2, 2, 85)
        position = 1
        for node, table in references:
            for index in range(table["row_count"]):
                values = [cell["text"] for cell in sorted(table["cells"], key=lambda cell: cell["column"]) if cell["row"] == index]
                detail.write_row(position, 0, [node["source_number"], *values], body)
                detail.set_row(position, 32)
                position += 1
        detail.autofilter(0, 0, position-1, 2)
    # Other source tables retain their actual columns and merged cells. They are
    # detail sheets for a single original item, not extra requirements.
    for table_index, (node, table) in enumerate(tables, 1):
        if table.get("projection_mode", "SOURCE_REFERENCE_LIST") == "SOURCE_REFERENCE_LIST":
            continue
        detail = workbook.add_worksheet(f"原表-{table_index}")
        detail.write_row(0, 0, ["所属原文", node["source_number"], node["display_title"]], header)
        detail.write_row(1, 0, ["来源页", table.get("page_number", table.get("page")), "完整来源参考；核对条目范围以主清单为准"], body)
        offset = 3
        for cell in table["cells"]:
            row, column = cell["row"] + offset, cell["column"]
            end_row = row + cell.get("row_span", 1) - 1
            end_column = column + cell.get("column_span", 1) - 1
            fmt = header if cell["row"] in table.get("header_rows", []) else body
            if row != end_row or column != end_column:
                detail.merge_range(row, column, end_row, end_column, cell["text"], fmt)
            else:
                detail.write(row, column, cell["text"], fmt)
        detail.set_column(0, table["column_count"]-1, 30)
        for row in range(offset, offset + table["row_count"]):
            detail.set_row(row, 44)
        detail.freeze_panes(offset + len(table.get("header_rows", [])), 0)
    image_facts = {}
    for node, table in tables:
        for cell in table["cells"]:
            for fact in cell.get("images", []):
                image_facts.setdefault(fact["id"], (node, table, fact, f"行{cell['row']+1} 列{cell['column']+1}"))
        for fact in table.get("floating_images", []):
            image_facts.setdefault(fact["id"], (node, table, fact, "跨行 " + ",".join(str(r+1) for r in fact["source_row_indexes"])))
    if image_facts:
        from PIL import Image
        images = workbook.add_worksheet("原图")
        images.set_column(0, 6, 18)
        position = 0
        for node, table, fact, location in sorted(image_facts.values(), key=lambda entry: (entry[2]["page"], entry[2]["bbox"][1], entry[2]["bbox"][0])):
            images.write_row(position, 0, [node["source_number"], node["display_title"], table["id"], location, f"PDF {fact['page']}"], header)
            data = source_crop(store, fact["page"], fact["bbox"])
            with Image.open(BytesIO(data)) as rendered:
                width, height = rendered.size
            scale = min(1, 850/width, 440/height)
            images.insert_image(position+1, 0, "source.png", {"image_data":BytesIO(data), "x_scale":scale, "y_scale":scale,
                "description":f"原PDF第{fact['page']}页 · {node['display_title']} · {location}"})
            position += 3 + math.ceil(height*scale/20)
    metadata = workbook.add_worksheet("_ReqPilotXMeta")
    metadata.write_row(0, 0, ["来源版本", store.source_revision])
    metadata.write_row(1, 0, ["用途", "原文核对候选；不是正式符合性结论"])
    for row_index, node in enumerate(rows, 2):
        metadata.write_row(row_index, 0, [node["node_id"], node["parent_id"] or ""])
    metadata.hide()
    workbook.close()
    return output.getvalue()
