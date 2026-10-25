"""A closed business result preserving distinct resource alternatives."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from al0.bundle_types import variant_type
from al0.data import Principal
from al0.execute import Broker, Interpreter, Response
from al0.frontend import compile_source
from al0.ir import Type
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.resource_bundles import catalog, handled
from examples.typed_task_resources import submit


def interface():
    def packet(**fields):return Type('Bundle',members=tuple(sorted(fields.items())))
    funds=Type('Budget','Budget')
    return variant_type({'Accepted':packet(operation=Type('Operation','send'),funds=funds),
        'Deferred':packet(request=Type('Intent','send',binding='request'),
            permission=Type('Permit','send',binding='request'),funds=funds),
        'Denied':packet(reason=Type('Data','Text'),funds=funds)})


def alternative(specs,tag):
    b=PlanBuilder(tag.lower(),specs)
    fields={}
    if tag=='Accepted':fields['operation']=b.operation_parameter('result',b.tool('send'))
    elif tag=='Deferred':
        fields['request']=b.intent_parameter('request',b.tool('send'))
        fields['permission']=b.permit_parameter('result',fields['request'])
    fields['funds']=b.budget_parameter('funds')
    if tag=='Denied':fields['reason']=b.literal('reason','Text','Denied')
    return b.finish(b.variant('value',interface(),tag,b.pack('packet',fields)))


def producer(specs):
    b=PlanBuilder('producer',specs)
    request=b.intent_parameter('request',b.tool('send'))
    permission=b.permit_parameter('permission',request)
    funds=b.budget_parameter('funds')
    decision=b.try_admit('decision',request,permission)
    return b.finish(b.match_admission('result',decision,admitted=alternative(specs,'Accepted'),
        returned=alternative(specs,'Deferred'),no_permit=alternative(specs,'Denied'),inputs={'request':request,'funds':funds}))


def consumer(specs,tag):
    b=PlanBuilder('handle-'+tag.lower(),specs)
    result=b.bundle_parameter('result',dict(interface().members)[tag])
    opened=b.unpack('opened',result)
    funds=b.take('funds',opened,'funds')
    if tag=='Accepted':
        op=b.take('operation',opened,'operation')
        b.join_budget('closed',funds)
        result=handled(b,'handled',op)
    elif tag=='Deferred':
        request=b.take('request',opened,'request')
        permission=b.take('permission',opened,'permission')
        op=b.invoke('operation',submit(specs),inputs={'request':request,'permission':permission},budget=funds)
        b.join_budget('closed',funds)
        result=handled(b,'handled',op)
    else:
        result=b.take('reason',opened,'reason')
        b.join_budget('closed',funds)
    return b.finish(result)


def completed(specs):
    b=PlanBuilder('completed',specs)
    value=b.variant_parameter('result',interface())
    return b.finish(b.match_variant('handled',value,{tag:consumer(specs,tag) for tag in ('Accepted','Deferred','Denied')}))


def build(specs,*,funded=True):
    other=PlanBuilder('failed',specs)
    failed=other.finish(other.literal('result','Text','Unavailable'))
    b=PlanBuilder('variant-funded' if funded else 'variant-deferred',specs)
    request=b.prepare('request',b.tool('send'),b.literal('message','Text','hello'))
    permit=b.authorize('permit',request)
    funds=b.split_budget('funds',{'u':2})
    amount={'u':1} if funded else {}
    scope=b.scope('scope',amount)
    task=b.spawn('task',scope,producer(specs),amount,inputs={'request':request,'permission':permit,'funds':funds})
    result=b.join_outcome('outcome',task)
    b.close_scope('closed',scope)
    return b.finish(b.match_task('result',result,completed=completed(specs),failed=failed,waiting=failed,cancelled=failed))


def demo(directory):
    specs=catalog()
    endpoint=Path(directory)/'endpoint.sqlite'
    with closing(sqlite3.connect(endpoint)) as db,db:
        db.execute('CREATE TABLE calls(attempt TEXT PRIMARY KEY, mode TEXT, fee INTEGER)')
    reports={}
    for mode in ('Accepted','Deferred','Denied'):
        plan=build(specs,funded=mode!='Deferred')
        source=compile_source(Path(__file__).with_suffix('.al0').read_text(encoding='utf-8'),specs,
            entry='variant-deferred' if mode=='Deferred' else 'variant-funded').plan
        assert source.encoded==plan.encoded
        store=Store(Path(directory)/(mode+'.sqlite'))
        who=Principal('example','developer')
        run=store.create_run(who,plan.fingerprint,{'u':10})
        grants={'send':store.grant(run,'send','output',expires=store.clock()+1000)}
        revoked=False
        def fault(point):
            nonlocal revoked
            if mode=='Denied' and point=='after_spawn_commit' and not revoked:
                revoked=True
                store.revoke(grants['send'])
        store.fault=fault
        def send(ticket):
            with closing(sqlite3.connect(endpoint)) as db,db:
                db.execute('INSERT INTO calls VALUES(?,?,1)',(ticket.attempt,mode))
            return Response('sent',{'u':1})
        broker=Broker(store,specs,{'send':send})
        try:
            result=Interpreter(broker).run(source,run,who,grants)
            broker.wait_for_workers(timeout=10)
            count=len(store.events(run,who))
            replay=Interpreter(broker).run(plan,run,who,grants)
            broker.wait_for_workers(timeout=10)
        finally:broker.wait_for_workers(timeout=10)
        reports[mode]={'result':result.data,'replay':replay.data,'identical_ir':True,'budget':store.budget(run,who),
            'replay_added_events':len(store.events(run,who))-count}
    with closing(sqlite3.connect(endpoint)) as db:
        reports['provider_calls']=db.execute('SELECT mode,fee FROM calls ORDER BY rowid').fetchall()
    return reports


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:print(json.dumps(demo(directory),indent=2))
