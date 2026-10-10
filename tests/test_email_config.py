import logging
import zipfile

import pytest

import main
from gunb_tool.config import ConfigError, load_config
from gunb_tool.migration import export_state
from gunb_tool.storage import LeadRepository


ENV = dict(SMTP_HOST='smtp.example.org', SMTP_FROM_ADDRESS='reports@example.org',
           SMTP_USERNAME='SHRT!', SMTP_PASSWORD='private-pass')
ACTIVE = '''email:
  enabled: true
  host: ${SMTP_HOST:-}
  from_address: ${SMTP_FROM_ADDRESS:-}
'''


def config_file(tmp_path, text):
    path = tmp_path / 'config.yaml'
    path.write_text("gunb:\n  voivodeships: ['30']\nstorage:\n  db_path: state.sqlite\n" + text, encoding='utf-8')
    return path


def test_default_has_no_smtp(tmp_path):
    assert load_config(config_file(tmp_path, ''), env={}).email.smtp is None


@pytest.mark.parametrize('tls,port', [('starttls', 587), ('implicit', 465)])
def test_active_smtp_is_validated_and_private(tmp_path, tls, port):
    cfg = load_config(config_file(tmp_path, ACTIVE + f'  tls: {tls}\n  port: {port}\n'), env=ENV)
    assert cfg.email.enabled and cfg.email.smtp.tls == tls
    assert cfg.email.smtp.port == port
    assert cfg.email.smtp.username == ENV['SMTP_USERNAME']
    assert all(value not in repr(cfg) for value in ENV.values())


@pytest.mark.parametrize('missing', ['SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_HOST', 'SMTP_FROM_ADDRESS'])
def test_active_requires_complete_configuration_without_private_errors(tmp_path, missing):
    env = {**ENV, missing: ''}
    with pytest.raises(ConfigError, match=r'email\.enabled') as exc:
        load_config(config_file(tmp_path, ACTIVE), env=env)
    assert all(value not in str(exc.value) for value in ENV.values())


@pytest.mark.parametrize('extra', ['port: 0', 'port: true', 'tls: none', 'timeout_seconds: 11',
                                  'username: secret', 'password: secret', 'unknown: secret'])
def test_invalid_or_yaml_credentials_rejected(tmp_path, extra):
    with pytest.raises(ConfigError, match=r'email\.'):
        load_config(config_file(tmp_path, ACTIVE + '  ' + extra + '\n'), env=ENV)


def test_disabled_still_rejects_yaml_credentials(tmp_path):
    with pytest.raises(ConfigError, match=r'email\.'):
        load_config(config_file(tmp_path, 'email:\n  enabled: false\n  password: secret\n'), env={})


def test_short_private_values_masked_in_tracebacks():
    formatter = main.RedactingFormatter('%(message)s', secrets=tuple(ENV.values()))
    record = logging.LogRecord('test', logging.ERROR, '', 0, 'login SHRT! private-pass', (), None)
    assert 'SHRT!' not in formatter.format(record)
    assert 'private-pass' not in formatter.format(record)


def test_active_email_rejects_unsupported_history_window(tmp_path):
    with pytest.raises(ConfigError, match='notifications.max_age_days'):
        load_config(config_file(tmp_path, ACTIVE + 'notifications:\n  max_age_days: 366\n'), env=ENV)


def test_export_removes_smtp_environment_values(tmp_path, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    path = config_file(tmp_path, ACTIVE)
    (tmp_path / '.env').write_text('\n'.join(f'{k}={v}' for k, v in ENV.items()), encoding='utf-8')
    with LeadRepository(tmp_path / 'state.sqlite'):
        pass
    package = export_state(path, tmp_path / 'export')
    with zipfile.ZipFile(package) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            assert all(value.encode() not in data for value in ENV.values())


def test_invalid_enabled_never_exposes_interpolated_secret(tmp_path, monkeypatch, capsys):
    path = config_file(tmp_path, ACTIVE.replace('enabled: true', 'enabled: ${SMTP_PASSWORD}'))
    with pytest.raises(ConfigError, match=r'email\.enabled') as exc:
        load_config(path, env=ENV)
    assert ENV['SMTP_PASSWORD'] not in str(exc.value)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert main.main(['--config', str(path), '--stats']) == 2
    assert ENV['SMTP_PASSWORD'] not in capsys.readouterr().err
    assert not (tmp_path / 'state.sqlite').exists()


@pytest.mark.parametrize('port,env', [('${SMTP_PORT}', dict(SMTP_PORT='587')),
                                    ('${SMTP_PORT:-587}', {}), ('${SMTP_PORT}', dict(SMTP_PORT='465'))])
def test_port_and_timeout_from_environment(tmp_path, port, env):
    path = config_file(tmp_path, ACTIVE + f'  port: {port}\n  timeout_seconds: ${{SMTP_TIMEOUT:-2.5}}\n')
    cfg = load_config(path, env={**ENV, **env})
    assert cfg.email.smtp.port == int(env.get('SMTP_PORT', '587'))
    assert cfg.email.smtp.timeout_seconds == 2.5


@pytest.mark.parametrize('field,value', [('port', 'true'), ('port', '587.0'), ('port', '-1'),
                                        ('port', '65536'), ('port', '1e3'), ('port', 'bad'),
                                        ('timeout_seconds', 'nan'), ('timeout_seconds', 'inf'),
                                        ('timeout_seconds', '-1'), ('timeout_seconds', 'true')])
def test_invalid_smtp_environment_numbers_fail_closed(tmp_path, field, value):
    path = config_file(tmp_path, ACTIVE + f'  {field}: ${{SMTP_NUMBER}}\n')
    with pytest.raises(ConfigError, match=r'email\.enabled'):
        load_config(path, env={**ENV, 'SMTP_NUMBER': value})
