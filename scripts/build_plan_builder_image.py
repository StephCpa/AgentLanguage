"""Build from an explicit source allowlist; never send the checkout to Docker."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from scripts.isolated_plan_build import ROOT, SOURCE_LABEL, POLICY_LABEL, POLICY_VERSION, implementation_digest


def context(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    for source in (ROOT/'al0').rglob('*'):
        if source.is_file() and (source.suffix == '.py' or source.name == 'py.typed'):
            target = folder/source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    shutil.copyfile(ROOT/'scripts/isolated_plan_worker.py', folder/'worker.py')
    (folder/'mypy.ini').write_text('[mypy]\npython_version = 3.10\nstrict = True\n'
        'follow_imports = silent\nshow_error_codes = True\npretty = False\n'
        'mypy_path = /opt/al0\n', encoding='utf-8')
    (folder/'Dockerfile').write_text('FROM python:3.12-slim-bookworm\n'
        'RUN python -m pip install --no-cache-dir mypy==1.18.2\n'
        'COPY al0 /opt/al0/al0\nCOPY worker.py /opt/al0/isolated_plan_worker.py\n'
        'COPY mypy.ini /opt/al0/mypy.ini\n'
        f'LABEL {SOURCE_LABEL}="{implementation_digest()}" {POLICY_LABEL}="{POLICY_VERSION}"\n'
        'ENV PYTHONPATH=/opt/al0 PYTHONDONTWRITEBYTECODE=1\n'
        'USER 65534:65534\nWORKDIR /tmp\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='al0-image-') as temporary:
        folder = Path(temporary)/'context'
        context(folder)
        subprocess.run(['docker', 'build', '--tag', args.tag, str(folder)], check=True)
    from scripts.isolated_plan_build import DockerBuilder
    builder = DockerBuilder(args.tag)
    report = {'image': builder.image, 'source_digest': implementation_digest(),
              'policy_version': POLICY_VERSION, 'tag': args.tag}
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
