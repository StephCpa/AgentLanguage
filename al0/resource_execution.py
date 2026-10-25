"""Detect checked task boundaries requiring durable affine custody.

Existing plans with only local resource captures retain their execution format.
The new path is selected by the checked task interface, never by JSON values.
"""
import json

from .ir import MOVED_RESOURCES, check_plan
from .bundle_types import is_affine, restore


def task_resources(plan, catalog):
    document = json.loads(plan.encoded)

    def walk(doc):
        for node in doc['nodes']:
            if node['op'] in ('infer_progress','resume_infer','try_infer'):
                return True
            if node['op']=='repeat' and is_affine(check_plan(node['plan'], catalog).result_type):
                return True
            if (node['op']=='variant' or node['op']=='parameter' and node['kind']=='Variant') and is_affine(restore(node['type'])):
                return True
            if node['op'] in ('pack','unpack','take') or node['op']=='parameter' and node['kind']=='Bundle':
                return True
            if node['op'] in ('spawn', 'spawn_with', 'spawn_caps'):
                child = node['plan']
                if any(n['op'] == 'parameter' and (n['kind'] in MOVED_RESOURCES | {'Intent'} or n['kind']=='Variant' and is_affine(restore(n['type'])))
                       for n in child['nodes']):
                    return True
                if is_affine(check_plan(child, catalog).result_type):
                    return True
            if 'plan' in node and walk(node['plan']):
                return True
            if node['op'] in ('if', 'match_task', 'match_admission','match_variant') and any(walk(b) for b in node['cases'].values()):
                return True
        return False

    return walk(document)
