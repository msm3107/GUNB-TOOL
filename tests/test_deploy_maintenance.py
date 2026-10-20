"""Real Bash functions, fake service manager; no host services/network/production data."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = (str(Path('C:/Program Files/Git/bin/bash.exe')) if sys.platform == 'win32'
        else shutil.which('bash'))
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).is_file(), reason='Bash unavailable')
SCRIPTS = ['install.sh', 'gunb-admin']


def run_script(tmp_path, script, body, *, load='loaded', active='active', failure='', final='inactive'):
    source = (ROOT / 'deploy' / script).read_text(encoding='utf-8')
    assert source.rstrip().endswith('main "$@"')
    source = source.rsplit('main "$@"', 1)[0]
    # Only declarations above; replace every external boundary before calling a function.
    harness = r'''
APP_ROOT=$PWD/root
STATE_DIR=$PWD/state
CONFIG=$STATE_DIR/config.yaml
CURRENT=$APP_ROOT/current
MARKER=$STATE_DIR/marker
RELEASE=$APP_ROOT/releases/new
RELEASE_ID=new
REPO_URL=fake-repo
STAGE=""
WORK_DIR=""
mkdir -p "$RELEASE" "$APP_ROOT/releases/old" "$STATE_DIR/data"
ln -s "$APP_ROOT/releases/old" "$CURRENT"
ln -s "$APP_ROOT/releases/old" "$APP_ROOT/previous"
touch "$STATE_DIR/data/test.sqlite"
cleanup() { :; }
need_root() { :; }
need_install() { :; }
say() { :; }
warn() { printf 'warning\n'; }
die() { printf 'blocked\n'; exit 37; }
systemctl() {
    printf 'ctl %s\n' "$*" >> events
    case $1 in
        show)
            [ "$FAILURE" != manager ] || return 1
            case $2 in
                --property=LoadState) printf '%s\n' "$LOAD" ;;
                --property=ActiveState) printf '%s\n' "$ACTIVE" ;;
                *) return 90 ;;
            esac ;;
        disable)
            [ "$FAILURE" != disable ] || return 1 ;;
        stop)
            if [ "$2" = gunb-whatsapp-webhook.service ]; then
                [ "$FAILURE" != stop ] || return 1
                ACTIVE=$FINAL
                [ "$FAILURE" != post-manager ] || FAILURE=manager
            fi ;;
        is-active) return 0 ;;
        start) [ "$2" = gunb-bot ] || return 92 ;;
        *) printf 'unexpected systemctl\n' >&2; return 91 ;;
    esac
}
database_path() { printf '%s/data/test.sqlite\n' "$STATE_DIR"; }
release_python() {
    printf 'backup\n' >> events
    [ "$FAILURE" != backup ]
}
install_system_files() { printf 'units\n' >> events; }
schema_of_release() { printf '15\n'; }
schema_of_database() { printf '15\n'; }
wait_until_running() { printf 'bot-ready\n' >> events; }
incoming() { printf 'incoming\n' >> events; printf 'synthetic-package\n'; }
app() { printf 'app %s\n' "$*" >> events; }
running() { return 1; }
'''
    # Synthetic fixed enum inputs only; no environment dump or inherited configuration.
    setup = '\n'.join(f'{key}=\'{value}\'' for key, value in
                      [('LOAD', load), ('ACTIVE', active), ('FAILURE', failure), ('FINAL', final)])
    result = subprocess.run([BASH, '--noprofile', '--norc'], input=source + harness + setup + '\n' + body,
                            text=True, encoding='utf-8', cwd=tmp_path, capture_output=True, timeout=20)
    events = (tmp_path / 'events').read_text(encoding='utf-8').splitlines() if (tmp_path / 'events').exists() else []
    return result, events


@pytest.mark.parametrize('script', SCRIPTS)
@pytest.mark.parametrize('load,active', [('not-found', 'inactive'), ('loaded', 'inactive'),
                                       ('loaded', 'active'), ('loaded', 'activating'),
                                       ('loaded', 'deactivating'), ('loaded', 'failed'), ('masked', 'inactive')])
def test_barrier_absent_is_noop_other_states_disable_stop_and_verify(tmp_path, script, load, active):
    result, events = run_script(tmp_path, script, 'quiesce_webhook\nprintf "mutation\\n" >> events',
                                load=load, active=active)
    assert result.returncode == 0, result.stderr
    if load == 'not-found':
        assert not any('disable' in e or 'stop ' in e for e in events)
    else:
        assert events.index('ctl disable --quiet gunb-whatsapp-webhook.service') < events.index(
            'ctl stop gunb-whatsapp-webhook.service') < len(events) - 1
        assert events[-2] == 'ctl show --property=ActiveState --value gunb-whatsapp-webhook.service'
    assert events[-1] == 'mutation'
    assert not any('ctl start' in e or 'ctl enable' in e for e in events)


@pytest.mark.parametrize('script', SCRIPTS)
@pytest.mark.parametrize('kwargs', [dict(failure='manager'), dict(failure='disable'), dict(failure='stop'),
                                   dict(failure='post-manager'),
                                   dict(final='active'), dict(final='deactivating'), dict(final=''),
                                   dict(load=''), dict(load='error'), dict(load='bad-setting'),
                                   dict(load='future-state'), dict(load='not-found', active='active')])
def test_barrier_errors_abort_even_when_caller_is_in_conditional(tmp_path, script, kwargs):
    result, events = run_script(tmp_path, script,
                                'if quiesce_webhook; then printf "mutation\\n" >> events; fi', **kwargs)
    assert result.returncode == 37, result.stderr
    assert 'mutation' not in events


def test_standalone_barriers_have_identical_behavioral_source():
    helpers = []
    for script in SCRIPTS:
        source = (ROOT / 'deploy' / script).read_text(encoding='utf-8')
        helpers.append(source.split('quiesce_webhook() {', 1)[1].split('\n}\n', 1)[0])
    assert helpers[0] == helpers[1]


@pytest.mark.parametrize('failure', ['', 'backup', 'stop'])
@pytest.mark.skipif(sys.platform == 'win32', reason='Real Linux release symlinks required; Git Bash copies directories')
def test_installer_switch_barrier_precedes_bot_backup_and_links(tmp_path, failure):
    result, events = run_script(tmp_path, 'install.sh', 'switch_to_release', failure=failure)
    old = tmp_path / 'root/releases/old'
    current = tmp_path / 'root/current'
    if failure == 'stop':
        assert result.returncode == 37 and 'backup' not in events
        assert 'ctl stop gunb-bot' not in events and current.resolve() == old
    else:
        assert events.index('ctl stop gunb-whatsapp-webhook.service') < events.index('ctl stop gunb-bot') < events.index('backup')
        if failure == 'backup':
            assert result.returncode == 37 and current.resolve() == old
            assert 'ctl start gunb-bot' in events and 'units' not in events
        else:
            assert result.returncode == 0 and current.resolve() == tmp_path / 'root/releases/new'
    assert not any('ctl start gunb-whatsapp' in e or 'ctl enable' in e for e in events)


@pytest.mark.skipif(sys.platform == 'win32', reason='Real Linux release symlinks required; Git Bash copies directories')
def test_installer_rollback_stops_webhook_before_bot_and_link_switch(tmp_path):
    result, events = run_script(tmp_path, 'install.sh', '''
rm "$CURRENT"
ln -s "$RELEASE" "$CURRENT"
rollback
''')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'root/current').resolve() == tmp_path / 'root/releases/old'
    assert events.index('ctl stop gunb-whatsapp-webhook.service') < events.index('ctl stop gunb-bot') < events.index('units')
    assert 'ctl start gunb-bot' in events


@pytest.mark.parametrize('command', ['stop', 'odtworz', 'importuj', 'eksport', 'wycofaj'])
@pytest.mark.parametrize('failure', ['', 'stop'])
def test_admin_maintenance_guard_precedes_side_effects(tmp_path, command, failure):
    # Fake bash target: never execute an installer or privileged operation.
    result, events = run_script(tmp_path, 'gunb-admin', f'''
bash() {{ printf 'installer %s\\n' "$*" >> events; }}
realpath() {{ printf 'different-source\\n'; }}
main {command} fake
''', failure=failure)
    mutations = [i for i, e in enumerate(events) if e.startswith(('app ', 'incoming', 'installer ', 'ctl stop gunb-bot'))]
    if failure:
        assert result.returncode == 37 and not mutations
    else:
        assert result.returncode == 0, result.stderr
        assert mutations and events.index('ctl stop gunb-whatsapp-webhook.service') < min(mutations)


def test_admin_current_code_guards_update_to_old_installer(tmp_path):
    result, events = run_script(tmp_path, 'gunb-admin', r'''
git() { case "$*" in *rev-parse*) printf 'fakecommit\n';; esac; }
mktemp() { printf '%s/work\n' "$PWD"; }
bash() { printf 'old-installer\n' >> events; }
update old-ref
''')
    assert result.returncode == 0, result.stderr
    assert events.index('ctl stop gunb-whatsapp-webhook.service') < events.index('old-installer')


@pytest.mark.parametrize('command', ['eksport --na-zywo', 'kopia', 'sprawdz fake', 'restart'])
def test_online_and_bot_restart_do_not_change_webhook(tmp_path, command):
    result, events = run_script(tmp_path, 'gunb-admin', f'''
sleep() {{ :; }}
systemctl() {{ printf 'ctl %s\\n' "$*" >> events; }}
running() {{ return 0; }}
main {command}
''')
    assert result.returncode == 0, result.stderr
    assert not any('gunb-whatsapp' in e for e in events)
