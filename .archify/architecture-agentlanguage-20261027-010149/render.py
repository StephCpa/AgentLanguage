"""Regenerate all five HTML diagrams with the pinned upstream Archify package."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archify', type=Path, required=True, help='Upstream bin/archify.mjs path')
args = parser.parse_args()
cli = args.archify.resolve(strict=True)
folder = Path(__file__).resolve().parent
root = folder.parents[1]
review = folder / ('rebuild-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
for kind in ('architecture', 'dataflow', 'sequence', 'tree', 'lifecycle'):
    command = ['node', str(cli), 'finalize', kind, str(folder/f'{kind}.json'),
               str(folder/f'{kind}.html'), '--repo-root', str(root), '--quality', 'showcase',
               '--out-dir', str(review/kind), '--json']
    result = subprocess.run(command, cwd=root, text=True, encoding='utf-8', capture_output=True)
    if result.returncode:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(result.returncode)
    receipt = json.loads(result.stdout)
    if receipt.get('status') != 'pass':
        raise SystemExit(f'{kind}: no passing finalize receipt')
    print(f'{kind}: {receipt["status"]}')
print(f'Evidence: {review}')
