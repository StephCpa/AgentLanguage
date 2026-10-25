"""Data-only AL-0 source lexer and AST. No Python evaluation or imports."""
from bisect import bisect_right
from dataclasses import asdict, dataclass
import re

from .data import load_json
from .errors import AL0Error, InvalidData
from .pure import OPERATORS


MAX_SOURCE_BYTES=1024*1024
MAX_TOKENS=100000


@dataclass(frozen=True)
class Span:
    source: str
    start: int
    end: int
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    span: Span
    hint: str
    related: tuple = ()

    def to_data(self):
        return asdict(self)

    def render(self):
        p=self.span
        result=f'{p.source}:{p.line}:{p.column}: {self.code}: {self.message}\n  hint: {self.hint}'
        for span in self.related:
            result+=f'\n  related definition: {span.source}:{span.line}:{span.column}'
        return result


class SourceError(AL0Error):
    code='AL8001'

    def __init__(self, diagnostic):
        self.diagnostic=diagnostic
        self.code=diagnostic.code
        super().__init__(diagnostic.message)


class Source:
    def __init__(self, text, filename):
        self.text,self.filename=text,filename
        self.lines=[0]+[m.end() for m in re.finditer('\n',text)]

    def span(self,start,end):
        row=bisect_right(self.lines,start)-1
        last=bisect_right(self.lines,end)-1
        return Span(self.filename,start,end,row+1,start-self.lines[row]+1,last+1,end-self.lines[last]+1)

    def fail(self,message,start,end,*,code='AL8001',hint='Use the documented AL-0 syntax.',related=()):
        raise SourceError(Diagnostic(code,message,self.span(start,end),hint,related))


@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    start: int
    end: int


def lex(source):
    text=source.text
    try:
        size=len(text.encode('utf-8'))
    except UnicodeEncodeError:
        source.fail('source contains an invalid Unicode scalar',0,0)
    if size>MAX_SOURCE_BYTES:
        source.fail('source exceeds the 1 MiB limit',0,0,hint='Split authoring modules before compilation; keep each entry within the IR limits.')
    tokens=[]
    i=0
    while i<len(text):
        if text[i] in ' \t\r\n':
            i+=1
            continue
        if text.startswith('//',i):
            end=text.find('\n',i)
            i=len(text) if end<0 else end
            continue
        start=i
        if text[i]=='"':
            i+=1
            while i<len(text):
                if text[i]=='\\':
                    i+=2
                elif text[i]=='"':
                    i+=1
                    break
                else:
                    i+=1
            try:
                value=load_json(text[start:i])
            except InvalidData:
                source.fail('invalid or unterminated JSON string',start,min(i,len(text)))
            tokens.append(Token('STRING',value,start,i))
        elif text[i].isascii() and (text[i].isalpha() or text[i]=='_'):
            i+=1
            while i<len(text) and text[i].isascii() and (text[i].isalnum() or text[i] in '_-'):
                i+=1
            if i-start>512:
                source.fail('identifier exceeds 512 characters',start,i)
            tokens.append(Token('NAME',text[start:i],start,i))
        elif text[i].isascii() and text[i].isdigit() or text[i]=='-' and i+1<len(text) and text[i+1].isascii() and text[i+1].isdigit():
            i+=1
            while i<len(text) and text[i].isascii() and text[i].isdigit():
                i+=1
            try:
                value=load_json(text[start:i])
            except InvalidData:
                source.fail('integer must be canonical JSON within the signed 64-bit domain',start,i)
            tokens.append(Token('NUMBER',value,start,i))
        elif text.startswith('->',i):
            i+=2
            tokens.append(Token('->','->',start,i))
        elif text[i] in '{}[]():;,=<>':
            i+=1
            tokens.append(Token(text[start],text[start],start,i))
        else:
            source.fail('unsupported source character',i,i+1,hint='Use JSON strings for data and quoted tool/effect names; expressions do not execute host code.')
        if len(tokens)>MAX_TOKENS:
            source.fail('source token limit exceeded',start,i)
    tokens.append(Token('EOF',None,len(text),len(text)))
    return tokens


@dataclass(frozen=True)
class Ref:
    name: str
    span: Span


@dataclass(frozen=True)
class TypeSyntax:
    kind: str
    name: str
    predicates: tuple = ()
    members: tuple = ()


@dataclass(frozen=True)
class Statement:
    name: str
    annotation: TypeSyntax | None
    op: str
    args: tuple
    span: Span


@dataclass(frozen=True)
class Workflow:
    name: str
    parameters: tuple
    uses: tuple
    statements: tuple
    result: Ref
    annotation: TypeSyntax | None
    span: Span
    kind: str = 'workflow'


class Parser:
    def __init__(self,text,filename='<source>'):
        self.source=Source(text,filename)
        self.tokens=lex(self.source)
        self.index=0
        self.count=0

    @property
    def token(self):
        return self.tokens[self.index]

    def take(self,kind,value=None):
        token=self.token
        if token.kind!=kind or value is not None and token.value!=value:
            self.source.fail('expected '+(value if value is not None else kind),token.start,token.end)
        self.index+=1
        return token

    def optional(self,kind):
        if self.token.kind==kind:
            return self.take(kind)
        return None

    def ref(self):
        token=self.take('NAME')
        return Ref(token.value,self.source.span(token.start,token.end))

    def typename(self,depth=0):
        if depth>16:
            self.source.fail('bundle type nesting exceeds 16 levels',self.token.start,self.token.end)
        name=self.take('NAME').value
        if not self.optional('<'):
            if name=='Budget':
                return TypeSyntax('Budget','Budget')
            return TypeSyntax('Data',name)
        if name in ('Bundle','Variant'):
            self.take('{')
            members=self.mapping(lambda:self.typename(depth+1))
            self.take('>')
            return TypeSyntax(name,'',members=tuple(members.items()))
        argument=self.take('STRING' if self.token.kind=='STRING' else 'NAME').value
        predicates=[]
        while self.optional(','):
            predicates.append(self.take('NAME').value)
        self.take('>')
        if name not in ('Data','Candidate','Observation','Checked','Operation','Intent','Permit','OpRef','Admission','Cap') or name!='Checked' and predicates:
            self.source.fail('unsupported type constructor',self.tokens[self.index-1].start,self.tokens[self.index-1].end)
        return TypeSyntax(name,argument,tuple(predicates))

    def items(self,close,reader):
        result=[]
        if self.token.kind!=close:
            while True:
                result.append(reader())
                if not self.optional(','):
                    break
                if self.token.kind==close:
                    break
        self.take(close)
        return tuple(result)

    def data(self,depth=0):
        token=self.token
        if depth>64:
            self.source.fail('data nesting exceeds 64 levels',token.start,token.end)
        if token.kind in ('STRING','NUMBER'):
            self.index+=1
            return token.value
        if token.kind=='NAME' and token.value in ('true','false','null'):
            self.index+=1
            return {'true':True,'false':False,'null':None}[token.value]
        if self.optional('['):
            return list(self.items(']',lambda:self.data(depth+1)))
        if self.optional('{'):
            return self.mapping(lambda:self.data(depth+1))
        self.source.fail('expected a JSON data literal',token.start,token.end,hint='Use an explicitly typed literal; references are only allowed in operation arguments.')

    def mapping(self,reader):
        result={}
        if self.token.kind!='}':
            while True:
                token=self.take('STRING' if self.token.kind=='STRING' else 'NAME')
                if token.value in result:
                    self.source.fail('duplicate mapping key',token.start,token.end)
                self.take(':')
                result[token.value]=reader()
                if not self.optional(',') or self.token.kind=='}':
                    break
        self.take('}')
        return result

    def refs(self):
        self.take('{')
        return self.mapping(self.ref)

    def expression(self):
        if self.token.kind!='NAME' or self.token.value in ('true','false','null'):
            return 'literal',(self.data(),)
        name=self.take('NAME')
        op=name.value
        self.take('(')
        # The surface is intentionally closed: no generic function dispatch.
        signatures={
            'variant':('tool','ref'),'match_variant':('ref','refs','refs'),
            'pack':('refs',),'unpack':('ref',),'take':('ref','tool'),
            'infer':('tool','ref'),'infer_result':('tool','ref'),'infer_progress':('tool','ref'),'try_infer':('tool','ref'),'resume_infer':('ref',),'observe':('tool','ref'),'check':('tool','ref'),
            'prepare':('tool','ref'),'reconcile':('tool','ref'),
            'authorize':('ref',),'admit':('ref','ref'),'try_admit':('ref','ref'),'inspect':('ref',),'await':('ref',),
            'match_admission':('ref','refs','refs'),
            'borrow_operation':('ref',),
            'join':('ref',),'handoff':('ref',),'join_outcome':('ref',),'cancel_outcome':('ref',),'close_scope':('ref',),
            'restrict':('ref','data'),'scope':('vector','data'),
            'spawn':('ref','ref','vector'),'spawn_with':('ref','ref','vector','refs'),
            'spawn_caps':('ref','ref','vector','refs','refs'),
            'race':('list',),'match_business':('ref','data'),'match_task':('ref','refs','refs'),
            'if':('ref','ref','ref','refs'),'field':('ref','tool'),'record':('tool','refs'),
            'call':('ref','refs'),'repeat':('ref','data','ref','ref','refs'),
            'invoke':('ref','refs'),'invoke_with':('ref','ref','refs'),
            'invoke_caps':('ref','refs','refs'),'invoke_caps_with':('ref','ref','refs','refs'),
            'capability':('tool',),'attenuate':('ref','data'),'authorize_with':('ref','ref'),
            'resources':('refs',),
            'split_budget':('vector',),'split_budget_from':('ref','vector'),'join_budget':('ref',),
            **{operator:('ref',)*arity for operator,arity in OPERATORS.items()}}
        if op not in signatures:
            self.source.fail('unknown operation',name.start,name.end,hint='Use call for pure functions and explicit workflow constructs for effects; arbitrary host calls are unavailable.')
        args=[]
        for index,kind in enumerate(signatures[op]):
            if index:
                self.take(',')
            if kind=='ref':
                args.append(self.ref())
            elif kind=='tool':
                args.append(self.take('STRING').value)
            elif kind=='data':
                args.append(self.data())
            elif kind=='vector':
                if self.token.kind=='NAME' and self.token.value not in ('true','false','null'):
                    args.append(self.ref())
                elif self.token.kind=='{':
                    args.append(self.data())
                else:
                    self.source.fail('budget amount must be a resource map or value reference',self.token.start,self.token.end)
            elif kind=='refs':
                args.append(self.refs())
            else:
                self.take('[')
                args.append(self.items(']',self.ref))
        self.take(')')
        return op,tuple(args)

    def parse(self):
        workflows=[]
        while self.token.kind!='EOF':
            kind=self.take('NAME')
            if kind.value not in ('workflow','fn'):
                self.source.fail('expected workflow or fn declaration',kind.start,kind.end)
            start=kind.start
            name=self.take('NAME').value
            self.take('(')
            def parameter():
                ref=self.ref()
                self.take(':')
                self.count+=1
                if self.count>10000:
                    self.source.fail('module contains more than 10000 bindings',ref.span.start,ref.span.end)
                return (ref,self.typename())
            parameters=self.items(')',parameter)
            annotation=self.typename() if self.optional('->') else None
            uses=()
            if kind.value=='workflow' or self.token.kind=='NAME' and self.token.value=='uses':
                self.take('NAME','uses')
                self.take('{')
                uses=self.items('}',lambda:self.take('STRING').value)
            self.take('{')
            statements=[]
            while self.token.kind=='NAME' and self.token.value=='let':
                begin=self.take('NAME','let').start
                binding=self.take('NAME').value
                typ=self.typename() if self.optional(':') else None
                self.take('=')
                op,args=self.expression()
                end=self.take(';').end
                self.count+=1
                if self.count>10000:
                    self.source.fail('module contains more than 10000 bindings',begin,end)
                statements.append(Statement(binding,typ,op,args,self.source.span(begin,end)))
            self.take('NAME','return')
            result=self.ref()
            self.take(';')
            end=self.take('}').end
            workflows.append(Workflow(name,parameters,uses,tuple(statements),result,annotation,self.source.span(start,end),kind.value))
        if not workflows:
            self.source.fail('source needs at least one workflow',0,0)
        return tuple(workflows)
