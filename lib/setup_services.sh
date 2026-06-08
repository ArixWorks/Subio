#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SUBIO-TUN  —  setup_services.sh
#  Install systemd services and Python scripts.
# ─────────────────────────────────────────────────────────────

install_python_scripts() {
    log_step "Installing Tunnel Scripts"

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

    # Copy Python scripts
    for script in subio-manager.py subio-lane-guard.py; do
        local src="${script_dir}/scripts/${script}"
        local dst="/usr/local/bin/${script}"
        if [[ -f "$src" ]]; then
            backup_file "$dst"
            cp "$src" "$dst"
            chmod 755 "$dst"
            log_ok "Installed ${dst}"
        else
            log_error "Script not found: ${src}"
            return 1
        fi
    done
}

install_systemd_services() {
    log_step "Installing Systemd Services"

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    local svc_dir="${script_dir}/configs/systemd"

    for svc in subio-ssh.service subio-manager.service subio-lane-guard.service; do
        local src="${svc_dir}/${svc}"
        local dst="/etc/systemd/system/${svc}"
        if [[ -f "$src" ]]; then
            backup_file "$dst"
            cp "$src" "$dst"
            chmod 644 "$dst"
            log_sub "Installed ${svc}"
        else
            log_warn "Service file not found: ${src}"
        fi
    done

    # Create privilege separation user for SUBIO-SSHD
    if ! getent group subio-sshd &>/dev/null; then
        groupadd --system subio-sshd 2>/dev/null || true
    fi
    if ! id -u subio-sshd &>/dev/null 2>&1; then
        useradd --system -g subio-sshd -d /var/empty \
            -s /usr/sbin/nologin -c "SUBIO SSHD privilege separation" subio-sshd 2>/dev/null || true
    fi
    mkdir -p /var/empty

    # Reload systemd
    systemctl daemon-reload
    log_ok "Systemd units reloaded"
}

enable_and_start_services() {
    log_step "Starting Services"

    local role="${SERVER_ROLE:-iran}"

    # SUBIO-SSH daemon — needed on BOTH sides (domestic listens for incoming
    # tunnels, foreign uses the binary as client)
    log_sub "Enabling subio-ssh.service..."
    systemctl enable subio-ssh.service 2>/dev/null || true
    systemctl restart subio-ssh.service
    if systemctl is-active --quiet subio-ssh.service; then
        log_ok "subio-ssh.service: active ✓"
    else
        log_warn "subio-ssh.service: failed to start (check: journalctl -u subio-ssh)"
    fi

    # SUBIO-REVERSE tunnel manager — runs on FOREIGN servers
    # (it initiates -R reverse tunnels TO the domestic hosts)
    if [[ "$role" == "kharej" ]]; then
        log_sub "Enabling subio-manager.service..."
        systemctl enable subio-manager.service 2>/dev/null || true
        systemctl restart subio-manager.service
        sleep 2
        if systemctl is-active --quiet subio-manager.service; then
            log_ok "subio-manager.service: active ✓"
        else
            log_warn "subio-manager.service: may take a moment to connect"
            log_sub "Check: journalctl -u subio-manager -f"
        fi
    else
        # On domestic (Iran) — the reverse service listens for incoming
        # tunnels from the foreign server. It still runs to manage
        # any domestic_to_foreign tunnels, but primarily the foreign
        # server drives the connection.
        log_sub "Enabling subio-manager.service (monitoring mode)..."
        systemctl enable subio-manager.service 2>/dev/null || true
        systemctl restart subio-manager.service
        if systemctl is-active --quiet subio-manager.service; then
            log_ok "subio-manager.service: active ✓"
        else
            log_sub "Service will activate once foreign server connects"
        fi
    fi

    # Open firewall for SUBIO port if needed
    open_firewall_port "${HPN_PORT:-2222}" "SUBIO-SSH"
}

open_firewall_port() {
    local port="$1"
    local label="$2"

    # UFW
    if command -v ufw &>/dev/null && ufw status | grep -q "active"; then
        ufw allow "$port/tcp" comment "$label" 2>/dev/null || true
        log_sub "UFW: opened port ${port}/tcp (${label})"
    fi

    # firewalld
    if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
        firewall-cmd --permanent --add-port="${port}/tcp" 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        log_sub "firewalld: opened port ${port}/tcp (${label})"
    fi

    # iptables fallback — only if no ufw/firewalld
    if ! command -v ufw &>/dev/null && ! command -v firewall-cmd &>/dev/null; then
        if command -v iptables &>/dev/null; then
            if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
                iptables -I INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
                log_sub "iptables: opened port ${port}/tcp (${label})"
            fi
        fi
    fi
}

setup_all_services() {
    install_python_scripts
    install_systemd_services
    enable_and_start_services
}
