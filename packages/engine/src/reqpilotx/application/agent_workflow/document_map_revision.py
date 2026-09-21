"""Source-conserving Map successors and stable SourceItem rebinding.

Inputs are already compiled, immutable artifacts. This module never captures a
new source, guesses equivalent handles, publishes, or makes semantic decisions.
Table previews must be the caller's pinned original previews: a typed Map binds
table regions, but does not contain the original cell text itself.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import re
import unicodedata
from typing import Any
from types import SimpleNamespace

from reqpilotx.application.agent_workflow import source_requirement_baseline as core


class DocumentMapRevisionError(ValueError):
    """An explicit structure revision cannot preserve the supplied source."""


def _load(model, value):
    return model.model_validate(value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value))


def _dump(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value)


def _seal_receipt(value):
    return {**value, "receipt_sha256": core.canonical_json_sha256(value)}


def _table_key(table):
    return core.canonical_json_sha256({"pages": table.pages, "fragments": [_dump(f) for f in table.fragments]})


def _graph(document_map):
    nodes = {n.node_id: n for n in document_map.nodes}
    roots = [n.node_id for n in document_map.nodes if n.parent_id is None]
    if (len(document_map.root_node_ids) != len(set(document_map.root_node_ids))
            or set(roots) != set(document_map.root_node_ids)
            or len({(n.parent_id, n.order) for n in document_map.nodes}) != len(nodes)):
        raise DocumentMapRevisionError("MAP_ROOT_OR_ORDER_AMBIGUOUS")
    owners = defaultdict(list)
    for node in document_map.nodes:
        seen, parent = {node.node_id}, node.parent_id
        while parent:
            if parent in seen or parent not in nodes:
                raise DocumentMapRevisionError("MAP_PARENT_GRAPH_INVALID")
            seen.add(parent); parent = nodes[parent].parent_id
        if not {s.page for s in (*node.source_content, *node.source_anchors) if s.page is not None} <= set(node.pages):
            raise DocumentMapRevisionError("MAP_SOURCE_PAGE_COVERAGE_CHANGED")
        for block in node.content_blocks:
            if block.logical_table_scope_id:
                owners[block.logical_table_scope_id].append(node.node_id)
    table_ids = {t.scope_id for t in document_map.logical_tables}
    if set(owners) != table_ids or any(len(v) != 1 for v in owners.values()):
        raise DocumentMapRevisionError("MAP_LOGICAL_TABLE_OWNER_AMBIGUOUS")
    if any(a.logical_table_scope_id and a.logical_table_scope_id not in table_ids
           for n in document_map.nodes for a in n.source_anchors):
        raise DocumentMapRevisionError("MAP_SOURCE_TABLE_REFERENCE_UNKNOWN")
    for table in document_map.logical_tables:
        owner = owners[table.scope_id][0]
        while nodes[owner].parent_id:
            owner = nodes[owner].parent_id
        # Older accepted Maps use native document-unit IDs distinct from Map
        # node IDs. Their correspondence is checked against the predecessor.
        if table.owner_unit_id in nodes and owner != table.owner_unit_id:
            raise DocumentMapRevisionError("MAP_LOGICAL_TABLE_UNIT_DRIFT")
    return nodes, {k: v[0] for k, v in owners.items()}


def _source_facts(document_map, scope_translation=None):
    translation = scope_translation or {}
    content, anchors, blocks = Counter(), Counter(), Counter()
    for node in document_map.nodes:
        for item in node.source_content:
            # is_title is an Agent-owned structure role, not an original fact.
            content[core.canonical_json_sha256(item.model_dump(mode="json", exclude={"is_title"}))] += 1
        for anchor in node.source_anchors:
            payload = _dump(anchor)
            scope = payload["logical_table_scope_id"]
            payload["logical_table_scope_id"] = translation.get(scope, scope)
            anchors[core.canonical_json_sha256(payload)] += 1
        for block in node.content_blocks:
            if not block.logical_table_scope_id:
                continue
            payload = _dump(block)
            scope = payload["logical_table_scope_id"]
            payload["logical_table_scope_id"] = translation.get(scope, scope)
            blocks[core.canonical_json_sha256(payload)] += 1
    return {"source_content": dict(content), "source_anchors": dict(anchors), "content_blocks": dict(blocks)}


def _heading_is_grounded(document_map, node):
    if not node.source_content:
        return False
    request = core.build_source_item_text_scope_request(document_map, node_id=node.node_id)
    if node.label:
        title_parts = [c.text for c in node.source_content if c.is_title]
        texts = [c.text for c in node.source_content] + [" ".join(title_parts)]
        label = unicodedata.normalize("NFKC", node.label).strip()
        if not any(re.match(r"^" + re.escape(label) + r"(?![0-9A-Za-z]|\.\d)",
                            unicodedata.normalize("NFKC", text).lstrip()) for text in texts):
            return False
    return any(core._is_structure_only_title(request=request, content=c, selected=c.text)
               for c in node.source_content)


def _check_maps(previous, candidate):
    if previous.source_sha256 != candidate.source_sha256:
        raise DocumentMapRevisionError("MAP_REVISION_SOURCE_CHANGED")
    old_nodes, old_owners = _graph(previous)
    new_nodes, new_owners = _graph(candidate)
    old_tables, new_tables = defaultdict(list), defaultdict(list)
    for table in previous.logical_tables: old_tables[_table_key(table)].append(table)
    for table in candidate.logical_tables: new_tables[_table_key(table)].append(table)
    if set(old_tables) != set(new_tables) or any(len(v) != 1 for v in (*old_tables.values(), *new_tables.values())):
        raise DocumentMapRevisionError("MAP_LOGICAL_TABLE_CHANGED_OR_AMBIGUOUS; explicit table-boundary revision is required")
    scope_map = {old_tables[k][0].scope_id: new_tables[k][0].scope_id for k in old_tables}
    if _source_facts(previous) != _source_facts(candidate, {v: k for k, v in scope_map.items()}):
        raise DocumentMapRevisionError("MAP_SOURCE_FACTS_CHANGED; text, handles, geometry, images and multiplicity must be conserved")
    if previous.accepted_decisions != candidate.accepted_decisions:
        raise DocumentMapRevisionError("MAP_ACCEPTED_DECISIONS_CHANGED")
    previously_title = {c.content_id for n in previous.nodes for c in n.source_content if c.is_title}
    for node in candidate.nodes:
        promoted = [c for c in node.source_content if c.is_title and c.content_id not in previously_title]
        if promoted:
            request = core.build_source_item_text_scope_request(candidate, node_id=node.node_id)
            if any(not core._is_structure_only_title(request=request, content=c, selected=c.text) for c in promoted):
                raise DocumentMapRevisionError(f"MAP_TITLE_ROLE_NOT_SOURCE_GROUNDED: {node.node_id}")
        old = old_nodes.get(node.node_id)
        changed = old is None or any(getattr(old, k) != getattr(node, k)
                                    for k in ("kind", "label", "title", "title_authority"))
        if not changed:
            continue
        if node.title_authority == "SOURCE_VISIBLE":
            if not _heading_is_grounded(candidate, node):
                raise DocumentMapRevisionError(f"MAP_TITLE_NOT_SOURCE_GROUNDED: {node.node_id}")
        elif node.title_authority == "AGENT_DISPLAY_LABEL":
            if node.label:
                raise DocumentMapRevisionError(f"MAP_DISPLAY_LABEL_CANNOT_CREATE_SOURCE_NUMBER: {node.node_id}")
        else:
            raise DocumentMapRevisionError(f"MAP_TITLE_AUTHORITY_UNSUPPORTED: {node.node_id}")
    for key, old_values in old_tables.items():
        old, new = old_values[0], new_tables[key][0]
        def root_id(node_id, nodes):
            while nodes[node_id].parent_id:
                node_id = nodes[node_id].parent_id
            return node_id
        new_root = root_id(new_owners[new.scope_id], new_nodes)
        prior_units = {table.owner_unit_id for table in previous.logical_tables
                       if root_id(old_owners[table.scope_id], old_nodes) == new_root}
        if new.owner_unit_id != new_root and new.owner_unit_id not in prior_units:
            raise DocumentMapRevisionError("MAP_LOGICAL_TABLE_UNIT_DRIFT")
        if (old.capture_gap, old.source_numbering_anomaly) != (new.capture_gap, new.source_numbering_anomaly):
            raise DocumentMapRevisionError("MAP_TABLE_SOURCE_DIAGNOSTICS_CHANGED")
        if old.title != new.title:
            owner = new_nodes[new_owners[new.scope_id]]
            if new.title != owner.title or not _heading_is_grounded(candidate, owner):
                raise DocumentMapRevisionError("MAP_TABLE_TITLE_NOT_SOURCE_GROUNDED")
    return old_nodes, new_nodes, old_owners, new_owners, scope_map


def _structure_payload(document_map):
    return {"root_node_ids": list(document_map.root_node_ids),
            "nodes": [n.model_dump(mode="json", exclude={"ancestor_path"}) for n in document_map.nodes],
            "logical_tables": [_dump(t) for t in document_map.logical_tables],
            "accepted_decisions": [_dump(d) for d in document_map.accepted_decisions]}


def _native_reference(previous, candidate):
    binding = _seal_receipt({"schema_version": "document-map-revision-binding/v1",
        "source_sha256": previous.source_sha256, "previous_document_map_artifact_id": previous.artifact_id,
        "previous_document_map_sha256": previous.artifact_sha256,
        "successor_structure_sha256": core.canonical_json_sha256(_structure_payload(candidate)),
        "source_facts_sha256": core.canonical_json_sha256(_source_facts(previous))})
    reference = {"schema_version": "native-structure-reference-public/v1",
        "source_sha256": candidate.source_sha256, "artifact_id": candidate.native_artifact_id,
        **({"source_format": candidate.source_format} if candidate.source_format else {}),
        "document_tree": {"root_ids": list(candidate.root_node_ids),
            "nodes": [n.model_dump(mode="json", exclude={"ancestor_path"}) for n in candidate.nodes]},
        "logical_tables": [_dump(t) for t in candidate.logical_tables],
        "structure_proposals": [{"proposal_id": d.proposal_id, "node_id": d.node_id, "status": d.status,
            "recommended_action": d.recommended_action, "decision": {"decision_id": d.decision_id}}
            for d in candidate.accepted_decisions],
        "continuation": {"evidence_state": candidate.evidence_state},
        "validation": {"receipt_sha256": binding["receipt_sha256"]}}
    return reference, binding


def _node_changes(old_nodes, new_nodes):
    result = []
    for key in sorted(old_nodes.keys() | new_nodes.keys()):
        old, new = old_nodes.get(key), new_nodes.get(key)
        fields = {}
        for field in ("parent_id", "order", "kind", "label", "title", "title_authority", "ancestor_path",
                      "pages", "source_content", "source_anchors", "content_blocks"):
            before = _dump(old).get(field) if old else None
            after = _dump(new).get(field) if new else None
            if before != after: fields[field] = {"before": before, "after": after}
        if fields: result.append({"node_id": key, "change": "ADDED" if old is None else "REMOVED" if new is None else "CHANGED", "fields": fields})
    return result


def compile_document_map_revision(previous_map, candidate_map) -> dict[str, Any]:
    """Compile an Agent's same-source structure successor; never mutate inputs.

    Return JSON artifacts plus the exact native reference which recompiles to
    document_map at its returned revision. No D2 or human-acceptance receipt is
    fabricated. Whole content blocks/anchors and complete logical tables may
    move or receive stable node/scope names; evidence split/merge is not guessed.
    """
    previous = _load(core.DocumentMapArtifactV1, previous_map)
    candidate = _load(core.DocumentMapArtifactV1, candidate_map)
    old_nodes, _, _, _, scope_map = _check_maps(previous, candidate)
    reference, binding = _native_reference(previous, candidate)
    compiled = core.compile_document_map_artifact(reference, revision=previous.revision + 1)
    if compiled.nodes != candidate.nodes:
        raise DocumentMapRevisionError("MAP_CANDIDATE_ANCESTRY_OR_ORDER_NOT_COMPILER_DERIVED")
    changes = _node_changes(old_nodes, {n.node_id: n for n in compiled.nodes})
    receipt = _seal_receipt({"schema_version": "document-map-revision-receipt/v1",
        "source_sha256": previous.source_sha256,
        "previous_document_map_artifact_id": previous.artifact_id, "previous_document_map_sha256": previous.artifact_sha256,
        "candidate_document_map_artifact_id": candidate.artifact_id, "candidate_document_map_sha256": candidate.artifact_sha256,
        "document_map_artifact_id": compiled.artifact_id, "document_map_sha256": compiled.artifact_sha256,
        "native_projection_sha256": compiled.native_projection_sha256, "binding_receipt": binding,
        "changed_node_ids": [c["node_id"] for c in changes], "node_changes": changes,
        "root_order_before": list(previous.root_node_ids), "root_order_after": list(compiled.root_node_ids),
        "table_scope_successors": scope_map,
        "table_changes": [{"previous": _dump(old), "successor": _dump(next(t for t in compiled.logical_tables if t.scope_id == scope_map[old.scope_id]))}
                          for old in previous.logical_tables if old != next(t for t in compiled.logical_tables if t.scope_id == scope_map[old.scope_id])],
        "source_facts_sha256": binding["source_facts_sha256"], "source_conserved": True,
        "semantic_authority": "AGENT_STRUCTURE_DECISION", "human_acceptance": "NOT_CLAIMED"})
    return {"document_map": _dump(compiled), "native_reference": reference, "receipt": receipt}


def _unique_owner(catalog, keys, subject):
    choices = [set(catalog.get(key, ())) for key in keys]
    owners = set.intersection(*choices) if choices else set()
    if len(owners) != 1:
        raise DocumentMapRevisionError(f"SOURCE_OWNER_NOT_UNIQUE: {subject}; explicit SourceItem boundary revision is required")
    return next(iter(owners))


def _preview(document_map, scope_id, previews):
    table = next(t for t in document_map.logical_tables if t.scope_id == scope_id)
    value = previews.get(scope_id)
    if not isinstance(value, dict) or value.get("source_sha256") != document_map.source_sha256:
        raise DocumentMapRevisionError(f"SOURCE_TABLE_PREVIEW_MISSING_OR_UNBOUND: {scope_id}")
    request = core.build_source_requirement_scope_request(document_map, value)
    actual = [(f.table_token, f.page, f.row_range, core.native_location_fields(f)) for f in request.fragments]
    expected = [(f.table_token, f.page, f.row_range, core.native_location_fields(f)) for f in table.fragments]
    if actual != expected:
        raise DocumentMapRevisionError(f"SOURCE_TABLE_FRAGMENT_DRIFT: {scope_id}")
    return request


def _replay(document_map, baseline, previews):
    """Use the existing compiler to verify original bindings and full partitions.

    Its temporary IDs are discarded. Stable identities are retained only after
    replayed source bindings compare exactly with the supplied baseline.
    """
    requests, candidates, text_requests, text_candidates = [], [], [], []
    judgment_fields = ("label", "source_form", "shared_source_content_ids", "requirementness", "requirement_kinds",
                       "non_requirement_role", "confidence", "decision_summary", "reason_codes")
    for scope in baseline.covered_scope_ids:
        if scope.startswith("image:"):
            continue
        selected = [r for r in baseline.records if core._record_scope_id(r) == scope and not r.image_bindings]
        if scope.startswith("text:"):
            contexts = [c for c in baseline.text_context_dispositions if c.scope_id == scope]
            content_ids = {b.content_id for r in selected for b in r.text_bindings} | {c.content_id for c in contexts}
            content_ids.update(c for r in selected for c in r.shared_source_content_ids)
            if not content_ids:
                raise DocumentMapRevisionError(f"SOURCE_TEXT_SCOPE_WITHOUT_BINDINGS: {scope}")
            request = core.build_source_item_text_scope_request(document_map, node_id=scope[5:], content_ids=tuple(sorted(content_ids)))
            text_requests.append(request)
            text_candidates.append(core.SourceItemTextSliceCandidateV1(
                source_sha256=request.source_sha256, document_map_artifact_id=document_map.artifact_id,
                document_map_artifact_sha256=document_map.artifact_sha256, request_sha256=request.request_sha256, scope_id=scope,
                items=tuple(core.SourceItemTextBoundaryCandidateV1(item_key=r.source_record_id,
                    text_spans=tuple(core.SourceItemTextSpanV1(content_id=b.content_id, start_offset=b.start_offset, end_offset=b.end_offset) for b in r.text_bindings),
                    **{k: getattr(r, k) for k in judgment_fields}) for r in selected),
                structure_only_spans=tuple(core.SourceItemStructureOnlySpanV1(
                    text_span=core.SourceItemTextSpanV1(content_id=c.content_id, start_offset=c.start_offset, end_offset=c.end_offset),
                    reason_codes=c.reason_codes) for c in contexts)))
        else:
            request = _preview(document_map, scope, previews)
            requests.append(request)
            candidates.append(core.SourceRequirementSliceCandidateV1(
                source_sha256=request.source_sha256, document_map_artifact_id=document_map.artifact_id,
                document_map_artifact_sha256=document_map.artifact_sha256, request_sha256=request.request_sha256, logical_table_scope_id=scope,
                records=tuple(core.SourceRequirementBoundaryCandidateV1(record_key=r.source_record_id,
                    row_groups=tuple(core.SourceRequirementRowGroupV1(fragment_id=f.fragment_id, row_start=f.row_start, row_end=f.row_end) for f in r.table_fragments),
                    **{k: getattr(r, k) for k in judgment_fields}) for r in selected),
                context_row_groups=tuple(core.SourceRequirementContextRowGroupV1(role=c.role,
                    row_group=core.SourceRequirementRowGroupV1(fragment_id=c.fragment_id, row_start=c.row_start, row_end=c.row_end),
                    reason_codes=c.reason_codes, exact_placeholder_values=c.exact_placeholder_values, empty_template_reason=c.empty_template_reason) for c in baseline.context_dispositions if c.logical_table_scope_id == scope)))
    if requests or text_requests:
        replayed, _ = core.compile_source_requirement_baseline(document_map=document_map, requests=tuple(requests),
            candidates=tuple(candidates), text_requests=tuple(text_requests), text_candidates=tuple(text_candidates))
        def evidence_key(record):
            return core.canonical_json_sha256({"text": [_dump(v) for v in record.text_bindings], "table": [_dump(v) for v in record.table_fragments]})
        expected = {evidence_key(r): r for r in baseline.records if not r.image_bindings}
        actual = {evidence_key(r): r for r in replayed.records}
        if len(expected) != len(actual) or expected.keys() != actual.keys():
            raise DocumentMapRevisionError("SOURCE_BINDING_REPLAY_DRIFT")
        for key, record in expected.items():
            for field in (*judgment_fields, "shared_header_cell_ids", "shared_table_cell_ids", "pages", "node_id", "ancestor_path", "logical_table_scope_id"):
                if getattr(record, field) != getattr(actual[key], field):
                    raise DocumentMapRevisionError(f"SOURCE_BINDING_REPLAY_DRIFT: {record.source_display_id}/{field}")
    for record in baseline.records:
        core.validate_source_image_record_against_map(document_map, record)


def _context_signature(document_map, record):
    nodes = {n.node_id: n for n in document_map.nodes}
    tables = {t.scope_id: _table_key(t) for t in document_map.logical_tables}
    def anchor_fact(anchor):
        value = _dump(anchor)
        scope = value["logical_table_scope_id"]
        if scope:
            value["logical_table_scope_id"] = tables[scope]
        return value
    path, node_id = [], record.node_id
    while node_id:
        node = nodes[node_id]
        path.append({"label": node.label, "title": node.title, "title_authority": node.title_authority,
            "source_content": [_dump(c) for c in node.source_content], "source_anchors": [anchor_fact(a) for a in node.source_anchors]})
        node_id = node.parent_id
    shared = [_dump(c) for n in document_map.nodes for c in n.source_content if c.content_id in record.shared_source_content_ids]
    return core.canonical_json_sha256({"path": path, "shared": shared})


def _seal_baseline(values):
    draft = core.SourceRequirementBaselineArtifactV1.model_construct(**values,
        artifact_id="source-requirement-baseline-" + "0" * 32, artifact_sha256="0" * 64)
    digest = core.canonical_json_sha256(core._baseline_hash_payload(draft))
    return core.SourceRequirementBaselineArtifactV1(**values,
        artifact_id=f"source-requirement-baseline-{digest[:32]}", artifact_sha256=digest)


def _recover_demoted_text(document_map, original, projected, selection, feedback_ids):
    """Finish a private Map projection with explicit, coverage-conserving text decisions.

    The projection's old STRUCTURE_ONLY dispositions may no longer be titles in
    the corrected Map. It is never returned/published as a valid transition.
    Only the fully replayed, direct successor below can leave this operation.
    """
    if (selection.get("operation", "REPLACE") != "REPLACE" or selection.get("table_decisions")
            or selection.get("comparison_scope_ids") or selection.get("undo_target_path") or not feedback_ids):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_REQUIRES_EXPLICIT_TEXT_REPLACE_AND_FEEDBACK")
    affected = selection.get("affected_scope_ids", [])
    decisions = selection.get("text_decisions", [])
    if (not affected or len(affected) != len(set(affected))
            or any(not scope.startswith("text:") for scope in affected)
            or set(affected) - set(projected.covered_scope_ids)
            or len(decisions) != len(affected)
            or {item.get("scope_id") for item in decisions} != set(affected)):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_SCOPES_MUST_MATCH_CORRECTED_MAP")
    requests, candidates = [], []
    for item in decisions:
        request = core.build_source_item_text_scope_request(document_map, node_id=item["scope_id"][5:])
        payload = deepcopy(item)
        payload.update(source_sha256=document_map.source_sha256, document_map_artifact_id=document_map.artifact_id,
            document_map_artifact_sha256=document_map.artifact_sha256, request_sha256=request.request_sha256)
        requests.append(request)
        candidates.append(core.SourceItemTextSliceCandidateV1.model_validate(payload))
    # This receipt checks structural/hash integrity for the internal projection;
    # semantic partition acceptance happens only after replacement and _replay.
    projected_validation = core._compile_validation_receipt_from_artifact(document_map=document_map, artifact=projected)
    arguments = dict(document_map=document_map, current_artifact=projected,
        current_validation_receipt=projected_validation, text_requests=tuple(requests), text_candidates=tuple(candidates))
    revision = core.build_source_item_scope_revision(**arguments, operation="REPLACE",
        affected_scope_ids=tuple(affected), comparison_scope_ids=(), feedback_ids=tuple(feedback_ids))
    compiled, _, proof = core.compile_source_item_scope_revision(**arguments, scope_revision=revision)
    before = {r.source_record_id: r for r in projected.records}
    after = {r.source_record_id: r for r in compiled.records}
    if any(sid not in after or record.model_dump(exclude={"source_order"}) != after[sid].model_dump(exclude={"source_order"})
           for sid, record in before.items()):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_CANNOT_CHANGE_EXISTING_ITEMS; revise existing boundaries or judgments separately")
    if [sid for sid in after if sid in before] != list(before):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_EXISTING_SOURCE_ORDER_CHANGED")
    additions = [r for r in compiled.records if r.source_record_id not in before]
    structural_positions = {(c.content_id, offset) for c in original.text_context_dispositions
                            for offset in range(c.start_offset, c.end_offset)}
    if not additions or proof.tombstoned_identities or proof.superseded_identities:
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_MUST_RESTORE_PREVIOUSLY_STRUCTURAL_TEXT")
    for record in additions:
        positions = {(b.content_id, offset) for b in record.text_bindings for offset in range(b.start_offset, b.end_offset)}
        if not positions or record.table_fragments or record.image_bindings or not positions <= structural_positions:
            raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_NEW_ITEM_ESCAPES_ORIGINAL_STRUCTURE_SPANS")
    original_positions = core._artifact_text_positions(original)
    if original_positions != core._artifact_text_positions(compiled):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_SOURCE_COVERAGE_CHANGED")
    values = {key: getattr(compiled, key) for key in type(compiled).model_fields if key not in {"artifact_id", "artifact_sha256"}}
    values.update(revision=original.revision + 1, predecessor_artifact_id=original.artifact_id,
        predecessor_artifact_sha256=original.artifact_sha256,
        source_display_high_watermark=proof.source_display_id_high_watermark_after)
    successor = _seal_baseline(values)
    recovery = {"schema_version": "source-item-map-text-recovery/v1",
        "projected_baseline_sha256": projected.artifact_sha256,
        "source_text_positions_sha256": core.canonical_json_sha256(sorted(original_positions)),
        "scope_revision": _dump(revision), "scope_receipt": _dump(proof),
        "added_source_item_ids": [r.source_record_id for r in additions],
        "added_src_ids": [r.source_display_id for r in additions],
        "added_req_ids": [r.requirement_display_id for r in additions if r.requirement_display_id]}
    return successor, recovery


def map_rebind_added_source_ids(receipt, *, current_baseline=None):
    """Validate the optional typed scope proof at downstream carry boundaries."""
    value = receipt.get("text_scope_recovery")
    if value is None:
        return set()
    if current_baseline is None:
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_REQUIRES_FINAL_BASELINE")
    final = _load(core.SourceRequirementBaselineArtifactV1, current_baseline)
    if not isinstance(value, dict) or value.get("schema_version") != "source-item-map-text-recovery/v1":
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_RECEIPT_INVALID")
    revision = core.SourceItemScopeRevisionV1.model_validate(value.get("scope_revision"))
    proof = core.SourceItemScopeRevisionReceiptV1.model_validate(value.get("scope_receipt"))
    created = {item.source_record_id: item for item in proof.created_identities}
    ids = value.get("added_source_item_ids")
    preserved_src = receipt.get("preserved_src_ids")
    if not isinstance(preserved_src, list) or any(not isinstance(sid, str) or re.fullmatch(r"SRC-[0-9]{4,}", sid) is None for sid in preserved_src):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_PREVIOUS_IDENTITIES_INVALID")
    floor = receipt.get("source_display_high_watermark")
    if floor is not None and (type(floor) is not int or floor < 0):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_PREVIOUS_WATERMARK_INVALID")
    expected_floor = max([floor or 0, *(int(sid[4:]) for sid in preserved_src)])
    allocation = receipt.get("source_identity_allocation")
    if allocation is not None:
        # The public review verifier regenerates this exact context from its
        # immutable prefix. Here bind it to this receipt's actual predecessor.
        previous_envelope = SimpleNamespace(source_sha256=receipt.get("source_sha256"),
            artifact_id=receipt.get("previous_baseline_artifact_id"),
            artifact_sha256=receipt.get("previous_baseline_sha256"),
            source_display_high_watermark=expected_floor, records=())
        expected_floor = core.validate_source_identity_allocation(previous_envelope, allocation)
    # The scoped compiler had one private projected predecessor. The public
    # successor only changed its revision/predecessor envelope and made its
    # reserved-ID floor explicit. Reconstruct that exact compiler output;
    # another self-consistent hash is not proof that it produced this result.
    compiled_values = {key: getattr(final, key) for key in type(final).model_fields if key not in {"artifact_id", "artifact_sha256"}}
    compiled_values.update(revision=final.revision + 1,
        predecessor_artifact_id=revision.current_artifact_id,
        predecessor_artifact_sha256=revision.current_artifact_sha256,
        source_display_high_watermark=None if floor is None and allocation is None else proof.source_display_id_high_watermark_after)
    compiled = _seal_baseline(compiled_values)
    if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or set(ids) != set(created)
            or value.get("added_src_ids") != [created[sid].source_display_id for sid in ids]
            or value.get("added_req_ids") != [created[sid].requirement_display_id for sid in ids if created[sid].requirement_display_id]
            or proof.tombstoned_identities or proof.superseded_identities
            or revision.operation.value != "REPLACE" or proof.operation != revision.operation
            or proof.scope_revision_sha256 != revision.revision_sha256
            or proof.feedback_ids != revision.feedback_ids or proof.affected_scope_ids != revision.affected_scope_ids
            or any(not scope.startswith("text:") for scope in proof.affected_scope_ids)
            or proof.source_sha256 != revision.source_sha256
            or proof.document_map_artifact_sha256 != revision.document_map_artifact_sha256
            or proof.document_map_artifact_id != revision.document_map_artifact_id
            or proof.predecessor_artifact_sha256 != value.get("projected_baseline_sha256")
            or revision.current_artifact_sha256 != proof.predecessor_artifact_sha256
            or revision.current_artifact_id != proof.predecessor_artifact_id
            or revision.current_artifact_id != f"source-requirement-baseline-{revision.current_artifact_sha256[:32]}"
            or revision.current_artifact_revision != final.revision
            or proof.artifact_sha256 != compiled.artifact_sha256 or proof.artifact_id != compiled.artifact_id
            or proof.artifact_revision != compiled.revision
            or final.artifact_sha256 != receipt.get("baseline_sha256") or final.artifact_id != receipt.get("baseline_artifact_id")
            or final.source_sha256 != proof.source_sha256
            or final.document_map_artifact_sha256 != proof.document_map_artifact_sha256
            or revision.current_validation_receipt_sha256 != proof.predecessor_validation_receipt_sha256
            or revision.source_display_id_high_watermark != proof.source_display_id_high_watermark_before
            or proof.source_display_id_high_watermark_before != expected_floor
            or proof.source_sha256 != receipt.get("source_sha256")
            or proof.document_map_artifact_sha256 != receipt.get("document_map_sha256")
            or proof.document_map_artifact_id != receipt.get("document_map_artifact_id")
            or proof.source_display_id_high_watermark_after != receipt.get("final_source_display_high_watermark")
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_text_positions_sha256", ""))) is None):
        raise DocumentMapRevisionError("MAP_TEXT_RECOVERY_RECEIPT_BINDING_DRIFT")
    return set(ids)


def rebind_baseline_for_document_map_revision(previous_map, new_map, baseline, validation_receipt, table_previews,
        *, text_scope_recovery=None, feedback_ids=(), source_identity_allocation=None) -> dict[str, Any]:
    """Retain source identities/judgments after unique whole-evidence reownership.

    Physical source_order and all original bindings remain intact. The Map owns
    corrected structure order. Cross-owner splits and capture-ID migrations are
    rejected; no semantic classification, decomposition or approval is inferred.
    """
    previous = _load(core.DocumentMapArtifactV1, previous_map)
    current = _load(core.DocumentMapArtifactV1, new_map)
    old = _load(core.SourceRequirementBaselineArtifactV1, baseline)
    allocation_floor = core.validate_source_identity_allocation(old, source_identity_allocation)
    validation = _load(core.SourceRequirementBaselineValidationReceiptV1, validation_receipt)
    old_nodes, new_nodes, _, new_table_owners, scope_map = _check_maps(previous, current)
    reference, _ = _native_reference(previous, current)
    if current != core.compile_document_map_artifact(reference, revision=previous.revision + 1):
        raise DocumentMapRevisionError("MAP_REVISION_PREDECESSOR_OR_COMPILER_PROJECTION_DRIFT")
    core._assert_validation_receipt_matches_artifact(document_map=previous, artifact=old, receipt=validation,
        error_code="MAP_REBIND_PREVIOUS_BASELINE_RECEIPT_DRIFT")
    previews = deepcopy(table_previews)
    if set(previews) != {t.scope_id for t in previous.logical_tables}:
        raise DocumentMapRevisionError("SOURCE_TABLE_PREVIEW_MISSING_OR_UNKNOWN; every complete original table is required")
    for table in previous.logical_tables:
        _preview(previous, table.scope_id, previews)
    _replay(previous, old, previews)
    contents, images = defaultdict(set), defaultdict(set)
    for node in current.nodes:
        for c in node.source_content: contents[c.content_id].add(node.node_id)
        for a in node.source_anchors:
            if a.anchor_kind in {"IMAGE", "FIGURE"}: images[a.anchor_id].add(node.node_id)
    records, rebound, context_changed = [], [], []
    for record in old.records:
        if record.table_fragments:
            scope = scope_map[record.logical_table_scope_id]
            owner = new_table_owners[scope]
        else:
            scope = ""
            owner = _unique_owner(contents if record.text_bindings else images,
                [b.content_id for b in record.text_bindings] or [b.anchor_id for b in record.image_bindings], record.source_display_id)
        node = new_nodes[owner]
        ancestry = node.ancestor_path + (" ".join(filter(None, (node.label, node.title))),)
        value = record.model_copy(update={"document_map_artifact_id": current.artifact_id,
            "document_map_artifact_sha256": current.artifact_sha256, "node_id": owner,
            "ancestor_path": ancestry, "logical_table_scope_id": scope})
        records.append(value)
        if (record.node_id, record.ancestor_path, record.logical_table_scope_id) != (value.node_id, value.ancestor_path, value.logical_table_scope_id):
            rebound.append(record.source_record_id)
        if _context_signature(previous, record) != _context_signature(current, value):
            context_changed.append(record.source_record_id)
    text_contexts = []
    for c in old.text_context_dispositions:
        owner = _unique_owner(contents, [c.content_id], c.content_id)
        text_contexts.append(c.model_copy(update={"node_id": owner, "scope_id": f"text:{owner}"}))
    contexts = tuple(c.model_copy(update={"logical_table_scope_id": scope_map[c.logical_table_scope_id]}) for c in old.context_dispositions)
    relations = []
    record_index = {r.source_record_id: r for r in records}
    for relation in old.table_image_relations:
        owner = _unique_owner(images, [relation.image_binding.anchor_id], relation.image_binding.anchor_id)
        table_record = record_index[relation.table_source_record_id]
        if owner != table_record.node_id:
            raise DocumentMapRevisionError("SOURCE_TABLE_IMAGE_REOWNERSHIP_SPLIT; explicit boundary revision is required")
        relations.append(relation.model_copy(update={"node_id": owner, "logical_table_scope_id": table_record.logical_table_scope_id}))
    covered = {core._record_scope_id(r) for r in records} | {c.scope_id for c in text_contexts}
    covered.update(c.logical_table_scope_id for c in contexts)
    covered.update(f"image:{r.node_id}" for r in relations)
    # Preserve title-only processed text scopes and refuse silently lost scopes.
    for scope in old.covered_scope_ids:
        if scope.startswith("text:") and not any(core._record_scope_id(r) == scope for r in old.records) and not any(c.scope_id == scope for c in old.text_context_dispositions):
            raise DocumentMapRevisionError(f"SOURCE_PROCESSED_SCOPE_CANNOT_BE_REBOUND: {scope}")
    values = old.model_dump(mode="python", exclude={"artifact_id", "artifact_sha256"})
    values.update(revision=old.revision + 1, document_map_artifact_id=current.artifact_id,
        document_map_artifact_sha256=current.artifact_sha256, predecessor_artifact_id=old.artifact_id,
        predecessor_artifact_sha256=old.artifact_sha256, records=tuple(records), covered_scope_ids=tuple(sorted(covered)),
        context_dispositions=contexts, text_context_dispositions=tuple(text_contexts), table_image_relations=tuple(relations))
    if allocation_floor is not None:
        values["source_display_high_watermark"] = allocation_floor
    successor = _seal_baseline(values)
    rebound_previews = {new_scope: {**deepcopy(previews[old_scope]), "logical_table_scope_id": new_scope}
                        for old_scope, new_scope in scope_map.items() if old_scope in previews}
    recovery = None
    if text_scope_recovery is not None:
        successor, recovery = _recover_demoted_text(current, old, successor, text_scope_recovery, feedback_ids)
    _replay(current, successor, rebound_previews)
    checked = core._compile_validation_receipt_from_artifact(document_map=current, artifact=successor)
    changes = _node_changes(old_nodes, new_nodes)
    receipt = _seal_receipt({"schema_version": "source-item-document-map-rebind-receipt/v1",
        "source_sha256": old.source_sha256, "previous_document_map_artifact_id": previous.artifact_id,
        "previous_document_map_sha256": previous.artifact_sha256, "document_map_artifact_id": current.artifact_id,
        "document_map_sha256": current.artifact_sha256, "previous_baseline_artifact_id": old.artifact_id,
        "previous_baseline_sha256": old.artifact_sha256, "baseline_artifact_id": successor.artifact_id,
        "baseline_sha256": successor.artifact_sha256, "previous_validation_receipt_sha256": validation.receipt_sha256,
        "validation_receipt_sha256": checked.receipt_sha256, "changed_node_ids": [v["node_id"] for v in changes],
        "rebound_source_item_ids": rebound, "context_changed_source_item_ids": context_changed,
        "preserved_source_item_ids": [r.source_record_id for r in old.records],
        "preserved_src_ids": [r.source_display_id for r in old.records],
        "preserved_req_ids": [r.requirement_display_id for r in old.records if r.requirement_display_id],
        "source_display_high_watermark": old.source_display_high_watermark,
        "table_previews_sha256": core.canonical_json_sha256(previews), "table_scope_successors": scope_map,
        "record_count": len(successor.records), "classification_unchanged": True, "original_bindings_unchanged": True,
        "human_acceptance": "NOT_CLAIMED",
        **({"source_identity_allocation": deepcopy(source_identity_allocation)} if source_identity_allocation is not None else {}),
        **({"text_scope_recovery": recovery, "final_source_display_high_watermark": successor.source_display_high_watermark}
           if recovery else {})})
    if recovery:
        map_rebind_added_source_ids(receipt, current_baseline=successor)
    return {"baseline": _dump(successor), "validation_receipt": _dump(checked), "receipt": receipt,
            "table_previews": rebound_previews}
