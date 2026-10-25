"""Compile AL-0 workflow source to the same checked IR used by the Python SDK."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from types import MappingProxyType

from .data import load_json
from .errors import AL0Error, InvalidData
from .flow import Label
from .ir import Catalog, CheckedPlan, Tool, Type, accepts, check_plan
from .source import Diagnostic, MAX_SOURCE_BYTES, Parser, Ref, SourceError
from .pure import OPERATORS


@dataclass(frozen=True)
class Compilation:
    plan: CheckedPlan
    entry: str
    workflows: object
    source_map: object


def fail(message,span,*,code='AL8002',hint='Use an earlier value binding or a declared workflow.',related=()):
    raise SourceError(Diagnostic(code,message,span,hint,related))


def bundle_syntax(typ):
    fields=dict(typ.members)
    members=[]
    for name,member in sorted(fields.items()):
        if member.kind in ('Bundle','Variant'):
            result=bundle_syntax(member)
        elif member.kind=='Permit':
            target=fields.get(member.name)
            result=Type('Permit',target.name if target is not None and target.kind=='Intent' else '',binding=member.name)
        else:
            result=Type(member.kind,member.name,frozenset(member.predicates),name if member.kind=='Intent' else '')
        members.append((name,result))
    return Type(typ.kind,members=tuple(members))


def refs_in(value):
    if isinstance(value,Ref):
        yield value
    elif isinstance(value,dict):
        for item in value.values():
            yield from refs_in(item)
    elif isinstance(value,(tuple,list)):
        for item in value:
            yield from refs_in(item)


def value_references(statement):
    if statement.op=='invoke_caps_with':
        args=statement.args[1:]
    elif statement.op=='invoke_caps':
        args=statement.args[1:]
    elif statement.op=='invoke_with':
        args=(statement.args[1],statement.args[2])
    elif statement.op in ('call','invoke'):
        args=(statement.args[1],)
    elif statement.op=='repeat':
        args=(statement.args[0],statement.args[2],statement.args[4])
    elif statement.op=='if':
        args=(statement.args[0],statement.args[3])
    elif statement.op in ('spawn','spawn_with','spawn_caps'):
        args=(statement.args[0],*statement.args[2:])
    elif statement.op in ('match_task','match_admission','match_variant'):
        args=(statement.args[0],statement.args[2])
    else:
        args=statement.args
    return refs_in(args)


def checked_error(error,workflow,definitions,statement=None):
    node=getattr(error,'node',None)
    statement=statement or next((s for s in workflow.statements if s.name==node),None)
    span=statement.span if statement else definitions.get(node,workflow.span)
    related=tuple(dict.fromkeys(definitions[ref.name] for ref in value_references(statement)
                              if ref.name in definitions)) if statement else ()
    message=str(error)
    code=error.code
    if len(message)>7 and message[:2]=='AL' and message[2:6].isdigit() and message[6:8]==': ':
        code,message=message[:6],message[8:]
    hint='Check schemas, evidence, declared effects, and affine handle ownership at the related definitions.'
    if 'effect' in message or 'writes' in message:
        hint='Declare the required effects in every parent boundary; move shared writes outside speculative tasks.'
    elif 'handled' in message or 'matched' in message or 'cover' in message:
        hint='Handle every outcome including Unknown/Waiting, and consume each task and scope handle.'
    fail(message,span,code=code,hint=hint,related=related)


class Compiler:
    def __init__(self,source,catalog,filename):
        self.ast=Parser(source,filename).parse()
        self.catalog=catalog
        self.workflows={}
        self.cache={}
        self.stack=[]
        self.maps={}
        for workflow in self.ast:
            if workflow.name in self.workflows:
                fail('workflow name is repeated',workflow.span,related=(self.workflows[workflow.name].span,))
            self.workflows[workflow.name]=workflow

    def compile(self,name,*,speculative=False,site=None):
        if name not in self.workflows:
            fail('workflow reference is not declared',site or self.ast[0].span)
        workflow=self.workflows[name]
        if name in self.stack:
            fail('recursive workflow expansion is not supported',site or workflow.span,
                 hint='Use an acyclic workflow graph or repeat for bounded iteration; recursive calls are unavailable.',
                 related=(workflow.span,))
        key=(name,speculative)
        if key in self.cache:
            return self.cache[key]
        if len(self.stack)>16:
            fail('workflow expansion exceeds 16 levels',site or workflow.span)
        self.stack.append(name)
        definitions={}
        nodes=[]
        child_count=0
        child_bytes=0
        depth=0
        scopes={}

        def define(binding,span):
            if binding in definitions:
                fail('value binding is repeated',span,related=(definitions[binding],))
            definitions[binding]=span

        def ref(reference):
            if reference.name not in definitions:
                fail('reference must name an earlier value binding',reference.span)
            return reference.name

        def child(reference,policy):
            nonlocal child_count,child_bytes,depth
            plan,count,child_depth=self.compile(reference.name,speculative=policy,site=reference.span)
            child_count+=count
            child_bytes+=len(plan.encoded.encode('utf-8'))
            depth=max(depth,child_depth+1)
            if child_count+len(workflow.parameters)+len(workflow.statements)>10000 or child_bytes>MAX_SOURCE_BYTES or depth>16:
                fail('expanded workflow exceeds the IR size or nesting limit',reference.span,
                     hint='Reduce repeated embedded workflows; declarations are expanded into bounded static IR.')
            return load_json(plan.encoded)

        for parameter,typ in workflow.parameters:
            define(parameter.name,parameter.span)
            from .bundle_types import describe
            nodes.append({'id':parameter.name,'op':'parameter','kind':typ.kind,
                          'type':describe(bundle_syntax(typ)) if typ.kind in ('Bundle','Variant') else typ.name,'predicates':list(typ.predicates)})
        for stmt in workflow.statements:
            op,args=stmt.op,stmt.args
            node={'id':stmt.name,'op':op}
            try:
                if op in OPERATORS:
                    node.update(op='compute',operator=op,inputs=[ref(value) for value in args])
                elif op in ('field','take'):
                    node.update(input=ref(args[0]),field=args[1])
                elif op=='unpack':
                    node.update(input=ref(args[0]))
                elif op=='variant':
                    if stmt.annotation is None or stmt.annotation.kind!='Variant':
                        fail('variant construction requires a complete Variant annotation',stmt.span)
                    node.update(type=describe(bundle_syntax(stmt.annotation)),tag=args[0],input=ref(args[1]))
                elif op=='pack':
                    node.update(fields={key:ref(value) for key,value in args[0].items()})
                elif op=='record':
                    node.update(type=args[0],fields={key:ref(value) for key,value in args[1].items()})
                elif op=='resources':
                    node.update(entries={key:ref(value) for key,value in args[0].items()})
                elif op=='if':
                    node.update(condition=ref(args[0]),cases={'Then':child(args[1],speculative),'Else':child(args[2],speculative)},
                                inputs={key:ref(value) for key,value in args[3].items()})
                elif op=='call':
                    node.update(plan=child(args[0],speculative),inputs={key:ref(value) for key,value in args[1].items()})
                elif op in ('invoke','invoke_with'):
                    node.update(op='invoke',plan=child(args[0],speculative),budget=ref(args[1]) if op=='invoke_with' else None,
                                inputs={key:ref(value) for key,value in args[2 if op=='invoke_with' else 1].items()})
                elif op in ('invoke_caps','invoke_caps_with'):
                    offset=2 if op=='invoke_caps_with' else 1
                    node.update(op='invoke_caps',plan=child(args[0],speculative),budget=ref(args[1]) if offset==2 else None,
                                caps={key:ref(value) for key,value in args[offset].items()},
                                inputs={key:ref(value) for key,value in args[offset+1].items()})
                elif op=='capability':
                    node.update(tool=args[0])
                elif op=='attenuate':
                    options=args[1]
                    if type(options) is not dict or not set(options)<={'expires','max_attempts','constraints'}:
                        raise InvalidData('invalid capability attenuation options')
                    node.update(cap=ref(args[0]),expires=options.get('expires'),max_attempts=options.get('max_attempts'),constraints=options.get('constraints',{}))
                elif op=='authorize_with':
                    node.update(intent=ref(args[0]),cap=ref(args[1]))
                elif op in ('split_budget','split_budget_from'):
                    node.update(op='split_budget',amount=ref(args[-1]) if isinstance(args[-1],Ref) else args[-1],parent=ref(args[0]) if op=='split_budget_from' else None)
                elif op=='join_budget':
                    node.update(budget=ref(args[0]))
                elif op=='repeat':
                    node.update(count=ref(args[0]),limit=args[1],initial=ref(args[2]),plan=child(args[3],speculative),
                                inputs={key:ref(value) for key,value in args[4].items()})
                elif op=='literal':
                    if stmt.annotation is None or stmt.annotation.kind!='Data':
                        fail('literal needs a data schema annotation',stmt.span,
                             hint='Write let name: Text = "..."; or use a registered record schema. Literals cannot manufacture evidence or capabilities.')
                    node.update(type=stmt.annotation.name,value=args[0])
                elif op in ('infer','infer_result','infer_progress','try_infer','observe','check','prepare','reconcile'):
                    node.update(tool=args[0],**{'operation' if op=='reconcile' else 'input':ref(args[1])})
                elif op=='resume_infer':
                    node.update(operation=ref(args[0]))
                elif op in ('restrict','match_business'):
                    node.update(input=ref(args[0]))
                    if op=='restrict':
                        node.update(label=Label.from_data(args[1]).to_data())
                    else:
                        if stmt.annotation is None or stmt.annotation.kind!='Data':
                            fail('business match needs a data schema annotation',stmt.span)
                        node.update(type=stmt.annotation.name,cases=args[1])
                elif op=='scope':
                    node.update(budget=ref(args[0]) if isinstance(args[0],Ref) else args[0],speculative=args[1])
                    scopes[stmt.name]=speculative or args[1] is True
                elif op in ('spawn','spawn_with','spawn_caps'):
                    scope=ref(args[0])
                    node.update(scope=scope,plan=child(args[1],scopes.get(scope,speculative)),budget=ref(args[2]) if isinstance(args[2],Ref) else args[2])
                    if op=='spawn_with':
                        node.update(inputs={key:ref(value) for key,value in args[3].items()})
                    elif op=='spawn_caps':
                        node.update(caps={key:ref(value) for key,value in args[3].items()},
                                    inputs={key:ref(value) for key,value in args[4].items()})
                elif op in ('match_task','match_admission','match_variant'):
                    node.update(input=ref(args[0]),cases={key:child(value,speculative) for key,value in args[1].items()},
                                inputs={key:ref(value) for key,value in args[2].items()})
                elif op=='race':
                    node.update(tasks=[ref(value) for value in args[0]])
                elif op in ('admit','try_admit'):
                    node.update(intent=ref(args[0]),permit=ref(args[1]))
                else:
                    field='intent' if op=='authorize' else 'operation' if op in ('inspect','await','borrow_operation') else 'scope' if op=='close_scope' else 'task'
                    node[field]=ref(args[0])
            except SourceError:
                raise
            except AL0Error as error:
                checked_error(error,workflow,definitions,stmt)
            define(stmt.name,stmt.span)
            nodes.append(node)
        result=ref(workflow.result)
        document={'ir_version':1,'name':name,'uses':list(workflow.uses),'nodes':nodes,'return':result}
        if workflow.kind=='fn':
            document['pure']=True
        try:
            checked=check_plan(document,self.catalog,speculative=speculative)
            types=dict(checked.node_types)
            for stmt in workflow.statements:
                if stmt.annotation is not None:
                    self.annotation(stmt.annotation,types[stmt.name],stmt.span)
            if workflow.annotation is not None:
                self.annotation(workflow.annotation,checked.result_type,workflow.result.span)
        except SourceError:
            raise
        except AL0Error as error:
            checked_error(error,workflow,definitions)
        self.stack.pop()
        self.cache[key]=(checked,len(nodes)+child_count,depth)
        self.maps[name]={'definition':asdict(workflow.span),'return':asdict(workflow.result.span),
                         'nodes':{key:asdict(span) for key,span in definitions.items()}}
        return self.cache[key]

    @staticmethod
    def annotation(typ,actual,span):
        expected=bundle_syntax(typ) if typ.kind in ('Bundle','Variant') else Type(typ.kind,typ.name,frozenset(typ.predicates))
        if len(typ.predicates)!=len(set(typ.predicates)) or not accepts(actual,expected):
            fail('type annotation does not match the checked value',span,code='AL6001',
                 hint='Annotations assert types; they cannot cast candidates, manufacture evidence, or change a tool result.')


def compile_source(source,catalog,*,filename='<source>',entry=None):
    """Check every declaration, then select one entry. Source metadata is not IR."""
    if type(source) is not str:
        raise InvalidData('source must be text')
    compiler=Compiler(source,catalog,str(filename))
    for workflow in compiler.ast:
        compiler.compile(workflow.name)
    if entry is None:
        if len(compiler.ast)==1:
            entry=compiler.ast[0].name
        elif 'main' in compiler.workflows:
            entry='main'
        else:
            fail('module has multiple workflows and no selected entry',compiler.ast[0].span,
                 hint='Select an entry workflow explicitly or name the intended entry main.')
    plan=compiler.compile(entry)[0]
    return Compilation(plan,entry,MappingProxyType({w.name:compiler.cache[(w.name,False)][0] for w in compiler.ast}),
                       {'source':filename,'workflows':compiler.maps})


def read_catalog(document: str) -> Catalog:
    """Load an explicit trusted catalog file, never Python code or plan declarations."""
    raw=load_json(document)
    if type(raw) is not dict or set(raw)!={'catalog_version','schemas','tools'} or type(raw['catalog_version']) is not int or raw['catalog_version']!=1:
        raise InvalidData('invalid catalog envelope')
    if type(raw['schemas']) is not dict or type(raw['tools']) is not list:
        raise InvalidData('catalog requires schema definitions and tool records')
    result=Catalog()
    for name,fields in raw['schemas'].items():
        result.schema(name,fields)
    allowed={'name','kind','input_type','output_type','scope','upper_bound','version','predicates','predicate',
             'retry_window','reconciles','shared_write','recipients','output_label','accepted_taints'}
    required={'name','kind','input_type','output_type','scope','upper_bound'}
    for entry in raw['tools']:
        if type(entry) is not dict or not required<=set(entry)<=allowed:
            raise InvalidData('invalid tool contract fields')
        entry=dict(entry)
        if 'output_label' in entry:
            entry['output_label']=Label.from_data(entry['output_label'])
        result.register(Tool.define(**entry))
    return result


def read_text(path: str | Path) -> str:
    with open(path,'rb') as stream:
        data=stream.read(MAX_SOURCE_BYTES+1)
    if len(data)>MAX_SOURCE_BYTES:
        raise InvalidData('input file exceeds 1 MiB')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise InvalidData('input must be UTF-8 text') from error


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='Check AL-0 source and optionally emit data-only IR. No tools execute.')
    parser.add_argument('source',type=Path)
    catalogs=parser.add_mutually_exclusive_group(required=True)
    catalogs.add_argument('--catalog',type=Path)
    catalogs.add_argument('--demo-catalog',action='store_true',help='use the local repair demo catalog')
    parser.add_argument('--entry')
    parser.add_argument('--emit-ir',type=Path)
    parser.add_argument('--source-map',type=Path)
    parser.add_argument('--json',action='store_true',dest='json_output')
    args=parser.parse_args(argv)
    try:
        paths=[p.resolve() for p in (args.source,args.catalog,args.emit_ir,args.source_map) if p is not None]
        if len(paths)!=len(set(paths)):
            raise InvalidData('input and output paths must be distinct')
        if args.demo_catalog:
            from .demo import catalog
            specs=catalog()
        else:
            specs=read_catalog(read_text(args.catalog))
        compiled=compile_source(read_text(args.source),specs,filename=str(args.source.resolve()),entry=args.entry)
        if args.emit_ir:
            args.emit_ir.write_text(compiled.plan.encoded+'\n',encoding='utf-8')
        if args.source_map:
            args.source_map.write_text(json.dumps(compiled.source_map,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except (AL0Error,OSError) as error:
        diagnostic=error.diagnostic.to_data() if isinstance(error,SourceError) else {'code':getattr(error,'code','AL8003'),'message':str(error)}
        print(json.dumps({'ok':False,'diagnostic':diagnostic},ensure_ascii=True) if args.json_output else
              error.diagnostic.render() if isinstance(error,SourceError) else diagnostic['code']+': '+diagnostic['message'])
        return 1
    message={'ok':True,'entry':compiled.entry,'fingerprint':compiled.plan.fingerprint}
    print(json.dumps(message) if args.json_output else f'Checked {compiled.entry}: {compiled.plan.fingerprint}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
