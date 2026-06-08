#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SUBIO-TUN  —  install_subio_ssh.sh
#  Install SUBIO-SSH from PPA (Ubuntu/Debian) or compile from source.
# ─────────────────────────────────────────────────────────────

install_subio_ssh() {
    log_step "Installing SUBIO-SSH"

    # Already installed?
    if [[ -x /opt/subio-ssh/bin/subio-ssh ]]; then
        local ver
        ver=$(/opt/subio-ssh/bin/subio-ssh -V 2>&1 | head -1 || echo "unknown")
        log_ok "SUBIO-SSH already installed: ${ver}"
        return 0
    fi

    detect_os

    if is_debian_based; then
        _install_subio_ssh_ppa
    elif is_rhel_based; then
        _install_subio_ssh_copr
    else
        _install_subio_ssh_source
    fi

    # Verify
    if [[ -x /opt/subio-ssh/bin/subio-ssh ]]; then
        log_ok "SUBIO-SSH installed successfully"
        /opt/subio-ssh/bin/subio-ssh -V 2>&1 || true
    elif command -v subio-ssh &>/dev/null; then
        # PPA installs to /usr/bin — create symlinks in /opt/subio-ssh
        _create_opt_symlinks
        log_ok "SUBIO-SSH installed via package manager"
    else
        log_error "SUBIO-SSH installation failed!"
        return 1
    fi
}

_install_subio_ssh_ppa() {
    log_info "Attempting PPA install for ${OS_ID}..."

    # Try PPA first (Ubuntu)
    if command -v add-apt-repository &>/dev/null; then
        if add-apt-repository -y ppa:rapier1/subio-ssh 2>/dev/null; then
            apt-get update -y
            if apt-get install -y subio-ssh 2>/dev/null; then
                log_ok "SUBIO-SSH installed from PPA"
                _create_opt_symlinks
                return 0
            fi
        fi
        log_warn "PPA install failed, falling back to source build..."
    fi

    _install_subio_ssh_source
}

_install_subio_ssh_copr() {
    log_info "Attempting COPR install for ${OS_ID}..."

    if command -v dnf &>/dev/null; then
        if dnf copr enable -y rapier1/subio-ssh 2>/dev/null; then
            if dnf install -y subio-ssh 2>/dev/null; then
                log_ok "SUBIO-SSH installed from COPR"
                _create_opt_symlinks
                return 0
            fi
        fi
    fi

    log_warn "COPR install failed, falling back to source build..."
    _install_subio_ssh_source
}

_install_subio_ssh_source() {
    log_info "Building SUBIO-SSH from source..."

    # Install build dependencies
    if is_debian_based; then
        pkg_update
        ensure_packages build-essential autoconf automake \
            libssl-dev zlib1g-dev libpam0g-dev \
            libselinux1-dev git
    elif is_rhel_based; then
        pkg_update
        ensure_packages gcc make autoconf automake \
            openssl-devel zlib-devel pam-devel \
            libselinux-devel git
    fi

    local build_dir="/tmp/subio-ssh-build-$$"
    mkdir -p "$build_dir"

    log_sub "Cloning SUBIO-SSH repository..."
    git clone --depth=1 https://github.com/rapier1/subio-ssh.git "$build_dir/subio-ssh"

    cd "$build_dir/subio-ssh"

    log_sub "Running autoreconf..."
    autoreconf -f -i

    log_sub "Configuring (prefix=/opt/subio-ssh)..."
    ./configure \
        --prefix=/opt/subio-ssh \
        --sysconfdir=/etc/subio-ssh \
        --with-privsep-path=/var/empty \
        --with-privsep-user=subio-sshd \
        --with-pam \
        --with-ssl-dir=/usr \
        --with-pid-dir=/var/run \
        2>&1 | tail -5

    log_sub "Compiling (this may take a few minutes)..."
    make -j"$(nproc)" 2>&1 | tail -3

    log_sub "Installing to /opt/subio-ssh..."
    make install 2>&1 | tail -3

    # Clean up
    cd /
    rm -rf "$build_dir"

    log_ok "SUBIO-SSH compiled and installed to /opt/subio-ssh"
}

_create_opt_symlinks() {
    # If SUBIO-SSH was installed from PPA, binaries may be in /usr/bin.
    # Create /opt/subio-ssh structure with symlinks for compatibility.
    local bins=( subio-ssh hpnscp subio-ssh-add subio-ssh-agent subio-ssh-keygen subio-ssh-keyscan )
    local sbins=( subio-sshd )

    ensure_dir /opt/subio-ssh/bin
    ensure_dir /opt/subio-ssh/sbin

    for b in "${bins[@]}"; do
        local src
        src=$(command -v "$b" 2>/dev/null || true)
        if [[ -n "$src" && ! -e "/opt/subio-ssh/bin/$b" ]]; then
            ln -sf "$src" "/opt/subio-ssh/bin/$b"
            log_debug "Symlinked $src → /opt/subio-ssh/bin/$b"
        fi
    done

    for b in "${sbins[@]}"; do
        local src
        src=$(command -v "$b" 2>/dev/null || true)
        [[ -z "$src" ]] && src="/usr/sbin/$b"
        if [[ -x "$src" && ! -e "/opt/subio-ssh/sbin/$b" ]]; then
            ln -sf "$src" "/opt/subio-ssh/sbin/$b"
            log_debug "Symlinked $src → /opt/subio-ssh/sbin/$b"
        fi
    done

    # libexec
    ensure_dir /opt/subio-ssh/libexec
    local libexecs=( subio-ssh-keysign subio-ssh-pkcs11-helper subio-ssh-sk-helper subio-sshd-auth subio-sshd-session )
    for b in "${libexecs[@]}"; do
        local src
        for search in /usr/libexec /usr/lib/openssh /usr/lib/ssh; do
            if [[ -x "$search/$b" ]]; then
                src="$search/$b"
                break
            fi
        done
        if [[ -n "${src:-}" && ! -e "/opt/subio-ssh/libexec/$b" ]]; then
            ln -sf "$src" "/opt/subio-ssh/libexec/$b"
        fi
    done
}

install_prerequisites() {
    log_step "Installing Prerequisites"

    detect_os
    log_info "Detected OS: ${OS_NAME}"

    pkg_update

    if is_debian_based; then
        ensure_packages python3 curl jq openssl ca-certificates \
            iproute2 net-tools procps
    elif is_rhel_based; then
        ensure_packages python3 curl jq openssl ca-certificates \
            iproute net-tools procps-ng
    else
        log_warn "Unsupported distro. Please ensure python3, curl, jq, openssl are installed."
    fi

    log_ok "Prerequisites installed"
}
