from __future__ import annotations

from reqpilotx.application.extraction.structure_boundary_rules import (
    EXPLICIT_LIST_PREFIX,
    IDEOGRAPHIC_NUMBERED_PREFIX,
    RESOLVER_ALPHABETIC_PEER_POLICY,
    AlphabeticMarkerEvidence,
    AlphabeticPeerPosition,
    alphabetic_pair_interstitial_is_transparent,
    alphabetic_peer_position,
    evaluate_alphabetic_peer_transition,
    implicit_cjk_root_evidence_from_text,
    normalize_boundary_title,
    parse_alphabetic_parenthesized_marker,
)
from reqpilotx.domain.extraction.source_ir import SourceNode, SourceNodeKind


_ALPHABETIC_LIST_MIN_RUN_LENGTH = 3
_ALPHABETIC_LIST_CLOSED_PAIR_LENGTH = 2
_IMPLICIT_ROOT_MIN_COHERENT_RUN_LENGTH = 3
_IMPLICIT_ROOT_PDF_X_TOLERANCE_POINTS = 12.0
_IMPLICIT_ROOT_PDF_HEIGHT_TOLERANCE_RATIO = 0.35


def credible_alphabetic_list_ordinals(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
    boundary_source_ids: set[str],
    allow_closed_pairs: bool,
) -> dict[str, int]:
    """Authorize only coherent, source-bound alphabetic peer sequences.

    This function belongs to the decision layer because it promotes accepted
    evidence into resolver ordinals.  Grammar and evidence helpers remain in
    ``structure_boundary_rules`` and do not themselves authorize tree writes.
    """

    resolved: dict[str, int] = {}
    run: list[
        tuple[SourceNode, AlphabeticMarkerEvidence, AlphabeticPeerPosition]
    ] = []
    pair_interstitial_is_transparent = True
    interstitial_since_last_marker: list[SourceNode] = []

    def flush(*, closed_by_structure_boundary: bool = False) -> None:
        nonlocal pair_interstitial_is_transparent
        long_run = bool(
            len(run) >= _ALPHABETIC_LIST_MIN_RUN_LENGTH
            and run[0][1].ordinal == 1
            and all(
                current[1].ordinal == previous[1].ordinal + 1
                for previous, current in zip(run, run[1:], strict=False)
            )
        )
        closed_pair = bool(
            allow_closed_pairs
            and closed_by_structure_boundary
            and len(run) == _ALPHABETIC_LIST_CLOSED_PAIR_LENGTH
            and run[0][1].ordinal == 1
            and run[1][1].ordinal == 2
            and pair_interstitial_is_transparent
        )
        if long_run or closed_pair:
            resolved.update(
                (node.source_node_id, marker.ordinal)
                for node, marker, _position in run
            )
        run.clear()
        pair_interstitial_is_transparent = True
        interstitial_since_last_marker.clear()

    for node in source_nodes:
        source_id = node.source_node_id
        if (
            explicit_labels.get(source_id) is not None
            or source_id in boundary_source_ids
        ):
            flush(closed_by_structure_boundary=True)
            continue
        if (
            EXPLICIT_LIST_PREFIX.match(node.raw_text) is not None
            or IDEOGRAPHIC_NUMBERED_PREFIX.match(node.raw_text) is not None
            or node.kind is SourceNodeKind.HEADING
        ):
            flush(closed_by_structure_boundary=True)
            continue
        if source_id in excluded_source_ids:
            if run:
                interstitial_since_last_marker.append(node)
            continue
        if node.kind not in {
            SourceNodeKind.PARAGRAPH,
            SourceNodeKind.LIST,
            SourceNodeKind.LIST_ITEM,
        }:
            if run:
                interstitial_since_last_marker.append(node)
            continue
        marker = parse_alphabetic_parenthesized_marker(
            node.raw_text,
            require_payload=False,
        )
        if marker is None:
            if run:
                interstitial_since_last_marker.append(node)
            continue
        position = alphabetic_peer_position(
            node,
            marker_start_offset=marker.start_offset,
            include_text_indent=False,
        )
        if marker.ordinal == 1:
            flush()
            run.append((node, marker, position))
            continue
        if not run:
            continue
        previous_node, previous_marker, previous_position = run[-1]
        transition = evaluate_alphabetic_peer_transition(
            marker,
            previous_marker=previous_marker,
            current_position=position,
            previous_position=previous_position,
            anchor_position=previous_position,
            policy=RESOLVER_ALPHABETIC_PEER_POLICY,
        )
        if transition.accepted:
            if len(run) == 1:
                pair_interstitial_is_transparent = (
                    alphabetic_pair_interstitial_is_transparent(
                        tuple(interstitial_since_last_marker),
                        before=previous_node,
                        after=node,
                    )
                )
            run.append((node, marker, position))
            interstitial_since_last_marker.clear()
            continue
        flush()
    flush()
    return resolved


def filter_unconfirmed_implicit_cjk_root_labels(
    source_nodes: tuple[SourceNode, ...],
    *,
    explicit_labels: dict[str, str | None],
    excluded_source_ids: set[str],
    toc_title_keys: frozenset[str] = frozenset(),
) -> dict[str, str | None]:
    """Fail closed for ambiguous paragraph roots such as ``30节点...``.

    A compact ``N标题`` paragraph is promoted only when one independent
    structural signal corroborates it: exact TOC title, an owned dotted child,
    adjacency to a stronger consecutive root, or a coherent run of at least
    three compact peers.  Ordinal value, customer vocabulary, document name,
    page number, and fixed headings are intentionally absent.
    """

    resolved = dict(explicit_labels)
    indexed_nodes = tuple(
        (index, node)
        for index, node in enumerate(source_nodes)
        if node.source_node_id not in excluded_source_ids
    )
    implicit: dict[str, tuple[int, SourceNode, int, str]] = {}
    single_roots: list[tuple[int, SourceNode, int, bool]] = []
    for index, node in indexed_nodes:
        label = resolved.get(node.source_node_id)
        if label is None or "." in label:
            continue
        try:
            ordinal = int(label)
        except ValueError:
            continue
        evidence = implicit_cjk_root_evidence_from_text(node.raw_text)
        is_implicit = bool(
            node.kind is not SourceNodeKind.HEADING
            and evidence is not None
            and evidence.label == label
        )
        single_roots.append((index, node, ordinal, is_implicit))
        if is_implicit and evidence is not None:
            implicit[node.source_node_id] = (
                index,
                node,
                ordinal,
                evidence.payload,
            )

    if not implicit:
        return resolved

    normalized_toc_titles = frozenset(
        normalize_boundary_title(value)
        for value in toc_title_keys
        if normalize_boundary_title(value)
    )
    corroborated: set[str] = set()
    for source_id, (index, _node, ordinal, payload) in implicit.items():
        if normalize_boundary_title(payload) in normalized_toc_titles:
            corroborated.add(source_id)
            continue
        next_single_root_index = next(
            (
                candidate_index
                for candidate_index, _candidate, _ordinal, _is_implicit in single_roots
                if candidate_index > index
            ),
            len(source_nodes),
        )
        if any(
            (label := resolved.get(candidate.source_node_id)) is not None
            and label.startswith(f"{ordinal}.")
            for candidate in source_nodes[index + 1 : next_single_root_index]
            if candidate.source_node_id not in excluded_source_ids
        ):
            corroborated.add(source_id)

    for position, (index, node, ordinal, is_implicit) in enumerate(single_roots):
        if not is_implicit:
            continue
        for peer_position in (position - 1, position + 1):
            if not 0 <= peer_position < len(single_roots):
                continue
            peer_index, peer_node, peer_ordinal, peer_is_implicit = single_roots[
                peer_position
            ]
            if peer_is_implicit or abs(peer_ordinal - ordinal) != 1:
                continue
            if (peer_index < index) != (peer_ordinal < ordinal):
                continue
            if _implicit_root_peers_share_layout(node, peer_node):
                corroborated.add(node.source_node_id)
                break

    implicit_runs: list[list[tuple[int, SourceNode, int, bool]]] = []
    current_run: list[tuple[int, SourceNode, int, bool]] = []
    for item in single_roots:
        if not item[3]:
            if current_run:
                implicit_runs.append(current_run)
                current_run = []
            continue
        if current_run and (
            item[2] != current_run[-1][2] + 1
            or not _implicit_root_peers_share_layout(item[1], current_run[-1][1])
        ):
            implicit_runs.append(current_run)
            current_run = []
        current_run.append(item)
    if current_run:
        implicit_runs.append(current_run)
    for run in implicit_runs:
        if len(run) >= _IMPLICIT_ROOT_MIN_COHERENT_RUN_LENGTH:
            corroborated.update(item[1].source_node_id for item in run)

    for source_id in implicit:
        if source_id not in corroborated:
            resolved[source_id] = None
    return resolved


def _implicit_root_peers_share_layout(first: SourceNode, second: SourceNode) -> bool:
    if abs(first.source_order - second.source_order) == 1:
        return True
    first_bbox = first.native_locator.bbox
    second_bbox = second.native_locator.bbox
    if first_bbox is None or second_bbox is None:
        return False
    first_height = max(0.0, first_bbox[3] - first_bbox[1])
    second_height = max(0.0, second_bbox[3] - second_bbox[1])
    if first_height <= 0.0 or second_height <= 0.0:
        return False
    return bool(
        abs(first_bbox[0] - second_bbox[0])
        <= _IMPLICIT_ROOT_PDF_X_TOLERANCE_POINTS
        and abs(first_height - second_height)
        <= max(first_height, second_height)
        * _IMPLICIT_ROOT_PDF_HEIGHT_TOLERANCE_RATIO
    )


__all__ = [
    "credible_alphabetic_list_ordinals",
    "filter_unconfirmed_implicit_cjk_root_labels",
]
