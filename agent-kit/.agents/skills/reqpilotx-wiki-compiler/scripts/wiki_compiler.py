#!/usr/bin/env python3
"""
ReqPilotX Wiki Compiler CLI Tool
--------------------------------
Provides deterministic utilities for ingesting single files or multi-file directories,
linting wikilink topologies, rebuilding Map-of-Content (MOC), and exporting
graph visualization data for the Obsidian-like frontend.
"""

import os
import sys
import re
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime

# Safe imports for document parsing
try:
    import docx
except ImportError:
    docx = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import pptx
except ImportError:
    pptx = None


def calculate_sha256(filepath, max_bytes=1024 * 1024):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        h.update(f.read(max_bytes))
    return h.hexdigest()[:16]


def parse_doc_outline(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    outline = []
    text_sample = ""
    tables_count = 0

    if ext == ".docx" and docx:
        try:
            d = docx.Document(filepath)
            tables_count = len(d.tables)
            for p in d.paragraphs:
                txt = p.text.strip()
                if p.style.name.startswith("Heading") or (
                    p.style.name == "Normal" and p.runs and p.runs[0].bold and 2 < len(txt) < 80
                ):
                    if txt and txt not in outline:
                        outline.append(txt)
            text_sample = "\n".join([p.text for p in d.paragraphs[:20]])
        except Exception as e:
            outline.append(f"Error parsing docx: {e}")

    elif ext == ".pdf" and pypdf:
        try:
            reader = pypdf.PdfReader(filepath)
            total_pages = len(reader.pages)
            if reader.outline:
                def walk_outline(nodes):
                    res = []
                    for n in nodes:
                        if isinstance(n, list):
                            res.extend(walk_outline(n))
                        elif hasattr(n, "title"):
                            res.append(n.title)
                    return res
                outline = walk_outline(reader.outline)
            if not outline and total_pages > 0:
                for p_num in range(min(5, total_pages)):
                    ptxt = reader.pages[p_num].extract_text()
                    for line in ptxt.split("\n"):
                        s = line.strip()
                        if any(s.startswith(str(i) + ".") for i in range(1, 10)) and len(s) < 80:
                            outline.append(s)
            text_sample = reader.pages[0].extract_text()[:1000] if total_pages > 0 else ""
        except Exception as e:
            outline.append(f"Error parsing pdf: {e}")

    elif ext == ".pptx" and pptx:
        try:
            prs = pptx.Presentation(filepath)
            for idx, slide in enumerate(prs.slides):
                for shape in slide.shapes:
                    if shape.has_text_frame and shape.text_frame.text.strip():
                        first_line = shape.text_frame.text.strip().split("\n")[0]
                        if 2 < len(first_line) < 60:
                            outline.append(f"Slide {idx+1}: {first_line}")
                            break
            text_sample = f"Total slides: {len(prs.slides)}"
        except Exception as e:
            outline.append(f"Error parsing pptx: {e}")

    return outline[:40], text_sample, tables_count


def scan_source_folder(source_dir, exclude_patterns=None):
    base = Path(source_dir)
    manifest = []
    exclude_patterns = exclude_patterns or []

    for root, dirs, files in os.walk(base):
        # check exclude
        if any(pat in root for pat in exclude_patterns):
            continue
        for f in sorted(files):
            if f.startswith(".") or f.endswith(".DS_Store"):
                continue
            fp = Path(root) / f
            rel = fp.relative_to(base)
            category = rel.parts[0] if len(rel.parts) > 1 else "root"
            stat = fp.stat()
            digest = calculate_sha256(fp)
            outline, _, tables_cnt = parse_doc_outline(str(fp))

            manifest.append({
                "name": f,
                "relative_path": str(rel),
                "category": category,
                "extension": fp.suffix.lower(),
                "size_bytes": stat.st_size,
                "partial_sha256": digest,
                "tables_count": tables_cnt,
                "sections_outline": outline
            })
    return manifest


def lint_wiki(wiki_dir):
    wiki_path = Path(wiki_dir)
    all_files = {}
    for root, dirs, files in os.walk(wiki_path):
        for f in files:
            if f.endswith(".md"):
                full_p = Path(root) / f
                rel_p = full_p.relative_to(wiki_path)
                with open(full_p, "r", encoding="utf-8") as fp:
                    all_files[str(rel_p)] = fp.read()

    link_pattern = re.compile(r'\[\[(.*?)\]\]')
    key_map = {}
    for f in all_files:
        base = f[:-3] if f.endswith(".md") else f
        key_map[base] = f
        key_map[os.path.basename(base)] = f

    broken_links = []
    inbound_links = {f: [] for f in all_files}

    for source_file, content in all_files.items():
        if source_file in ["WIKI_RULES.md", "index.md", "log.md"]:
            continue
        links = link_pattern.findall(content)
        for target in links:
            # handle alias like [[specs/electrical_spec|电气特性]]
            target_clean = target.split("|")[0].split("#")[0].strip()
            resolved = key_map.get(target_clean)
            if not resolved:
                broken_links.append((source_file, target))
            else:
                inbound_links[resolved].append(source_file)

    orphan_pages = []
    for f, inbounds in inbound_links.items():
        if f not in ["WIKI_RULES.md", "index.md", "log.md"] and len(inbounds) == 0:
            orphan_pages.append(f)

    return {
        "total_markdown_files": len(all_files),
        "broken_links": broken_links,
        "orphan_pages": orphan_pages,
        "is_healthy": len(broken_links) == 0 and len(orphan_pages) == 0
    }


def export_graph_data(wiki_dir):
    """
    Exports nodes and links formatted specifically for Obsidian-like
    Force Graph visualizations (e.g. react-force-graph / D3-force).
    """
    wiki_path = Path(wiki_dir)
    all_files = {}
    frontmatters = {}

    for root, dirs, files in os.walk(wiki_path):
        for f in files:
            if f.endswith(".md") and f not in ["WIKI_RULES.md", "log.md"]:
                full_p = Path(root) / f
                rel_p = str(full_p.relative_to(wiki_path))
                with open(full_p, "r", encoding="utf-8") as fp:
                    text = fp.read()
                    all_files[rel_p] = text

                    # parse frontmatter title & category
                    fm_match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
                    title = f[:-3]
                    category = rel_p.split("/")[0] if "/" in rel_p else "root"
                    tags = []
                    if fm_match:
                        fm_text = fm_match.group(1)
                        t_m = re.search(r"^title:\s*(.*)$", fm_text, re.MULTILINE)
                        if t_m:
                            title = t_m.group(1).strip()
                        c_m = re.search(r"^category:\s*(.*)$", fm_text, re.MULTILINE)
                        if c_m:
                            category = c_m.group(1).strip()
                        tags_m = re.search(r"^tags:\s*\[(.*?)\]", fm_text, re.MULTILINE)
                        if tags_m:
                            tags = [t.strip() for t in tags_m.group(1).split(",") if t.strip()]

                    frontmatters[rel_p] = {
                        "title": title,
                        "category": category,
                        "tags": tags
                    }

    # Category color mapping (matching modern Obsidian graph themes)
    category_colors = {
        "root": "#888888",
        "specs": "#3b82f6",                 # Blue
        "ee_hardware": "#8b5cf6",           # Electric Violet
        "software_diagnostics": "#06b6d4",  # Cyan
        "dvp_tests": "#10b981",             # Emerald Green
        "interfaces": "#f59e0b",            # Amber
        "integration_rules": "#ec4899",     # Pink
        "functional_safety": "#f43f5e"      # Rose Red
    }

    nodes = []
    key_map = {}
    for rel_p, fm in frontmatters.items():
        node_id = rel_p[:-3] if rel_p.endswith(".md") else rel_p
        key_map[node_id] = node_id
        key_map[os.path.basename(node_id)] = node_id

        cat = fm["category"]
        color = category_colors.get(cat, "#6366f1")
        val = 15 if rel_p == "index.md" else 8

        nodes.append({
            "id": node_id,
            "name": fm["title"],
            "category": cat,
            "tags": fm["tags"],
            "path": rel_p,
            "val": val,
            "color": color
        })

    link_pattern = re.compile(r'\[\[(.*?)\]\]')
    links = []
    seen_links = set()

    for rel_p, content in all_files.items():
        source_id = rel_p[:-3] if rel_p.endswith(".md") else rel_p
        found_links = link_pattern.findall(content)
        for target in found_links:
            target_clean = target.split("|")[0].split("#")[0].strip()
            target_id = key_map.get(target_clean)
            if target_id and target_id != source_id:
                link_key = tuple(sorted([source_id, target_id]))
                if link_key not in seen_links:
                    seen_links.add(link_key)
                    links.append({
                        "source": source_id,
                        "target": target_id,
                        "value": 1
                    })

    return {
        "nodes": nodes,
        "links": links,
        "categories": list(set(n["category"] for n in nodes)),
        "stats": {
            "node_count": len(nodes),
            "link_count": len(links),
            "generated_at": datetime.now().isoformat()
        }
    }


def main():
    parser = argparse.ArgumentParser(description="ReqPilotX Wiki Compiler CLI")
    subparsers = parser.add_subparsers(dest="command")

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan source documents")
    p_scan.add_argument("--source", required=True, help="Source directory")
    p_scan.add_argument("--exclude", nargs="*", default=["customer SOR"], help="Substrings to exclude")
    p_scan.add_argument("--output", default="sources_manifest.json", help="Output manifest file")

    # lint
    p_lint = subparsers.add_parser("lint", help="Lint wiki markdown files")
    p_lint.add_argument("--wiki", required=True, help="Wiki directory")

    # export-graph
    p_graph = subparsers.add_parser("export-graph", help="Export graph data for Obsidian-like UI")
    p_graph.add_argument("--wiki", required=True, help="Wiki directory")
    p_graph.add_argument("--output", required=True, help="Output JSON file for graph")

    args = parser.parse_args()

    if args.command == "scan":
        manifest = scan_source_folder(args.source, args.exclude)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"total_sources": len(manifest), "sources": manifest}, f, indent=2, ensure_ascii=False)
        print(f"Scanned {len(manifest)} sources -> {args.output}")

    elif args.command == "lint":
        res = lint_wiki(args.wiki)
        print(f"Total markdown files: {res['total_markdown_files']}")
        if res["is_healthy"]:
            print("✓ Wiki is healthy! 0 broken links, 0 orphan pages.")
        else:
            print("❌ Broken links:", len(res["broken_links"]))
            for b in res["broken_links"]:
                print("  -", b)
            print("❌ Orphan pages:", len(res["orphan_pages"]))
            for o in res["orphan_pages"]:
                print("  -", o)
            sys.exit(1)

    elif args.command == "export-graph":
        graph_data = export_graph_data(args.wiki)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, indent=2, ensure_ascii=False)
        print(f"Graph exported: {graph_data['stats']['node_count']} nodes, {graph_data['stats']['link_count']} links -> {args.output}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
