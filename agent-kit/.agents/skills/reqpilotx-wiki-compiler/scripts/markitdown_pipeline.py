#!/usr/bin/env python3
"""
MarkItDown Ingestion Pipeline
-----------------------------
Uses Microsoft MarkItDown to convert heterogeneous office formats
(PPTX, XLSX, DOCX, PDF) into standardized clean Markdown.
"""

import os
import sys
import argparse
from pathlib import Path
from markitdown import MarkItDown

def convert_single_file(input_file: str, output_file: str = None) -> str:
    md = MarkItDown()
    inp = Path(input_file)
    if not inp.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    print(f"Converting {inp.name} via MarkItDown...")
    result = md.convert(str(inp))
    markdown_content = result.text_content

    if output_file:
        out_p = Path(output_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        print(f"✓ Saved normalized markdown to: {out_p} ({len(markdown_content)} chars)")

    return markdown_content

def batch_convert_folder(source_dir: str, output_dir: str, exclude_patterns=None):
    base = Path(source_dir)
    out_base = Path(output_dir)
    exclude_patterns = exclude_patterns or ["customer SOR", ".DS_Store"]
    supported_extensions = {".pptx", ".docx", ".pdf", ".xlsx", ".xlsb"}

    md = MarkItDown()
    results = []

    for root, dirs, files in os.walk(base):
        if any(pat in root for pat in exclude_patterns):
            continue
        for f in sorted(files):
            if f.startswith("."):
                continue
            fp = Path(root) / f
            ext = fp.suffix.lower()
            if ext in supported_extensions:
                rel = fp.relative_to(base)
                out_file = out_base / rel.with_suffix(".md")
                out_file.parent.mkdir(parents=True, exist_ok=True)

                print(f"Processing: [{rel.parts[0]}] {f} ...")
                try:
                    res = md.convert(str(fp))
                    text = res.text_content
                    with open(out_file, "w", encoding="utf-8") as out_fp:
                        out_fp.write(text)
                    results.append({
                        "source": str(rel),
                        "output": str(out_file.relative_to(out_base)),
                        "length": len(text),
                        "status": "success"
                    })
                    print(f"  -> Successfully converted ({len(text)} chars)")
                except Exception as e:
                    print(f"  -> Error converting {f}: {e}")
                    results.append({
                        "source": str(rel),
                        "error": str(e),
                        "status": "failed"
                    })

    print(f"\nBatch conversion complete: {len([r for r in results if r['status'] == 'success'])} succeeded, {len([r for r in results if r['status'] == 'failed'])} failed.")
    return results

def main():
    parser = argparse.ArgumentParser(description="MarkItDown Pipeline CLI")
    parser.add_argument("--file", help="Single file to convert")
    parser.add_argument("--folder", help="Folder to batch convert")
    parser.add_argument("--output", default="prototype/runtime-data/normalized_markdown", help="Output directory or file")
    parser.add_argument("--exclude", nargs="*", default=["customer SOR"], help="Patterns to exclude")
    args = parser.parse_args()

    if args.file:
        convert_single_file(args.file, args.output)
    elif args.folder:
        batch_convert_folder(args.folder, args.output, args.exclude)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
