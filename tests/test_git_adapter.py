"""Integration tests use Git itself as the external endpoint, not a SQL mock."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from al0.data import Intent, Principal
from al0.errors import ContractError, EvidenceMismatch, FlowDenied, InvalidData
from al0.execute import Broker, Interpreter
from al0.flow import Label
from al0.git_adapter import GitPublisher, PUBLISH, QUERY
from al0.store import Store
from examples.git_publication import build, run as publish_workflow


def setup_git(folder):
    source, remote = folder/'source', folder/'remote.git'
    source.mkdir()
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME='AL0 test', GIT_AUTHOR_EMAIL='al0@example.invalid',
               GIT_COMMITTER_NAME='AL0 test', GIT_COMMITTER_EMAIL='al0@example.invalid')
    def git(*args):
        return subprocess.run(['git', '-C', str(source), *args], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '--quiet')
    git('init', '--bare', '--quiet', str(remote))
    (source/'report.txt').write_text('first\n', encoding='utf-8')
    git('add', 'report.txt'); git('commit', '--quiet', '-m', 'first')
    first = git('rev-parse', 'HEAD')
    (source/'report.txt').write_text('second\n', encoding='utf-8')
    git('add', 'report.txt'); git('commit', '--quiet', '-m', 'second')
    return source, remote, env, first, git('rev-parse', 'HEAD')


class GitAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source, self.remote, self.env, self.first, self.second = setup_git(self.folder)
        self.adapter = GitPublisher(self.source, str(self.remote), recipients=['public'],
                                    commits=[self.first, self.second], environment=self.env)
        self.store = Store(self.folder/'runtime.sqlite')
        self.who = Principal('git-tests', 'owner')
        self.callbacks = self.adapter.callbacks(self.store, self.who)
        self.broker = Broker(self.store, self.adapter.catalog, self.callbacks)
        self.plan = build(self.adapter.catalog, self.first)
        self.run = self.store.create_run(self.who, self.plan.fingerprint, {'git_dispatches': 8})
        self.grants = {n: self.store.grant(self.run, n, spec.scope, expires=self.store.clock()+1000)
                       for n, spec in self.adapter.catalog.tools.items()}

    def execute(self, broker=None):
        return Interpreter(broker or self.broker).run(self.plan, self.run, self.who, self.grants)

    def operation(self):
        return next(e['data']['operation'] for e in self.store.events(self.run, self.who) if e['kind'] == 'Admitted')

    def query(self, key='query'):
        return self.broker.reconcile(self.run, self.who, key, self.operation(), QUERY, self.grants[QUERY])

    def refs(self):
        result = subprocess.run(['git', '--git-dir', str(self.remote), 'for-each-ref',
            '--format=%(refname) %(objectname)'], env=self.env, check=True, capture_output=True, text=True)
        return [tuple(line.split()) for line in result.stdout.splitlines()]

    def test_real_git_publication_and_broker_replay_do_not_repeat_dispatch(self):
        self.assertEqual(self.execute().data, 'Published')
        refs = self.refs()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0][1], self.first)
        before = self.store.events(self.run, self.who)
        self.assertEqual(self.execute().data, 'Published')
        self.assertEqual(self.store.events(self.run, self.who), before)
        self.assertEqual(self.query()['business'], 'Applied')
        self.assertEqual(self.store.budget(self.run, self.who)['git_dispatches']['confirmed'], 2)

    def test_reply_loss_restart_query_and_late_quota_settlement(self):
        def lost(ticket):
            self.adapter.publish(ticket)
            raise ConnectionError('lost after Git accepted the ref')
        broker = Broker(self.store, self.adapter.catalog, {**self.callbacks, PUBLISH: lost})
        self.assertEqual(self.execute(broker).data, 'Unknown')
        self.assertEqual(len(self.refs()), 1)
        self.store.finish_run(self.run, self.who)
        self.store = Store(self.folder/'runtime.sqlite')
        self.broker = Broker(self.store, self.adapter.catalog, self.adapter.callbacks(self.store, self.who))
        self.assertEqual(self.query()['business'], 'Applied')
        self.assertEqual(self.store.status(self.run, self.who)['status'], 'CompletedClean')
        self.assertEqual(self.store.budget(self.run, self.who)['git_dispatches']['confirmed'], 2)
        self.assertEqual(self.execute().data, 'Published')
        self.assertEqual(len(self.refs()), 1)

    def test_absence_does_not_prove_no_effect_before_delayed_cancelled_push(self):
        entered, release = Event(), Event()
        original = self.adapter._command
        def delayed(args):
            if 'push' in args:
                entered.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release Git push')
            return original(args)
        with patch.object(self.adapter, '_command', delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(self.query()['business'], 'Unknown')
                self.assertEqual(self.refs(), [])
                self.store.cancel(self.operation(), self.who)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=20).data, 'Published')
        self.assertEqual(self.query('late-query')['business'], 'Applied')
        self.assertEqual(len(self.refs()), 1)

    def test_empty_lease_refuses_rebinding_to_a_different_commit(self):
        self.assertEqual(self.execute().data, 'Published')
        ref = self.refs()[0][0]
        result = self.adapter._command(['push', '--porcelain', '--force-with-lease='+ref+':',
                                       '--', str(self.remote), self.second+':'+ref])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.refs(), [(ref, self.first)])

    def test_namespace_corruption_is_not_positive_reconciliation_evidence(self):
        self.execute()
        ref = self.refs()[0][0]
        # Deliberately violate the host's immutable-namespace contract.
        result = self.adapter._command(['push', '--porcelain', '--force', '--',
                                       str(self.remote), self.second+':'+ref])
        self.assertEqual(result.returncode, 0)
        with self.assertRaises(EvidenceMismatch):
            self.query()

    def test_private_request_never_reaches_public_git_adapter(self):
        spec = self.adapter.catalog.tools[PUBLISH]
        intent = Intent(PUBLISH, spec.version, spec.scope, {'commit': self.first}, label=Label({'owner'}))
        with self.assertRaises(FlowDenied):
            self.store.authorize(self.run, self.who, intent, self.grants[PUBLISH])
        self.assertEqual(self.refs(), [])

    def test_host_binding_changes_catalog_and_plan_identity(self):
        other = GitPublisher(self.source, str(self.remote), recipients=['public'],
                             commits=[self.first, self.second], prefix='refs/heads/al0/other/', environment=self.env)
        self.assertNotEqual(other.catalog.fingerprint(), self.adapter.catalog.fingerprint())
        self.assertNotEqual(build(other.catalog, self.first).fingerprint, self.plan.fingerprint)
        with self.assertRaises(InvalidData):
            GitPublisher(self.source, 'origin', recipients=['public'], commits=[self.first], environment=self.env)

    def test_unapproved_commit_and_revision_syntax_never_reach_git(self):
        spec = self.adapter.catalog.tools[PUBLISH]
        for commit in ('HEAD', self.first+':report.txt', '--all', 'f'*40, '0'*40):
            intent = Intent(PUBLISH, spec.version, spec.scope, {'commit': commit})
            with self.subTest(commit=commit), patch.object(self.adapter, '_command') as command:
                with self.assertRaises((InvalidData, EvidenceMismatch)):
                    self.adapter._intent(intent)
                command.assert_not_called()
        narrow = GitPublisher(self.source, str(self.remote), recipients=['public'],
                              commits=[self.first], environment=self.env)
        self.assertNotEqual(narrow.version, self.adapter.version)

    def test_cli_workflow_source_sdk_restart_and_independent_git_oracle(self):
        state = self.folder/'host-state'
        first = publish_workflow(self.source, str(self.remote), self.first, state,
                                 recipients=['public'], environment=self.env)
        second = publish_workflow(self.source, str(self.remote), self.first, state,
                                  recipients=['public'], environment=self.env)
        self.assertEqual(first, second)
        self.assertTrue(first['source_sdk_ir_equal'])
        self.assertEqual(first['final'], 'Published')
        self.assertEqual(first['replay_added_events'], 0)
        self.assertEqual(self.refs(), [(first['reference'], self.first)])

    def test_timeout_is_unknown_and_never_retried(self):
        with patch.object(self.adapter, '_command', side_effect=ConnectionError('transport unavailable')):
            self.assertEqual(self.execute().data, 'Unknown')
        with self.assertRaises(ContractError):
            self.store.retry(self.operation(), self.who)
        self.assertEqual(self.execute().data, 'Unknown')
        self.assertEqual(self.refs(), [])

    def test_process_death_after_git_commit_recovers_through_query(self):
        config = self.folder/'worker.json'
        config.write_text(json.dumps({'source': str(self.source), 'remote': str(self.remote),
            'database': self.store.path, 'commit': self.first, 'approved': [self.first, self.second], 'run': self.run,
            'grants': self.grants}), encoding='utf-8')
        result = subprocess.run([sys.executable, '-m', 'tests.git_adapter_worker', str(config)],
                                env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 77, result.stderr.decode(errors='replace'))
        self.assertEqual(len(self.refs()), 1)
        self.assertEqual(self.execute().data, 'Unknown')
        self.assertEqual(self.query()['business'], 'Applied')
        self.assertEqual(self.store.budget(self.run, self.who)['git_dispatches']['reserved'], 0)
