"""Docker construction boundary: no runtime state, credentials or writable mounts.

The host selects a trusted builder image. Linux kernel, Docker daemon and image
are part of the TCB; this does not defend against container escape exploits.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Event, Lock, Thread
import time
import uuid

from al0.data import canonical
from al0.ir import check_plan

ROOT = Path(__file__).absolute().parents[1]
POLICY_VERSION = '1'
MAX_INPUT = 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
SOURCE_LABEL = 'org.agentlanguage.builder.source'
POLICY_LABEL = 'org.agentlanguage.builder.policy'


def implementation_digest():
    paths = [*sorted((ROOT/'al0').rglob('*.py')), ROOT/'scripts/isolated_plan_worker.py']
    material = {str(p.relative_to(ROOT)).replace('\\', '/'): p.read_text(encoding='utf-8') for p in paths}
    return hashlib.sha256(canonical(material).encode()).hexdigest()


class BuilderUnavailable(RuntimeError):
    pass


def control(args, *, timeout=20):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuilderUnavailable(type(error).__name__+': Docker control command failed') from None
    return result


class DockerBuilder:
    def __init__(self, image, *, docker='docker', timeout=90, output_limit=MAX_OUTPUT):
        if not isinstance(image, str) or not image or image.startswith('-'):
            raise ValueError('select an existing trusted builder image')
        if not 0 < timeout <= 300 or not 1024 <= output_limit <= MAX_OUTPUT:
            raise ValueError('invalid construction limits')
        self.docker, self.timeout, self.output_limit = docker, timeout, output_limit
        inspected = control([docker, 'image', 'inspect', image])
        if inspected.returncode:
            raise BuilderUnavailable('builder image unavailable; build it explicitly before evaluation')
        try:
            record, = json.loads(inspected.stdout)
            labels = record['Config']['Labels'] or {}
            if record['Os'] != 'linux' or labels.get(SOURCE_LABEL) != implementation_digest() or labels.get(POLICY_LABEL) != POLICY_VERSION:
                raise ValueError('builder image does not match this implementation and policy')
            self.image = record['Id']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', self.image):
                raise ValueError('image must resolve to an immutable ID')
        except (KeyError, TypeError, ValueError) as error:
            raise BuilderUnavailable(str(error)) from None

    def _execute(self, folder, mode):
        name = 'al0-build-'+uuid.uuid4().hex
        start = time.monotonic()
        report = {'mode': mode, 'image': self.image, 'policy_version': POLICY_VERSION,
                  'container': name, 'returncode': None, 'timeout': False, 'output_limited': False,
                  'stdout': '', 'stderr': '', 'removed': False}
        # Only this freshly generated input directory is shared. No .git, home,
        # Docker socket, endpoint, grant or output directory is mounted.
        args = [self.docker, 'create', '--name', name, '--pull=never', '--network=none',
            '--read-only', '--user=65534:65534', '--cap-drop=ALL',
            '--security-opt=no-new-privileges=true', '--pids-limit=64',
            '--memory=512m', '--memory-swap=512m', '--cpus=1', '--log-driver=none',
            '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777',
            '--mount', 'type=bind,src='+str(folder)+',dst=/input,readonly',
            '--workdir=/tmp', '--env=HOME=/tmp', '--env=PYTHONDONTWRITEBYTECODE=1',
            '--entrypoint=python', self.image, '/opt/al0/isolated_plan_worker.py', mode]
        process = None
        threads = []
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        stop = Event()
        try:
            created = control(args)
            if created.returncode:
                raise BuilderUnavailable('Docker refused the construction policy: '+created.stderr[:1000])
            process = subprocess.Popen([self.docker, 'start', '--attach', name], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            lock = Lock()
            size = 0
            def read(stream, key):
                nonlocal size
                with stream:
                    while True:
                        chunk = stream.read(8192)
                        if not chunk:
                            return
                        with lock:
                            room = self.output_limit-size
                            output[key].extend(chunk[:max(0, room)])
                            size += len(chunk)
                            if size > self.output_limit:
                                stop.set()
                                return
            for stream, key in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                thread = Thread(target=read, args=(stream, key), daemon=True)
                thread.start()
                threads.append(thread)
            while process.poll() is None:
                if stop.wait(.05):
                    report['output_limited'] = True
                    break
                if time.monotonic()-start >= self.timeout:
                    report['timeout'] = True
                    break
            state = control([self.docker, 'inspect', '--format', '{{json .}}', name])
            if state.returncode:
                raise BuilderUnavailable('container terminal state unavailable')
            actual = json.loads(state.stdout)
            report['container_state'] = actual['State']
            host = actual['HostConfig']
            mounts = actual['Mounts']
            report['observed_policy'] = {k: host.get(k) for k in ('NetworkMode', 'ReadonlyRootfs',
                'CapDrop', 'SecurityOpt', 'Memory', 'MemorySwap', 'NanoCpus', 'PidsLimit', 'Tmpfs', 'LogConfig')}
            report['observed_policy']['user'] = actual['Config']['User']
            report['observed_policy']['mounts'] = [{'destination': m['Destination'], 'type': m['Type'], 'rw': m['RW']} for m in mounts]
            inputs = [m for m in mounts if m['Destination'] == '/input']
            if not (host['NetworkMode'] == 'none' and host['ReadonlyRootfs']
                    and actual['Config']['User'] == '65534:65534'
                    and 'ALL' in host['CapDrop'] and 'no-new-privileges=true' in host['SecurityOpt']
                    and host['Memory'] == host['MemorySwap'] == 512*1024*1024
                    and host['NanoCpus'] == 1000000000 and host['PidsLimit'] == 64
                    and host['LogConfig']['Type'] == 'none'
                    and len(inputs) == 1 and inputs[0]['Type'] == 'bind' and not inputs[0]['RW']
                    and all(m['Destination'] in ('/input', '/tmp') for m in mounts)):
                raise BuilderUnavailable('observed container policy differs from requested boundary')
            if not report['timeout'] and not report['output_limited']:
                if report['container_state']['Running']:
                    raise BuilderUnavailable('Docker attach ended while container was still running')
                report['returncode'] = report['container_state']['ExitCode']
        except OSError:
            raise BuilderUnavailable('Docker attach could not start') from None
        finally:
            # Killing the CLI alone leaves containers/descendants alive. Always
            # remove our uniquely named container before accepting any output.
            removed = control([self.docker, 'rm', '--force', name])
            report['removed'] = removed.returncode == 0
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                report['attach_returncode'] = process.returncode
                for thread in threads:
                    thread.join(timeout=5)
                if any(thread.is_alive() for thread in threads):
                    raise BuilderUnavailable('Docker output collection did not terminate')
                report['output_limited'] = report['output_limited'] or stop.is_set()
                report.update({key: bytes(value).decode('utf-8', errors='replace') for key, value in output.items()})
            if not report['removed']:
                raise BuilderUnavailable('container cleanup unconfirmed: '+name)
        report['seconds'] = time.monotonic()-start
        return report

    def build(self, *, arm, source, catalog_document, commit, catalog):
        if arm not in ('al0', 'sdk'):
            raise ValueError('unknown front end')
        if len(source.encode('utf-8')) > MAX_INPUT or len(catalog_document.encode('utf-8')) > MAX_INPUT:
            return {'failure': 'input_limit', 'stages': {}, 'plan': None}
        from al0.codegen import generate_bindings
        stages = {}
        with tempfile.TemporaryDirectory(prefix='al0-build-input-') as temporary:
            folder = Path(temporary)
            folder.chmod(0o755)
            files = {'submission.py' if arm == 'sdk' else 'submission.al0': source,
                'catalog.json': catalog_document, 'bindings.py': generate_bindings(catalog),
                'request.json': json.dumps({'commit': commit})}
            for name, contents in files.items():
                path = folder/name
                path.write_text(contents, encoding='utf-8')
                path.chmod(0o644)
            # The static checker and arbitrary Python run in distinct containers.
            # A submission cannot monkeypatch the gate that decides its typing.
            for mode in (('typecheck', 'sdk') if arm == 'sdk' else ('al0',)):
                result = self._execute(folder, mode)
                stages[mode] = result
                if result['timeout'] or result['output_limited']:
                    return {'failure': 'construction_limit', 'stages': stages, 'plan': None}
                if result['returncode'] != 0:
                    return {'failure': 'static_rejection' if mode in ('typecheck', 'al0') else 'build_failure',
                            'stages': stages, 'plan': None}
        try:
            # The container is untrusted. In particular, never trust its claimed
            # checker status, fingerprint, Python objects or serialized authority.
            plan = check_plan(result['stdout'], catalog)
        except Exception as error:
            return {'failure': 'ir_rejection', 'stages': stages, 'plan': None,
                    'diagnostic': {'error': type(error).__name__, 'message': str(error)}}
        return {'failure': None, 'stages': stages, 'plan': plan.encoded}
