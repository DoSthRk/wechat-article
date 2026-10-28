"""Isolated release validation using the deployment's existing Python interpreter.

Usage:
  /release/.venv/bin/python verify_image_gate_release.py \
      --source /release --work-root /tmp/gm-image-gate.XXXXXX

Copies only code/tests/configuration into a fresh directory; never imports code
from a release whose runtime/outputs/inputs might point at production state.
No pytest dependency. Results and isolated logs are retained under --work-root.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import argparse
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


MODULES = (
    'test_lab_sso', 'test_editorial',
    'test_caption_figures', 'test_figure_quality', 'test_figure_crop_geometry',
    'test_pdf_figure_extractor', 'test_vision_figures', 'test_figure_strategy',
    'test_image_packages', 'test_image_queue_schema', 'test_image_package_consumers',
    'test_auto_blog', 'test_blog_notifications', 'test_image_workflow_worker',
    'test_image_review_api', 'test_blog_pipeline', 'test_blog_image_repair',
    'test_wechat_draft_repair', 'test_distribute', 'test_auto_cover',
    'test_database', 'test_workflow_runner', 'test_dashboard_api', 'test_panel_runner',
)
UI_MODULES = ('test_image_review_ui', 'test_operator_state')


def copy_source(source, destination):
    destination.mkdir()
    ignored = shutil.ignore_patterns('__pycache__', '*.pyc', '.env', '.env.*', '.DS_Store')
    for name in ('core', 'db', 'utils', 'tests', 'templates', 'static', 'genemedi_blog', 'data', 'prompts'):
        shutil.copytree(source / name, destination / name, ignore=ignored)
    for path in source.glob('*.py'):
        shutil.copy2(path, destination / path.name)
    packaged_inputs = source / 'inputs.release-template'
    if not packaged_inputs.is_dir():
        packaged_inputs = source / 'inputs'
        if packaged_inputs.is_symlink():
            raise RuntimeError('No packaged input templates; refusing to copy production input symlink')
    for name in ('lines', 'products', 'style_templates', 'product_modules'):
        shutil.copytree(packaged_inputs / name, destination / 'inputs' / name, ignore=ignored)
    for name in ('runtime', 'logs', 'outputs/jobs', 'inputs/pdfs', 'inputs/image_pools', 'inputs/archived_pdfs'):
        (destination / name).mkdir(parents=True, exist_ok=True)


def install_guards(run_root, node):
    """Audit hooks remain active even when a test replaces a Python wrapper."""
    node_path = Path(node).resolve() if node else None

    def inside(path):
        return Path(os.fsdecode(path)).resolve().is_relative_to(run_root)

    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.sendto'):
            raise RuntimeError('Release test guard: network access is forbidden')
        if event == 'sqlite3.connect':
            database = os.fsdecode(args[0])
            if database.startswith('file:'):
                uri = urlsplit(database)
                if uri.netloc not in ('', 'localhost'):
                    raise RuntimeError('Release test guard: nonlocal SQLite URI is forbidden')
                database = unquote(uri.path)
            if database != ':memory:' and not inside(database):
                raise RuntimeError('Release test guard: SQLite must remain in the isolated directory')
        if event == 'subprocess.Popen':
            executable, arguments, _cwd, _env = args
            candidate = shutil.which(os.fsdecode(executable))
            allowed = (node_path and candidate and Path(candidate).resolve() == node_path
                       and isinstance(arguments, (list, tuple)) and len(arguments) >= 2
                       and arguments[1] == '-e')
            if not allowed:
                raise RuntimeError('Release test guard: real workers, SSH and non-UI subprocesses are forbidden')
        if event in ('os.system', 'os.exec', 'os.posix_spawn'):
            raise RuntimeError('Release test guard: external process launch is forbidden')

    sys.addaudithook(audit)


def module_suite(name, *, skip_reason=None):
    module = importlib.import_module('tests.' + name)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    # unittest does not discover the two plain-function operator JS tests.
    for function_name, function in inspect.getmembers(module, inspect.isfunction):
        if function_name.startswith('test_') and function.__module__ == module.__name__:
            suite.addTest(unittest.FunctionTestCase(function, description=f'{name}.{function_name}'))
    if not suite.countTestCases():
        raise RuntimeError(f'No tests discovered in required module {name}')
    if skip_reason:
        def skipped():
            raise unittest.SkipTest(skip_reason)
        replacement = unittest.TestSuite()
        for index in range(suite.countTestCases()):
            replacement.addTest(unittest.FunctionTestCase(skipped, description=f'{name}: unavailable UI test {index + 1}'))
        return replacement
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--work-root', type=Path, default=Path(tempfile.gettempdir()))
    args = parser.parse_args()
    source, work_root = args.source.resolve(), args.work_root.resolve()
    if not (source / 'tests/test_image_packages.py').is_file() or not work_root.is_dir():
        parser.error('source must contain this release; work-root must already exist')
    if work_root == source or work_root.is_relative_to(source):
        parser.error('work-root must be outside the source/release directory')
    run_root = Path(tempfile.mkdtemp(prefix='image-gate-tests-', dir=work_root)).resolve()
    sandbox = run_root / 'source'
    copy_source(source, sandbox)
    temporary = run_root / 'tmp'
    temporary.mkdir()
    # Keep runtime/library paths, but do not carry production service secrets or
    # behavior flags into tests. Queue persistence is exercised, while the audit
    # guard forbids any real worker process even if a test forgets to mock kick.
    keep = {key: value for key, value in os.environ.items()
            if key in {'PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'SYSTEMROOT'}}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update({
        'DATABASE_URL': f'sqlite:///{sandbox / "runtime/test-default.db"}',
        'ARTICLE_CONTENT_DIR': str(sandbox / 'outputs/jobs'),
        'AUTO_BLOG_ENABLED': 'true', 'PYTHON_DOTENV_DISABLED': '1',
        'PYTHONPATH': str(sandbox), 'PYTHONDONTWRITEBYTECODE': '1',
        'TMPDIR': str(temporary), 'TMP': str(temporary), 'TEMP': str(temporary),
    })
    tempfile.tempdir = str(temporary)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(sandbox))
    os.chdir(sandbox)
    node = shutil.which('node')
    node_guard = run_root / 'node-no-network.cjs'
    node_guard.write_text("""const deny = () => { throw new Error('Release UI test guard: external I/O forbidden'); };
for (const name of ['node:http', 'node:https']) {
  const module = require(name); module.request = deny; module.get = deny;
}
const net = require('node:net'); net.connect = deny; net.createConnection = deny; net.Socket.prototype.connect = deny;
const dns = require('node:dns'); dns.lookup = deny; dns.resolve = deny;
for (const name of ['exec', 'execFile', 'spawn', 'fork', 'execSync', 'execFileSync', 'spawnSync']) require('node:child_process')[name] = deny;
global.fetch = deny;
""", encoding='utf-8')
    os.environ['NODE_OPTIONS'] = f'--require={node_guard}'
    install_guards(run_root, node)
    started = time.monotonic()
    print(f'ISOLATED_TEST_ROOT={run_root}', flush=True)
    print(f'PYTHON={sys.executable}', flush=True)
    print(f'NODE_UI={node or "SKIPPED: Node executable unavailable"}', flush=True)
    with ExitStack() as stack:
        log = stack.enter_context((run_root / 'tests.log').open('w', encoding='utf-8'))
        stack.enter_context(redirect_stdout(log))
        stack.enter_context(redirect_stderr(log))
        stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        suite = unittest.TestSuite(module_suite(name) for name in MODULES)
        for name in UI_MODULES:
            suite.addTest(module_suite(name, skip_reason=None if node else 'Node unavailable; UI tests NOT run'))
        import db.database as database
        if not Path(database.__file__).resolve().is_relative_to(sandbox):
            raise RuntimeError('Release test isolation failed: module loaded outside sandbox')
        result = unittest.TextTestRunner(verbosity=1, stream=log).run(suite)
    summary = {
        'source_release': str(source), 'isolated_test_root': str(run_root),
        'python': sys.executable, 'tests_run': result.testsRun,
        'tests_executed': result.testsRun - len(result.skipped),
        'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': [{'test': test.shortDescription() or test.id(), 'reason': reason} for test, reason in result.skipped],
        'node_ui': 'executed' if node else 'skipped_unavailable',
        'node_path': node, 'seconds': round(time.monotonic() - started, 3),
        'passed': result.wasSuccessful(),
        'scope': 'isolated unit/API/UI-logic tests; not production visual or publication acceptance',
    }
    (run_root / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('RELEASE_TEST_SUMMARY=' + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
