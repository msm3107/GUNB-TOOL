#!/usr/bin/env bash
# GUNB Lead Tool – bot Telegram 24/7 na serwerze Linux
# (Debian 12+, Ubuntu 22.04+, Raspberry Pi OS Bookworm+; wystarczy 512 MB RAM).
#
# Instalacja i późniejsze aktualizacje – to samo jedno polecenie:
#   curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/main/deploy/install.sh | sudo bash
#
# Za pierwszym razem skrypt pyta o token bota i ID czatu, pobiera dane GUNB i uruchamia bota jako
# usługę systemd (sam wstaje po awarii i po restarcie serwera). Kolejne uruchomienia aktualizują kod
# i restartują bota – baza leadów, filtry i zapisane leady zostają.
#
# Bez pytań (np. automatyczna instalacja): ... | sudo TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... bash
# Inne repozytorium (fork, test lokalny):   ... | sudo GUNB_REPO_URL=<adres lub ścieżka> bash
set -euo pipefail

APP_DIR=/opt/gunb-tool
APP_USER=gunb
REPO_URL=${GUNB_REPO_URL:-https://github.com/msm3107/GUNB-TOOL.git}
INSTALL_CMD="curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/main/deploy/install.sh | sudo bash"
SERVICE=gunb-bot
DB_FILE=data/gunb_leads.sqlite   # storage.db_path z config.yaml
APT=(apt-get -qq -o DPkg::Lock::Timeout=600)   # świeży VPS: czekaj, aż skończą się automatyczne aktualizacje

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
as_app() { runuser -u "$APP_USER" -- "$@"; }

ask() {  # ask <zmienna> <pytanie> <wzorzec>: pyta do skutku (z /dev/tty – stdin to ten skrypt)
    local answer
    while true; do
        read -r -p "$2: " answer </dev/tty
        answer=${answer//[[:space:]]/}
        if [[ $answer =~ $3 ]]; then
            printf -v "$1" '%s' "$answer"
            return
        fi
        echo "  To nie wygląda poprawnie – spróbuj jeszcze raz."
    done
}

check_token() {  # wypisuje nazwę bota; kod 1 = Telegram odrzucił token, 2 = brak połączenia
    as_app env BOT_TOKEN="$1" .venv/bin/python - <<'PY'
import os, sys
import requests
try:
    response = requests.get(f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getMe", timeout=20)
except requests.RequestException:
    sys.exit(2)
if response.status_code in (401, 404):
    sys.exit(1)
if not response.ok:
    sys.exit(2)
print(f"  ✔ bot: @{response.json()['result']['username']}")
PY
}

setup_env() {
    local token=${TELEGRAM_BOT_TOKEN:-} chat_id=${TELEGRAM_CHAT_ID:-} rc
    while true; do
        [ -n "$token" ] || ask token "Token bota od @BotFather (123456789:AAH...)" '^[0-9]+:[A-Za-z0-9_-]{30,}$'
        rc=0
        check_token "$token" || rc=$?
        case $rc in
            0) break ;;
            1) echo "  Telegram nie zna tego tokenu – skopiuj go jeszcze raz od @BotFather."; token="" ;;
            *) echo "  Nie mogę teraz połączyć się z Telegramem – zapisuję token bez sprawdzenia."; break ;;
        esac
    done
    [ -n "$chat_id" ] || ask chat_id "Twój ID w Telegramie – administrator bota (liczba; podaje ją @userinfobot)" '^-?[0-9]{5,}$'
    # Ten sam czat: administrator abonamentów i alerty o awariach (GUNB/ULDK/baza) – można je potem rozdzielić.
    (umask 077 && printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\nADMIN_CHAT_ID=%s\nTELEGRAM_ADMIN_CHAT_ID=%s\n' \
        "$token" "$chat_id" "$chat_id" "$chat_id" > .env)
    chown "$APP_USER:$APP_USER" .env
}

add_admin_alerts() {  # starsze instalacje: dopisz czat alertów (ten sam co admina), jeśli go brak
    local chat_id
    if grep -q '^TELEGRAM_ADMIN_CHAT_ID=' .env; then
        return
    fi
    chat_id=$(sed -n 's/^TELEGRAM_CHAT_ID=//p' .env | head -n 1)
    if [ -n "$chat_id" ]; then
        printf 'TELEGRAM_ADMIN_CHAT_ID=%s\n' "$chat_id" >> .env
    fi
}

main() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Uruchom jako administrator (z sudo):  $INSTALL_CMD" >&2
        exit 1
    fi
    cd / || exit 1

    say "Pakiety systemowe (python3, venv, git)"
    "${APT[@]}" update
    DEBIAN_FRONTEND=noninteractive "${APT[@]}" install -y python3 python3-venv git ca-certificates tzdata >/dev/null
    if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        echo "Potrzebny Python 3.10+ – użyj Debiana 12, Ubuntu 22.04 lub Raspberry Pi OS Bookworm (albo nowszych)." >&2
        exit 1
    fi

    say "Konto usługi: $APP_USER"
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    fi

    say "Kod aplikacji: $APP_DIR"
    if [ -d "$APP_DIR/.git" ]; then
        if ! as_app git -C "$APP_DIR" pull --quiet --ff-only; then
            echo "Nie udało się pobrać aktualizacji – czy pliki w $APP_DIR były zmieniane ręcznie?" >&2
            exit 1
        fi
    else
        git clone --quiet --depth 1 "$REPO_URL" "$APP_DIR"
        chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    fi
    cd "$APP_DIR" || exit 1
    as_app git log -1 --format='  wersja: %h z %cd – %s' --date=short

    say "Środowisko Python i zależności"
    as_app python3 -m venv .venv
    as_app .venv/bin/pip install --quiet --no-cache-dir --upgrade pip
    as_app .venv/bin/pip install --quiet --no-cache-dir -r requirements.txt

    local first_install=0
    if [ ! -s .env ]; then
        say "Dane bota Telegram (zostają tylko na tym serwerze, w $APP_DIR/.env)"
        setup_env
        first_install=1
    fi
    add_admin_alerts

    say "Test połączeń (ULDK, baza SQLite)"
    as_app .venv/bin/python sanity_check.py || echo "Uwaga: test nie przeszedł – sprawdź, czy serwer ma dostęp do internetu."

    if [ ! -f "$DB_FILE" ]; then
        say "Pierwsze pobieranie danych GUNB (kilka minut – każda działka jest lokalizowana w ULDK)"
        as_app env TZ=Europe/Warsaw .venv/bin/python main.py --fetch \
            || echo "Uwaga: pobieranie nie powiodło się – bot ponowi je sam o godzinie z bot.fetch_times."
    fi

    say "Usługa systemd: $SERVICE"
    install -m 644 deploy/gunb-bot.service "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    systemctl enable --quiet "$SERVICE"
    systemctl restart "$SERVICE"
    sleep 5
    if ! systemctl is-active --quiet "$SERVICE"; then
        journalctl -u "$SERVICE" -n 30 --no-pager
        echo "Bot nie wystartował – szczegóły powyżej." >&2
        exit 1
    fi
    if [ "$first_install" = 1 ]; then
        if as_app .venv/bin/python main.py --test-alert >/dev/null; then
            echo "  ✔ na Twój Telegram poszła wiadomość testowa kanału alertów"
        else
            echo "  Uwaga: alert testowy nie doszedł – sprawdź TELEGRAM_ADMIN_CHAT_ID w $APP_DIR/.env"
        fi
        say "Historia 18 miesięcy dla przypomnień ⏰ Kiedy dzwonić – dociąga się w tle (ok. 30 min)"
        systemd-run --quiet --unit=gunb-historia --uid="$APP_USER" --gid="$APP_USER" \
            --working-directory="$APP_DIR" --setenv=TZ=Europe/Warsaw \
            "$APP_DIR/.venv/bin/python" main.py --fetch --since "$(date -d '18 months ago' +%F)" --historical \
            || echo "  Uwaga: nie udało się uruchomić importu historii – można to zrobić ręcznie (README)."
        echo "  postęp: journalctl -u gunb-historia -f"
    fi

    cat <<EOF

✅ Gotowe! Bot działa 24/7 jako usługa – sam wstaje po awarii i po restarcie serwera.
   Napisz do bota /start w Telegramie.

   Ważne: jeden token = jeden działający bot. Wyłącz bota na komputerze, inaczej będą się kłócić.

   logi na żywo:  journalctl -u $SERVICE -f
   restart:       systemctl restart $SERVICE
   aktualizacja:  $INSTALL_CMD
EOF
}

main "$@"
