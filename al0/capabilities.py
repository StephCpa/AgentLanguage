"""Persistent capability derivation with transitive current-authority checks."""
import json
import uuid

from .data import canonical, identifier, quantity
from .errors import AuthorizationDenied, InvalidData, ReplayMismatch
from .flow import Label, PUBLIC


CAPABILITY_SCHEMA="""
CREATE TABLE IF NOT EXISTS grant_derivations(
 child TEXT PRIMARY KEY REFERENCES grants(id), run TEXT NOT NULL REFERENCES runs(id),
 parent TEXT NOT NULL REFERENCES grants(id), parent_epoch INTEGER NOT NULL,
 derive_key TEXT NOT NULL, request TEXT NOT NULL, label TEXT NOT NULL, UNIQUE(run,derive_key));
CREATE TABLE IF NOT EXISTS capability_bindings(
 run TEXT NOT NULL REFERENCES runs(id), activation TEXT NOT NULL, node TEXT NOT NULL,
 tool TEXT NOT NULL, grant_id TEXT NOT NULL REFERENCES grants(id), label TEXT NOT NULL, PRIMARY KEY(run,activation,node));
"""
MAX_CAP_DEPTH=64


class CapabilityMixin:
    def _capability(self,db,run,grant):
        row=db.execute('SELECT * FROM grants WHERE id=? AND run=?',(grant,run)).fetchone()
        if row is None:
            raise AuthorizationDenied('capability is unavailable in this run')
        return row

    def _grant_chain(self,db,run,grant,*,epoch=None,args=None,check_args=False):
        chain=[]
        expected=epoch
        while grant is not None:
            if len(chain)>=MAX_CAP_DEPTH:
                raise AuthorizationDenied('capability ancestry exceeds 64 grants')
            row=self._capability(db,run,grant)
            if not row['active'] or row['expires']<=self.clock() or expected is not None and row['epoch']!=expected:
                raise AuthorizationDenied('capability or ancestor is expired, revoked or has changed epoch')
            if chain and (row['tool'],row['scope'])!=(chain[0][0]['tool'],chain[0][0]['scope']):
                raise AuthorizationDenied('capability ancestry has inconsistent scope')
            link=db.execute('SELECT * FROM grant_derivations WHERE child=?',(grant,)).fetchone()
            constraints={} if link is None else json.loads(link['request'])['constraints']
            if check_args and constraints:
                if type(args) is not dict or any(key not in args or canonical(args[key])!=canonical(value) for key,value in constraints.items()):
                    raise AuthorizationDenied('action arguments exceed capability constraints')
            chain.append((row,constraints))
            grant=None if link is None else link['parent']
            expected=None if link is None else link['parent_epoch']
        return chain

    def capability_info(self,run,principal,grant):
        with self._tx() as db:
            self._run(db,run,principal)
            return dict(self._capability(db,run,grant))

    def bind_capability(self,run,principal,node,tool,grant,*,scope=None,task_lease=None,control=PUBLIC):
        identifier(node)
        identifier(tool)
        activation='0' if task_lease is None else task_lease.task
        with self._tx() as db:
            self._run(db,run,principal)
            old=db.execute('SELECT * FROM capability_bindings WHERE run=? AND activation=? AND node=?',
                           (run,activation,node)).fetchone()
            if old:
                if (old['tool'],old['grant_id'])!=(tool,grant):
                    raise ReplayMismatch('capability binding changed')
                if scope is not None and self._capability(db,run,grant)['scope']!=scope:
                    raise AuthorizationDenied('host capability binding covers a different scope')
                return {'grant':old['grant_id'],'label':json.loads(old['label'])}
            self._control_worker(db,run,principal,task_lease)
            chain=self._grant_chain(db,run,grant)
            if chain[0][0]['tool']!=tool or scope is not None and chain[0][0]['scope']!=scope:
                raise AuthorizationDenied('host capability binding covers a different tool or scope')
            derived=db.execute('SELECT label FROM grant_derivations WHERE child=?',(grant,)).fetchone()
            label=canonical(control.join(PUBLIC if derived is None else Label.from_data(json.loads(derived['label']))).to_data())
            db.execute('INSERT INTO capability_bindings VALUES(?,?,?,?,?,?)',(run,activation,node,tool,grant,label))
            self._event(db,run,'CapabilityBound',node=node,activation=activation,grant=grant,tool=tool)
            self.fault('before_capability_bind_commit')
        self.fault('after_capability_bind_commit')
        return {'grant':grant,'label':json.loads(label)}

    def attenuate_capability(self,run,principal,key,parent,*,expires=None,max_attempts=None,constraints=None,task_lease=None,control=PUBLIC):
        identifier(key)
        if expires is not None:
            quantity(expires)
        if max_attempts is not None:
            quantity(max_attempts)
            if max_attempts<1:
                raise InvalidData('capability attempt limit must be positive')
        constraints={} if constraints is None else constraints
        if type(constraints) is not dict:
            raise InvalidData('capability constraints must be a field equality map')
        for field in constraints:
            identifier(field)
        request=canonical({'expires':expires,'max_attempts':max_attempts,'constraints':constraints})
        with self._tx() as db:
            self._run(db,run,principal)
            old=db.execute('SELECT * FROM grant_derivations WHERE run=? AND derive_key=?',(run,key)).fetchone()
            if old:
                if (old['parent'],old['request'])!=(parent,request):
                    raise ReplayMismatch('capability derivation key changed')
                return {'grant':old['child'],'label':json.loads(old['label'])}
            self._control_worker(db,run,principal,task_lease)
            chain=self._grant_chain(db,run,parent)
            if len(chain)>=MAX_CAP_DEPTH:
                raise AuthorizationDenied('capability ancestry exceeds 64 grants')
            source=chain[0][0]
            deadline=source['expires'] if expires is None else expires
            attempts=source['max_attempts'] if max_attempts is None else max_attempts
            if deadline>source['expires'] or deadline<=self.clock() or attempts>source['max_attempts']:
                raise AuthorizationDenied('capability attenuation cannot extend authority')
            for _,inherited in chain:
                if any(field in constraints and canonical(constraints[field])!=canonical(value) for field,value in inherited.items()):
                    raise AuthorizationDenied('capability attenuation contradicts an inherited constraint')
            child=str(uuid.uuid4())
            parent_label=db.execute('SELECT label FROM grant_derivations WHERE child=?',(parent,)).fetchone()
            label=control.join(PUBLIC if parent_label is None else Label.from_data(json.loads(parent_label['label'])))
            db.execute('INSERT INTO grants(id,run,tool,scope,expires,max_attempts) VALUES(?,?,?,?,?,?)',
                       (child,run,source['tool'],source['scope'],deadline,attempts))
            db.execute('INSERT INTO grant_derivations VALUES(?,?,?,?,?,?,?)',(child,run,parent,source['epoch'],key,request,canonical(label.to_data())))
            self._event(db,run,'CapabilityAttenuated',grant=child,parent=parent,key=key)
            self.fault('before_capability_derive_commit')
        self.fault('after_capability_derive_commit')
        return {'grant':child,'label':label.to_data()}

    def _check_capability_map(self,db,run,caps):
        if type(caps) is not dict:
            raise InvalidData('capability map must bind tool names to grant references')
        for tool,grant in caps.items():
            identifier(tool)
            identifier(grant)
            if self._capability(db,run,grant)['tool']!=tool:
                raise AuthorizationDenied('capability map has a different tool binding')

    def _grant_descends(self,db,run,candidate,limit):
        # Structural authority containment is separate from current liveness;
        # historical completed operations can replay after authority revocation.
        for _ in range(MAX_CAP_DEPTH):
            self._capability(db,run,candidate)
            if candidate==limit:
                return True
            link=db.execute('SELECT parent FROM grant_derivations WHERE child=?',(candidate,)).fetchone()
            if link is None:
                return False
            candidate=link['parent']
        return False

    def check_capability_boundary(self,run,principal,caps,resources):
        with self._tx() as db:
            self._run(db,run,principal)
            self._check_capability_map(db,run,caps)
            self._check_capability_resources(db,run,principal,caps,resources)

    def _check_capability_resources(self,db,run,principal,caps,resources):
        for kind,reference in resources:
            if kind=='Cap':
                grant=reference
            elif kind in ('Permit','Operation'):
                if kind=='Operation':
                    operation=self._operation(db,reference,principal)
                    if operation['run']!=run:
                        raise AuthorizationDenied('operation is outside this run')
                    reference=operation['permit']
                row=db.execute('SELECT grant_id FROM permits WHERE id=? AND run=?',(reference,run)).fetchone()
                if row is None:
                    raise AuthorizationDenied('permit is unavailable in this run')
                grant=row['grant_id']
            else:
                continue
            tool=self._capability(db,run,grant)['tool']
            if tool not in caps or not self._grant_descends(db,run,grant,caps[tool]):
                raise AuthorizationDenied('captured authority exceeds the invocation capability boundary')
