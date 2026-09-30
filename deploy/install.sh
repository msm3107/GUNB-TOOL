#!/usr/bin/env bash
# Żółta Tablica (GUNB-TOOL) – instalacja, aktualizacja i wycofanie wersji bota na serwerze Linux
# (Ubuntu 24.04 LTS; także Debian 12+, Ubuntu 22.04+, Raspberry Pi OS Bookworm+; wystarczy 512 MB RAM).
#
# Nowy bot na czystym serwerze – pyta o token, pobiera dane GUNB i uruchamia bota:
#   curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/main/deploy/install.sh | sudo bash
# Serwer pod przeniesienie działającego bota – instaluje wszystko, ale niczego nie uruchamia, nie pobiera
# danych i nie wysyła wiadomości (dalsze kroki: docs/WDROZENIE.md):
#   curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/main/deploy/install.sh | sudo bash -s -- --przygotuj
# Na zainstalowanym serwerze:  sudo gunb-admin aktualizuj <wersja>   ·   sudo gunb-admin wycofaj
#
# Układ na serwerze:
#   /opt/gunb-tool/releases/<id>/  kod jednej wersji z własnym .venv (właściciel root – usługa tylko czyta)
#   /opt/gunb-tool/current         dowiązanie do działającej wersji (przełączane w jednym kroku)
#   /opt/gunb-tool/previous        poprzednia wersja – do niej wraca „gunb-admin wycofaj”
#   /var/lib/gunb-tool/            stan: config.yaml, .env (sekrety, 600), data/ (baza, kopie, eksport)
# Aktualizacja nie zmienia plików pod działającym procesem: nowa wersja powstaje obok, bot staje, powstaje
# kopia bazy i dopiero wtedy przełącza się „current”. Gdy zależności się nie zainstalują albo nowa wersja
# nie przyjmie obecnej konfiguracji, aktualizacja przerywa się, zanim cokolwiek zmieni – bot działa dalej.
set -euo pipefail

APP_USER=gunb
APP_ROOT=/opt/gunb-tool
STATE_DIR=/var/lib/gunb-tool
CONFIG=$STATE_DIR/config.yaml
MARKER=$STATE_DIR/PRZENIESIENIE-W-TOKU
SERVICE=gunb-bot
HEALTH_TIMER=gunb-zdrowie.timer
KEEP_RELEASES=3
DEFAULT_REPO=https://github.com/msm3107/GUNB-TOOL.git
REPO_URL=${GUNB_REPO_URL:-}
REF=main
MODE=install
RELEASE=""
RELEASE_ID=""
STAGE=""
APT=(apt-get -qq -o DPkg::Lock::Timeout=600)   # świeży VPS: czekaj, aż skończą się automatyczne aktualizacje

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '  Uwaga: %s\n' "$*" >&2; }
die() { printf '\n\033[1;31mBłąd:\033[0m %s\n' "$*" >&2; exit 1; }
as_app() { runuser -u "$APP_USER" -- "$@"; }
cleanup() { if [ -n "$STAGE" ]; then rm -rf -- "$STAGE"; fi; }
trap cleanup EXIT

usage() {
    cat <<'EOF'
Użycie: install.sh [--przygotuj] [--wersja <tag|gałąź|commit>] [--repo <adres repozytorium>]
        install.sh --wycofaj     powrót do poprzedniej wersji kodu (sprawdza zgodność ze schematem bazy)
        install.sh --sekrety     token bota i ID administratora w /var/lib/gunb-tool/.env
  --przygotuj   serwer pod przeniesienie bota z innego komputera: bez uruchamiania bota,
                bez pobierania danych GUNB i bez wiadomości; dalej: gunb-admin importuj / sekrety / start
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --przygotuj) MODE=prepare ;;
            --wycofaj) MODE=rollback ;;
            --sekrety) MODE=secrets ;;
            --wersja)
                [ $# -ge 2 ] || die "--wersja: podaj tag, gałąź albo commit"
                REF=$2
                shift ;;
            --repo)
                [ $# -ge 2 ] || die "--repo: podaj adres repozytorium albo ścieżkę"
                REPO_URL=$2
                shift ;;
            -h|--pomoc|--help) usage; exit 0 ;;
            *) usage >&2; die "nieznana opcja: $1" ;;
        esac
        shift
    done
}

# --- Python wybranej wersji (zawsze z konta usługi, więc pliki w katalogu stanu nie zmieniają właściciela) ---

release_python() {  # release_python <katalog wersji> <argumenty pythona...>
    local release=$1
    shift
    (cd "$STATE_DIR" && as_app env PYTHONPATH="$release" PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
        "$release/.venv/bin/python" "$@")
}

schema_of_release() {
    release_python "$1" -c 'from gunb_tool.storage import SCHEMA_VERSION; print(SCHEMA_VERSION)'
}

database_path() {  # database_path <katalog wersji>: storage.db_path z konfiguracji w katalogu stanu
    release_python "$1" -c 'import sys; from gunb_tool.config import load_config; print(load_config(sys.argv[1]).storage.db_path)' "$CONFIG"
}

schema_of_database() {  # schema_of_database <katalog wersji> <baza>: numer schematu albo -1 (brak bazy); tylko odczyt
    release_python "$1" - "$2" <<'PY'
import sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    print(-1)
    sys.exit()
conn = sqlite3.connect(path)
conn.execute("PRAGMA query_only = ON")
print(conn.execute("PRAGMA user_version").fetchone()[0])
PY
}

env_is_set() { [ -f "$STATE_DIR/.env" ] && release_python "$1" -m gunb_tool.envfile jest "$STATE_DIR/.env" "$2"; }

# --- Instalacja ---------------------------------------------------------------------------------------

install_packages() {
    say "Pakiety systemowe (python3, venv, git)"
    "${APT[@]}" update
    DEBIAN_FRONTEND=noninteractive "${APT[@]}" install -y python3 python3-venv git ca-certificates tzdata curl >/dev/null
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
        || die "Potrzebny Python 3.10+ – użyj Ubuntu 22.04/24.04, Debiana 12 albo nowszych."
}

ensure_account() {
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        say "Konto usługi: $APP_USER (bez logowania i bez uprawnień administratora)"
        useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    fi
}

refuse_old_layout() {
    if [ -d "$APP_ROOT/.git" ]; then
        die "W $APP_ROOT jest instalacja w starym układzie (kod i dane razem) – ten instalator jej nie nadpisze.
  Przenieś ją tak jak bota z komputera (docs/WDROZENIE.md): zatrzymaj usługę ($SERVICE), zmień nazwę
  katalogu na $APP_ROOT.stary, uruchom instalator z --przygotuj, a eksport zrób poleceniem
  sudo gunb-admin eksport --config $APP_ROOT.stary/config.yaml"
    fi
}

fetch_release() {  # pobiera wersję REF do /opt/gunb-tool/releases/<data>-<commit>; ustawia RELEASE i RELEASE_ID
    local commit git_opts=()
    [ -n "$REPO_URL" ] || REPO_URL=$(cat "$APP_ROOT/repo" 2>/dev/null || echo "$DEFAULT_REPO")
    if [ -d "$REPO_URL" ]; then
        git_opts=(-c "safe.directory=*")  # lokalne repozytorium (np. próba instalacji) należy do innego konta
    fi
    say "Kod aplikacji: wersja '$REF' z $REPO_URL"
    install -d -m 755 "$APP_ROOT" "$APP_ROOT/releases"
    STAGE=$(mktemp -d "$APP_ROOT/releases/.pobieranie-XXXXXX")
    git "${git_opts[@]}" clone --quiet "$REPO_URL" "$STAGE/kod"
    commit=$(git -C "$STAGE/kod" rev-parse --verify --quiet "origin/$REF^{commit}" \
        || git -C "$STAGE/kod" rev-parse --verify --quiet "$REF^{commit}") \
        || die "Nie ma wersji '$REF' w $REPO_URL (podaj tag, gałąź albo commit)"
    git -C "$STAGE/kod" checkout --quiet --detach "$commit"
    [ -f "$STAGE/kod/deploy/gunb-admin" ] \
        || die "Wersja '$REF' jest starsza niż układ z /opt/gunb-tool/releases – tym instalatorem jej nie zainstalujesz"
    RELEASE_ID="$(date -u +%Y%m%d-%H%M%S)-$(git -C "$STAGE/kod" rev-parse --short=10 HEAD)"
    RELEASE=$APP_ROOT/releases/$RELEASE_ID
    mv "$STAGE/kod" "$RELEASE"
    git -C "$RELEASE" log -1 --format='  wersja: %h z %cd – %s' --date=short
}

build_release() {
    say "Środowisko Python i zależności (dokładne wersje z requirements.lock)"
    python3 -m venv "$RELEASE/.venv"
    export PIP_ROOT_USER_ACTION=ignore  # kod wersji należy do roota celowo (usługa tylko go czyta)
    local pip=("$RELEASE/.venv/bin/python" -m pip --quiet --disable-pip-version-check --no-cache-dir)
    if [ -f "$RELEASE/requirements.lock" ]; then
        "${pip[@]}" install --require-virtualenv --no-deps -r "$RELEASE/requirements.lock"
    else
        warn "ta wersja nie ma requirements.lock – instaluję zakresy z requirements.txt"
        "${pip[@]}" install --require-virtualenv -r "$RELEASE/requirements.txt"
    fi
    "${pip[@]}" check >/dev/null || die "Zależności nowej wersji są niespójne (pip check) – działająca wersja zostaje"
    "$RELEASE/.venv/bin/python" -m compileall -q "$RELEASE/gunb_tool" "$RELEASE/main.py" >/dev/null
    chown -R root:root "$RELEASE"
    chmod -R go-w "$RELEASE"
}

prepare_state() {
    say "Katalog stanu: $STATE_DIR (config.yaml, .env, data/)"
    install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$STATE_DIR" "$STATE_DIR/data"
    if [ ! -f "$CONFIG" ]; then
        install -o "$APP_USER" -g "$APP_USER" -m 640 "$RELEASE/config.yaml" "$CONFIG"
        echo "  config.yaml: domyślna konfiguracja tej wersji (przy przenoszeniu bota zastąpi ją ta z pakietu)"
    fi
    if [ -f "$STATE_DIR/.env" ]; then
        chown "$APP_USER:$APP_USER" "$STATE_DIR/.env"
        chmod 600 "$STATE_DIR/.env"
    fi
    release_python "$RELEASE" -c 'import sys; from gunb_tool.config import load_config; load_config(sys.argv[1])' "$CONFIG" \
        || die "Nowa wersja nie przyjmuje konfiguracji $CONFIG (błąd powyżej) – działająca wersja zostaje bez zmian"
}

install_system_files() {  # install_system_files <katalog wersji>: jednostki systemd i gunb-admin tej wersji
    local release=$1
    install -m 644 "$release/deploy/gunb-bot.service" "/etc/systemd/system/$SERVICE.service"
    install -m 644 "$release/deploy/gunb-zdrowie.service" /etc/systemd/system/gunb-zdrowie.service
    install -m 644 "$release/deploy/gunb-zdrowie.timer" "/etc/systemd/system/$HEALTH_TIMER"
    install -m 755 "$release/deploy/gunb-admin" /usr/local/bin/gunb-admin
    systemctl daemon-reload
}

switch_to_release() {  # zatrzymuje bota, robi kopię bazy i przełącza „current”; ustawia WAS_ACTIVE
    local previous db
    previous=$(readlink -e "$APP_ROOT/current" 2>/dev/null || true)
    WAS_ACTIVE=0
    if systemctl is-active --quiet "$SERVICE"; then
        WAS_ACTIVE=1
        say "Zatrzymuję bota – kończy bieżący krok (do 90 s)"
        systemctl stop "$SERVICE"
    fi
    db=$(database_path "$RELEASE")
    if [ -f "$db" ]; then
        say "Kopia bazy przed zmianą wersji"
        if ! release_python "$RELEASE" -m gunb_tool.migration kopia --config "$CONFIG" --etykieta "przed-$RELEASE_ID"; then
            if [ "$WAS_ACTIVE" = 1 ]; then systemctl start "$SERVICE"; fi
            die "Kopia bazy nie powstała – wersja nie została zmieniona (bot działa dalej na poprzedniej)"
        fi
    fi
    if [ -n "$previous" ] && [ "$previous" != "$RELEASE" ]; then
        ln -sfn "$previous" "$APP_ROOT/previous.nowe"
        mv -Tf "$APP_ROOT/previous.nowe" "$APP_ROOT/previous"
    fi
    ln -sfn "$RELEASE" "$APP_ROOT/current.nowe"
    mv -Tf "$APP_ROOT/current.nowe" "$APP_ROOT/current"
    printf '%s\n' "$REPO_URL" > "$APP_ROOT/repo"
    install_system_files "$RELEASE"
    echo "  działająca wersja: $RELEASE_ID"
}

wait_until_running() {
    local attempt
    for attempt in $(seq 1 20); do
        sleep 1
        systemctl is-active --quiet "$SERVICE" || break
        [ "$attempt" -lt 20 ] || { echo "  ✔ bot działa"; return 0; }
    done
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "Bot nie wystartował (dziennik powyżej). Powrót do poprzedniej wersji: sudo gunb-admin wycofaj"
}

start_bot() {
    systemctl enable --quiet "$SERVICE"
    systemctl restart "$SERVICE"
    wait_until_running
    if env_is_set "$RELEASE" HEALTHCHECK_PING_URL; then
        systemctl enable --quiet --now "$HEALTH_TIMER"
    fi
}

prune_releases() {  # zostają: działająca, poprzednia i KEEP_RELEASES najnowszych
    local current previous release count=0
    current=$(readlink -e "$APP_ROOT/current" 2>/dev/null || true)
    previous=$(readlink -e "$APP_ROOT/previous" 2>/dev/null || true)
    while IFS= read -r release; do
        count=$((count + 1))
        if [ "$count" -gt "$KEEP_RELEASES" ] && [ "$release" != "$current" ] && [ "$release" != "$previous" ]; then
            rm -rf -- "$release"
        fi
    done < <(find "$APP_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -name '2*' | sort -r)
}

# --- Sekrety ------------------------------------------------------------------------------------------

ask() {  # ask <zmienna> <pytanie> <wzorzec> [domyślna]: pyta do skutku (z /dev/tty – stdin to ten skrypt)
    local answer
    while true; do
        read -r -p "$2${4:+ [Enter = $4]}: " answer </dev/tty
        answer=${answer//[[:space:]]/}
        [ -n "$answer" ] || answer=${4:-}
        if [[ $answer =~ $3 ]]; then
            printf -v "$1" '%s' "$answer"
            return
        fi
        echo "  To nie wygląda poprawnie – spróbuj jeszcze raz."
    done
}

check_token() {  # wypisuje nazwę bota; kod 1 = Telegram odrzucił token, 2 = brak połączenia
    (export GUNB_TOKEN="$2"; release_python "$1" - <<'PY'
import os, sys
import requests
try:
    response = requests.get(f"https://api.telegram.org/bot{os.environ['GUNB_TOKEN']}/getMe", timeout=20)
except requests.RequestException:
    sys.exit(2)
if response.status_code in (401, 404):
    sys.exit(1)
if not response.ok:
    sys.exit(2)
print(f"  ✔ bot: @{response.json()['result']['username']}")
PY
    )
}

env_value() {  # wartość zmiennej niebędącej sekretem (np. ID czatu) – do podpowiedzi w pytaniu
    sed -n "s/^$1=//p" "$STATE_DIR/.env" 2>/dev/null | tail -n 1 | tr -d "'\""
}

setup_secrets() {  # setup_secrets <katalog wersji>; bez terminala: TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID ze środowiska
    local release=$1 token=${TELEGRAM_BOT_TOKEN:-} chat_id=${TELEGRAM_CHAT_ID:-} rc
    say "Dane bota Telegram (zostają tylko na tym serwerze, w $STATE_DIR/.env)"
    while true; do
        [ -n "$token" ] || ask token "Token bota od @BotFather (123456789:AAH...)" '^[0-9]+:[A-Za-z0-9_-]{30,}$'
        rc=0
        check_token "$release" "$token" || rc=$?
        case $rc in
            0) break ;;
            1) echo "  Telegram nie zna tego tokenu – skopiuj go jeszcze raz od @BotFather."; token="" ;;
            *) echo "  Nie mogę teraz połączyć się z Telegramem – zapisuję token bez sprawdzenia."; break ;;
        esac
    done
    [ -n "$chat_id" ] || ask chat_id "Twój ID w Telegramie – administrator bota (liczba; podaje ją @userinfobot)" \
        '^-?[0-9]{5,}$' "$(env_value TELEGRAM_CHAT_ID)"
    (
        export GUNB_SET_TELEGRAM_BOT_TOKEN="$token" GUNB_SET_TELEGRAM_CHAT_ID="$chat_id"
        # Ten sam czat: administrator abonamentów i alerty o awariach – chyba że już są ustawione osobno.
        if [ -z "$(env_value ADMIN_CHAT_ID)" ]; then export GUNB_SET_ADMIN_CHAT_ID="$chat_id"; fi
        if [ -z "$(env_value TELEGRAM_ADMIN_CHAT_ID)" ]; then export GUNB_SET_TELEGRAM_ADMIN_CHAT_ID="$chat_id"; fi
        release_python "$release" -m gunb_tool.envfile ustaw "$STATE_DIR/.env"
    )
}

# --- Tryby --------------------------------------------------------------------------------------------

install_or_update() {
    local fresh=0 db
    refuse_old_layout
    install_packages
    ensure_account
    fetch_release
    build_release
    prepare_state
    db=$(database_path "$RELEASE")
    if [ "$MODE" = prepare ]; then
        [ ! -f "$db" ] || die "Ten serwer ma już bazę bota ($db) – --przygotuj jest dla nowego serwera.
  Aktualizacja: sudo gunb-admin aktualizuj <wersja>"
        printf 'Serwer przygotowany pod przeniesienie bota (%s).\nNastępne kroki: docs/WDROZENIE.md\n' \
            "$(date -u +%Y-%m-%dT%H:%MZ)" > "$MARKER"
    elif [ -e "$MARKER" ] && [ ! -f "$db" ]; then
        die "Ten serwer czeka na dane bota z komputera – najpierw: sudo gunb-admin importuj <pakiet>
  (nowy, pusty bot na tym samym tokenie 'zgubiłby' wszystkich użytkowników)"
    elif [ ! -f "$db" ]; then
        fresh=1
        env_is_set "$RELEASE" TELEGRAM_BOT_TOKEN || setup_secrets "$RELEASE"
    fi
    switch_to_release
    prune_releases

    if [ "$MODE" = prepare ]; then
        systemctl disable --quiet "$SERVICE" 2>/dev/null || true
        cat <<EOF

✅ Serwer przygotowany. Bot NIE działa i niczego nie wysłał; nic nie zostało pobrane z GUNB.
   Dalej (docs/WDROZENIE.md, 'Przeniesienie bota z komputera'):
     sudo gunb-admin sprawdz  <pakiet.zip>
     sudo gunb-admin importuj <pakiet.zip> --konfiguracja-z-pakietu
     sudo gunb-admin sekrety
     sudo gunb-admin start          # dopiero gdy bot na komputerze jest wyłączony
EOF
        return
    fi
    if [ "$fresh" = 1 ]; then
        say "Test połączeń (ULDK, baza SQLite)"
        (cd "$STATE_DIR" && as_app env PYTHONDONTWRITEBYTECODE=1 "$RELEASE/.venv/bin/python" "$RELEASE/sanity_check.py") \
            || warn "test nie przeszedł – sprawdź, czy serwer ma dostęp do internetu"
        say "Pierwsze pobieranie danych GUNB (kilka minut – każda działka jest lokalizowana w ULDK)"
        (cd "$STATE_DIR" && as_app env TZ=Europe/Warsaw "$APP_ROOT/current/.venv/bin/python" \
            "$APP_ROOT/current/main.py" --config "$CONFIG" --fetch) \
            || warn "pobieranie nie powiodło się – bot ponowi je sam o godzinie z bot.fetch_times"
    fi
    if [ "$fresh" = 0 ] && [ "$WAS_ACTIVE" = 0 ] && ! systemctl is-enabled --quiet "$SERVICE"; then
        # np. serwer po imporcie danych, zanim bot na komputerze został wyłączony – dwa boty na jednym tokenie
        echo "  Bot był zatrzymany i zostaje zatrzymany (nowa wersja jest gotowa). Start: sudo gunb-admin start"
        return
    fi
    if env_is_set "$RELEASE" TELEGRAM_BOT_TOKEN; then
        say "Uruchamiam bota ($SERVICE)"
        start_bot
    else
        warn "brak tokenu bota – bot nie wystartował; ustaw go: sudo gunb-admin sekrety"
        return
    fi
    if [ "$fresh" = 1 ]; then
        if (cd "$STATE_DIR" && as_app "$APP_ROOT/current/.venv/bin/python" "$APP_ROOT/current/main.py" \
                --config "$CONFIG" --test-alert >/dev/null); then
            echo "  ✔ na Twój Telegram poszła wiadomość testowa kanału alertów"
        else
            warn "alert testowy nie doszedł – sprawdź TELEGRAM_ADMIN_CHAT_ID (sudo gunb-admin sekrety)"
        fi
        say "Historia (ok. 27 miesięcy – najdłuższe okno etapów budowy) dociąga się w tle (do godziny)"
        systemd-run --quiet --unit=gunb-historia --uid="$APP_USER" --gid="$APP_USER" \
            --working-directory="$STATE_DIR" --setenv=TZ=Europe/Warsaw \
            "$APP_ROOT/current/.venv/bin/python" "$APP_ROOT/current/main.py" --config "$CONFIG" --fetch --historical \
            || warn "nie udało się uruchomić importu historii – można to zrobić ręcznie (docs/WDROZENIE.md)"
        echo "  postęp: journalctl -u gunb-historia -f"
    fi
    cat <<EOF

✅ Gotowe – działa wersja $RELEASE_ID. Bot sam wstaje po awarii i po restarcie serwera.
   Jeden token = jeden działający bot: bot na innym komputerze musi być wyłączony.
   Stan za minutę:  sudo gunb-admin zdrowie        Dziennik:  sudo gunb-admin logi -f
   Poprzednia wersja (gdyby coś było nie tak):  sudo gunb-admin wycofaj
EOF
}

rollback() {
    local current previous db db_schema old_schema was_active=0
    current=$(readlink -e "$APP_ROOT/current" 2>/dev/null || true)
    previous=$(readlink -e "$APP_ROOT/previous" 2>/dev/null || true)
    [ -n "$previous" ] && [ -d "$previous" ] \
        || die "Brak zapisanej poprzedniej wersji. Wybraną wersję zainstalujesz: sudo gunb-admin aktualizuj <wersja>"
    [ "$previous" != "$current" ] || die "Poprzednia wersja jest już tą działającą."
    say "Powrót: $(basename "$current") → $(basename "$previous")"
    old_schema=$(schema_of_release "$previous")
    db=$(database_path "$previous")
    db_schema=$(schema_of_database "$previous" "$db")
    if [ "$db_schema" -gt "$old_schema" ]; then
        die "Baza ma już schemat v$db_schema, a poprzednia wersja zna najwyżej v$old_schema – sam kod nie wystarczy.
  Trzeba też wrócić z bazą do kopii sprzed aktualizacji. Wszystko, co bot zapisał po tej kopii (nowe osoby,
  abonamenty, notatki, zapisane leady, stan wysyłek), przepadnie – zostanie tylko w kopii obecnej bazy:
    sudo gunb-admin kopie                      # kopia '...-przed-$(basename "$current")-...' albo '...-przed-v$db_schema-...'
    sudo gunb-admin stop
    sudo gunb-admin odtworz <ścieżka kopii>     # obecna baza najpierw trafi do kopii 'przed-odtworzeniem'
    sudo gunb-admin wycofaj"
    fi
    if systemctl is-active --quiet "$SERVICE"; then
        was_active=1
        systemctl stop "$SERVICE"
    fi
    ln -sfn "$current" "$APP_ROOT/previous.nowe"
    mv -Tf "$APP_ROOT/previous.nowe" "$APP_ROOT/previous"
    ln -sfn "$previous" "$APP_ROOT/current.nowe"
    mv -Tf "$APP_ROOT/current.nowe" "$APP_ROOT/current"
    install_system_files "$previous"
    RELEASE=$previous
    if [ "$was_active" = 1 ]; then
        systemctl start "$SERVICE"
        wait_until_running
    fi
    echo "✅ Działa wersja $(basename "$previous"). Powrót do $(basename "$current"): sudo gunb-admin wycofaj"
}

main() {
    parse_args "$@"
    [ "$(id -u)" -eq 0 ] || die "Uruchom jako administrator (z sudo)."
    cd /
    case $MODE in
        rollback) rollback ;;
        secrets)
            [ -x "$APP_ROOT/current/.venv/bin/python" ] || die "Najpierw zainstaluj bota (install.sh)."
            RELEASE=$(readlink -e "$APP_ROOT/current")
            setup_secrets "$RELEASE"
            if systemctl is-active --quiet "$SERVICE"; then
                systemctl restart "$SERVICE"
                wait_until_running
            fi ;;
        *) install_or_update ;;
    esac
}

main "$@"
