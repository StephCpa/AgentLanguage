"""Shared evaluation validity gate and explicit checkout provenance."""
import json
import os
from pathlib import Path


def infrastructure_failure(failure):
    return str(failure).startswith('infrastructure:') or failure == 'static_tool_failure'


def require_valid_evaluations(reports):
    """Call after persisting reports. Invalid scoring is not a model outcome."""
    failures = [row['failure'] for report in reports for row in report['rows']
                if infrastructure_failure(row['failure'])]
    if failures:
        raise RuntimeError('evaluation infrastructure failed; reports preserved: '+', '.join(sorted(set(failures))))


def checkout_provenance(executed_commit):
    head = None
    event = os.environ.get('GITHUB_EVENT_PATH')
    if event:
        head = json.loads(Path(event).read_text(encoding='utf-8')).get('pull_request', {}).get('head', {}).get('sha')
    return {'executed_commit_sha': executed_commit, 'pr_head_sha': head}
