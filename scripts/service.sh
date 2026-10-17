#!/usr/bin/env bash
# Vision Engine Services: Linux operator menu and repeatable CLI commands.
set -uo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
COMPOSE_FILE="$PROJECT_DIR/config/.generated/pipeline.compose.yaml"
cd -- "$PROJECT_DIR" || exit 1
DOCKER=()
PYTHON=""
TARGETS=()
CYAN='' GREEN='' YELLOW='' RED='' BOLD='' RESET=''
if [[ -t 1 && -z "${NO_COLOR+x}" && "${TERM:-dumb}" != dumb ]]; then
    CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
fi
info() { printf '%s  + %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$RESET"; }
error() { printf '%s  x %s%s\n' "$RED" "$*" "$RESET" >&2; }
heading() { printf '\n%s%s  %s%s\n' "$BOLD" "$CYAN" "$*" "$RESET"; }

banner() {
    heading 'VISION ENGINE / SERVICES'
    printf '  Configuration  >  Pipeline  >  Containers  >  Health\n'
    printf '  ----------------------------------------------------\n'
    printf '  Project: %s\n' "$PROJECT_DIR"
    printf '  Compose: config/.generated/pipeline.compose.yaml\n'
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
    if ! "$PYTHON" -c 'import yaml' >/dev/null 2>&1; then
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
    compose config --quiet
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
        deploy|start|stop|restart|build|logs|status) ;;
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
    printf '\n  0) All configured services\n'
    local i
    for ((i=1; i<${#choices[@]}; i++)); do printf '  %s) %s\n' "$i" "${choices[$i]}"; done
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
        cat <<'MENU'

   1  Validate + generate       6  Restart containers
   2  Build + deploy           7  Status
   3  Build images only        8  Live logs
   4  Start existing images    9  Diagnostics (read-only)
   5  Stop containers         10  Enable Docker at boot
                              0  Exit
MENU
        read -r -p '  Select: ' choice || break
        case "$choice" in
            0) break ;;
            1) action=generate ;; 2) action=deploy ;; 3) action=build ;;
            4) action=start ;; 5) action=stop ;; 6) action=restart ;;
            7) action=status ;; 8) action=logs ;; 9) action=diagnose ;;
            10) action=enable-boot ;; *) warn 'Choose a listed number.'; continue ;;
        esac
        CHOSEN=all
        case "$action" in
            deploy|build|start|stop|restart|status|logs) pick_service || continue ;;
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
