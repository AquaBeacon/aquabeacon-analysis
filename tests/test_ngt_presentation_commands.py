"""Explicit selection and failure cleanup for presentation-only entry points."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('script', ['render_ngt_cad_positions', 'plot_ngt_aerial_hires'])
def test_required_experiment_and_help(script):
    command = [sys.executable, str(ROOT/'scripts'/f'{script}.py')]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 2 and '--experiment' in result.stderr
    help_result = subprocess.run(command+['--help'], capture_output=True, text=True, timeout=15,
                                 check=False)
    assert help_result.returncode == 0 and '--experiment' in help_result.stdout


def test_timeout_terminates_then_kills_only_owned_process_and_records_exit(tmp_path):
    module = load_script('render_ngt_cad_positions')

    class Process:
        pid = 123
        code = None
        def __init__(self):
            self.events = []

        def wait(self, timeout):
            self.events.append(('wait', timeout))
            if self.code is None:
                raise subprocess.TimeoutExpired('owned', timeout)
            return self.code

        def poll(self):
            return self.code

        def terminate(self):
            self.events.append('terminate')

        def kill(self):
            self.events.append('kill')
            self.code = -9

    process = Process()
    path = tmp_path/'process.json'
    with pytest.raises(subprocess.TimeoutExpired):
        module.wait_owned(process, 1, {'pid': 123}, path)
    record = json.loads(path.read_text())
    assert record['exited'] and record['returncode'] == -9 and record['ended_utc']
    assert process.events == [('wait', 1), 'terminate', ('wait', 30), 'kill', ('wait', 15)]


def test_static_mosaic_reuses_cache_without_network(tmp_path, monkeypatch):
    module = load_script('plot_ngt_aerial_hires')
    monkeypatch.setattr(module, 'OUT', tmp_path)
    (tmp_path/'world_imagery_static.png').write_bytes(b'cached')
    monkeypatch.setattr(module.urllib.request, 'urlopen',
                        lambda *a, **k: pytest.fail('Cached map must not refetch'))
    module.load_static_mosaic({})
