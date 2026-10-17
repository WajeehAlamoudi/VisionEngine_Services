#!/usr/bin/env bash
# Vision Engine Services: Linux operator menu and repeatable CLI commands.
set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
COMPOSE_FILE="$PROJECT_DIR/config/.generated/pipeline.compose.yaml"
cd -- "$PROJECT_DIR" || exit 1
DOCKER=()
PYTHON=""
TARGETS=()
CYAN='' GREEN='' YELLOW='' RED='' BOLD='' DIM='' RESET=''
if [[ -t 1 && -z "${NO_COLOR+x}" && "${TERM:-dumb}" != dumb ]]; then
    CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
fi
info() { printf '%s  + %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$RESET"; }
error() { printf '%s  x %s%s\n' "$RED" "$*" "$RESET" >&2; }
heading() { printf '\n%s%s  %s%s\n' "$BOLD" "$CYAN" "$*" "$RESET"; }

# Animate only quiet checks. Builds, logs, and password prompts stay visible.
# The spinner runs separately; the actual command retains its normal exit code.
run_quiet() (
    local label="$1"; shift
    if [[ ! -t 1 || "${TERM:-dumb}" == dumb || "${VISIONENGINE_NO_ANIMATION:-0}" == 1 ]]; then
        "$@"
        exit $?
    fi
    local output spinner_pid='' result
    output="$(mktemp)" || exit 1
    cleanup_spinner() {
        if [[ -n "$spinner_pid" ]]; then
            kill "$spinner_pid" 2>/dev/null || true
            wait "$spinner_pid" 2>/dev/null || true
        fi
        rm -f -- "$output"
    }
    trap cleanup_spinner EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    (
        local frame
        while true; do
            for frame in '|' '/' '-' '\'; do
                printf '\r  %s[%s]%s %s...' "$CYAN" "$frame" "$RESET" "$label"
                sleep 0.12
            done
        done
    ) &
    spinner_pid=$!
    "$@" >"$output" 2>&1
    result=$?
    kill "$spinner_pid" 2>/dev/null || true
    wait "$spinner_pid" 2>/dev/null || true
    spinner_pid=''
    printf '\r\033[2K'
    if (( result == 0 )); then
        info "$label"
    else
        error "$label failed (exit $result)."
        cat -- "$output"
    fi
    exit "$result"
)

# Keep terminal layout separate from deployment behavior. Widths apply to plain
# labels only; ANSI color escapes are printed outside padded fields.
layout_width() {
    local columns="${COLUMNS:-}"
    if [[ ! "$columns" =~ ^[0-9]+$ ]]; then
        columns="$(tput cols 2>/dev/null || printf '80')"
    fi
    [[ "$columns" =~ ^[0-9]+$ ]] || columns=80
    UI_WIDTH=$((10#$columns))
    (( UI_WIDTH <= 100 )) || UI_WIDTH=100
    (( UI_WIDTH >= 36 )) || UI_WIDTH=36
}

rule() {
    local line
    printf -v line '%*s' "$((UI_WIDTH - 4))" ''
    printf '  %s%s%s\n' "$DIM" "${line// /─}" "$RESET"
}

context_line() {
    local label="$1" value="$2" available=$((UI_WIDTH - 16))
    if (( ${#value} > available )); then value="...${value: -$((available - 3))}"; fi
    printf '  %s%-9s%s %s\n' "$DIM" "$label" "$RESET" "$value"
}

banner() {
    layout_width
    printf '\n%s%s' "$BOLD" "$CYAN"
    if (( UI_WIDTH >= 100 )); then
        cat <<'BRAND'
  ██╗   ██╗██╗███████╗██╗ ██████╗ ███╗   ██╗    ███████╗███╗   ██╗ ██████╗ ██╗███╗   ██╗███████╗
  ██║   ██║██║██╔════╝██║██╔═══██╗████╗  ██║    ██╔════╝████╗  ██║██╔════╝ ██║████╗  ██║██╔════╝
  ██║   ██║██║███████╗██║██║   ██║██╔██╗ ██║    █████╗  ██╔██╗ ██║██║  ███╗██║██╔██╗ ██║█████╗
  ╚██╗ ██╔╝██║╚════██║██║██║   ██║██║╚██╗██║    ██╔══╝  ██║╚██╗██║██║   ██║██║██║╚██╗██║██╔══╝
   ╚████╔╝ ██║███████║██║╚██████╔╝██║ ╚████║    ███████╗██║ ╚████║╚██████╔╝██║██║ ╚████║███████╗
    ╚═══╝  ╚═╝╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝    ╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝╚═╝  ╚═══╝╚══════╝
BRAND
    elif (( UI_WIDTH >= 62 )); then
        cat <<'BRAND'
  ██╗   ██╗██╗███████╗██╗ ██████╗ ███╗   ██╗
  ██║   ██║██║██╔════╝██║██╔═══██╗████╗  ██║
  ██║   ██║██║███████╗██║██║   ██║██╔██╗ ██║
  ╚██╗ ██╔╝██║╚════██║██║██║   ██║██║╚██╗██║
   ╚████╔╝ ██║███████║██║╚██████╔╝██║ ╚████║
    ╚═══╝  ╚═╝╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝
  E N G I N E   /   S E R V I C E S
BRAND
    else
        printf '  V I S I O N   E N G I N E\n  S E R V I C E S\n'
    fi
    printf '%s\n' "$RESET"
    if (( UI_WIDTH >= 62 )); then
        printf '  %s SERVICES %s  /  DOCKER COMPOSE  /  EDGE OPERATIONS\n' "$BOLD" "$RESET"
        printf '  %sConfigure. Deploy. Observe.%s\n' "$DIM" "$RESET"
    else
        printf '  %sEDGE OPERATIONS / DOCKER COMPOSE%s\n' "$DIM" "$RESET"
    fi
    rule
    context_line 'PROJECT' "$PROJECT_DIR"
    context_line 'COMPOSE' 'config/.generated/pipeline.compose.yaml'
    rule
}

menu_pair() {
    printf '  %s[%2s]%s %-26s  %s[%2s]%s %s\n' \
        "$CYAN" "$1" "$RESET" "$2" "$CYAN" "$3" "$RESET" "$4"
}

menu_item() {
    printf '  %s[%2s]%s %s\n' "$CYAN" "$1" "$RESET" "$2"
}

menu_options() {
    printf '\n'
    if (( UI_WIDTH >= 82 )); then
        printf '       %s%-35s%s%s\n\n' "$BOLD" 'DEPLOYMENT' 'OPERATIONS' "$RESET"
        menu_pair 1 'Validate + generate' 6 'Restart containers'
        menu_pair 2 'Build + deploy' 7 'Container status'
        menu_pair 3 'Build images only' 8 'Live service logs'
        menu_pair 4 'Start existing images' 9 'Diagnostics / read-only'
        menu_pair 5 'Stop containers' 10 'Enable Docker at boot'
        printf '%33s' ''; menu_item 11 'Performance report'
    else
        printf '  %sDEPLOYMENT%s\n\n' "$BOLD" "$RESET"
        menu_item 1 'Validate + generate'
        menu_item 2 'Build + deploy'
        menu_item 3 'Build images only'
        menu_item 4 'Start existing images'
        menu_item 5 'Stop containers'
        printf '\n  %sOPERATIONS%s\n\n' "$BOLD" "$RESET"
        menu_item 6 'Restart containers'
        menu_item 7 'Container status'
        menu_item 8 'Live service logs'
        menu_item 9 'Diagnostics / read-only'
        menu_item 10 'Enable Docker at boot'
        menu_item 11 'Performance report'
    fi
    printf '\n'
    rule
    menu_item 0 'Exit'
    printf '\n'
}

help_text() {
    banner
    cat <<'HELP'

  Usage: bash scripts/service.sh [COMMAND] [all|COMPOSE_SERVICE]
  No command opens the interactive menu.

  generate     Validate YAML and generate pipeline/Compose (no containers).
  deploy       Generate, validate, build, then recreate selected containers.
  start        Start/reconcile selected containers using existing images.
  stop         Stop selected containers; preserve data.
  restart      Restart selected containers; does not apply new mounts/env.
  build        Build selected images without restarting containers.
  logs         Follow the last 30 lines; Ctrl+C exits the view.
  status       Show selected containers, including stopped containers.
  diagnose     Read-only checks for host, config, deployment and health.
  report       Read-only container resources, sensors and health snapshot.
  enable-boot  Enable Docker at boot; container restart policies still apply.
  help         Show this guide.

  Default scope: all services in this generated deployment.
  A selected service uses --no-deps: start its inputs separately first.
  Deploy reloads YAML by recreating containers; expect a brief interruption.
  Starting ingest/notifier enables configured backend/notification delivery.
  No package installation, Git pull, volume deletion or systemd unit creation.
HELP
}

find_python() {
    if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
        PYTHON="$PROJECT_DIR/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    else
        error 'Python 3 is missing. Install the controller prerequisites first.'; return 1
    fi
    if ! run_quiet 'Checking Python dependencies' "$PYTHON" -c 'import yaml'; then
        error "PyYAML is missing for $PYTHON. Install config/controller/requirements.txt."; return 1
    fi
}

# Use the current user's Docker access where possible; ask sudo only if needed.
require_docker() {
    command -v docker >/dev/null 2>&1 || { error 'Docker is not installed.'; return 1; }
    DOCKER=(docker)
    if ! docker info >/dev/null 2>&1; then
        if (( EUID != 0 )) && command -v sudo >/dev/null 2>&1; then
            warn 'Checking Docker with sudo (a password may be requested).'
            DOCKER=(sudo docker)
        fi
        "${DOCKER[@]}" info >/dev/null 2>&1 || {
            error 'Docker is unavailable. Check: sudo systemctl status docker'; return 1;
        }
    fi
    "${DOCKER[@]}" compose version || return 1
}
compose() { "${DOCKER[@]}" compose -f "$COMPOSE_FILE" "$@"; }
validate_compose() {
    [[ -f "$COMPOSE_FILE" ]] || { error 'Compose is missing. Run generate first.'; return 1; }
    run_quiet 'Validating generated Compose' compose config --quiet
}
select_target() {
    TARGETS=()
    local requested="${1:-all}" services
    [[ "$requested" == all ]] && return 0
    services="$(compose config --services)" || return 1
    if ! grep -Fxq -- "$requested" <<< "$services"; then
        error "Unknown Compose service: $requested"
        printf 'Available services:\n%s\n' "$services"; return 1
    fi
    TARGETS=("$requested")
}

generate() {
    find_python || return 1
    heading '1 / Validate installation YAML'
    "$PYTHON" -m config.controller --details || return 1
    heading '2 / Generate pipeline and Compose'
    "$PYTHON" -m config.controller --generate
}

diagnose() {
    local failures=0
    heading 'Host'
    uname -srm
    df -h "$PROJECT_DIR"
    command -v free >/dev/null 2>&1 && free -h
    if command -v systemctl >/dev/null 2>&1; then
        printf '  Docker running: '; systemctl is-active docker || true
        printf '  Docker at boot: '; systemctl is-enabled docker || true
    fi
    heading 'Configuration (no generation or writes)'
    if find_python; then
        "$PYTHON" -m config.controller --details || failures=$((failures + 1))
    else
        failures=$((failures + 1))
    fi
    heading 'Docker and deployment'
    require_docker || return 1
    validate_compose || return 1
    compose ps -a || return 1
    # Inspect this project's containers only, without printing config/secrets.
    local ids id state health policy bad=0 running=0
    ids="$(compose ps -a -q)" || return 1
    if [[ -z "$ids" ]]; then
        warn 'No containers created for this project.'; failures=$((failures + 1))
    fi
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        state="$("${DOCKER[@]}" inspect --format '{{.State.Status}}' "$id")" || return 1
        health="$("${DOCKER[@]}" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}}' "$id")" || return 1
        policy="$("${DOCKER[@]}" inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$id")" || return 1
        printf '  %.12s  state=%s  health=%s  restart=%s\n' "$id" "$state" "$health" "$policy"
        [[ "$state" == running ]] && running=$((running + 1))
        if [[ "$state" != running || "$health" != healthy ]]; then bad=$((bad + 1)); fi
    done <<< "$ids"
    local configured
    configured="$(compose config --services)" || return 1
    local expected=0
    while IFS= read -r id; do [[ -z "$id" ]] || expected=$((expected + 1)); done <<< "$configured"
    printf '  Configured services: %s | Running containers: %s | Need attention: %s\n' "$expected" "$running" "$bad"
    if (( bad > 0 || running < expected )); then
        warn 'Some configured services are missing, starting, stopped or unhealthy.'
        failures=$((failures + 1))
    fi
    warn 'Healthy containers do not prove backend delivery or detection accuracy.'
    warn 'Check service logs and receiver tools for payloads; diagnostics do not contact cameras or the API.'
    (( failures == 0 )) && info 'Basic checks passed.'
    (( failures == 0 ))
}

# Read sensors only. Missing vendor tools/sensors are not a deployment failure.
report_sensors() (
    shopt -s nullglob
    local sensor value label chip count=0 sample result
    heading 'Temperature / host sensors'
    for sensor in /sys/class/thermal/thermal_zone*/temp; do
        value="$(cat -- "$sensor" 2>/dev/null)" || continue
        [[ "$value" =~ ^-?[0-9]+$ ]] || continue
        label="$(cat -- "${sensor%/temp}/type" 2>/dev/null)"
        awk -v label="${label:-${sensor%/temp}}" -v value="$value" \
            'BEGIN {printf "  %s: %.1f C\n", label, value / 1000}'
        count=$((count + 1))
    done
    (( count > 0 )) || printf '  Temperature sensors unavailable or unreadable.\n'
    heading 'Power / hardware telemetry'
    count=0
    for sensor in /sys/class/hwmon/hwmon*/power*_input /sys/class/hwmon/hwmon*/power*_average; do
        value="$(cat -- "$sensor" 2>/dev/null)" || continue
        [[ "$value" =~ ^[0-9]+$ ]] || continue
        chip="$(cat -- "${sensor%/*}/name" 2>/dev/null)"
        label="$(cat -- "${sensor%_*}_label" 2>/dev/null)"
        awk -v label="${chip:-hwmon}/${label:-${sensor##*/}}" -v value="$value" \
            'BEGIN {printf "  %s: %.3f W\n", label, value / 1000000}'
        count=$((count + 1))
    done
    if (( count == 0 )); then
        printf '  No readable hwmon power sensors; board watts are not estimated.\n'
    else
        printf '  Rail readings may overlap; they are not summed into board power.\n'
    fi
    if command -v vcgencmd >/dev/null 2>&1; then
        printf '  Raspberry Pi throttle flags (current + historical): '
        vcgencmd get_throttled || warn 'Throttle flags unavailable.'
    fi
    if command -v tegrastats >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
        printf '  Jetson sample (native output; power rails use the displayed units):\n'
        sample="$(timeout 3 tegrastats --interval 1000 2>/dev/null)"; result=$?
        if [[ -n "$sample" ]] && (( result == 0 || result == 124 )); then
            printf '%s\n' "$sample"
        else
            warn 'Jetson telemetry unavailable with current permissions.'
        fi
    elif command -v nvidia-smi >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
        printf '  NVIDIA GPU sample (GPU watts, not total board consumption):\n'
        timeout 5 nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
            --format=csv || warn 'GPU telemetry unavailable on this driver/device.'
    fi
)

performance_report() {
    heading 'Performance report / read-only snapshot'
    printf '  Captured: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '  Scope: %s\n' "${TARGETS[*]:-all configured services}"
    heading 'Host resources (whole device)'
    uname -srm
    command -v uptime >/dev/null 2>&1 && uptime
    command -v free >/dev/null 2>&1 && free -h
    df -h "$PROJECT_DIR" || warn 'Filesystem usage unavailable.'
    heading 'Containers / health'
    local ids id record name state health restarts oom service configured expected_service
    local checked=0 attention=0 missing=0 stats_failed=0
    local running_ids=()
    local -A observed=()
    ids="$(compose ps -a -q "${TARGETS[@]}")" || return 1
    configured="$(compose config --services)" || return 1
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        record="$("${DOCKER[@]}" inspect --format '{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{index .Config.Labels "com.docker.compose.service"}}' "$id")" || return 1
        IFS='|' read -r name state health restarts oom service <<< "$record"
        printf '  %s\n    state=%s  health=%s  restarts=%s  OOM=%s\n' "${name#/}" "$state" "$health" "$restarts" "$oom"
        checked=$((checked + 1))
        [[ -z "$service" ]] || observed["$service"]=1
        if [[ "$state" == running ]]; then running_ids+=("$id"); fi
        if [[ "$state" != running || "$health" != healthy || "$oom" == true ]]; then
            attention=$((attention + 1))
        fi
    done <<< "$ids"
    if (( ${#TARGETS[@]} > 0 )); then configured="${TARGETS[0]}"; fi
    while IFS= read -r expected_service; do
        [[ -n "$expected_service" ]] || continue
        if [[ -z "${observed[$expected_service]:-}" ]]; then
            warn "Missing container: $expected_service"
            missing=$((missing + 1))
        fi
    done <<< "$configured"
    heading 'Container resource sample'
    if (( ${#running_ids[@]} > 0 )); then
        "${DOCKER[@]}" stats --no-stream \
            --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}\t{{.PIDs}}' \
            "${running_ids[@]}" || stats_failed=1
        printf '  CPU: 100%% represents one core. Network/block I/O are cumulative counters.\n'
    else
        warn 'No running containers to sample.'
    fi
    report_sensors
    heading 'Snapshot assessment'
    printf '  Containers checked: %s | Need attention: %s | Missing services: %s\n' "$checked" "$attention" "$missing"
    if (( attention > 0 || missing > 0 || checked == 0 || stats_failed > 0 )); then
        warn 'ATTENTION: inspect container state, health, OOM or resource-read failures.'
    else
        info 'Container health snapshot passed.'
    fi
    printf '  Restart counts describe this container lifetime; they are not rates.\n'
    printf '  FPS, queue drops, delivery and long-run stability are NOT validated by this snapshot.\n'
    printf '  Use live service logs for pipeline rates; no power-per-container estimate is made.\n'
    (( attention == 0 && missing == 0 && checked > 0 && stats_failed == 0 ))
}


enable_boot() {
    command -v systemctl >/dev/null 2>&1 || { error 'This host does not use systemctl.'; return 1; }
    local admin=()
    if (( EUID != 0 )); then
        command -v sudo >/dev/null 2>&1 || { error 'sudo is required.'; return 1; }
        admin=(sudo)
    fi
    "${admin[@]}" systemctl enable docker || return 1
    info 'Docker enabled at boot. Container restart policies choose what resumes.'
    warn 'Manually stopped unless-stopped containers stay stopped. No containers were started.'
}

run_action() {
    local action="$1" target="${2:-all}"
    case "$action" in
        help|-h|--help) help_text; return ;;
        generate) generate; return ;;
        diagnose) diagnose; return ;;
        enable-boot) enable_boot; return ;;
        deploy|start|stop|restart|build|logs|status|report) ;;
        *) error "Unknown command: $action"; help_text; return 2 ;;
    esac
    if [[ "$action" == deploy ]]; then generate || return 1; fi
    require_docker && validate_compose && select_target "$target" || return 1
    heading "$action / $target"
    local isolated=()
    (( ${#TARGETS[@]} == 0 )) || isolated=(--no-deps)
    case "$action" in
        deploy)
            warn 'Recreates selected services and enables their configured delivery. Data volumes are preserved.'
            compose build "${TARGETS[@]}" || return 1
            if (( ${#TARGETS[@]} == 0 )); then
                compose up --no-build -d --force-recreate --remove-orphans || return 1
            else
                compose up --no-build -d --no-deps --force-recreate "${TARGETS[@]}" || return 1
            fi
            heading 'Post-deployment status'
            compose ps -a "${TARGETS[@]}" || return 1
            info 'Containers started. Health checks may need time; run diagnose after startup.' ;;
        start)
            warn 'Starts selected services, including configured backend/notification delivery.'
            compose up --no-build -d "${isolated[@]}" "${TARGETS[@]}" ;;
        stop) compose stop "${TARGETS[@]}" ;;
        restart)
            warn 'Restart does not apply changed environment or mounts. Use deploy for updates.'
            compose restart --no-deps "${TARGETS[@]}" ;;
        build) compose build "${TARGETS[@]}" ;;
        logs)
            printf '  Ctrl+C exits logs; containers keep running.\n'
            compose logs -f --tail 30 "${TARGETS[@]}" ;;
        status) compose ps -a "${TARGETS[@]}" ;;
        report) performance_report ;;
    esac
}

pick_service() {
    CHOSEN=all
    [[ -f "$COMPOSE_FILE" ]] || return 0
    require_docker || return 1
    local services line answer
    services="$(compose config --services)" || return 1
    local choices=(all)
    while IFS= read -r line; do [[ -z "$line" ]] || choices+=("$line"); done <<< "$services"
    heading 'SELECT SERVICE'; rule; menu_item 0 'All configured services'
    local i
    for ((i=1; i<${#choices[@]}; i++)); do menu_item "$i" "${choices[$i]}"; done
    read -r -p '  Scope [0]: ' answer || return 1
    answer="${answer:-0}"
    for ((i=0; i<${#choices[@]}; i++)); do
        if [[ "$answer" == "$i" ]]; then CHOSEN="${choices[$i]}"; return 0; fi
    done
    error 'Invalid service selection.'; return 1
}

menu() {
    [[ -t 0 ]] || { help_text; return 0; }
    local choice action rc
    # Keep Ctrl+C in a log follower from terminating the parent menu.
    trap 'printf "\n"' INT
    while true; do
        banner
        menu_options
        read -r -p '  Select action [0-11]: ' choice || break
        case "$choice" in
            0) break ;;
            1) action=generate ;; 2) action=deploy ;; 3) action=build ;;
            4) action=start ;; 5) action=stop ;; 6) action=restart ;;
            7) action=status ;; 8) action=logs ;; 9) action=diagnose ;;
            10) action=enable-boot ;; 11) action=report ;; *) warn 'Choose a listed number.'; continue ;;
        esac
        CHOSEN=all
        case "$action" in
            deploy|build|start|stop|restart|status|logs|report) pick_service || continue ;;
        esac
        run_action "$action" "$CHOSEN"; rc=$?
        if (( rc != 0 && rc != 130 )); then error "Action needs attention (exit $rc). See the output above."; fi
        read -r -p '  Press Enter to return to the menu...' choice || break
    done
    return 0
}

if (( $# == 0 )); then menu; else
    (( $# <= 2 )) || { error 'Expected COMMAND and optional service name.'; exit 2; }
    run_action "$@"
fi
