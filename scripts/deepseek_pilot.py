"""Explicit, bounded DeepSeek calls and export for separate Docker evaluation."""
import argparse
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from scripts.git_pair_evaluation import ROOT, dump
from scripts.generation_journal import (initialize, reserve, receive, refine_cost_bound, summary,
                                        database, configuration, rounds, encoded)

ENDPOINT = 'https://api.deepseek.com/chat/completions'
PROFILE = ROOT/'experiments/deepseek_pilot_v1/profile.json'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('provider redirect refused')


def credential():
    value = os.environ.get('DEEPSEEK_API_KEY')
    if not value and os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value = winreg.QueryValueEx(key, 'DEEPSEEK_API_KEY')[0]
    if not isinstance(value, str) or not value.strip():
        raise ValueError('DEEPSEEK_API_KEY is unavailable')
    return value.strip()


def token_cost_upper(prompt_tokens, output_tokens, profile):
    if any(type(v) is not int or v < 0 for v in (prompt_tokens, output_tokens)):
        raise ValueError('provider token counts must be nonnegative integers')
    if prompt_tokens > profile['context_tokens'] or output_tokens > profile['parameters']['max_tokens']:
        raise ValueError('provider usage exceeds reserved model bounds')
    rates = profile['peak_usd_per_million']
    amount = (Decimal(prompt_tokens)*Decimal(rates['input_cache_miss'])+
              Decimal(output_tokens)*Decimal(rates['output']))/Decimal(1000000)
    return str(amount.quantize(Decimal('0.000000001'), rounding=ROUND_CEILING))


def initialize_pilot(folder, package):
    folder = Path(folder)
    profile = json.loads(PROFILE.read_text(encoding='utf-8'))
    if Decimal(profile['budget_usd_per_arm'])*2 > Decimal(profile['total_budget_usd']):
        raise ValueError('arm budgets exceed total authorization')
    materials = {name: (PROFILE.parent/(name+'.txt')).read_text(encoding='utf-8') for name in ('shared', 'al0', 'sdk')}
    folder.mkdir(parents=True, exist_ok=False)
    dump(folder/'profile.json', profile)
    return initialize(folder/'journal', package, {'provider': 'deepseek', 'model': profile['model'],
        'parameters': profile['parameters'], 'max_attempts': profile['max_attempts_per_arm'],
        'budget_usd_per_arm': profile['budget_usd_per_arm'], 'evidence_mode': 'provider_import',
        'provider_profile': profile}, materials)


def ingest(folder, request_id):
    """Recover a stored response without resending a possibly accepted request."""
    folder = Path(folder)
    transport = folder/'transport'/request_id
    request = json.loads((transport/'request.json').read_text(encoding='utf-8'))
    profile = json.loads((folder/'profile.json').read_text(encoding='utf-8'))
    if not (transport/'response.json').is_file():
        raise ValueError('dispatch has no complete stored response; inspect the original process, never resend')
    raw = json.loads((transport/'response.json').read_text(encoding='utf-8'))
    if 'transport_error' in raw:
        receipt = {'request_id': request_id, 'model': request['model'], 'status': 'error',
                   'raw_response': raw, 'usage': None, 'cost_usd': None}
        return receive(folder/'journal', request_id, receipt)
    choices = raw.get('choices', [])
    content = choices[0].get('message', {}).get('content') if choices else None
    usage = raw.get('usage', {})
    bounded = None
    usage_error = None
    try:
        bounded = token_cost_upper(usage['prompt_tokens'], usage['completion_tokens'], profile)
        counts = {'input_tokens': usage['prompt_tokens'], 'output_tokens': usage['completion_tokens']}
    except (KeyError, ValueError, TypeError) as error:
        counts, usage_error = None, type(error).__name__
    receipt = {'request_id': request_id, 'model': request['model'], 'provider_model': raw.get('model'),
        'provider_request_id': raw.get('id'), 'status': 'output' if isinstance(content, str) else 'refused',
        'raw_response': raw, 'output_pointer': '/choices/0/message/content', 'usage': counts, 'cost_usd': None,
        'usage_validation_error': usage_error, 'cost_upper_usd': bounded,
        'cost_basis': 'observed provider token counts at frozen peak uncached rates; not a bill'}
    recorded = receive(folder/'journal', request_id, receipt)
    if bounded is not None:
        refine_cost_bound(folder/'journal', request_id, bounded, {'provider_request_id': raw.get('id'),
            'usage': usage, 'rates': profile['peak_usd_per_million'], 'pricing_source': profile['pricing_source'],
            'basis': receipt['cost_basis']})
    dump(transport/'receipt.json', receipt)
    return {**recorded, 'usage': counts, 'cost_upper_usd': bounded, 'provider_model': raw.get('model')}


def send(folder, arm):
    folder = Path(folder)
    profile = json.loads((folder/'profile.json').read_text(encoding='utf-8'))
    # Limit the public pilot to exactly the frozen protocol. No tools, redirects,
    # auto-retries, multimodal content, or caller-selected endpoint.
    if profile['model'] != 'deepseek-flash' or profile['parameters'] != {
            'thinking': {'type': 'disabled'}, 'temperature': 0, 'max_tokens': 4096, 'stream': False}:
        raise ValueError('unexpected provider profile')
    with database(folder/'journal') as db:
        config = configuration(db)['config']
        if config.get('provider_profile') != profile:
            raise ValueError('provider profile changed after initialization')
        if config.get('source_protocol'):
            if config.get('execution_gate') != 'frozen':
                raise ValueError('prospective task-set/order/budget/stop plan is not frozen')
            if any(r['phase'] != 'evaluated' or json.loads(r['evaluation'])['category'] == 'infrastructure_failure'
                   for r in rounds(db)):
                raise ValueError('resolve the current output/evaluation before another provider call')
            done = {r['arm'] for r in rounds(db) if r['evaluation'] and json.loads(r['evaluation'])['task_success']}
            counts = {a: len(rounds(db, a)) for a in ('sdk', 'al0')}
            remaining = []
            for assigned in config['call_order']:
                if counts[assigned]:
                    counts[assigned] -= 1
                elif assigned not in done:
                    remaining.append(assigned)
            if not remaining or remaining[0] != arm:
                raise ValueError('requested arm differs from frozen call order')
        for row in rounds(db):
            if row['receipt']:
                receipt = json.loads(row['receipt'])
                if receipt.get('usage_validation_error'):
                    raise ValueError('unverified prior usage; stop rather than spend more')
        # Both arms must have the same observed provider model identifier.
        observed = {json.loads(r['receipt']).get('provider_model') for r in rounds(db) if r['receipt']}
        if len(observed - {None}) > 1:
            raise ValueError('provider model identity changed during pilot')
    cap = token_cost_upper(profile['context_tokens'], profile['parameters']['max_tokens'], profile)
    request = reserve(folder/'journal', arm, cap)
    secret = credential()
    transport = folder/'transport'/request['id']
    transport.mkdir(parents=True, exist_ok=False)
    body = {'model': request['model'], 'messages': request['messages'], **request['parameters']}
    dump(transport/'request.json', request)
    dump(transport/'wire-request.json', body)
    # Durable pre-dispatch marker. A second CLI invocation cannot resend this
    # request; the journal still has an unresolved round.
    with (transport/'dispatch.json').open('x', encoding='utf-8') as stream:
        stream.write(encoded({'endpoint': ENDPOINT, 'request_id': request['id'], 'started_at': time.time()}))
        stream.flush()
        os.fsync(stream.fileno())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    call = urllib.request.Request(ENDPOINT, data=encoded(body).encode('utf-8'),
        headers={'Authorization': 'Bearer '+secret, 'Content-Type': 'application/json'}, method='POST')
    try:
        with opener.open(call, timeout=180) as response:
            raw = response.read(4*1024*1024+1)
        if len(raw) > 4*1024*1024:
            raise ValueError('provider response exceeded capture limit')
        json.loads(raw)
    except Exception as error:
        # Never print exceptions or request headers containing authentication.
        record = {'transport_error': type(error).__name__, 'http_status': getattr(error, 'code', None),
                  'request_id': request['id'], 'finished_at': time.time()}
        raw = encoded(record).encode('utf-8')
    temporary = transport/'response.pending'
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(transport/'response.json')
    dump(transport/'completed.json', {'finished_at': time.time()})
    return ingest(folder, request['id'])


def export(folder, output):
    folder, output = Path(folder), Path(output)
    with database(folder/'journal') as db:
        state = configuration(db)
        trials = [{'request': json.loads(r['request']), 'receipt': json.loads(r['receipt']),
                   'evaluation': json.loads(r['evaluation']) if r['evaluation'] else None}
                  for r in rounds(db) if r['receipt']]
    bundle = {'version': state['version'], 'kind': 'prospective_development_pilot' if state['version'] == 2 else 'deepseek_development_pilot', 'profile': json.loads((folder/'profile.json').read_text()),
        'participant': state['public'], 'materials': state['materials'], 'implementation': state['implementation'],
        'trials': trials, 'summary': summary(folder/'journal')}
    # Only the allowlisted public materials and receipts go into the publication
    # bundle. The database, host grants, task paths and runtime state stay local.
    output.parent.mkdir(parents=True, exist_ok=True)
    dump(output, bundle)
    return {'trials': len(trials), 'summary': bundle['summary']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', required=True, type=Path)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('init'); p.add_argument('--package', required=True, type=Path)
    p = sub.add_parser('send'); p.add_argument('--arm', choices=('al0', 'sdk'), required=True)
    p = sub.add_parser('ingest'); p.add_argument('--request-id', required=True)
    p = sub.add_parser('export'); p.add_argument('--output', type=Path, required=True)
    sub.add_parser('summary')
    args = vars(parser.parse_args())
    folder, mode = args.pop('pilot'), args.pop('mode')
    function = {'init': initialize_pilot, 'send': send, 'ingest': ingest, 'export': export,
                'summary': lambda path: summary(path/'journal')}[mode]
    print(json.dumps(function(folder, **args), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
