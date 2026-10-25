"""Durable trusted flow policies. Plan data cannot install or relax policies."""
import json

from .data import canonical, identifier
from .errors import FlowDenied, ReplayMismatch
from .flow import Label, PUBLIC, names


FLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_flows(
 tool TEXT NOT NULL, version TEXT NOT NULL, policy TEXT NOT NULL,
 PRIMARY KEY(tool,version), FOREIGN KEY(tool,version) REFERENCES contracts(tool,version));
CREATE TABLE IF NOT EXISTS task_flows(
 task TEXT PRIMARY KEY REFERENCES tasks(id), control TEXT NOT NULL, upper_bound TEXT NOT NULL);
"""


class FlowMixin:
    def register_tool_flow(self, tool, version, *, recipients, output=PUBLIC, accepted_taints=None):
        identifier(tool)
        identifier(version)
        policy=canonical({'recipients':sorted(names(recipients)), 'output':output.to_data(),
                          'accepted_taints':None if accepted_taints is None else sorted(names(accepted_taints))})
        with self._tx() as db:
            old=db.execute('SELECT policy FROM tool_flows WHERE tool=? AND version=?',(tool,version)).fetchone()
            if old and old['policy']!=policy:
                raise ReplayMismatch('tool information-flow policy is immutable within a version')
            db.execute('INSERT OR IGNORE INTO tool_flows VALUES(?,?,?)',(tool,version,policy))

    def _flow_policy(self, db, intent):
        value=intent.value
        row=db.execute('SELECT policy FROM tool_flows WHERE tool=? AND version=?',(value['tool'],value['version'])).fetchone()
        return None if row is None else json.loads(row['policy'])

    def _check_flow(self, db, intent):
        policy=self._flow_policy(db,intent)
        if policy is None:
            if intent.label!=PUBLIC:
                raise FlowDenied('labelled calls require a trusted tool flow policy')
            return
        intent.label.check_sink(policy['recipients'],policy['accepted_taints'])

    def operation_label(self, operation, principal):
        with self._tx() as db:
            op=self._operation(db,operation,principal)
            intent=self._intent(op['intent'])
            policy=self._flow_policy(db,intent)
            return intent.label.join(PUBLIC if policy is None else Label.from_data(policy['output']))

    def _task_flow(self, db, task):
        row=db.execute('SELECT control,upper_bound FROM task_flows WHERE task=?',(task,)).fetchone()
        return (PUBLIC,PUBLIC) if row is None else (Label.from_data(json.loads(row['control'])),Label.from_data(json.loads(row['upper_bound'])))

    def task_label(self, task, principal):
        with self._tx() as db:
            self._task(db,task,principal)
            return self._task_flow(db,task)[1]
