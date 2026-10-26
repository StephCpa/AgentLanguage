from al0.ir import Catalog, CheckedPlan
from al0.sdk import PlanBuilder, Ref
from bindings import Bindings, Schema_ExampleRecord


def build(catalog: Catalog, commit: str) -> CheckedPlan[str]:
    b = PlanBuilder('main', catalog)
    api = Bindings(b)
    value = api.parameter_Text('value')
    row: Ref[Schema_ExampleRecord] = b.record('row', 'ExampleRecord', {'value': value})
    answer = api.field_ExampleRecord_value('answer', row)
    return b.finish(answer)
