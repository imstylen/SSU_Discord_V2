#!/usr/bin/env bash
# Ubuntu 26.04 installation. Run from the uploaded project: sudo bash ./install.sh
# Sourceable functions keep system-changing operations testable with command mocks.
set +x
set -Eeuo pipefail
umask 022

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR=/opt/ssu-membership
SERVICE_USER=ssu-membership
CADDY_ROOT=/etc/caddy/Caddyfile
CADDY_SITE=/etc/caddy/ssu-membership.caddy
WORK_DIR=""
CURRENT_STEP=preflight
CADDY_PENDING=0
SSH_PORTS=()
EXPLICIT_SSH_PORTS=()
APP_HOST=""
APP_URL=""
DATABASE_PATH=""

log() { printf '\n[SSU] %s\n' "$*"; }
die() { printf '[SSU] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: sudo bash ./install.sh [--ssh-port PORT]...

Install SSU Membership on Ubuntu 26.04 under /opt/ssu-membership.
The uploaded project must contain a configured production .env.
--ssh-port PORT  Preserve an additional SSH TCP port (repeatable).
--help           Show this help without changing the server.

Existing SSH ports are also detected automatically. DNS and any cloud firewall
must already allow this server to serve the hostname in APP_URL on ports 80/443.
USAGE
}

valid_port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }

parse_args() {
    while (( $# )); do
        case "$1" in
            --ssh-port)
                (( $# >= 2 )) || die "--ssh-port requires a port number."
                valid_port "$2" || die "SSH port must be between 1 and 65535."
                EXPLICIT_SSH_PORTS+=("$((10#$2))")
                shift 2 ;;
            --help|-h) usage; exit 0 ;;
            *) die "Unknown argument. Run install.sh --help for usage." ;;
        esac
    done
}

check_env_conflict() {
    [[ -f "$SOURCE_DIR/.env" && ! -L "$SOURCE_DIR/.env" ]] || die "Provide a regular .env file beside install.sh."
    if [[ "$SOURCE_DIR" != "$INSTALL_DIR" && -f "$INSTALL_DIR/.env" ]]; then
        cmp -s -- "$SOURCE_DIR/.env" "$INSTALL_DIR/.env" || die \
            "Source .env differs from /opt/ssu-membership/.env. Reconcile them explicitly, then rerun; neither was changed."
    fi
}

check_paths() {
    local entry
    [[ "$INSTALL_DIR" != "$SOURCE_DIR/"* ]] || die "Run from a project directory outside the installation directory's ancestors."
    [[ ! -L "$INSTALL_DIR" ]] || die "The installation directory must not be a symlink."
    for entry in .env .venv data backups app bot scripts deploy; do
        [[ ! -L "$INSTALL_DIR/$entry" ]] || die "Refusing a symlink at installed $entry."
    done
    for entry in app/main.py app/config.py bot/main.py pyproject.toml requirements.lock \
        scripts/install_support.py deploy/ssu-membership-web.service \
        deploy/ssu-membership-bot.service deploy/ssu-membership-backup.service \
        deploy/ssu-membership-backup.timer; do
        [[ -f "$SOURCE_DIR/$entry" ]] || die "Required project file is missing: $entry"
    done
    check_env_conflict
}

unit_owns_pid() {
    local pid=$1 unit=$2 _hierarchy _controllers group
    [[ -r "/proc/$pid/cgroup" ]] || return 1
    while IFS=: read -r _hierarchy _controllers group; do
        if [[ "$group" == */"$unit" || "$group" == */"$unit"/* ]]; then return 0; fi
    done < "/proc/$pid/cgroup"
    return 1
}

check_listener() {
    local port=$1 unit=$2 protocol=${3:-tcp} listeners pid line
    local -a pids=()
    if [[ "$protocol" == udp ]]; then
        listeners=$(ss -H -lunp "sport = :$port")
    else
        listeners=$(ss -H -ltnp "sport = :$port")
    fi
    [[ -n "$listeners" ]] || return 0
    while IFS= read -r line; do
        [[ "$line" == *pid=* ]] || die "A listener on port $port/$protocol has no identifiable owner."
    done <<< "$listeners"
    mapfile -t pids < <(printf '%s\n' "$listeners" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    (( ${#pids[@]} )) || die "Port $port/$protocol is occupied and its owner could not be identified."
    for pid in "${pids[@]}"; do
        unit_owns_pid "$pid" "$unit" || die "Port $port/$protocol is occupied outside $unit. Stop or relocate that service first."
    done
}

check_ports() {
    check_listener 8010 ssu-membership-web.service
    check_listener 80 caddy.service
    check_listener 443 caddy.service
    check_listener 443 caddy.service udp
}

detect_ssh_ports() {
    local candidate key rest address kind _recvq _sendq
    local -a candidates=("${EXPLICIT_SSH_PORTS[@]}") connection=()
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        read -r -a connection <<< "$SSH_CONNECTION"
        if (( ${#connection[@]} == 4 )); then candidates+=("${connection[3]}"); fi
    fi
    if command -v sshd >/dev/null; then
        while read -r key candidate rest; do
            if [[ "$key" == port ]]; then candidates+=("$candidate"); fi
        done < <(sshd -T 2>/dev/null || true)
    fi
    while read -r key _recvq _sendq address rest; do
        if [[ "$rest" == *'"sshd"'* ]]; then candidates+=("${address##*:}"); fi
    done < <(ss -H -ltnp 2>/dev/null || true)
    while read -r address kind; do
        if [[ "$kind" == '(Stream)' ]]; then candidates+=("${address##*:}"); fi
    done < <(systemctl show ssh.socket --property=Listen --value 2>/dev/null \
        | grep -oE '[^[:space:]]+ \(Stream\)' || true)
    SSH_PORTS=()
    for candidate in "${candidates[@]}"; do
        if valid_port "$candidate"; then SSH_PORTS+=("$((10#$candidate))"); fi
    done
    (( ${#SSH_PORTS[@]} )) || die "Cannot determine SSH access ports. Rerun with --ssh-port PORT before enabling UFW."
    mapfile -t SSH_PORTS < <(printf '%s\n' "${SSH_PORTS[@]}" | sort -nu)
}

configure_firewall() {
    detect_ssh_ports
    local port
    for port in "${SSH_PORTS[@]}"; do
        ufw prepend allow "$port/tcp" comment 'SSU installer: preserve SSH'
    done
    ufw prepend allow 80/tcp comment 'SSU HTTP and certificate validation'
    ufw prepend allow 443/tcp comment 'SSU HTTPS'
    # Never reset UFW or remove existing rules/defaults.
    ufw --force enable
}

restore_caddy() {
    (( CADDY_PENDING )) || return 0
    if [[ -f "$WORK_DIR/Caddyfile.before" ]]; then
        cp -p -- "$WORK_DIR/Caddyfile.before" "$CADDY_ROOT"
    else
        rm -f -- "$CADDY_ROOT"
    fi
    if [[ -f "$WORK_DIR/site.before" ]]; then
        cp -p -- "$WORK_DIR/site.before" "$CADDY_SITE"
    else
        rm -f -- "$CADDY_SITE"
    fi
    CADDY_PENDING=0
}

cleanup() {
    local status=$?
    if (( CADDY_PENDING )); then restore_caddy || true; fi
    if [[ -n "$WORK_DIR" && "$WORK_DIR" == /tmp/ssu-install.* && -d "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
    return "$status"
}

failed() {
    local status=$1 line=$2
    printf '[SSU] Installation stopped during %s (line %s, exit %s).\n' "$CURRENT_STEP" "$line" "$status" >&2
    case "$CURRENT_STEP" in
        preflight|'installing Ubuntu dependencies'|'validating configuration and Python dependencies'|'preparing service account')
            printf '[SSU] See the Installer: or package error above. Application services have not been installed or changed in this run.\n' >&2 ;;
        *)
            printf '[SSU] Check: systemctl status ssu-membership-web ssu-membership-bot caddy\n' >&2
            printf '[SSU] Logs: journalctl -u ssu-membership-web -u ssu-membership-bot -u caddy -n 80\n' >&2 ;;
    esac
    exit "$status"
}

install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3.14 python3.14-venv python3.14-dev build-essential \
        ca-certificates curl gnupg debian-keyring debian-archive-keyring \
        apt-transport-https rsync ufw iproute2
    python3.14 -c 'import sys; assert sys.version_info[:2] == (3, 14)'
}

prepare_environment() {
    # Stage dependencies and validate before stopping an existing application.
    python3.14 -m venv "$WORK_DIR/venv"
    "$WORK_DIR/venv/bin/python" -m pip install --disable-pip-version-check \
        -r "$SOURCE_DIR/requirements.lock" setuptools wheel
    "$WORK_DIR/venv/bin/python" "$SOURCE_DIR/scripts/install_support.py" validate \
        --env "$SOURCE_DIR/.env" --install-dir "$INSTALL_DIR" > "$WORK_DIR/config.json"
    APP_HOST=$("$WORK_DIR/venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["hostname"])' "$WORK_DIR/config.json")
    APP_URL=$("$WORK_DIR/venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["app_url"])' "$WORK_DIR/config.json")
    DATABASE_PATH=$("$WORK_DIR/venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["database"])' "$WORK_DIR/config.json")
    "$WORK_DIR/venv/bin/python" -m pip check
    "$WORK_DIR/venv/bin/python" -c 'import fastapi, sqlalchemy, discord, httpx, audioop'
}

prepare_account() {
    if ! getent group "$SERVICE_USER" >/dev/null; then groupadd --system "$SERVICE_USER"; fi
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --gid "$SERVICE_USER" --home-dir "$INSTALL_DIR" \
            --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    [[ $(id -u "$SERVICE_USER") != 0 ]] || die "The service account must not be root."
    [[ $(getent passwd "$SERVICE_USER" | cut -d: -f7) == /usr/sbin/nologin ]] \
        || die "Existing service account must have /usr/sbin/nologin as its shell."
    [[ $(id -gn "$SERVICE_USER") == "$SERVICE_USER" ]] \
        || die "Existing service account must use the ssu-membership primary group."
    install -d -o root -g root -m 0755 "$INSTALL_DIR"
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
}

stop_and_backup() {
    local unit helper
    for unit in ssu-membership-backup.timer ssu-membership-backup.service \
        ssu-membership-bot.service ssu-membership-web.service; do
        if systemctl cat "$unit" >/dev/null 2>&1; then systemctl stop "$unit"; fi
    done
    if [[ -f "$DATABASE_PATH" ]]; then
        # Root opens only the helper source. Python runs as the service account;
        # no temporary directory or credential permissions need to be relaxed.
        helper=$(cat -- "$SOURCE_DIR/scripts/install_support.py")
        runuser -u "$SERVICE_USER" -- python3.14 -c "$helper" backup \
            --database "$DATABASE_PATH" --destination "$INSTALL_DIR/backups"
    fi
}

install_application() {
    if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
        rsync -r --safe-links --exclude='.env' --exclude='.env.*' --exclude='.venv*' \
            --exclude='.git' --exclude='.vscode' --exclude='.tools' --exclude='.pytest_cache' \
            --exclude='.ruff_cache' --exclude='__pycache__' --exclude='*.py[cod]' \
            --exclude='*.egg-info' --exclude='data' --exclude='backups' \
            --exclude='*.db*' --exclude='*.sqlite*' --exclude='*.log' \
            --exclude='dist' --exclude='build' "$SOURCE_DIR/" "$INSTALL_DIR/"
        if [[ ! -e "$INSTALL_DIR/.env" ]]; then
            install -o root -g "$SERVICE_USER" -m 0640 "$SOURCE_DIR/.env" "$INSTALL_DIR/.env"
        fi
    fi
    chown root:"$SERVICE_USER" "$INSTALL_DIR/.env"
    chmod 0640 "$INSTALL_DIR/.env"
    # Do not recurse through runtime data, virtual environments, or symlinks.
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
        ! -name data ! -name backups ! -name .venv ! -name .env \
        -exec chown -h root:root {} +
    local directory
    for directory in app bot scripts deploy; do
        find "$INSTALL_DIR/$directory" -type d -exec chown root:root {} + -exec chmod 0755 {} +
        find "$INSTALL_DIR/$directory" -type f -exec chown root:root {} + -exec chmod 0644 {} +
    done
    find "$INSTALL_DIR" -maxdepth 1 -type f ! -name .env -exec chmod 0644 {} +
    chmod 0755 "$INSTALL_DIR/install.sh"
    if [[ -d "$INSTALL_DIR/.venv" ]] && ! "$INSTALL_DIR/.venv/bin/python" -c \
        'import sys; raise SystemExit(sys.version_info[:2] != (3, 14))' 2>/dev/null; then
        mv -- "$INSTALL_DIR/.venv" "$INSTALL_DIR/.venv.pre-install.$(date -u +%Y%m%dT%H%M%S%N)"
    fi
    python3.14 -m venv "$INSTALL_DIR/.venv"
    "$INSTALL_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$INSTALL_DIR/requirements.lock"
    "$INSTALL_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-deps "$INSTALL_DIR"
    "$INSTALL_DIR/.venv/bin/python" -m pip check
    find "$INSTALL_DIR/.venv" -exec chown -h root:root {} +
    find "$INSTALL_DIR/.venv" -type d -exec chmod go-w,go+rx {} +
    find "$INSTALL_DIR/.venv" -type f -exec chmod go-w,go+r {} +
    cd -- "$INSTALL_DIR"
    runuser -u "$SERVICE_USER" -- env -i PATH=/usr/bin:/bin "$INSTALL_DIR/.venv/bin/python" -c \
        'from app.config import Settings; from app.database import Database; import bot.main; db = Database(Settings().database_url); db.initialize(); db.engine.dispose()'
}

install_caddy_package() {
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 60 \
        https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o "$WORK_DIR/caddy.key"
    gpg --batch --yes --dearmor -o "$WORK_DIR/caddy.gpg" "$WORK_DIR/caddy.key"
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 60 \
        https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o "$WORK_DIR/caddy.list"
    grep -Fq 'signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg' "$WORK_DIR/caddy.list" \
        || die "Caddy repository configuration does not reference its expected signing key."
    install -o root -g root -m 0644 "$WORK_DIR/caddy.gpg" /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    install -o root -g root -m 0644 "$WORK_DIR/caddy.list" /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
}

configure_caddy() {
    [[ ! -L "$CADDY_ROOT" && ! -L "$CADDY_SITE" ]] || die "Caddy managed config paths must not be symlinks."
    if [[ -f "$CADDY_SITE" ]] && ! grep -Fq '# Managed by SSU install.sh.' "$CADDY_SITE"; then
        die "The dedicated Caddy site file already exists and is not installer-managed; move it before rerunning."
    fi
    if [[ -f "$CADDY_ROOT" ]]; then cp -p -- "$CADDY_ROOT" "$WORK_DIR/Caddyfile.before"; fi
    if [[ -f "$CADDY_SITE" ]]; then cp -p -- "$CADDY_SITE" "$WORK_DIR/site.before"; fi
    "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/install_support.py" caddy-root \
        --root "$CADDY_ROOT" --site "$CADDY_SITE" --output "$WORK_DIR/Caddyfile.next"
    cat > "$WORK_DIR/site.next" <<CADDY
# Managed by SSU install.sh. Do not enable URL access logs here.
$APP_HOST {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8010
}
CADDY
    CADDY_PENDING=1
    install -o root -g root -m 0644 "$WORK_DIR/site.next" "$CADDY_SITE"
    if [[ -f "$CADDY_ROOT" ]]; then
        cat "$WORK_DIR/Caddyfile.next" > "$CADDY_ROOT"
    else
        install -o root -g root -m 0644 "$WORK_DIR/Caddyfile.next" "$CADDY_ROOT"
    fi
    if ! caddy validate --config "$CADDY_ROOT" --adapter caddyfile > "$WORK_DIR/caddy-validation.log" 2>&1; then
        restore_caddy
        die "Caddy configuration validation failed; previous files were restored. Check for a duplicate APP_URL site or invalid existing config."
    fi
    if ! systemctl reload-or-restart caddy; then
        restore_caddy
        systemctl reload-or-restart caddy || true
        die "Caddy reload failed; previous configuration was restored."
    fi
    CADDY_PENDING=0
    systemctl enable caddy
}

install_services() {
    local unit
    for unit in ssu-membership-web.service ssu-membership-bot.service \
        ssu-membership-backup.service ssu-membership-backup.timer; do
        install -o root -g root -m 0644 "$INSTALL_DIR/deploy/$unit" "/etc/systemd/system/$unit"
    done
    systemd-analyze verify /etc/systemd/system/ssu-membership-{web,bot,backup}.service \
        /etc/systemd/system/ssu-membership-backup.timer
    systemctl daemon-reload
    systemctl enable --now ssu-membership-web.service ssu-membership-bot.service ssu-membership-backup.timer
}

verify_installation() {
    local attempt invocation logs unit ready=0 failures=0
    for ((attempt=0; attempt<20; attempt++)); do
        if curl -fsS --max-time 3 -H "Host: $APP_HOST" http://127.0.0.1:8010/healthz \
            | grep -q '"status":"ok"'; then ready=1; break; fi
        sleep 2
    done
    (( ready )) || die "Web health check failed. Inspect journalctl -u ssu-membership-web."
    systemctl start ssu-membership-backup.service
    for unit in ssu-membership-web.service ssu-membership-bot.service ssu-membership-backup.timer caddy; do
        if ! systemctl is-active --quiet "$unit"; then log "NOT READY: $unit"; failures=1; fi
    done
    ready=0
    for ((attempt=0; attempt<15; attempt++)); do
        invocation=$(systemctl show ssu-membership-bot.service --property=InvocationID --value)
        logs=$(journalctl --no-pager -o cat "_SYSTEMD_INVOCATION_ID=$invocation" -n 100)
        if [[ "$logs" == *'Membership bot connected'* ]]; then
            if [[ "$logs" != *'ERROR'* ]]; then ready=1; fi
            break
        fi
        sleep 2
    done
    if (( ! ready )); then
        log "Bot readiness not confirmed. Check token, guild membership, intents and role permissions with journalctl -u ssu-membership-bot."
        failures=1
    fi
    ready=0
    for ((attempt=0; attempt<12; attempt++)); do
        if curl -fsS --connect-timeout 3 --max-time 5 "$APP_URL/healthz" \
            | grep -q '"status":"ok"'; then ready=1; break; fi
        sleep 3
    done
    if (( ! ready )); then
        log "Public HTTPS is not ready. Check DNS (including AAAA), cloud firewall ports 80/443, and journalctl -u caddy."
        failures=1
    fi
    (( failures == 0 )) || die "Services were installed, but readiness checks are incomplete. Fix the reported issue and rerun."
    log "Installation complete: $APP_URL/admin"
    printf 'OAuth redirect: %s/auth/discord/callback\n' "$APP_URL"
    printf 'Daily backups: %s/backups (30-day retention; copy off-server separately).\n' "$INSTALL_DIR"
}

main() {
    parse_args "$@"
    (( EUID == 0 )) || die "Run with sudo bash ./install.sh."
    [[ -f /etc/os-release ]] || die "Ubuntu 26.04 is required."
    # shellcheck source=/dev/null
    . /etc/os-release
    [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 26.04 ]] || die "This installer targets Ubuntu 26.04 only."
    [[ -d /run/systemd/system ]] || die "A running systemd host is required."
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin
    if ! command -v flock >/dev/null || ! command -v ss >/dev/null; then
        die "Install util-linux and iproute2 first."
    fi
    exec 9>/run/lock/ssu-membership-install.lock
    flock -n 9 || die "Another SSU installation is already running."
    trap 'failed "$?" "$LINENO"' ERR
    trap cleanup EXIT
    check_paths
    check_ports
    detect_ssh_ports
    WORK_DIR=$(mktemp -d /tmp/ssu-install.XXXXXXXX)
    CURRENT_STEP='installing Ubuntu dependencies'; log "$CURRENT_STEP"; install_packages
    CURRENT_STEP='validating configuration and Python dependencies'; log "$CURRENT_STEP"; prepare_environment
    CURRENT_STEP='preparing service account'; log "$CURRENT_STEP"; prepare_account
    # Check again after package installation; no unrelated listener is stopped.
    check_ports
    check_env_conflict
    CURRENT_STEP='stopping SSU and backing up existing data'; log "$CURRENT_STEP"; stop_and_backup
    CURRENT_STEP='installing application'; log "$CURRENT_STEP"; install_application
    CURRENT_STEP='installing Caddy'; log "$CURRENT_STEP"; install_caddy_package
    CURRENT_STEP='configuring firewall'; log "$CURRENT_STEP"; configure_firewall
    CURRENT_STEP='installing systemd services'; log "$CURRENT_STEP"; install_services
    CURRENT_STEP='configuring HTTPS'; log "$CURRENT_STEP"; configure_caddy
    CURRENT_STEP='checking services, backups and HTTPS'; log "$CURRENT_STEP"; verify_installation
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
