from __future__ import annotations

import argparse
from pathlib import Path

from app.services.extractor import extract_atomic_requirements
from app.services.parser import parse_document


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate extraction quality on local PDF samples.")
    parser.add_argument("sample_dir", type=Path, help="Directory that contains sample PDF files.")
    parser.add_argument("--top", type=int, default=8, help="How many requirement examples to print per file.")
    args = parser.parse_args()

    files = sorted(args.sample_dir.glob("*.pdf"))
    if not files:
        raise SystemExit(f"No PDF files found in: {args.sample_dir}")

    for pdf_file in files:
        _, segments, _ = parse_document(str(pdf_file))
        requirements = extract_atomic_requirements(segments)
        print(f"\n{'=' * 80}")
        print(f"{pdf_file.name}")
        print(f"segments={len(segments)} requirements={len(requirements)}")
        for req in requirements[: args.top]:
            print(
                f"- {req['req_code']} [{req['requirement_type']}/{req['priority']}] "
                f"{req['requirement_text'][:160]}"
            )


if __name__ == "__main__":
    main()

