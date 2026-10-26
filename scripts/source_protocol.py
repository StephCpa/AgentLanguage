"""Prospective common output protocol; historical pilot parsing stays frozen."""
from al0.data import digest

VERSION = 'source-or-single-fence-v1'


def parse_source(raw):
    if not isinstance(raw, str):
        raise ValueError('provider output must be text')
    normalized = raw.replace('\r\n', '\n').replace('\r', '\n')
    lines = normalized.splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    fences = [i for i, line in enumerate(lines) if line.lstrip().startswith('```')]
    source, kind, error = normalized, 'raw', None
    if not normalized.strip():
        source, kind, error = None, None, 'empty_output'
    elif fences:
        if (len(lines) >= 3 and fences == [0, len(lines)-1]
                and lines[0].strip() in ('```', '```al0', '```python', '```py')
                and lines[-1].strip() == '```'):
            source, kind = ''.join(lines[1:-1]), 'single_fence'
            if not source.strip():
                source, error = None, 'empty_output'
        else:
            source, kind, error = None, None, 'mixed_multiple_or_invalid_fence'
    return {'parser_version': VERSION, 'raw_text': raw, 'raw_digest': digest(raw),
            'source': source, 'source_digest': digest(source) if source is not None else None,
            'format': kind, 'accepted': error is None, 'error': error,
            'normalization': 'universal-newlines-to-lf; outer fence removal only'}
