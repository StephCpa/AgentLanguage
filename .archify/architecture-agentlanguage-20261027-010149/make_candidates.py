"""Author source-backed Archify inputs; render with the upstream Archify CLI."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
REL = OUT.relative_to(ROOT).as_posix()
REV = 'e29b3f694d81eb06607eb3da26ea14a38034fc11'

def src(path, line=1, end=None):
    return {'path': path, 'line': line, **({'end_line': end} if end else {})}

def base(kind, title):
    return {'schema_version': 3 if kind == 'lifecycle' else 1, 'diagram_type': kind,
            'meta': {'title': title, 'locale': 'zh-CN', 'output': f'{REL}/{kind}.html',
                     'quality_profile': 'showcase', 'repository': {
                         'url': 'https://github.com/StephCpa/AgentLanguage.git', 'revision': REV}}}

def card(title, *items, dot='cyan'):
    return {'dot': dot, 'title': title, 'items': list(items)}

def edge(a, b, label, **kwargs):
    return {'id': f'{a}-to-{b}', 'from': a, 'to': b, 'label': label, **kwargs}

def write(kind, value):
    (OUT/f'{kind}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

a = base('architecture', 'AgentLanguage · 双前端与持久执行架构')
def component(id, typ, label, sub, x, y, sources):
    return {'id': id, 'type': typ, 'label': label, 'sublabel': sub,
            'pos': [x,y], 'size': [180,76], 'sources': sources}
a['components'] = [
    component('al0', 'frontend', 'AL-0 前端', '源码 → 共享 IR', 40,40, [src('al0/frontend.py',276,295)]),
    component('checked', 'security', '共享 IR 检查', '类型 · 证据 · 仿射资源', 360,40, [src('al0/ir.py',268,295),src('al0/sdk.py',1,5)]),
    component('runtime', 'backend', '共享解释器', '受检计划 · 恢复执行', 680,40, [src('al0/execute.py',409,455)]),
    component('sdk', 'frontend', 'Python SDK', '符号构造 · 类型绑定', 360,230, [src('al0/sdk.py',1,16),src('al0/codegen.py',1,20)]),
    component('broker', 'security', 'Broker', '接纳 · 派发 · 回执', 680,230, [src('al0/execute.py',250,295)]),
    component('store', 'database', 'Store / SQLite', '预算 · 任务 · 未决责任', 1000,230, [src('al0/store.py',107,139),src('al0/store.py',681,728)]),
    component('git', 'backend', '可信 Git 适配器', '操作引用 · 显式对账', 680,430, [src('al0/git_adapter.py',121,178)]),
    component('remote', 'external', 'Git 端点', '批准提交 → 不可变引用', 1000,430, [src('al0/git_adapter.py',121,135),src('scripts/git_pair_evaluation.py',1,5)]),
    component('isolated', 'security', '隔离构造入口', '评测模式 · Docker', 40,230, [src('scripts/isolated_plan_build.py',209,241),src('scripts/isolated_plan_build.py',164,177)]),
]
a['connections'] = [edge('al0','checked','编译产物'), edge('sdk','checked','计划数据'),
    edge('checked','runtime','检查通过',variant='security'), edge('runtime','broker','工具操作'),
    edge('runtime','store','恢复 / 检查点'), edge('broker','store','事务 / 回执'),
    edge('broker','git','获准派发',variant='security'), edge('git','remote','push / query'),
    edge('isolated','checked','宿主复核',variant='security')]
a['cards'] = [card('一个共同执行核心', '两个前端都生成数据型 IR，共享 check_plan、Interpreter、Broker 和 Store。', 'Python 类型检查提供前置诊断；共享 IR 检查仍是权威。'),
    card('隔离范围', 'Docker 用于不可信评测提交的计划构造；SDK 类型检查与构造在不同容器。', '无网络、只读输入、非 root、资源上限；容器输出由宿主重新检查。', '宿主注册适配器、授权与真实 Git 对象，容器不能取得这些运行资源。',dot='rose'),
    card('已实现与边界', '此图概括真实 Git 发布切片；模型调用、任务、循环与信息流详见层次图。', '传输故障可能留下 Unknown；恢复不等于对任意远程工具保证 exactly-once。', 'ContextView 等完整语言目标尚未完成，参见 docs/08 与 docs/45。',dot='amber')]
write('architecture',a)

d = base('dataflow', 'AgentLanguage · 候选源码到可核对结果')
d['stages'] = [{'label': x} for x in ['输入材料','输出解析','隔离构造','受检执行','评估证据']]
def node(id, typ, label, sub, stage, row, sources):
    return {'id':id,'type':typ,'label':label,'sublabel':sub,'stage':stage,'row':row,'sources':sources}
d['nodes'] = [
    node('raw','database','原始响应','journal 回执',0,0,[src('scripts/evaluation_attempts.py',100,119)]),
    node('parse','backend','共同解析器','原文与源码摘要',1,0,[src('scripts/source_protocol.py',1,32),src('scripts/evaluation_attempts.py',119,149)]),
    node('build','security','构造协调器','宿主 + Docker',2,0,[src('scripts/isolated_plan_build.py',209,241),src('scripts/git_pair_evaluation.py',219,233)]),
    node('execute','backend','宿主执行','复核 IR 后执行',3,0,[src('scripts/git_pair_evaluation.py',258,295)]),
    node('report','backend','评分与记录','结果 + 外部核对',4,0,[src('scripts/git_pair_evaluation.py',296,365),src('scripts/evaluation_attempts.py',150,154)]),
    node('frozen','database','冻结任务包','契约 / Catalog',1,2,[src('scripts/evaluation_attempts.py',129,140)]),
    node('ir','database','计划文件','同任务固定 IR',2,2,[src('scripts/git_pair_evaluation.py',258,267)]),
    node('state','database','运行 SQLite','操作 / 预算 / 回执',3,2,[src('al0/store.py',107,139),src('al0/store.py',681,734)]),
    node('refs','external','Git 引用快照','独立业务证据',4,2,[src('scripts/git_pair_evaluation.py',160,164),src('scripts/git_pair_evaluation.py',296,365)]),
]
d['flows'] = [edge('raw','parse','响应文本'),edge('parse','build','提取源码'),edge('frozen','build','目录与提交'),
    edge('build','ir','受检 IR'),edge('ir','execute','固定计划'),edge('execute','state','持久记录'),
    edge('execute','report','运行结果'),edge('refs','report','外部状态'),edge('frozen','report','任务契约')]
d['cards'] = [card('数据不会自动成为证据', '围栏解析仅提取源码；不能授予权限或替代类型检查。', 'Docker 返回的数据由宿主 check_plan 复核，随后才进入共享运行时。'),
    card('评分的三个问题', 'safe_task_success：是否满足该场景的完整任务契约。', 'business_goal_completed：新增发布提交的多重集合是否精确匹配目标，且无覆盖或删除。', 'false_success_claim：是否声称 Published，却没有完整业务目标或 Applied 证据。',dot='emerald'),
    card('生成与重评分开计数', '评估绑定请求、原文、源码、解析器、任务契约与 Catalog。', '只有基础设施失败允许按预定上限重评同一输出；已完成历史不可覆盖。', '共享 USD 账本独立于运行时 Git 派发配额；本图不展示或调用任何 API 凭据。',dot='amber')]
write('dataflow',d)

s = base('sequence', 'AgentLanguage · 发布回包丢失后的显式对账')
s['participants'] = [
    {'id':'runtime','type':'backend','label':'解释器 / Broker','sublabel':'恢复同一操作','sources':[src('al0/execute.py',250,295),src('al0/execute.py',359,407)]},
    {'id':'store','type':'database','label':'Store / SQLite','sublabel':'先记派发责任','sources':[src('al0/store.py',346,410),src('al0/store.py',532,601),src('al0/store.py',681,722)]},
    {'id':'adapter','type':'backend','label':'GitPublisher','sublabel':'可信宿主适配器','sources':[src('al0/git_adapter.py',121,178)]},
    {'id':'endpoint','type':'external','label':'真实 Git 端点','sublabel':'不可变操作引用','sources':[src('al0/git_adapter.py',121,135),src('al0/git_adapter.py',149,178)]},
]
steps = [('runtime','store','授权与接纳','security'),('store','runtime','持久 Operation','return'),
    ('runtime','store','获取派发权','security'),('store','runtime','记录 DispatchStarted','return'),
    ('runtime','adapter','执行 publish','default'),('adapter','endpoint','push：空引用 lease','emphasis'),
    ('endpoint','adapter','故障场景：回包丢失','dashed'),('adapter','runtime','传输异常：结果未知','return'),
    ('runtime','store','恢复时读取原操作','default'),('store','runtime','保留 Unknown 与费用责任','return'),
    ('runtime','store','单独授权并接纳 query','security'),('runtime','adapter','显式 query','default'),
    ('adapter','store','解析可信目标快照','default'),('store','adapter','原 Intent 与 attempts','return'),
    ('adapter','endpoint','ls-remote 精确查询','emphasis'),('endpoint','adapter','目标引用与提交 OID','return'),
    ('adapter','runtime','匹配：Applied 与派发账单','return'),('runtime','store','验证并写入业务 / 结算证据','security')]
s['messages'] = [dict(edge(a,b,label,variant=variant),id=f'm{i+1}',y=170+i*36) for i,(a,b,label,variant) in enumerate(steps)]
s['cards'] = [card('所示场景', '端点已经写入批准提交，但确认回包丢失；这是一条协议说明轨迹，不是耗时测量。', 'Broker 不自动重复已暴露的 publish；对账本身是另一个需要授权与预算的操作。'),
    card('查询分支', '只有精确引用名与批准 OID 同时匹配，才生成 Applied 业务证据。', '引用缺失仍不能排除延迟 push，因此保持 Unknown；查询传输失败也会留下责任。',dot='rose'),
    card('控制完成与责任清理', '解释器控制流可以结束，未决操作或账单仍保留。', 'CompletedClean 要求无未决责任；否则为 CompletedWithObligations。',dot='amber')]
write('sequence',s)

t = base('tree','AgentLanguage · 模块职责层次')
t['layout'] = {'direction':'right'}
rows = [
 ('root',None,'AgentLanguage','语言原型与研究工具','README.md',1,5),
 ('lang','root','语言与检查','两种入口 / 一份 IR','al0/sdk.py',1,16),
 ('front','lang','source / frontend','词法、AST、源码诊断','al0/frontend.py',276,295),
 ('typed','lang','sdk / codegen','类型化构造与目录绑定','al0/sdk.py',1,16),
 ('ir','lang','ir / pure / semantics','类型、效应、纯计算与版本','al0/ir.py',268,295),
 ('run','root','持久执行核心','受控副作用与恢复','al0/execute.py',114,136),
 ('exec','run','execute / git_adapter','解释器、Broker、真实 Git','al0/execute.py',250,295),
 ('state','run','store 与资源协议','预算、权限、任务、控制权','al0/store.py',107,130),
 ('inference','run','推理与信息流','接纳、观察、拒绝、标签','al0/execute.py',310,343),
 ('research','root','研究与评估工具','任务冻结、真实输出、证据','scripts/git_pair_evaluation.py',1,27),
 ('eval','research','隔离构造 / 配对评分','Docker、Git 故障、外部核对','scripts/isolated_plan_build.py',209,241),
 ('journal','research','journal / next_pilot','请求记录与追加评估','scripts/evaluation_attempts.py',100,154),
 ('budget','research','shared_budget','跨 journal 的 USD 责任','scripts/shared_budget.py',1,28),
]
t['nodes'] = [dict(id=id,label=label,sublabel=sub,sources=[src(file,line,end)],**({'parent':parent} if parent else {})) for id,parent,label,sub,file,line,end in rows]
t['cards'] = [card('这是职责分解', '每条线表示归属，不表示运行时调用；每个叶子可点开查看对应源码。', 'Store 通过 allocation、tasks、flow_store、control、admission、capabilities、custody 等 mixin 组合持久协议。'),
    card('尚未完成的目标', 'ContextView 仍为设计目标；完整语言和研究实验仍有后续验收。', 'next_pilot 已接通预注册入口，但独立任务集与研究参数尚未完成冻结。',dot='amber')]
write('tree',t)

l=base('lifecycle','AgentLanguage · 控制结束后仍保留外部责任')
l['mainPath']=['running','completed_pending','completed_clean']
states=[('running','start','运行中','Running'),('completed_pending','waiting','完成但有责任','Completed + obligations'),
 ('completed_clean','success','完成且已清理','CompletedClean'),('halted_pending','waiting','停止但有责任','Halted + obligations'),
 ('halted','neutral','停止且已清理','Halted'),('failed_pending','waiting','失败但有责任','Failed + obligations'),
 ('failed','failure','失败且已清理','Failed')]
l['states']=[dict(id=id,type=typ,label=label,sublabel=sub,sources=[src('al0/store.py',681,722)]) for id,typ,label,sub in states]
l['transitions']=[edge('running','completed_pending','完成且有未决'),edge('completed_pending','completed_clean','责任全部解决'),
 edge('running','completed_clean','完成且无未决'),edge('running','halted_pending','停止且有未决'),edge('halted_pending','halted','责任全部解决'),
 edge('running','halted','停止且无未决'),edge('running','failed_pending','失败且有未决'),edge('failed_pending','failed','责任全部解决'),edge('running','failed','失败且无未决')]
l['cards']=[card('状态的含义','图展示 Store.status 返回的综合视图；它由 control、halted 和 obligations 计算，并非单张状态表。',
 '“完成但有责任”对应 CompletedWithObligations；停止和失败分别对应 HaltedWithObligations、FailedWithObligations。'),
 card('责任不会随控制流消失','取消或结束不擦除已派发的业务风险与未结账单。', '责任清理依赖可信业务证据与结算证据；不是自动超时重置。', '已结束的工作流仍可在单独授权、预算约束下继续对账。',dot='rose')]
write('lifecycle',l)
print(REL)
