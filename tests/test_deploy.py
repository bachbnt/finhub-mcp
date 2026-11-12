# Copyright (c) 2026 bachbnt. All rights reserved.

import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from finhub_mcp import alerts, config, providers
from finhub_mcp.providers import VnstockUnavailableError

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
DAEMON = SCRIPTS / 'daemon.sh'
IS_MAC = sys.platform == 'darwin'


def run_daemon(*args, env=None, timeout=30):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(['bash', str(DAEMON), *args], capture_output=True, text=True, env=full_env, timeout=timeout)


class PlistTests(unittest.TestCase):
    def render(self, **env):
        result = run_daemon('plist', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return plistlib.loads(result.stdout.encode()), result.stdout

    def test_plist_is_valid_and_has_the_expected_job_settings(self):
        plist, _ = self.render(FINHUB_ROOT='/opt/finhub', FINHUB_LAUNCHD_LABEL='com.example.finhub',
                               FINHUB_LOG_DIR='/var/tmp/finhub-logs', HOME='/Users/someone')
        self.assertEqual(plist['Label'], 'com.example.finhub')
        self.assertEqual(plist['ProgramArguments'], ['/opt/finhub/.venv/bin/python', '/opt/finhub/alert_daemon.py'])
        self.assertEqual(plist['WorkingDirectory'], '/opt/finhub')
        self.assertTrue(plist['RunAtLoad'])
        self.assertEqual(plist['KeepAlive'], {'SuccessfulExit': False})
        self.assertGreaterEqual(plist['ThrottleInterval'], 10)  # a crash loop must not spin
        self.assertEqual(plist['StandardOutPath'], '/var/tmp/finhub-logs/daemon.out.log')
        self.assertEqual(plist['StandardErrorPath'], '/var/tmp/finhub-logs/daemon.err.log')
        self.assertEqual(plist['EnvironmentVariables'], {'PYTHONUNBUFFERED': '1'})

    def test_default_label_and_paths(self):
        plist, _ = self.render(HOME='/Users/someone')
        self.assertEqual(plist['Label'], 'com.finhub.alertdaemon')
        self.assertEqual(plist['StandardOutPath'], '/Users/someone/Library/Logs/finhub-mcp/daemon.out.log')
        self.assertEqual(plist['ProgramArguments'][1], str(ROOT / 'alert_daemon.py'))

    def test_secrets_never_reach_the_plist(self):
        secrets = {'TELEGRAM_BOT_TOKEN': 'TELEGRAM-SECRET-123', 'FINHUB_HTTP_TOKEN': 'HTTP-SECRET-456'}
        _, text = self.render(**secrets)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('TELEGRAM', text)

    def test_config_overrides_are_passed_through_when_set(self):
        plist, _ = self.render(FINHUB_CONFIG='/etc/finhub/config.json', FINHUB_ALERT_STORE='/var/lib/finhub/alerts.json')
        env = plist['EnvironmentVariables']
        self.assertEqual(env['FINHUB_CONFIG'], '/etc/finhub/config.json')
        self.assertEqual(env['FINHUB_ALERT_STORE'], '/var/lib/finhub/alerts.json')

    def test_xml_special_characters_in_paths_are_escaped(self):
        plist, text = self.render(FINHUB_ROOT='/opt/a&b <c>')
        self.assertEqual(plist['WorkingDirectory'], '/opt/a&b <c>')
        self.assertIn('a&amp;b &lt;c&gt;', text)

    def test_help_lists_every_command(self):
        for args in (['help'], ['--help'], []):
            out = run_daemon(*args).stdout
            for command in ('start', 'stop', 'restart', 'status', 'logs', 'uninstall', 'plist'):
                self.assertIn(f'daemon.sh {command}', out)
        self.assertEqual(run_daemon('bogus').returncode, 2)


class LocationGuardTests(unittest.TestCase):
    @unittest.skipUnless(IS_MAC, 'launchd is macOS only')
    def test_start_refuses_macos_protected_folders(self):
        home = Path.home()
        for folder in ('Desktop', 'Documents', 'Downloads'):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as agents:
                result = run_daemon('start', env={
                    'FINHUB_ROOT': str(home / folder / 'finhub-mcp'),
                    'FINHUB_LAUNCH_AGENTS_DIR': agents, 'FINHUB_LOG_DIR': agents,
                    'FINHUB_PYTHON': sys.executable,
                })
                self.assertEqual(result.returncode, 1)
                self.assertIn('macOS-protected folder', result.stderr)
                self.assertIn('Docker', result.stderr)
                self.assertIn('FINHUB_ALLOW_PROTECTED', result.stderr)
                self.assertEqual(os.listdir(agents), [], 'nothing may be installed when the check fails')

    @unittest.skipUnless(IS_MAC, 'launchd is macOS only')
    def test_a_venv_in_a_protected_folder_is_also_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_daemon('start', env={
                'FINHUB_ROOT': tmp, 'FINHUB_PYTHON': str(Path.home() / 'Downloads' / 'venv' / 'bin' / 'python'),
                'FINHUB_LAUNCH_AGENTS_DIR': tmp, 'FINHUB_LOG_DIR': tmp,
            })
        self.assertEqual(result.returncode, 1)
        self.assertIn('macOS-protected folder', result.stderr)

    @unittest.skipIf(IS_MAC, 'only meaningful where launchd does not exist')
    def test_start_explains_that_launchd_needs_macos(self):
        result = run_daemon('start')
        self.assertEqual(result.returncode, 1)
        self.assertIn('only exists on macOS', result.stderr)

    @unittest.skipUnless(IS_MAC, 'launchd is macOS only')
    def test_status_and_stop_are_harmless_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'FINHUB_LAUNCHD_LABEL': 'com.finhub.test.notinstalled', 'FINHUB_LAUNCH_AGENTS_DIR': tmp,
                   'FINHUB_LOG_DIR': tmp}
            status = run_daemon('status', env=env)
            stop = run_daemon('stop', env=env)
        self.assertEqual(status.returncode, 3)
        self.assertIn('Not installed', status.stdout)
        self.assertEqual(stop.returncode, 0)
        self.assertIn('nothing to stop', stop.stdout)


class ClickToRunTests(unittest.TestCase):
    EXPECTED = {'Start': 'start', 'Stop': 'stop', 'Status': 'status', 'Uninstall': 'uninstall'}

    def test_each_command_file_is_executable_and_runs_the_right_subcommand(self):
        for label, subcommand in self.EXPECTED.items():
            path = SCRIPTS / f'FinHub Daemon - {label}.command'
            with self.subTest(file=path.name):
                self.assertTrue(path.exists(), f'{path.name} is missing')
                self.assertTrue(path.stat().st_mode & stat.S_IXUSR, f'{path.name} is not executable')
                text = path.read_text()
                self.assertTrue(text.startswith('#!/bin/bash'))
                self.assertIn('cd "$(dirname "$0")"', text)  # works wherever the folder lives
                self.assertIn(f'./daemon.sh {subcommand}\n', text)
                self.assertIn('read -n 1', text)  # keeps the Terminal window open to read the result

    def test_the_daemon_script_is_executable(self):
        self.assertTrue(DAEMON.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run(['bash', '-n', str(DAEMON)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class DockerFilesTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / name).read_text()

    def test_dockerfile_runs_as_non_root_with_a_data_volume_and_the_http_server(self):
        text = self.read('Dockerfile')
        self.assertRegex(text, r'(?m)^USER finhub$')
        self.assertIn('FINHUB_ALERT_STORE=/data/alerts.json', text)
        self.assertIn('VOLUME ["/data"]', text)
        self.assertIn('"--transport", "streamable-http"', text)
        self.assertIn('/healthz', text)
        self.assertNotRegex(text, r'(?m)^(COPY|ADD) .*\.env')

    def test_secrets_and_local_state_are_kept_out_of_the_build_context(self):
        ignored = set(self.read('.dockerignore').split())
        for item in ('.env', 'alerts.json', '.venv', '.git'):
            self.assertIn(item, ignored)

    def test_vendored_vnstock_is_ignored_by_git_but_the_folder_is_kept(self):
        gitignore = self.read('.gitignore')
        self.assertIn('docker/vendor/*', gitignore)
        self.assertIn('!docker/vendor/.gitkeep', gitignore)
        self.assertTrue((ROOT / 'docker' / 'vendor' / '.gitkeep').exists())

    def test_compose_is_locked_down(self):
        text = self.read('docker-compose.yml')
        self.assertIn('${FINHUB_HTTP_TOKEN:?', text)  # no token, no start
        self.assertRegex(text, r'"127\.0\.0\.1:\$\{FINHUB_PORT:-8000\}:8000"')  # never published on 0.0.0.0
        self.assertEqual(text.count('read_only: true'), 2)
        self.assertEqual(text.count('cap_drop: [ALL]'), 2)
        self.assertEqual(text.count('no-new-privileges:true'), 2)
        self.assertIn('uid=10001,gid=10001', text)  # vnstock writes ~/.vnstock
        daemon = text.split('  daemon:')[1]
        self.assertIn('FINHUB_HTTP_TOKEN: ""', daemon)  # the daemon does not receive the HTTP token

    @unittest.skipUnless(shutil.which('docker'), 'docker CLI not available')
    def test_docker_compose_accepts_the_file_and_requires_a_token(self):
        env = {k: v for k, v in os.environ.items() if k != 'FINHUB_HTTP_TOKEN'}
        missing = subprocess.run(['docker', 'compose', 'config', '--quiet'], cwd=ROOT, capture_output=True,
                                 text=True, env={**env, 'COMPOSE_ENV_FILES': '/dev/null'}, timeout=60)
        if 'compose' in missing.stderr and 'not a docker command' in missing.stderr:
            self.skipTest('docker compose plugin not available')
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn('FINHUB_HTTP_TOKEN', missing.stderr)
        ok = subprocess.run(['docker', 'compose', 'config', '--quiet'], cwd=ROOT, capture_output=True, text=True,
                            env={**env, 'FINHUB_HTTP_TOKEN': 'a-valid-token-0123456789'}, timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr)


class FreezeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('freeze_vnstock', SCRIPTS / 'freeze_vnstock.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_collect_returns_real_files_inside_site_packages_only(self):
        entries = self.module.collect('defusedxml')
        arcnames = [arc for _, arc in entries]
        self.assertTrue(any(a.startswith('defusedxml/') for a in arcnames))
        self.assertTrue(any('.dist-info/' in a for a in arcnames))
        for path, arc in entries:
            self.assertTrue(path.is_file())
            self.assertNotIn('__pycache__', arc)
            self.assertFalse(arc.endswith('.pyc'))
            self.assertFalse(arc.startswith('..'))

    def test_it_only_packs_the_vnstock_distributions(self):
        self.assertEqual(self.module.DISTRIBUTIONS, ('vnstock', 'vnai'))
        self.assertEqual(self.module.OUTPUT.name, 'vnstock-site-packages.tar.gz')
        self.assertEqual(self.module.OUTPUT.parent, ROOT / 'docker' / 'vendor')

    def test_it_explains_itself_when_vnstock_is_missing(self):
        from importlib import metadata
        with patch.object(self.module.metadata, 'version', side_effect=metadata.PackageNotFoundError('vnstock')), \
                patch('sys.stderr') as err:
            self.assertEqual(self.module.main(), 1)
        self.assertTrue(err.write.called)

    def test_it_refuses_a_non_4x_version(self):
        with patch.object(self.module.metadata, 'version', return_value='6.0.0'), patch('sys.stderr'):
            self.assertEqual(self.module.main(), 1)


class OptionalVnstockTests(unittest.TestCase):
    def test_missing_vnstock_raises_a_clear_error_from_silent_import(self):
        missing = ModuleNotFoundError("No module named 'vnstock'", name='vnstock')
        with patch('importlib.import_module', side_effect=missing):
            with self.assertRaises(VnstockUnavailableError) as caught:
                providers.silent_import('vnstock.api.quote', 'Quote')
        message = str(caught.exception)
        self.assertIn("optional 'vnstock'", message)
        self.assertIn('README', message)
        self.assertIn('Crypto', message)

    def test_a_missing_submodule_of_vnstock_counts_as_missing(self):
        missing = ModuleNotFoundError("No module named 'vnstock.api'", name='vnstock.api')
        with patch('importlib.import_module', side_effect=missing), self.assertRaises(VnstockUnavailableError):
            providers.silent_import('vnstock.api.quote', 'Quote')

    def test_other_missing_modules_are_not_disguised(self):
        other = ModuleNotFoundError("No module named 'seaborn'", name='seaborn')
        with patch('importlib.import_module', side_effect=other), self.assertRaises(ModuleNotFoundError) as caught:
            providers.silent_import('vnstock.api.quote', 'Quote')
        self.assertNotIsInstance(caught.exception, VnstockUnavailableError)

    def test_vn_stock_helper_reports_missing_vnstock(self):
        missing = ModuleNotFoundError("No module named 'vnstock'", name='vnstock')
        with patch('importlib.import_module', side_effect=missing), self.assertRaises(VnstockUnavailableError):
            providers.vn_stock('FPT')

    def test_the_error_reaches_the_user_as_a_provider_failure_with_the_explanation(self):
        import asyncio

        async def call():
            with patch('finhub_mcp.tools.vn_stock.fetch_vn_latest', side_effect=VnstockUnavailableError()):
                result = await server.mcp.call_tool('get_vn_stock_price', {'symbol': 'FPT'})
            return json.loads(result[1]['result'])

        providers.reset_cooldowns()
        payload = asyncio.run(call())
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['meta']['error_type'], 'provider_failure')
        self.assertIn("optional 'vnstock'", payload['error'])

    def test_non_vietnam_tools_do_not_need_vnstock(self):
        blocked = {'vnstock': None}
        with patch.dict(sys.modules, blocked):
            from finhub_mcp import derivatives, news, worldmarkets  # noqa: F401
            self.assertTrue(callable(news.collect_news))


class AlertStoreOverrideTests(unittest.TestCase):
    def test_environment_variable_overrides_the_configured_store(self):
        with patch.dict(os.environ, {'FINHUB_ALERT_STORE': '/data/alerts.json'}):
            self.assertEqual(config.get_alert_store_path(), '/data/alerts.json')

    def test_config_json_is_used_when_the_variable_is_unset_or_empty(self):
        for value in (None, ''):
            env = {k: v for k, v in os.environ.items() if k != 'FINHUB_ALERT_STORE'}
            if value is not None:
                env['FINHUB_ALERT_STORE'] = value
            with patch.dict(os.environ, env, clear=True):
                self.assertTrue(config.get_alert_store_path().endswith('alerts.json'))
                self.assertTrue(Path(config.get_alert_store_path()).is_absolute())

    def test_tilde_in_the_override_is_not_silently_relative(self):
        with patch.dict(os.environ, {'FINHUB_ALERT_STORE': '~/finhub/alerts.json'}):
            path = Path(config.get_alert_store_path())
        self.assertTrue(path.is_absolute())
        self.assertNotIn('~', str(path))

    def test_alert_module_picks_up_the_override_at_import(self):
        self.assertTrue(alerts.ALERTS_FILE)
        self.assertEqual(alerts.ALERTS_LOCK_FILE, f'{alerts.ALERTS_FILE}.lock')


if __name__ == '__main__':
    unittest.main()
