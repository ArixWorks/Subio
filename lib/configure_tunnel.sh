#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SUBIO-TUN  —  configure_tunnel.sh
#  Generate tunnel keys, configs, and known_hosts.
# ─────────────────────────────────────────────────────────────

# ── Globals set by the interactive wizard ────────────────────
# SERVER_ROLE        = "iran" | "kharej"
# SERVER_IPV4        = this server's public IPv4
# SERVER_NAME        = human name for this host (e.g. "my-ir1")
# CLUSTER_NAME       = tunnel cluster name (e.g. "my_tunnel")
# FOREIGN_SERVERS[]  = associative info collected during wizard
# SOCKS_PORTS{}      = port per site code
# HPN_PORT           = SUBIO-SSH listen port (default 2222)

TUNNEL_KEY_DIR="/root/.ssh"
TUNNEL_KEY_FILE="${TUNNEL_KEY_DIR}/tunnel_key"
TUNNEL_KEY_PUB_FILE="${TUNNEL_KEY_DIR}/tunnel_key.pub"
CONFIG_FILE="/etc/subio-manager.json"
CURRENT_HOST_FILE="/etc/subio-manager-current-host.txt"
DEFAULT_FILE="/etc/default/subio-manager"
SUBIO_SSH_CONFIG_DIR="/etc/subio-ssh"

generate_tunnel_keypair() {
    log_step "Generating Tunnel Keypair"

    ensure_dir "$TUNNEL_KEY_DIR" 0700

    if [[ -f "$TUNNEL_KEY_FILE" ]]; then
        log_warn "Tunnel key already exists: ${TUNNEL_KEY_FILE}"
        if ask_yes_no "Overwrite existing tunnel key?" "n"; then
            backup_file "$TUNNEL_KEY_FILE"
            backup_file "$TUNNEL_KEY_PUB_FILE"
        else
            log_ok "Keeping existing tunnel key"
            return 0
        fi
    fi

    log_sub "Generating Ed25519 keypair..."
    ssh-keygen -t ed25519 -N "" -C "${CLUSTER_NAME}-subio" -f "$TUNNEL_KEY_FILE" -q
    chmod 600 "$TUNNEL_KEY_FILE"
    chmod 644 "$TUNNEL_KEY_PUB_FILE"

    log_ok "Tunnel keypair generated"
    echo ""
    separator
    echo -e "${BOLD}${YELLOW}  ⚠  PUBLIC KEY — Copy this to the other server(s):${NC}"
    echo ""
    echo -e "  ${GREEN}$(cat "$TUNNEL_KEY_PUB_FILE")${NC}"
    echo ""
    separator
}

setup_authorized_keys() {
    log_sub "Setting up authorized_keys..."
    local auth_file="${TUNNEL_KEY_DIR}/authorized_keys"

    ensure_dir "$TUNNEL_KEY_DIR" 0700

    local pubkey
    pubkey=$(cat "$TUNNEL_KEY_PUB_FILE" 2>/dev/null || true)

    if [[ -z "$pubkey" ]]; then
        log_warn "No public key found at ${TUNNEL_KEY_PUB_FILE}"
        return 0
    fi

    touch "$auth_file"
    chmod 600 "$auth_file"

    if ! grep -qF "$pubkey" "$auth_file" 2>/dev/null; then
        echo "$pubkey" >> "$auth_file"
        log_ok "Public key added to authorized_keys"
    else
        log_ok "Public key already in authorized_keys"
    fi
}

get_host_key_ed25519() {
    # Get the ed25519 host key fingerprint for known_hosts
    local keyfile="/etc/subio-ssh/ssh_host_ed25519_key.pub"
    if [[ -f "$keyfile" ]]; then
        awk '{print $2}' "$keyfile"
    elif [[ -f "/etc/ssh/ssh_host_ed25519_key.pub" ]]; then
        awk '{print $2}' "/etc/ssh/ssh_host_ed25519_key.pub"
    else
        echo ""
    fi
}

configure_subio_ssh_daemon() {
    log_step "Configuring SUBIO-SSH Daemon"

    ensure_dir "$SUBIO_SSH_CONFIG_DIR"
    ensure_dir "${SUBIO_SSH_CONFIG_DIR}/sshd_config.d"

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    local template="${script_dir}/configs/subio-sshd_config.template"

    if [[ -f "$template" ]]; then
        backup_file "${SUBIO_SSH_CONFIG_DIR}/sshd_config"
        cp "$template" "${SUBIO_SSH_CONFIG_DIR}/sshd_config"
        # Update port if non-default
        if [[ "${HPN_PORT:-2222}" != "2222" ]]; then
            sed -i "s/^Port 2222/Port ${HPN_PORT}/" "${SUBIO_SSH_CONFIG_DIR}/sshd_config"
        fi
        
        # Ensure PidFile is set to avoid conflict with system sshd
        if ! grep -q "^PidFile" "${SUBIO_SSH_CONFIG_DIR}/sshd_config"; then
            echo "PidFile /var/run/hpnsshd.pid" >> "${SUBIO_SSH_CONFIG_DIR}/sshd_config"
        fi
        
        log_ok "SUBIO-SSH daemon configured on port ${HPN_PORT:-2222}"
    else
        log_warn "Template not found at ${template}, using defaults"
        backup_file "${SUBIO_SSH_CONFIG_DIR}/sshd_config"
        cat <<EOF > "${SUBIO_SSH_CONFIG_DIR}/sshd_config"
Port ${HPN_PORT:-2222}
PidFile /var/run/hpnsshd.pid
PermitRootLogin yes
PasswordAuthentication yes
X11Forwarding no
AllowTcpForwarding yes
HostKey /etc/subio-ssh/ssh_host_rsa_key
HostKey /etc/subio-ssh/ssh_host_ecdsa_key
HostKey /etc/subio-ssh/ssh_host_ed25519_key
EOF
    fi

    # Generate host keys if missing
    for alg in rsa ecdsa ed25519; do
        if [[ ! -s "${SUBIO_SSH_CONFIG_DIR}/ssh_host_${alg}_key" ]]; then
            # Copy from system SSH if available
            if [[ -f "/etc/ssh/ssh_host_${alg}_key" ]]; then
                cp -a "/etc/ssh/ssh_host_${alg}_key" "${SUBIO_SSH_CONFIG_DIR}/"
                cp -a "/etc/ssh/ssh_host_${alg}_key.pub" "${SUBIO_SSH_CONFIG_DIR}/"
                log_sub "Copied ${alg} host key from /etc/ssh"
            elif [[ -x /opt/subio-ssh/bin/subio-ssh-keygen ]]; then
                /opt/subio-ssh/bin/subio-ssh-keygen -q -t "$alg" -N "" \
                    -f "${SUBIO_SSH_CONFIG_DIR}/ssh_host_${alg}_key"
                log_sub "Generated ${alg} host key"
            elif command -v ssh-keygen &>/dev/null; then
                ssh-keygen -q -t "$alg" -N "" \
                    -f "${SUBIO_SSH_CONFIG_DIR}/ssh_host_${alg}_key"
                log_sub "Generated ${alg} host key (with ssh-keygen)"
            fi
        fi
    done

    # Copy moduli if missing
    if [[ ! -f "${SUBIO_SSH_CONFIG_DIR}/moduli" && -f /etc/ssh/moduli ]]; then
        cp /etc/ssh/moduli "${SUBIO_SSH_CONFIG_DIR}/moduli"
    fi
}

configure_default_env() {
    log_sub "Writing /etc/default/subio-manager..."
    backup_file "$DEFAULT_FILE"
    cat > "$DEFAULT_FILE" << 'ENVFILE'
# SUBIO reverse runtime overrides.
# Shared fleet default is AES-128-GCM based on the current 10GB
# long-session canary bench.
#
# HPN_CIPHER=default
HPN_CIPHER=aes128-gcm@openssh.com
# HPN_CIPHER=chacha20-poly1305-mt@subio-ssh.org
# HPN_CIPHER=chacha20-poly1305@openssh.com
# HPN_CIPHER=aes256-gcm@openssh.com

# Per-lane reset protection for subio-lane-guard. The guard queues a targeted
# lane recycle instead of bouncing the entire subio-manager service.
# HPN_LANE_GUARD_LANE_RESET_COOLDOWN=180
ENVFILE
    log_ok "Environment file written"
}

write_current_host_file() {
    log_sub "Writing current host identifier..."
    echo "$SERVER_IPV4" > "$CURRENT_HOST_FILE"
    log_ok "Current host: ${SERVER_IPV4} → ${CURRENT_HOST_FILE}"
}

# ─────────────────────────────────────────────────────────────
#  JSON config builder
# ─────────────────────────────────────────────────────────────

# These arrays are populated by the interactive wizard in setup.sh:
#   FOREIGN_NAMES[i]    = name
#   FOREIGN_IPS[i]      = ipv4
#   FOREIGN_SITES[i]    = site code (e.g. DE, FR)
#   FOREIGN_HOSTKEYS[i] = ed25519 host key (or "pending")
#   SOCKS_PORTS[site]   = port number
#   DOMESTIC_NAMES[i]   = name
#   DOMESTIC_IPS[i]     = ipv4
#   DOMESTIC_HOSTKEYS[i]= ed25519 host key (or "pending")

build_config_json() {
    log_step "Building Tunnel Configuration" >&2

    local hostkey_self
    hostkey_self=$(get_host_key_ed25519)
    [[ -z "$hostkey_self" ]] && hostkey_self="pending"

    local tunnel_private=""
    local tunnel_public=""
    if [[ -f "$TUNNEL_KEY_FILE" ]]; then
        tunnel_private=$(cat "$TUNNEL_KEY_FILE")
    fi
    if [[ -f "$TUNNEL_KEY_PUB_FILE" ]]; then
        tunnel_public=$(cat "$TUNNEL_KEY_PUB_FILE")
    fi

    # Build JSON with Python for proper escaping
    python3 << PYEOF
import json, sys

cluster = {
    "name": "${CLUSTER_NAME}",
    "foreign_hosts": [],
    "domestic_hosts": [],
    "foreign_to_domestic_ports_by_site": {},
    "domestic_to_foreign_ports_by_site": {}
}

# Foreign hosts
foreign_names = """${FOREIGN_NAMES_STR:-}""".strip().split("|")
foreign_ips = """${FOREIGN_IPS_STR:-}""".strip().split("|")
foreign_sites = """${FOREIGN_SITES_STR:-}""".strip().split("|")
foreign_hostkeys = """${FOREIGN_HOSTKEYS_STR:-}""".strip().split("|")

for i in range(len(foreign_names)):
    if not foreign_names[i]:
        continue
    hk = foreign_hostkeys[i] if i < len(foreign_hostkeys) and foreign_hostkeys[i] else "pending"
    cluster["foreign_hosts"].append({
        "name": foreign_names[i],
        "site": foreign_sites[i] if i < len(foreign_sites) else "XX",
        "ipv4": foreign_ips[i] if i < len(foreign_ips) else "",
        "hostkey_ed25519_ssh": hk,
        "hostkey_ed25519_hpn": hk
    })

# Domestic hosts
domestic_names = """${DOMESTIC_NAMES_STR:-}""".strip().split("|")
domestic_ips = """${DOMESTIC_IPS_STR:-}""".strip().split("|")
domestic_hostkeys = """${DOMESTIC_HOSTKEYS_STR:-}""".strip().split("|")

for i in range(len(domestic_names)):
    if not domestic_names[i]:
        continue
    hk = domestic_hostkeys[i] if i < len(domestic_hostkeys) and domestic_hostkeys[i] else "pending"
    cluster["domestic_hosts"].append({
        "name": domestic_names[i],
        "ipv4": domestic_ips[i] if i < len(domestic_ips) else "",
        "hostkey_ed25519_ssh": hk,
        "hostkey_ed25519_hpn": hk
    })

# Ports by site
ports_str = """${SOCKS_PORTS_STR:-}""".strip()
if ports_str:
    for entry in ports_str.split("|"):
        if "=" not in entry:
            continue
        site, port = entry.split("=", 1)
        cluster["foreign_to_domestic_ports_by_site"][site] = [int(port)]

# Tunnel key
private_key = open("${TUNNEL_KEY_FILE}", "r").read().strip() if "${TUNNEL_KEY_FILE}" and __import__("os").path.isfile("${TUNNEL_KEY_FILE}") else None
public_key = open("${TUNNEL_KEY_PUB_FILE}", "r").read().strip() if "${TUNNEL_KEY_PUB_FILE}" and __import__("os").path.isfile("${TUNNEL_KEY_PUB_FILE}") else None

if private_key:
    cluster["tunnel_key_private"] = private_key
if public_key:
    cluster["tunnel_key_public"] = public_key

config = [cluster]
print(json.dumps(config, indent=2, ensure_ascii=False))
PYEOF
}

write_config_json() {
    backup_file "$CONFIG_FILE"
    build_config_json > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    log_ok "Configuration written to ${CONFIG_FILE}"
}

generate_full_config() {
    generate_tunnel_keypair
    setup_authorized_keys
    configure_subio_ssh_daemon
    configure_default_env
    write_current_host_file
    write_config_json
}
