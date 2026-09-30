"""Source checkout config precedence, without browsing or deleting test artifacts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def checkout(tmp_path):
    for name in ('launch.py', 'library_login.py'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    package = tmp_path / 'paper_search_mcp'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    shutil.copyfile(ROOT / 'paper_search_mcp' / 'config.py', package / 'config.py')
    (package / 'comm_server.py').write_text(
        'import os, json\ndef main():\n'
        ' print(json.dumps({k: os.getenv(k) for k in '
        '["COMM_MCP_DATA_DIR", "COMM_CNKI_BROWSER_PATH"]}))\n', encoding='utf-8')
    provider = '''import os
_worker = None
async def authenticate():
    if os.getenv('TEST_AUTH') == 'error':
        return {'success': False, 'message': 'test_auth_failed'}
    if os.getenv('TEST_AUTH') == 'direct':
        return {'access_mode': 'direct', 'authentication_required': False}
    return {'authenticated': True}
'''
    for name in ('comm_cnki.py', 'comm_wos.py'):
        (package / name).write_text(provider, encoding='utf-8')
    return tmp_path


def run(path, *args, extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('COMM_', 'PAPER_SEARCH_MCP_', 'TEST_AUTH'))}
    env.update(extra or {})
    return subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(path), *args],
                          env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)


@pytest.mark.parametrize('override', ['none', 'process', 'file'])
def test_launcher_configuration_precedence(tmp_path, override):
    root = checkout(tmp_path)
    (root / '.env').write_text("COMM_MCP_DATA_DIR='local data'\nCOMM_CNKI_BROWSER_PATH='local browser'\n", encoding='utf-8')
    extra, expected = {}, 'local data'
    if override == 'process':
        extra, expected = {'COMM_MCP_DATA_DIR': 'process data'}, 'process data'
    if override == 'file':
        explicit = root / 'external.env'
        explicit.write_text("COMM_MCP_DATA_DIR='external data'\n", encoding='utf-8')
        extra, expected = {'PAPER_SEARCH_MCP_ENV_FILE': str(explicit)}, 'external data'
    result = run(root / 'launch.py', extra=extra)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual['COMM_MCP_DATA_DIR'] == expected
    assert actual['COMM_CNKI_BROWSER_PATH'] == (None if override == 'file' else 'local browser')


@pytest.mark.parametrize('provider,state,code', [('cnki', 'direct', 0), ('cnki', 'ok', 0),
                                              ('wos', 'ok', 0), ('wos', 'error', 1)])
def test_login_helper_exit_status(tmp_path, provider, state, code):
    root = checkout(tmp_path)
    (root / '.env').write_text('TEST_AUTH=' + state + '\n', encoding='utf-8')
    result = run(root / 'library_login.py', provider)
    assert result.returncode == code, result.stderr
    if code:
        assert 'test_auth_failed' in result.stderr
