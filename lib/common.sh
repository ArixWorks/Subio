#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SUBIO-TUN  —  common.sh
#  Shared utility functions for the tunnel setup toolkit.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Colour

# ── Logging ──────────────────────────────────────────────────
log_info()    { echo -e "${CYAN}[INFO]${NC}    $*"; }
log_ok()      { echo -e "${GREEN}[  OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}    $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC}   $*"; }
log_step()    { echo -e "\n${BOLD}${MAGENTA}━━━ $* ━━━${NC}\n"; }
log_sub()     { echo -e "  ${BLUE}▸${NC} $*"; }
log_debug()   { [[ "${HPN_DEBUG:-0}" == "1" ]] && echo -e "${DIM}[DEBUG]   $*${NC}" || true; }

# ── Banner ───────────────────────────────────────────────────
show_banner() {
    echo -e "${BOLD}${CYAN}"
    cat << 'BANNER'
  ╦ ╦╔═╗╔╗╔   ╔╦╗╦ ╦╔╗╔
  ╠═╣╠═╝║║║    ║ ║ ║║║║
  ╩ ╩╩  ╝╚╝    ╩ ╚═╝╝╚╝
  ─── Reverse SOCKS Tunnel Manager ───
BANNER
    echo -e "${NC}"
    echo -e "  ${DIM}SUBIO-SSH based secure tunnel toolkit${NC}"
    echo -e "  ${DIM}Version 1.0.0${NC}"
    echo ""
}

# ── OS / distro helpers ──────────────────────────────────────
detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_VERSION="${VERSION_ID:-unknown}"
        OS_NAME="${PRETTY_NAME:-unknown}"
    elif command -v lsb_release &>/dev/null; then
        OS_ID="$(lsb_release -si | tr '[:upper:]' '[:lower:]')"
        OS_VERSION="$(lsb_release -sr)"
        OS_NAME="$(lsb_release -sd)"
    else
        OS_ID="unknown"
        OS_VERSION="unknown"
        OS_NAME="$(uname -s) $(uname -r)"
    fi
    export OS_ID OS_VERSION OS_NAME
}

is_debian_based() { [[ "$OS_ID" =~ ^(debian|ubuntu|linuxmint|pop|kali|raspbian)$ ]]; }
is_rhel_based()   { [[ "$OS_ID" =~ ^(centos|rhel|fedora|rocky|almalinux|ol)$ ]]; }

# ── Package management helpers ───────────────────────────────
pkg_install() {
    if is_debian_based; then
        apt-get install -y "$@"
    elif is_rhel_based; then
        if command -v dnf &>/dev/null; then
            dnf install -y "$@"
        else
            yum install -y "$@"
        fi
    else
        log_error "Unsupported distribution: $OS_ID"
        return 1
    fi
}

pkg_update() {
    if is_debian_based; then
        apt-get update -y
    elif is_rhel_based; then
        if command -v dnf &>/dev/null; then
            dnf makecache -y
        else
            yum makecache -y
        fi
    fi
}

ensure_packages() {
    local missing=()
    for pkg in "$@"; do
        if ! dpkg -s "$pkg" &>/dev/null 2>&1 && ! rpm -q "$pkg" &>/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_info "Installing missing packages: ${missing[*]}"
        pkg_install "${missing[@]}"
    fi
}

# ── Root check ───────────────────────────────────────────────
require_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root."
        exit 1
    fi
}

# ── IP detection ─────────────────────────────────────────────
detect_public_ipv4() {
    local ip=""
    local services=(
        "https://api.ipify.org"
        "https://ifconfig.me"
        "https://icanhazip.com"
        "https://ipinfo.io/ip"
        "https://api.ip.sb/ip"
    )
    for svc in "${services[@]}"; do
        ip=$(curl -4 -s --connect-timeout 5 --max-time 8 "$svc" 2>/dev/null | tr -d '[:space:]') || true
        if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "$ip"
            return 0
        fi
    done
    return 1
}

detect_primary_local_ipv4() {
    local iface
    # Find default route interface that doesn't start with tun or wg
    iface=$(ip -4 route show default 2>/dev/null | awk '/default/ && $5 !~ /^(tun|wg)/ {print $5; exit}')
    
    if [[ -n "$iface" ]]; then
        ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n 1
    else
        # Fallback to any global IP not on tun/wg
        ip -4 -o addr show scope global 2>/dev/null | awk '{if ($2 !~ /^(tun|wg)/) print $4}' | cut -d/ -f1 | head -n 1
    fi
}

# ── Interactive helpers ──────────────────────────────────────
ask_input() {
    # ask_input "Prompt text" DEFAULT_VALUE
    local prompt="$1"
    local default="${2:-}"
    local answer
    while true; do
        if [[ -n "$default" ]]; then
            read -rp "$(echo -e "${BOLD}${prompt}${NC} ${DIM}[${default}]${NC}: ")" answer
            answer="${answer:-$default}"
        else
            read -rp "$(echo -e "${BOLD}${prompt}${NC}: ")" answer
        fi
        
        if [[ -z "$answer" ]]; then
            echo -e "${RED}  ⚠ This field is required.${NC}" >&2
            continue
        fi
        
        # Check if answer contains any character outside the ASCII printable range (space to tilde)
        if LC_ALL=C echo "$answer" | grep -q '[^ -~]'; then
            echo -e "${RED}  ⚠ Error: Only English characters and numbers are allowed. Please try again.${NC}" >&2
            continue
        fi
        
        echo "$answer"
        return 0
    done
}

ask_yes_no() {
    # ask_yes_no "Prompt" DEFAULT(y/n) — returns 0 for yes, 1 for no
    local prompt="$1"
    local default="${2:-y}"
    local hint="[Y/n]"
    [[ "$default" == "n" ]] && hint="[y/N]"
    local answer
    read -rp "$(echo -e "${BOLD}${prompt}${NC} ${hint}: ")" answer
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy] ]]
}

ask_choice() {
    # ask_choice "Prompt" "opt1" "opt2" ...
    local prompt="$1"; shift
    local options=("$@")
    echo -e "\n${BOLD}${prompt}${NC}" >&2
    local i=1
    for opt in "${options[@]}"; do
        echo -e "  ${CYAN}${i})${NC} ${opt}" >&2
        ((i++))
    done
    local choice
    while true; do
        read -rp "$(echo -e "${BOLD}Select [1-${#options[@]}]${NC}: ")" choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#options[@]} )); then
            echo "${options[$((choice - 1))]}"
            return 0
        fi
        echo -e "${RED}  ⚠ Invalid selection. Try again.${NC}" >&2
    done
}

validate_ipv4() {
    local ip="$1"
    if [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 0
    fi
    return 1
}

validate_port() {
    local port="$1"
    if [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )); then
        return 0
    fi
    return 1
}

# ── File helpers ─────────────────────────────────────────────
backup_file() {
    local file="$1"
    if [[ -f "$file" ]]; then
        local bak="${file}.bak.$(date +%Y%m%d%H%M%S)"
        cp -a "$file" "$bak"
        log_sub "Backed up ${file} → ${bak}"
    fi
}

ensure_dir() {
    local dir="$1"
    local mode="${2:-0755}"
    mkdir -p "$dir"
    chmod "$mode" "$dir"
}

# ── Separator ────────────────────────────────────────────────
separator() {
    echo -e "${DIM}$(printf '%.0s─' {1..60})${NC}"
}

# ── Network Optimization ─────────────────────────────────────
enable_bbr() {
    log_step "TCP Optimization (BBR)"
    local current_cc
    current_cc=$(sysctl net.ipv4.tcp_congestion_control 2>/dev/null | awk '{print $3}')
    
    if [[ "$current_cc" == "bbr" ]]; then
        log_ok "BBR is already enabled."
    else
        log_sub "Enabling Google BBR for faster tunnel throughput..."
        # Load module if not loaded
        if ! lsmod | grep -q tcp_bbr; then
            modprobe tcp_bbr 2>/dev/null || true
        fi
        
        # Add to sysctl if missing
        if ! grep -q "net.core.default_qdisc=fq" /etc/sysctl.conf; then
            echo "net.core.default_qdisc=fq" >> /etc/sysctl.conf
        fi
        if ! grep -q "net.ipv4.tcp_congestion_control=bbr" /etc/sysctl.conf; then
            echo "net.ipv4.tcp_congestion_control=bbr" >> /etc/sysctl.conf
        fi
        
        sysctl -p >/dev/null 2>&1
        
        # Verify
        current_cc=$(sysctl net.ipv4.tcp_congestion_control 2>/dev/null | awk '{print $3}')
        if [[ "$current_cc" == "bbr" ]]; then
            log_ok "BBR successfully enabled!"
        else
            log_warn "Failed to enable BBR (maybe kernel does not support it)."
        fi
    fi
}
