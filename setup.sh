#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SUBIO-TUN  —  setup.sh
#  Main interactive setup script for SUBIO-SSH tunnel.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# Find script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR

# Source libraries
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/install_subio_ssh.sh"
source "${SCRIPT_DIR}/lib/configure_tunnel.sh"
source "${SCRIPT_DIR}/lib/setup_services.sh"
source "${SCRIPT_DIR}/lib/health_check.sh"

# ── Globals ──────────────────────────────────────────────────
export SERVER_ROLE=""
export SERVER_IPV4=""
export SERVER_NAME=""
export CLUSTER_NAME="hpn_tunnel"
export HPN_PORT="2222"

export FOREIGN_NAMES_STR=""
export FOREIGN_IPS_STR=""
export FOREIGN_SITES_STR=""
export FOREIGN_HOSTKEYS_STR=""

export DOMESTIC_NAMES_STR=""
export DOMESTIC_IPS_STR=""
export DOMESTIC_HOSTKEYS_STR=""

export SOCKS_PORTS_STR=""

# ── Interactive Wizard ───────────────────────────────────────
wizard_collect_info() {
    show_banner
    require_root

    log_step "Server Configuration"

    SERVER_ROLE=$(ask_choice "Is this server located in Iran or Kharej (Foreign)?" "iran" "kharej")
    
    local default_ip
    default_ip=$(detect_primary_local_ipv4)
    if [[ -z "$default_ip" ]]; then
        default_ip=$(detect_public_ipv4)
    fi
    
    while true; do
        SERVER_IPV4=$(ask_input "Public IP of THIS server" "$default_ip")
        if validate_ipv4 "$SERVER_IPV4"; then break; fi
        log_error "Invalid IPv4 address"
    done

    SERVER_NAME=$(ask_input "Short name for THIS server (e.g. ir1, de1)" "${SERVER_ROLE}1")
    CLUSTER_NAME=$(ask_input "Cluster name (must be same on both sides)" "hpn_tunnel")
    
    while true; do
        HPN_PORT=$(ask_input "SUBIO-SSH Listen Port" "2222")
        if validate_port "$HPN_PORT"; then break; fi
        log_error "Invalid port number"
    done

    separator

    if [[ "$SERVER_ROLE" == "iran" ]]; then
        DOMESTIC_NAMES_STR="$SERVER_NAME"
        DOMESTIC_IPS_STR="$SERVER_IPV4"
        DOMESTIC_HOSTKEYS_STR=""

        echo -e "${CYAN}Configuration for Foreign Server(s)${NC}"
        local i=1
        while true; do
            echo -e "\n${BOLD}Foreign Server #$i${NC}"
            local fname
            fname=$(ask_input "Short name for Foreign Server #$i" "kharej$i")
            
            local fip
            while true; do
                fip=$(ask_input "Public IP of Foreign Server #$i" "")
                if validate_ipv4 "$fip"; then break; fi
                log_error "Invalid IPv4 address"
            done
            
            local fsite
            fsite=$(ask_input "Site Code (e.g. DE, FR, TR)" "DE")
            fsite=$(echo "$fsite" | tr '[:lower:]' '[:upper:]')
            
            local sport
            while true; do
                sport=$(ask_input "SOCKS port for this site on Iran server" "10810")
                if validate_port "$sport"; then break; fi
                log_error "Invalid port number"
            done

            if [[ -z "$FOREIGN_NAMES_STR" ]]; then
                FOREIGN_NAMES_STR="$fname"
                FOREIGN_IPS_STR="$fip"
                FOREIGN_SITES_STR="$fsite"
                SOCKS_PORTS_STR="${fsite}=${sport}"
            else
                FOREIGN_NAMES_STR="${FOREIGN_NAMES_STR}|${fname}"
                FOREIGN_IPS_STR="${FOREIGN_IPS_STR}|${fip}"
                FOREIGN_SITES_STR="${FOREIGN_SITES_STR}|${fsite}"
                SOCKS_PORTS_STR="${SOCKS_PORTS_STR}|${fsite}=${sport}"
            fi

            if ! ask_yes_no "Add another Foreign Server?" "n"; then
                break
            fi
            ((i++))
        done

    else
        # Role: Kharej
        FOREIGN_NAMES_STR="$SERVER_NAME"
        FOREIGN_IPS_STR="$SERVER_IPV4"
        
        local fsite
        fsite=$(ask_input "Site Code for THIS server (e.g. DE, FR)" "DE")
        FOREIGN_SITES_STR=$(echo "$fsite" | tr '[:lower:]' '[:upper:]')
        FOREIGN_HOSTKEYS_STR=""

        echo -e "\n${CYAN}Configuration for Iran Server${NC}"
        
        local dname
        dname=$(ask_input "Short name for Iran Server" "iran1")
        
        local dip
        while true; do
            dip=$(ask_input "Public IP of Iran Server" "")
            if validate_ipv4 "$dip"; then break; fi
            log_error "Invalid IPv4 address"
        done

        DOMESTIC_NAMES_STR="$dname"
        DOMESTIC_IPS_STR="$dip"

        local sport
        while true; do
            sport=$(ask_input "SOCKS port for this server on Iran side" "10810")
            if validate_port "$sport"; then break; fi
            log_error "Invalid port number"
        done
        SOCKS_PORTS_STR="${FOREIGN_SITES_STR}=${sport}"
    fi
}

# ── Main ─────────────────────────────────────────────────────
main() {
    if [[ "${1:-}" == "status" ]]; then
        require_root
        run_health_check
        exit 0
    fi

    if [[ "${1:-}" == "test" ]]; then
        require_root
        # Run test script (assumes subio-manager.py has 'test' arg)
        /usr/local/bin/subio-manager.py test "$@"
        exit $?
    fi

    wizard_collect_info

    log_step "Starting Installation"
    
    install_prerequisites
    install_subio_ssh
    generate_full_config
    setup_all_services
    enable_bbr
    
    show_final_info
}

main "$@"
