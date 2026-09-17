import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_init_env_creates_private_secrets_and_never_overwrites(tmp_path):
    output = tmp_path / '.env'
    command = [sys.executable, str(ROOT / 'scripts/init_env.py'), '--domain', 'search.example.com', '--output', str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0
    content = output.read_text()
    values = dict(line.split('=', 1) for line in content.splitlines())
    assert len(base64.b64decode(values['MASTER_KEY'])) == 32
    assert len(values['BOOTSTRAP_PASSWORD']) >= 16
    if os.name != 'nt':
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_text() == content


def test_init_env_rejects_injected_domain(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/init_env.py'), '--domain', 'https://evil.invalid/path',
                             '--output', str(tmp_path / '.env')], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / '.env').exists()
