"""Run public server regressions with a generated save and isolated local I/O."""
import argparse
import contextlib
import hashlib
import ipaddress
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import unquote, urlsplit

SERVER = Path(__file__).resolve().parent


def source_hashes(inputs=()):
    paths = list(SERVER.glob('*.py')) + list(SERVER.glob('*.json')) + list(SERVER.glob('*.sql'))
    paths += list((SERVER / 'data').glob('*.json')) + list((SERVER / 'tests').rglob('*.py'))
    paths += [SERVER / name for name in inputs]
    paths += [SERVER / 'tests' / 'requirements.txt']
    return {str(path.relative_to(SERVER)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def isolate(root):
    """Allow generated SQLite files and loopback sockets used by transport tests."""
    def audit(event, args):
        if event == 'sqlite3.connect':
            name = os.fsdecode(args[0])
            if name in (':memory:', ''):
                return
            if name.startswith('file:'):
                name = unquote(urlsplit(name).path)
            if not Path(name).resolve().is_relative_to(root):
                raise RuntimeError('Tests may only open temporary databases: ' + name)
        if event in ('socket.connect', 'socket.sendto', 'socket.getaddrinfo'):
            address = args[0] if event == 'socket.getaddrinfo' else args[1]
            host = address[0] if isinstance(address, tuple) else address
            if host == 'localhost':
                return
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return
            except ValueError:
                pass
            raise RuntimeError('Tests may only use loopback sockets: ' + str(host))
    sys.addaudithook(audit)


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.status = {}

    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def addSuccess(self, test):
        self.status[test.id()] = 'passed'
        super().addSuccess(test)

    def addError(self, test, err):
        self.status[test.id()] = 'error'
        super().addError(test, err)

    def addFailure(self, test, err):
        self.status[test.id()] = 'failed'
        super().addFailure(test, err)

    def addSkip(self, test, reason):
        self.status[test.id()] = 'skipped'
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err:
            self.status[test.id()] = 'failed' if issubclass(err[0], test.failureException) else 'error'
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        self.cases.append(dict(test=test.id(), status=self.status.get(test.id(), 'passed'),
                               seconds=round(time.perf_counter() - self.started, 6)))
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Save tests.log, result.json and seed manifest here')
    parser.add_argument('--pattern', default='test_*.py', help='unittest discovery file pattern')
    parser.add_argument('--test', action='append', help='Selected module, class or test (repeatable)')
    args = parser.parse_args()
    output = args.output.resolve() if args.output else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
        for name in ('tests.log', 'result.json', 'seed.manifest.json'):
            if (output / name).exists():
                parser.error('Output already exists: ' + str(output / name))
    os.environ['TZ'] = 'Asia/Shanghai'
    if hasattr(time, 'tzset'):
        time.tzset()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SERVER))
    sys.path.insert(0, str(SERVER / 'tests'))
    os.chdir(SERVER)
    with tempfile.TemporaryDirectory(prefix='apc-public-tests-') as tmp:
        root = Path(tmp).resolve()
        os.environ['TMPDIR'] = str(root)
        tempfile.tempdir = str(root)
        seed = root / 'seed.db'
        os.environ['ACCOUNT_DB_PATH'] = str(root / 'bootstrap.db')
        os.environ.pop('GM_TEST_DB_SEED', None)
        from test_seed import generate_seed, digest
        started = time.perf_counter()
        manifest = generate_seed(seed)
        seed_hash = digest(seed)
        os.environ['GM_TEST_DB_SEED'] = str(seed)
        isolate(root)
        before = source_hashes(manifest['inputs'])
        with contextlib.ExitStack() as stack:
            stream = stack.enter_context((output / 'tests.log').open('w', encoding='utf-8')) if output else sys.stderr
            if output:
                stack.enter_context(contextlib.redirect_stdout(stream))
                stack.enter_context(contextlib.redirect_stderr(stream))
            loader = unittest.TestLoader()
            suite = (loader.loadTestsFromNames(args.test) if args.test
                     else loader.discover(str(SERVER / 'tests'), pattern=args.pattern))
            expected_tests = suite.countTestCases()
            unittest.installHandler()
            result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result).run(suite)
        after = source_hashes(manifest['inputs'])
        summary = dict(tests=result.testsRun, expected_tests=expected_tests,
                       complete=result.testsRun == expected_tests,
                       failures=len(result.failures), errors=len(result.errors),
                       skipped=len(result.skipped), successful=result.wasSuccessful(),
                       seconds=round(time.perf_counter() - started, 6),
                       python=sys.version, timezone=os.environ['TZ'],
                       sqlite=sqlite3.sqlite_version,
                       dependencies={name: metadata.version(name) for name in ('Flask', 'cryptography', 'cffi', 'pycparser')},
                       selection=args.test or args.pattern,
                       database_sha256=seed_hash, seed_unchanged=digest(seed) == seed_hash,
                       source_unchanged=before == after, source_sha256=before,
                       cases=result.cases)
        summary['successful'] = bool(summary['successful'] and summary['tests'] and
                                     summary['complete'] and summary['seed_unchanged'] and summary['source_unchanged'])
        if output:
            (output / 'result.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            (output / 'seed.manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(json.dumps({key: value for key, value in summary.items() if key not in ('source_sha256', 'cases')}, ensure_ascii=False))
        return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
