"""Create the synthetic RX03 source and Map decisions outside the checkout data roots."""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / 'plugins/reqpilot/tests'))
    from portable_fixtures import make_fixture
    with contextlib.redirect_stdout(sys.stderr):
        source, _, mapping = make_fixture(args.output / 'fixture')
    mapping_path = args.output / 'mapping.json'
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'source': str(source), 'mapping': str(mapping_path),
        'source_sha256': json.loads((args.output / 'fixture/capture/source_ir.json').read_text())['source_sha256']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
