#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SUBIO-TUN  —  health_check.sh
#  Verify tunnel health, show status, test SOCKS.
# ─────────────────────────────────────────────────────────────

show_service_status() {
    log_step "Service Status"

    local services=( "subio-ssh" "subio-manager" )

    for svc in "${services[@]}"; do
        local state
        state=$(systemctl is-active "${svc}.service" 2>/dev/null || echo "not-found")
        local enabled
        enabled=$(systemctl is-enabled "${svc}.service" 2>/dev/null || echo "not-found")

        case "$state" in
            active)
                echo -e "  ${GREEN}●${NC} ${BOLD}${svc}${NC}: ${GREEN}active${NC} (enabled: ${enabled})"
                ;;
            inactive)
                echo -e "  ${YELLOW}○${NC} ${BOLD}${svc}${NC}: ${YELLOW}inactive${NC} (enabled: ${enabled})"
                ;;
            failed)
                echo -e "  ${RED}✖${NC} ${BOLD}${svc}${NC}: ${RED}failed${NC} (enabled: ${enabled})"
                ;;
            *)
                echo -e "  ${DIM}?${NC} ${BOLD}${svc}${NC}: ${DIM}${state}${NC} (enabled: ${enabled})"
                ;;
        esac
    done
    echo ""
}

check_hpn_port_listening() {
    local port="${HPN_PORT:-2222}"
    if ss -tlnp 2>/dev/null | grep -q ":${port} " || \
       netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
        echo -e "  ${GREEN}●${NC} SUBIO-SSH port ${BOLD}${port}${NC}: ${GREEN}listening${NC}"
        return 0
    else
        echo -e "  ${RED}✖${NC} SUBIO-SSH port ${BOLD}${port}${NC}: ${RED}not listening${NC}"
        return 1
    fi
}

check_socks_ports() {
    log_step "Connection Status"

    local config_file="/etc/subio-manager.json"
    if [[ ! -f "$config_file" ]]; then
        log_warn "Config file not found: ${config_file}"
        return 1
    fi

    local parsed_role_data
    parsed_role_data=$(python3 -c "
import json, socket, os, subprocess

try:
    with open('/etc/subio-manager.json') as f:
        config = json.load(f)
    cluster = config[0] if isinstance(config, list) else config.get('clusters', [])[0]
    
    token = ''
    if os.path.exists('/etc/subio-manager-current-host.txt'):
        with open('/etc/subio-manager-current-host.txt') as f:
            token = f.read().strip()
            
    if not token and os.path.exists('/etc/subio-manager-manual-ipv4.txt'):
        with open('/etc/subio-manager-manual-ipv4.txt') as f:
            token = f.read().strip()
            
    role = 'unknown'
    for h in cluster.get('domestic_hosts', []):
        if token and token in (h['name'], h['ipv4']):
            role = 'domestic'
            break
    if role == 'unknown':
        for h in cluster.get('foreign_hosts', []):
            if token and token in (h['name'], h['ipv4']):
                role = 'foreign'
                break
                
    if role == 'unknown':
        try:
            ip_out = subprocess.check_output(['ip', '-4', 'addr', 'show'], text=True)
            for h in cluster.get('domestic_hosts', []):
                if h['ipv4'] in ip_out:
                    role = 'domestic'
                    break
            if role == 'unknown':
                for h in cluster.get('foreign_hosts', []):
                    if h['ipv4'] in ip_out:
                        role = 'foreign'
                        break
        except:
            pass

    print(f'DETECTED_ROLE=\"{role}\"')

    if role == 'domestic' or role == 'unknown':
        ports = set()
        for site, ps in cluster.get('foreign_to_domestic_ports_by_site', {}).items():
            if isinstance(ps, list):
                for p in ps: ports.add((site, int(p)))
            else:
                ports.add((site, int(ps)))
        ports_str = '\n'.join([f'{s}:{p}' for s, p in sorted(ports, key=lambda x: x[1])])
        print(f'DOMESTIC_PORTS=\"{ports_str}\"')
    elif role == 'foreign':
        targets = []
        for h in cluster.get('domestic_hosts', []):
            targets.append(f\"{h['name']}:{h['ipv4']}:{h.get('hpn_port', 2222)}\")
        targets_str = '\n'.join(targets)
        print(f'FOREIGN_TARGETS=\"{targets_str}\"')

except Exception as e:
    print('DETECTED_ROLE=\"unknown\"')
" 2>/dev/null)

    eval "$parsed_role_data"
    local all_ok=true

    if [[ "$DETECTED_ROLE" == "foreign" ]]; then
        echo ""
        echo -e "  ${BOLD}${CYAN}Target    IP Address       Port    Status${NC}"
        echo -e "  ${DIM}──────    ──────────────   ──────  ──────────────${NC}"

        while IFS=: read -r target_name target_ip target_port; do
            [[ -z "$target_name" ]] && continue
            local status="DOWN"
            local color="$RED"

            # Check for ESTABLISHED connection to target_ip:target_port
            if ss -tnp 2>/dev/null | grep "ESTAB" | grep -q "${target_ip}:${target_port}"; then
                status="CONNECTED"
                color="$GREEN"
            else
                all_ok=false
            fi

            printf "  ${color}%-10s${NC}%-17s%-8s${color}%-16s${NC}\n" \
                "$target_name" "$target_ip" "$target_port" "$status"
        done <<< "$FOREIGN_TARGETS"
        
        echo ""
        if $all_ok; then
            log_ok "All tunnels to Iran servers are CONNECTED!"
        else
            log_warn "Some tunnels are not connected."
            log_sub "Check 'journalctl -u subio-manager -f' for logs"
        fi
        
    else
        # Domestic / Default logic
        if [[ -z "$DOMESTIC_PORTS" ]]; then
            log_warn "No SOCKS ports found in config"
            return 1
        fi

        echo ""
        echo -e "  ${BOLD}${CYAN}Site    Port    Status          Egress IP${NC}"
        echo -e "  ${DIM}──────  ──────  ──────────────  ─────────────────${NC}"

        while IFS=: read -r site port; do
            [[ -z "$site" ]] && continue
            local status="DOWN"
            local egress_ip="-"
            local color="$RED"

            # Check if port is listening
            if ss -tlnp 2>/dev/null | grep -q ":${port} " || \
               netstat -tlnp 2>/dev/null | grep -q ":${port} "; then

                # Test SOCKS proxy egress
                if command -v curl &>/dev/null; then
                    egress_ip=$(curl -s --connect-timeout 5 --max-time 10 \
                        --proxy "socks5h://127.0.0.1:${port}" \
                        "https://api.ipify.org" 2>/dev/null || echo "")
                    if [[ -n "$egress_ip" && "$egress_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                        status="CONNECTED"
                        color="$GREEN"
                    else
                        status="LISTENING"
                        color="$YELLOW"
                        egress_ip="(no egress)"
                        all_ok=false
                    fi
                else
                    status="LISTENING"
                    color="$YELLOW"
                    egress_ip="(curl n/a)"
                fi
            else
                all_ok=false
            fi

            printf "  ${color}%-8s${NC}%-8s${color}%-16s${NC}%s\n" \
                "$site" "$port" "$status" "$egress_ip"

        done <<< "$DOMESTIC_PORTS"

        echo ""
        if $all_ok; then
            log_ok "All SOCKS tunnels are connected and working!"
        else
            log_warn "Some tunnels are not fully connected yet."
            log_sub "On Iran server: wait for the foreign server to connect"
            log_sub "On foreign server: check 'journalctl -u subio-manager -f'"
        fi
    fi
}

show_tunnel_summary() {
    log_step "Tunnel Summary"

    local config_file="/etc/subio-manager.json"
    [[ ! -f "$config_file" ]] && return 1

    python3 << 'PYEOF'
import json, os

config_file = "/etc/subio-manager.json"
with open(config_file) as f:
    config = json.load(f)

clusters = config if isinstance(config, list) else config.get("clusters", [])

for c in clusters:
    name = c.get("name", "unknown")
    foreign = c.get("foreign_hosts", [])
    domestic = c.get("domestic_hosts", [])
    ports = c.get("foreign_to_domestic_ports_by_site", {})

    print(f"  Cluster: \033[1m{name}\033[0m")
    print()
    print(f"  \033[36mForeign Servers (Kharej):\033[0m")
    for h in foreign:
        print(f"    • {h['name']:20s}  {h['ipv4']:16s}  site={h.get('site','?')}")
    print()
    print(f"  \033[36mDomestic Servers (Iran):\033[0m")
    for h in domestic:
        print(f"    • {h['name']:20s}  {h['ipv4']:16s}")
    print()
    print(f"  \033[36mSOCKS Ports (on Iran):\033[0m")
    for site, ps in sorted(ports.items()):
        port_list = ps if isinstance(ps, list) else [ps]
        for p in port_list:
            print(f"    • {site:6s} → 127.0.0.1:{p}")
    print()
PYEOF
}

show_final_info() {
    log_step "Final Information"

    local role="${SERVER_ROLE:-unknown}"

    echo -e "  ${BOLD}Server Role:${NC}  ${CYAN}${role}${NC}"
    echo -e "  ${BOLD}Server IP:${NC}    ${SERVER_IPV4:-unknown}"
    echo ""

    show_service_status
    check_hpn_port_listening
    echo ""
    show_tunnel_summary
    check_socks_ports

    separator
    echo ""

    if [[ "$role" == "iran" ]]; then
        echo -e "  ${BOLD}${GREEN}▸ SOCKS Outbounds for Sanayi Panel:${NC}"
        echo ""

        local config_file="/etc/subio-manager.json"
        if [[ -f "$config_file" ]]; then
            python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        config = json.load(f)
    clusters = config if isinstance(config, list) else config.get("clusters", [])
    for c in clusters:
        for site, ps in sorted(c.get("foreign_to_domestic_ports_by_site", {}).items()):
            port_list = ps if isinstance(ps, list) else [ps]
            for p in port_list:
                print(f"    socks5://127.0.0.1:{p}   ← Site {site}")
                print("")
                print("  \033[2mUse the following command to test connection speed:\033[0m")
                sq = chr(39)
                cmd = f"speed=$(curl -x socks5h://127.0.0.1:{p} -o /dev/null  -L -w {sq}%{{speed_download}}{sq} https://fsn1-speed.hetzner.com/100MB.bin) && awk -v s=\"$speed\" {sq}BEGIN {{ printf \"Speed: %.2f MB/s = %.2f Mbps\\n\", s/1000000, s*8/1000000 }}{sq}"
                print(f"    {cmd}")
                print("")
except Exception as e:
    pass
' "$config_file" 2>/dev/null
        fi

        echo ""
        echo -e "  ${DIM}Use these addresses as Outbound SOCKS in the Sanayi panel.${NC}"
    else
        echo -e "  ${BOLD}${GREEN}▸ Server is connecting to Iran...${NC}"
        echo -e "  ${DIM}Check tunnel status: journalctl -u subio-manager -f${NC}"
    fi

    echo ""
    separator
    echo ""
    echo -e "  ${BOLD}Useful commands:${NC}"
    echo -e "    ${CYAN}systemctl status subio-ssh${NC}           — SUBIO-SSH daemon status"
    echo -e "    ${CYAN}systemctl status subio-manager${NC}      — Tunnel manager status"
    echo -e "    ${CYAN}journalctl -u subio-manager -f${NC}      — Live tunnel logs"
    echo -e "    ${CYAN}subio-tun status${NC}                    — Quick status check"
    echo -e "    ${CYAN}subio-tun test${NC}                      — Test SOCKS connectivity"
    echo ""
}

run_health_check() {
    show_service_status
    check_hpn_port_listening
    echo ""
    check_socks_ports
}
