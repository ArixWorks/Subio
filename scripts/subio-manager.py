#!/usr/bin/env python3
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Dict, List, NoReturn, Optional, Tuple


@dataclass(frozen=True)
class ClusterHost:
    name: str
    ipv4: str
    hostkey_ed25519_hpn: str
    hostkey_ed25519_ssh: str
    user: str = "root"
    bind_ip: str = "127.0.0.1"
    hpn_port: int = 2222
    ssh_port: int = 22
    site: Optional[str] = None


@dataclass(frozen=True)
class ClusterConfig:
    name: str
    foreign_hosts: Tuple[ClusterHost, ...]
    domestic_hosts: Tuple[ClusterHost, ...]
    foreign_to_domestic_ports_by_site: Dict[str, Tuple[int, ...]]
    domestic_to_foreign_ports_by_site: Dict[str, Tuple[int, ...]]
    tunnel_key_private: Optional[str] = None
    tunnel_key_public: Optional[str] = None


@dataclass(frozen=True)
class RuntimeConfig:
    clusters: Tuple[ClusterConfig, ...]


@dataclass(frozen=True)
class CurrentHost:
    role: str
    host: ClusterHost


@dataclass(frozen=True)
class ActiveCluster:
    cluster: ClusterConfig
    current_host: CurrentHost


@dataclass(frozen=True)
class TunnelSpec:
    cluster_name: str
    direction: str
    site: str
    source_host: str
    target: ClusterHost
    bind_ip: str
    listen_port: int
    dynamic_mode: str

    @property
    def key(self) -> Tuple[str, str, str, str, int]:
        return (
            self.cluster_name,
            self.direction,
            self.site,
            self.target.name,
            self.listen_port,
        )

    @property
    def name(self) -> str:
        return (
            f"{self.cluster_name}/{self.direction}/"
            f"{self.site}/{self.target.name}/{self.listen_port}"
        )


class ConfigError(Exception):
    pass


CONFIG_FILE = Path(os.environ.get("SUBIO_CONFIG", "/etc/subio/subio.json"))
CURRENT_HOST_FILE = Path(
    os.environ.get("SUBIO_CURRENT_HOST_FILE", "/etc/subio/subio-current-host.txt")
)
MANUAL_IPV4_FILE = Path(os.environ.get("SUBIO_IPV4_FILE", "/etc/subio/ipv4"))
KNOWN_HOSTS_FILE = Path("/tmp/subio-known_hosts")
RESET_REQUEST_DIR = Path(
    os.environ.get("SUBIO_LANE_RESET_REQUEST_DIR", "/var/lib/subio/reset-requests")
)

HOME = Path.home()
SSH_DIR = HOME / ".ssh"
TUNNEL_KEY_FILE = Path(os.environ.get("HPN_TUNNEL_KEY_FILE", SSH_DIR / "tunnel_key"))
TUNNEL_KEY_PUB_FILE = Path(
    os.environ.get("HPN_TUNNEL_KEY_PUB_FILE", SSH_DIR / "tunnel_key.pub")
)
AUTHORIZED_KEYS_FILE = Path(
    os.environ.get("HPN_AUTHORIZED_KEYS_FILE", SSH_DIR / "authorized_keys")
)

SUBIO_SSH_BIN: Optional[str] = os.environ.get("SUBIO_SSH_BIN") or None
SSH_BIN: Optional[str] = os.environ.get("SSH_BIN") or None
IDENTITY_OVERRIDE = os.environ.get("HPN_IDENTITY_FILE") or os.environ.get("IDENTITY_FILE")
IDENTITY_FILE: Optional[str] = None


# Leave cipher selection to SUBIO_SSH by default so it can negotiate the
# threaded mt variant. Set HPN_CIPHER to force a specific cipher.
CIPHER = (os.environ.get("HPN_CIPHER") or "default").strip()
HOSTKEY_ALGORITHMS = "ssh-ed25519"
PUBKEY_ACCEPTED_ALGORITHMS = "ssh-ed25519"
KEX_ALGORITHMS = "curve25519-sha256"
SERVER_ALIVE_INTERVAL = "5"
SERVER_ALIVE_COUNT_MAX = "6"
REKEY_LIMIT = "64G 24h"
IPQOS = "throughput"
CONNECT_TIMEOUT = os.environ.get("HPN_CONNECT_TIMEOUT", "20")
START_DELAY = 0.05
HPN_FAST_FAIL_SECONDS = float(os.environ.get("HPN_FAST_FAIL_SECONDS", "20"))
HPN_FAIL_COOLDOWN_SECONDS = float(os.environ.get("HPN_FAIL_COOLDOWN_SECONDS", "30"))
HPN_PRIMARY_PROVEN_SECONDS = float(os.environ.get("HPN_PRIMARY_PROVEN_SECONDS", "20"))
HPN_COLD_START_RECOVERY_ONLY_SECONDS = float(
    os.environ.get("HPN_COLD_START_RECOVERY_ONLY_SECONDS", "180")
)
HPN_COLD_START_FALLBACK_RETRY_SECONDS = float(
    os.environ.get("HPN_COLD_START_FALLBACK_RETRY_SECONDS", "120")
)
HPN_PRIMARY_RECOVERY_ONLY_SECONDS = float(
    os.environ.get("HPN_PRIMARY_RECOVERY_ONLY_SECONDS", "90")
)
HPN_PRIMARY_FALLBACK_RETRY_SECONDS = float(
    os.environ.get("HPN_PRIMARY_FALLBACK_RETRY_SECONDS", "60")
)
BACKOFF_RESET_AFTER_SECONDS = float(os.environ.get("HPN_BACKOFF_RESET_AFTER_SECONDS", "20"))
BACKOFF_STEPS = [0.5, 1.0, 5.0, 60.0, 300.0, 600.0]
TEST_URL = os.environ.get("HPN_TEST_URL", "https://fsn1-speed.hetzner.com/100MB.bin")
TEST_CONNECT_TIMEOUT = float(os.environ.get("HPN_TEST_CONNECT_TIMEOUT", "10"))
TEST_PORT_PROBE_TIMEOUT = float(os.environ.get("HPN_TEST_PORT_PROBE_TIMEOUT", "1"))
TEST_MAX_TIME = float(os.environ.get("HPN_TEST_MAX_TIME", "0"))


service_stop_event = threading.Event()
reload_event = threading.Event()
processes_lock = threading.Lock()
processes: List[subprocess.Popen] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def fatal(msg: str) -> NoReturn:
    log(f"ERROR: {msg}")
    raise SystemExit(1)


def require_str(raw: dict, key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{CONFIG_FILE}: {context}.{key} must be a non-empty string")
    return value


def first_non_empty_line(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
    except OSError as exc:
        raise ConfigError(f"failed to read {path}: {exc}") from exc
    return None


def load_hosts(raw_hosts: object, field_name: str, require_site: bool) -> Tuple[ClusterHost, ...]:
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ConfigError(f"{CONFIG_FILE}: {field_name} must be a non-empty list")

    hosts: List[ClusterHost] = []
    for index, item in enumerate(raw_hosts, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"{CONFIG_FILE}: {field_name}[{index}] must be an object")
        context = f"{field_name}[{index}]"
        site = item.get("site")
        if require_site:
            if not isinstance(site, str) or not site:
                raise ConfigError(f"{CONFIG_FILE}: {context}.site must be a non-empty string")
        elif site is not None and (not isinstance(site, str) or not site):
            raise ConfigError(f"{CONFIG_FILE}: {context}.site must be a non-empty string")

        try:
            hosts.append(
                ClusterHost(
                    name=require_str(item, "name", context),
                    ipv4=require_str(item, "ipv4", context),
                    hostkey_ed25519_hpn=require_str(item, "hostkey_ed25519_hpn", context),
                    hostkey_ed25519_ssh=require_str(item, "hostkey_ed25519_ssh", context),
                    user=str(item.get("user", "root")),
                    bind_ip=str(item.get("bind_ip", "127.0.0.1")),
                    hpn_port=int(item.get("hpn_port", 2222)),
                    ssh_port=int(item.get("ssh_port", 22)),
                    site=site,
                )
            )
        except ValueError as exc:
            raise ConfigError(f"{CONFIG_FILE}: {context} has invalid integer field: {exc}") from exc
    return tuple(hosts)


def parse_ports_by_site(raw_value: object, field_name: str) -> Dict[str, Tuple[int, ...]]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ConfigError(f"{CONFIG_FILE}: {field_name} must be an object")

    parsed: Dict[str, Tuple[int, ...]] = {}
    for site, ports in raw_value.items():
        if not isinstance(site, str) or not site:
            raise ConfigError(f"{CONFIG_FILE}: {field_name} keys must be non-empty strings")
        if isinstance(ports, int):
            ports = [ports]
        if not isinstance(ports, list):
            raise ConfigError(f"{CONFIG_FILE}: {field_name}[{site!r}] must be an integer list")
        normalized: List[int] = []
        for port in ports:
            if not isinstance(port, int):
                raise ConfigError(
                    f"{CONFIG_FILE}: {field_name}[{site!r}] must only contain integers"
                )
            normalized.append(port)
        parsed[site] = tuple(normalized)
    return parsed


def validate_host_uniqueness(hosts: Tuple[ClusterHost, ...], context: str) -> None:
    seen_names = set()
    seen_ipv4s = set()
    for host in hosts:
        if host.name in seen_names:
            raise ConfigError(f"{CONFIG_FILE}: duplicate host name in {context}: {host.name}")
        if host.ipv4 in seen_ipv4s:
            raise ConfigError(f"{CONFIG_FILE}: duplicate host ipv4 in {context}: {host.ipv4}")
        seen_names.add(host.name)
        seen_ipv4s.add(host.ipv4)


def load_clusters(raw_clusters: object) -> Tuple[ClusterConfig, ...]:
    if not isinstance(raw_clusters, list) or not raw_clusters:
        raise ConfigError(f"{CONFIG_FILE}: root must be a non-empty array of cluster objects")

    clusters: List[ClusterConfig] = []
    seen_names = set()
    for index, item in enumerate(raw_clusters, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"{CONFIG_FILE}: cluster[{index}] must be an object")
        context = f"cluster[{index}]"
        name = require_str(item, "name", context)
        if name in seen_names:
            raise ConfigError(f"{CONFIG_FILE}: duplicate cluster name: {name}")
        seen_names.add(name)

        foreign_hosts = load_hosts(item.get("foreign_hosts"), f"{context}.foreign_hosts", True)
        domestic_hosts = load_hosts(item.get("domestic_hosts"), f"{context}.domestic_hosts", False)
        validate_host_uniqueness(foreign_hosts, f"{name}.foreign_hosts")
        validate_host_uniqueness(domestic_hosts, f"{name}.domestic_hosts")

        foreign_ports = parse_ports_by_site(
            item.get("foreign_to_domestic_ports_by_site"),
            f"{context}.foreign_to_domestic_ports_by_site",
        )
        domestic_ports = parse_ports_by_site(
            item.get("domestic_to_foreign_ports_by_site"),
            f"{context}.domestic_to_foreign_ports_by_site",
        )

        for host in foreign_hosts:
            if host.site not in foreign_ports:
                raise ConfigError(
                    f"{CONFIG_FILE}: cluster {name} missing foreign_to_domestic ports for site {host.site!r}"
                )
        declared_foreign_sites = {host.site for host in foreign_hosts if host.site}
        extra_forward_sites = sorted(site for site in foreign_ports if site not in declared_foreign_sites)
        if extra_forward_sites:
            raise ConfigError(
                f"{CONFIG_FILE}: cluster {name} defines foreign_to_domestic ports for "
                f"unknown site(s): {', '.join(extra_forward_sites)}"
            )
        extra_domestic_sites = sorted(site for site in domestic_ports if site not in declared_foreign_sites)
        if extra_domestic_sites:
            raise ConfigError(
                f"{CONFIG_FILE}: cluster {name} defines domestic_to_foreign ports for "
                f"unknown site(s): {', '.join(extra_domestic_sites)}"
            )

        tunnel_key_private = item.get("tunnel_key_private")
        tunnel_key_public = item.get("tunnel_key_public")
        if tunnel_key_private is not None and not isinstance(tunnel_key_private, str):
            raise ConfigError(f"{CONFIG_FILE}: {context}.tunnel_key_private must be a string")
        if tunnel_key_public is not None and not isinstance(tunnel_key_public, str):
            raise ConfigError(f"{CONFIG_FILE}: {context}.tunnel_key_public must be a string")

        clusters.append(
            ClusterConfig(
                name=name,
                foreign_hosts=foreign_hosts,
                domestic_hosts=domestic_hosts,
                foreign_to_domestic_ports_by_site=foreign_ports,
                domestic_to_foreign_ports_by_site=domestic_ports,
                tunnel_key_private=tunnel_key_private,
                tunnel_key_public=tunnel_key_public,
            )
        )

    return tuple(clusters)


def load_runtime_config() -> RuntimeConfig:
    if not CONFIG_FILE.is_file():
        raise ConfigError(f"missing runtime config: {CONFIG_FILE}")
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"failed to parse {CONFIG_FILE}: {exc}") from exc

    raw_clusters = raw.get("clusters") if isinstance(raw, dict) else raw
    return RuntimeConfig(clusters=load_clusters(raw_clusters))


def detect_primary_ipv4() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def detect_all_ipv4s() -> List[str]:
    ips: List[str] = []
    primary = detect_primary_ipv4()
    if primary:
        ips.append(primary)
    try:
        proc = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split()
                if "inet" not in parts:
                    continue
                ip = parts[parts.index("inet") + 1].split("/", 1)[0]
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


def find_memberships_for_token(config: RuntimeConfig, token: str) -> Tuple[ActiveCluster, ...]:
    matches: List[ActiveCluster] = []
    for cluster in config.clusters:
        role_match: Optional[CurrentHost] = None
        for host in cluster.foreign_hosts:
            if token in {host.name, host.ipv4}:
                if role_match is not None:
                    raise ConfigError(
                        f"{CONFIG_FILE}: token {token!r} is ambiguous inside cluster {cluster.name}"
                    )
                role_match = CurrentHost(role="foreign", host=host)
        for host in cluster.domestic_hosts:
            if token in {host.name, host.ipv4}:
                if role_match is not None:
                    raise ConfigError(
                        f"{CONFIG_FILE}: token {token!r} is ambiguous inside cluster {cluster.name}"
                    )
                role_match = CurrentHost(role="domestic", host=host)
        if role_match is not None:
            matches.append(ActiveCluster(cluster=cluster, current_host=role_match))
    return tuple(matches)


def resolve_active_clusters(config: RuntimeConfig) -> Tuple[str, Tuple[ActiveCluster, ...]]:
    current_host_token = first_non_empty_line(CURRENT_HOST_FILE)
    if current_host_token:
        matches = find_memberships_for_token(config, current_host_token)
        if not matches:
            raise ConfigError(
                f"{CURRENT_HOST_FILE}: {current_host_token!r} was not found in any cluster"
            )
        log(f"Detected current host token from {CURRENT_HOST_FILE}: {current_host_token}")
        return current_host_token, matches

    manual_ipv4 = first_non_empty_line(MANUAL_IPV4_FILE)
    if manual_ipv4:
        matches = find_memberships_for_token(config, manual_ipv4)
        if not matches:
            raise ConfigError(f"{MANUAL_IPV4_FILE}: {manual_ipv4!r} was not found in any cluster")
        log(f"Detected current host token from {MANUAL_IPV4_FILE}: {manual_ipv4}")
        return manual_ipv4, matches

    for ip in detect_all_ipv4s():
        matches = find_memberships_for_token(config, ip)
        if matches:
            log(f"Detected current host from live IPv4 {ip}")
            return ip, matches

    raise ConfigError(
        "could not detect the current host. "
        f"Set {CURRENT_HOST_FILE} to a configured host name or ipv4, "
        f"or provide {MANUAL_IPV4_FILE} with the host IPv4."
    )


def ensure_directory_mode(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)


def derive_public_key(private_key_file: Path) -> Optional[str]:
    ssh_keygen = which("ssh-keygen")
    if not ssh_keygen:
        return None
    proc = subprocess.run(
        [ssh_keygen, "-y", "-f", str(private_key_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def choose_cluster_keypair(active_clusters: Tuple[ActiveCluster, ...]) -> Tuple[Optional[str], Optional[str]]:
    privates = {
        active.cluster.tunnel_key_private.strip()
        for active in active_clusters
        if active.cluster.tunnel_key_private and active.cluster.tunnel_key_private.strip()
    }
    publics = {
        active.cluster.tunnel_key_public.strip()
        for active in active_clusters
        if active.cluster.tunnel_key_public and active.cluster.tunnel_key_public.strip()
    }

    if len(privates) > 1:
        raise ConfigError(
            "multiple active clusters define different tunnel_key_private values; "
            "shared ~/.ssh/tunnel_key would be ambiguous"
        )
    if len(publics) > 1:
        raise ConfigError(
            "multiple active clusters define different tunnel_key_public values; "
            "shared ~/.ssh/tunnel_key.pub would be ambiguous"
        )

    private_key = next(iter(privates), None)
    public_key = next(iter(publics), None)
    return private_key, public_key


def ensure_local_tunnel_auth(active_clusters: Tuple[ActiveCluster, ...]) -> None:
    private_key, public_key = choose_cluster_keypair(active_clusters)
    ensure_directory_mode(SSH_DIR, 0o700)

    desired_private = private_key.rstrip("\n") + "\n" if private_key else None
    existing_private = TUNNEL_KEY_FILE.read_text(encoding="utf-8") if TUNNEL_KEY_FILE.exists() else None
    if desired_private is not None:
        if existing_private != desired_private:
            TUNNEL_KEY_FILE.write_text(desired_private, encoding="utf-8")
            log(f"Updated {TUNNEL_KEY_FILE}")
        TUNNEL_KEY_FILE.chmod(0o600)
    elif not TUNNEL_KEY_FILE.exists():
        raise ConfigError(
            f"{TUNNEL_KEY_FILE} is missing and no active cluster provided tunnel_key_private"
        )
    else:
        TUNNEL_KEY_FILE.chmod(0o600)

    if not public_key:
        if TUNNEL_KEY_PUB_FILE.exists():
            public_key = TUNNEL_KEY_PUB_FILE.read_text(encoding="utf-8").strip()
        else:
            public_key = derive_public_key(TUNNEL_KEY_FILE)

    desired_public = public_key.rstrip("\n") + "\n" if public_key else None
    existing_public = TUNNEL_KEY_PUB_FILE.read_text(encoding="utf-8") if TUNNEL_KEY_PUB_FILE.exists() else None
    if desired_public is not None:
        if existing_public != desired_public:
            TUNNEL_KEY_PUB_FILE.write_text(desired_public, encoding="utf-8")
            log(f"Updated {TUNNEL_KEY_PUB_FILE}")
        TUNNEL_KEY_PUB_FILE.chmod(0o644)
    elif not TUNNEL_KEY_PUB_FILE.exists():
        raise ConfigError(
            f"{TUNNEL_KEY_PUB_FILE} is missing and tunnel_key_public could not be derived"
        )
    else:
        TUNNEL_KEY_PUB_FILE.chmod(0o644)

    if not public_key:
        public_key = TUNNEL_KEY_PUB_FILE.read_text(encoding="utf-8").strip()
    if not public_key:
        raise ConfigError("tunnel public key is empty")

    existing_keys = set()
    if AUTHORIZED_KEYS_FILE.exists():
        existing_keys = {
            line.strip()
            for line in AUTHORIZED_KEYS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    else:
        AUTHORIZED_KEYS_FILE.touch(mode=0o600, exist_ok=True)
    if public_key not in existing_keys:
        with AUTHORIZED_KEYS_FILE.open("a", encoding="utf-8") as handle:
            if existing_keys:
                handle.write("\n")
            handle.write(public_key.rstrip("\n") + "\n")
        log(f"Appended tunnel key to {AUTHORIZED_KEYS_FILE}")
    AUTHORIZED_KEYS_FILE.chmod(0o600)


def select_identity_file() -> Optional[str]:
    candidates: List[Tuple[Path, bool]] = []
    if IDENTITY_OVERRIDE:
        candidates.append((Path(os.path.expanduser(IDENTITY_OVERRIDE)), True))
    candidates.extend(
        [
            (TUNNEL_KEY_FILE, False),
            (SSH_DIR / "id_ed25519", False),
            (SSH_DIR / "id_rsa", False),
        ]
    )

    for candidate, explicit in candidates:
        if candidate.is_file():
            return str(candidate)
        if explicit:
            log(f"WARNING: configured identity file does not exist, ignoring: {candidate}")
    return None


def write_known_hosts_file(active_clusters: Tuple[ActiveCluster, ...]) -> str:
    seen = set()
    lines = []
    for active in active_clusters:
        cluster = active.cluster
        for host in cluster.foreign_hosts + cluster.domestic_hosts:
            hpn_line = f"[{host.ipv4}]:{host.hpn_port} ssh-ed25519 {host.hostkey_ed25519_hpn}"
            ssh_line = f"{host.ipv4} ssh-ed25519 {host.hostkey_ed25519_ssh}"
            if hpn_line not in seen:
                seen.add(hpn_line)
                lines.append(hpn_line)
            if ssh_line not in seen:
                seen.add(ssh_line)
                lines.append(ssh_line)

    content = "\n".join(lines) + "\n"
    if KNOWN_HOSTS_FILE.exists():
        try:
            if KNOWN_HOSTS_FILE.read_text(encoding="utf-8") == content:
                return str(KNOWN_HOSTS_FILE)
        except OSError:
            pass
    KNOWN_HOSTS_FILE.write_text(content, encoding="utf-8")
    KNOWN_HOSTS_FILE.chmod(0o600)
    return str(KNOWN_HOSTS_FILE)


def find_binary(configured: Optional[str], name: str, required: bool) -> Optional[str]:
    if configured:
        return configured
    found = which(name)
    if found:
        return found
    if required:
        fatal(f"required binary not found: {name}")
    return None


def common_ssh_options(strict_host_key: bool = True) -> List[str]:
    opts = [
        "-NT",
        "-q",
        "-4",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "AddressFamily=inet",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        f"KexAlgorithms={KEX_ALGORITHMS}",
        "-o",
        f"HostKeyAlgorithms={HOSTKEY_ALGORITHMS}",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        f"PubkeyAcceptedAlgorithms={PUBKEY_ACCEPTED_ALGORITHMS}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ChallengeResponseAuthentication=no",
        "-o",
        "GSSAPIAuthentication=no",
        "-o",
        "HostbasedAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        "-o",
        "TCPKeepAlive=no",
        "-o",
        "Compression=no",
        "-o",
        f"RekeyLimit={REKEY_LIMIT}",
        "-o",
        f"IPQoS={IPQOS}",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ControlMaster=no",
        "-o",
        "ClearAllForwardings=no",
    ]

    if CIPHER and CIPHER.lower() != "default":
        opts[2:2] = ["-c", CIPHER]

    if strict_host_key:
        opts.extend(
            [
                "-o",
                f"UserKnownHostsFile={KNOWN_HOSTS_FILE}",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
        )
    else:
        opts.extend(
            [
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "StrictHostKeyChecking=no",
            ]
        )

    if IDENTITY_FILE:
        opts.extend(["-i", IDENTITY_FILE])
    return opts


def build_command(
    mode: str,
    binary: str,
    spec: TunnelSpec,
    strict_host_key: bool = True,
) -> List[str]:
    target = spec.target
    if mode in {"subio", "ssh-subio"}:
        port = target.hpn_port
    elif mode == "ssh":
        port = target.ssh_port
    else:
        raise ValueError(f"unknown mode: {mode}")

    cmd = [binary]
    cmd.extend(common_ssh_options(strict_host_key=strict_host_key))
    cmd.extend(["-p", str(port)])

    if spec.dynamic_mode == "remote":
        cmd.extend(["-R", f"{spec.bind_ip}:{spec.listen_port}"])
    elif spec.dynamic_mode == "local":
        cmd.extend(["-D", f"{spec.bind_ip}:{spec.listen_port}"])
    else:
        raise ValueError(f"unknown dynamic mode: {spec.dynamic_mode}")

    cmd.append(f"{target.user}@{target.ipv4}")
    return cmd


def is_host_key_failure(stderr_text: str) -> bool:
    text = stderr_text.lower()
    markers = [
        "host key verification failed",
        "remote host identification has changed",
        "no matching host key",
        "offending",
        "strict checking",
    ]
    return any(marker in text for marker in markers)


def register_process(proc: subprocess.Popen) -> None:
    with processes_lock:
        processes.append(proc)


def unregister_process(proc: subprocess.Popen) -> None:
    with processes_lock:
        if proc in processes:
            processes.remove(proc)


def stop_all_processes() -> None:
    service_stop_event.set()
    with processes_lock:
        current = list(processes)

    for proc in current:
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    deadline = time.time() + 5
    while time.time() < deadline:
        if all(proc.poll() is not None for proc in current):
            return
        time.sleep(0.1)

    for proc in current:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def handle_stop_signal(_signum, _frame) -> None:
    log("Received stop signal.")
    stop_all_processes()


def handle_reload_signal(_signum, _frame) -> None:
    log("Received reload signal.")
    reload_event.set()


class TunnelWorker:
    def __init__(
        self,
        spec: TunnelSpec,
        subio_ssh_bin: Optional[str],
        ssh_bin: str,
    ) -> None:
        self.spec = spec
        self.subio_ssh_bin = subio_ssh_bin
        self.ssh_bin = ssh_bin
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.process: Optional[subprocess.Popen] = None
        self.thread = threading.Thread(target=self.run, daemon=False)

    def start(self) -> None:
        self.thread.start()

    def should_stop(self) -> bool:
        return service_stop_event.is_set() or self.stop_event.is_set()

    def set_process(self, proc: subprocess.Popen) -> None:
        with self.process_lock:
            self.process = proc

    def clear_process(self, proc: subprocess.Popen) -> None:
        with self.process_lock:
            if self.process is proc:
                self.process = None

    def terminate_process(self) -> None:
        with self.process_lock:
            proc = self.process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        self.terminate_process()

    def stop_and_join(self, timeout: float = 10.0) -> None:
        self.stop()
        self.thread.join(timeout=timeout)

    def run(self) -> None:
        spec = self.spec
        name = spec.name
        ssh_fallback_allowed = spec.direction != "foreign_to_domestic"
        hpn_skip_until = 0.0
        hpn_primary_verified = False
        cold_start_hpn_outage_since: Optional[float] = None
        hpn_outage_since: Optional[float] = None
        last_fallback_cycle_at = 0.0
        failure_streak = 0

        while not self.should_stop():
            candidates = []
            now = time.time()
            recovery_only = (
                hpn_primary_verified
                and hpn_outage_since is not None
                and (now - hpn_outage_since) < HPN_PRIMARY_RECOVERY_ONLY_SECONDS
            )
            cold_start_recovery_only = (
                self.subio_ssh_bin is not None
                and not hpn_primary_verified
                and cold_start_hpn_outage_since is not None
                and (now - cold_start_hpn_outage_since)
                < HPN_COLD_START_RECOVERY_ONLY_SECONDS
            )

            if self.subio_ssh_bin and now >= hpn_skip_until:
                candidates.append(("subio", self.subio_ssh_bin))

            if recovery_only:
                if not candidates:
                    wait_for = max(BACKOFF_STEPS[0], hpn_skip_until - now)
                    log(
                        f"[{name}] holding SUBIO as primary during recovery; "
                        f"next SUBIO retry in {wait_for:.1f}s"
                    )
                    time.sleep(wait_for)
                    continue
            elif cold_start_recovery_only:
                if not candidates:
                    remaining = HPN_COLD_START_RECOVERY_ONLY_SECONDS - (
                        now - cold_start_hpn_outage_since
                    )
                    wait_for = max(
                        BACKOFF_STEPS[0],
                        min(remaining, max(BACKOFF_STEPS[0], hpn_skip_until - now)),
                    )
                    log(
                        f"[{name}] delaying SSH fallback for another "
                        f"{remaining:.1f}s while giving SUBIO first claim "
                        f"on a fresh lane"
                    )
                    time.sleep(wait_for)
                    continue
            elif hpn_primary_verified and hpn_outage_since is not None:
                retry_fallbacks = (
                    now - last_fallback_cycle_at
                ) >= HPN_PRIMARY_FALLBACK_RETRY_SECONDS
                if ssh_fallback_allowed and retry_fallbacks:
                    candidates.append(("ssh-subio", self.ssh_bin))
                    candidates.append(("ssh", self.ssh_bin))
                elif not candidates:
                    wait_for = max(
                        BACKOFF_STEPS[0],
                        HPN_PRIMARY_FALLBACK_RETRY_SECONDS - (now - last_fallback_cycle_at),
                    )
                    log(
                        f"[{name}] waiting {wait_for:.1f}s before another SSH fallback cycle; "
                        f"SUBIO remains the preferred primary"
                    )
                    time.sleep(wait_for)
                    continue
            elif self.subio_ssh_bin is not None and cold_start_hpn_outage_since is not None:
                retry_fallbacks = (
                    now - last_fallback_cycle_at
                ) >= HPN_COLD_START_FALLBACK_RETRY_SECONDS
                if ssh_fallback_allowed and retry_fallbacks:
                    candidates.append(("ssh-subio", self.ssh_bin))
                    candidates.append(("ssh", self.ssh_bin))
                elif not candidates:
                    wait_for = max(
                        BACKOFF_STEPS[0],
                        HPN_COLD_START_FALLBACK_RETRY_SECONDS
                        - (now - last_fallback_cycle_at),
                    )
                    log(
                        f"[{name}] waiting {wait_for:.1f}s before another "
                        f"cold-start SSH fallback cycle; SUBIO is still preferred"
                    )
                    time.sleep(wait_for)
                    continue
            else:
                if ssh_fallback_allowed:
                    candidates.append(("ssh-subio", self.ssh_bin))
                    candidates.append(("ssh", self.ssh_bin))

            if not candidates:
                time.sleep(BACKOFF_STEPS[0])
                continue

            cycle_had_long_lived_session = False
            for mode, binary in candidates:
                if self.should_stop():
                    return

                target = spec.target
                transport_port = target.hpn_port if mode in {"subio", "ssh-subio"} else target.ssh_port
                attempts = [("strict", True), ("relaxed-hostkey", False)]
                last_code = 0
                last_runtime = 0.0
                for _attempt_name, strict_host_key in attempts:
                    cmd = build_command(
                        mode=mode,
                        binary=binary,
                        spec=spec,
                        strict_host_key=strict_host_key,
                    )
                    host_key_note = "" if strict_host_key else " (host-key fallback)"
                    bind_side = "remote" if spec.dynamic_mode == "remote" else "local"
                    log(
                        f"[{name}] starting {mode.upper()} tunnel{host_key_note}: "
                        f"{bind_side} SOCKS {spec.bind_ip}:{spec.listen_port} -> "
                        f"{target.user}@{target.ipv4}:{transport_port}"
                    )

                    started_at = time.time()
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    register_process(proc)
                    self.set_process(proc)
                    _, stderr_text = proc.communicate()
                    self.clear_process(proc)
                    last_code = proc.returncode
                    last_runtime = time.time() - started_at
                    unregister_process(proc)

                    if self.should_stop():
                        return

                    log(
                        f"[{name}] {mode.upper()} exited with code {last_code} "
                        f"after {last_runtime:.1f}s"
                    )

                    if last_runtime >= BACKOFF_RESET_AFTER_SECONDS:
                        cycle_had_long_lived_session = True
                    if mode == "subio" and last_runtime >= HPN_PRIMARY_PROVEN_SECONDS:
                        if not hpn_primary_verified:
                            log(
                                f"[{name}] SUBIO stayed up for {last_runtime:.1f}s; "
                                f"marking it as the primary transport"
                            )
                        hpn_primary_verified = True
                        cold_start_hpn_outage_since = None
                        hpn_outage_since = None
                        last_fallback_cycle_at = 0.0

                    if last_code == 0:
                        break
                    if strict_host_key and is_host_key_failure(stderr_text or ""):
                        log(
                            f"[{name}] static host-key verification failed; "
                            f"retrying {mode.upper()} once without pinned fingerprint"
                        )
                        continue
                    break

                if mode == "subio":
                    if not hpn_primary_verified and cold_start_hpn_outage_since is None:
                        cold_start_hpn_outage_since = time.time()
                        log(
                            f"[{name}] SUBIO has not proven itself yet; delaying SSH "
                            f"fallback for {HPN_COLD_START_RECOVERY_ONLY_SECONDS:.0f}s"
                        )
                    if hpn_primary_verified and hpn_outage_since is None:
                        hpn_outage_since = time.time()
                        log(
                            f"[{name}] primary SUBIO session dropped; "
                            f"entering SUBIO-first recovery mode"
                        )
                    if last_code != 0 and last_runtime <= HPN_FAST_FAIL_SECONDS:
                        hpn_skip_until = time.time() + HPN_FAIL_COOLDOWN_SECONDS
                        log(
                            f"[{name}] SUBIO fast-failed; cooling SUBIO retries for "
                            f"{HPN_FAIL_COOLDOWN_SECONDS:.0f}s"
                        )
                    if (
                        hpn_primary_verified
                        and hpn_outage_since is not None
                        and (time.time() - hpn_outage_since) < HPN_PRIMARY_RECOVERY_ONLY_SECONDS
                    ):
                        break
                    if (
                        not hpn_primary_verified
                        and cold_start_hpn_outage_since is not None
                        and (time.time() - cold_start_hpn_outage_since)
                        < HPN_COLD_START_RECOVERY_ONLY_SECONDS
                    ):
                        break
                    if not ssh_fallback_allowed:
                        delay = BACKOFF_STEPS[min(failure_streak, len(BACKOFF_STEPS) - 1)]
                        log(
                            f"[{name}] SUBIO-only mode: retrying in {delay:.1f}s "
                            f"(failure streak {failure_streak + 1})"
                        )
                        time.sleep(delay)
                        failure_streak += 1
                        break
                    log(f"[{name}] falling back to OpenSSH on port {target.hpn_port}")
                    continue

                if mode == "ssh-subio":
                    if hpn_primary_verified and hpn_outage_since is not None:
                        last_fallback_cycle_at = time.time()
                    log(f"[{name}] falling back to normal SSH on port {target.ssh_port}")
                    continue

                if hpn_primary_verified and hpn_outage_since is not None:
                    last_fallback_cycle_at = time.time()
                if cycle_had_long_lived_session and failure_streak:
                    log(f"[{name}] resetting retry backoff after stable tunnel runtime")
                    failure_streak = 0

                delay = BACKOFF_STEPS[min(failure_streak, len(BACKOFF_STEPS) - 1)]
                log(
                    f"[{name}] all modes failed; retrying in {delay:.1f}s "
                    f"(failure streak {failure_streak + 1})"
                )
                time.sleep(delay)
                failure_streak += 1
                break
            else:
                if cycle_had_long_lived_session and failure_streak:
                    log(f"[{name}] resetting retry backoff after stable tunnel runtime")
                    failure_streak = 0


def worker_key_from_reset_request(payload: dict[str, object]) -> Optional[Tuple[str, str, str, str, int]]:
    cluster_name = payload.get("cluster_name")
    direction = payload.get("direction")
    site = payload.get("site")
    target_name = payload.get("target_name")
    listen_port = payload.get("listen_port")
    if not isinstance(cluster_name, str) or not cluster_name:
        return None
    if not isinstance(direction, str) or not direction:
        return None
    if not isinstance(site, str) or not site:
        return None
    if not isinstance(target_name, str) or not target_name:
        return None
    try:
        port = int(listen_port)
    except Exception:
        return None
    return (cluster_name, direction, site, target_name, port)


def consume_lane_reset_requests() -> list[dict[str, object]]:
    if not RESET_REQUEST_DIR.is_dir():
        return []

    requests: list[dict[str, object]] = []
    for path in sorted(RESET_REQUEST_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"[manager] ignoring invalid lane reset request {path}: {exc}")
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            path.unlink()
        except OSError as exc:
            log(f"[manager] failed to remove processed lane reset request {path}: {exc}")
        if isinstance(payload, dict):
            requests.append(payload)
        else:
            log(f"[manager] ignoring non-object lane reset request {path}")
    return requests


def apply_lane_reset_requests(
    workers: Dict[Tuple[str, str, str, str, int], TunnelWorker],
    requests: list[dict[str, object]],
    subio_ssh_bin: Optional[str],
    ssh_bin: str,
) -> int:
    reset_count = 0
    for payload in requests:
        key = worker_key_from_reset_request(payload)
        reason = str(payload.get("reason") or "manual lane reset request")
        if key is None:
            log(f"[manager] ignoring malformed lane reset request: {payload!r}")
            continue
        worker = workers.get(key)
        if worker is None:
            log(f"[manager] lane reset request ignored; worker not found for key={key!r}")
            continue
        log(f"[manager] resetting tunnel {worker.spec.name}: {reason}")
        worker.stop_and_join()
        del workers[key]
        replacement = TunnelWorker(spec=worker.spec, subio_ssh_bin=subio_ssh_bin, ssh_bin=ssh_bin)
        workers[key] = replacement
        replacement.start()
        reset_count += 1
        time.sleep(START_DELAY)
    return reset_count


def validate_tunnel_conflicts(specs: Dict[Tuple[str, str, str, str, int], TunnelSpec]) -> None:
    local_conflicts: Dict[Tuple[str, int], TunnelSpec] = {}
    remote_conflicts: Dict[Tuple[str, str, int], TunnelSpec] = {}
    for spec in specs.values():
        if spec.dynamic_mode == "local":
            signature = (spec.bind_ip, spec.listen_port)
            previous = local_conflicts.get(signature)
            if previous:
                raise ConfigError(
                    "local tunnel conflict: "
                    f"{previous.name} and {spec.name} both want {spec.bind_ip}:{spec.listen_port}"
                )
            local_conflicts[signature] = spec
        else:
            signature = (spec.target.ipv4, spec.bind_ip, spec.listen_port)
            previous = remote_conflicts.get(signature)
            if previous:
                raise ConfigError(
                    "remote tunnel conflict: "
                    f"{previous.name} and {spec.name} both want "
                    f"{spec.target.ipv4}:{spec.bind_ip}:{spec.listen_port}"
                )
            remote_conflicts[signature] = spec


def build_tunnel_specs(
    active_clusters: Tuple[ActiveCluster, ...]
) -> Dict[Tuple[str, str, str, str, int], TunnelSpec]:
    specs: Dict[Tuple[str, str, str, str, int], TunnelSpec] = {}

    for active in active_clusters:
        cluster = active.cluster
        current_host = active.current_host
        if current_host.role == "foreign":
            site = current_host.host.site
            if not site:
                raise ConfigError(
                    f"{CONFIG_FILE}: foreign host {current_host.host.name} is missing site"
                )
            ports = cluster.foreign_to_domestic_ports_by_site.get(site)
            if not ports:
                raise ConfigError(
                    f"{CONFIG_FILE}: cluster {cluster.name} missing foreign_to_domestic ports for site {site}"
                )
            
            foreign_hosts_in_site = [h for h in cluster.foreign_hosts if h.site == site]
            try:
                host_idx = foreign_hosts_in_site.index(current_host.host)
            except ValueError:
                host_idx = 0
                
            if host_idx < len(ports):
                my_ports = [ports[host_idx]]
            else:
                log(f"[manager] WARNING: not enough ports configured for site {site}. "
                    f"Host {current_host.host.name} (index {host_idx}) gets no port.")
                my_ports = []
                
            for target in cluster.domestic_hosts:
                for port in my_ports:
                    spec = TunnelSpec(
                        cluster_name=cluster.name,
                        direction="foreign_to_domestic",
                        site=site,
                        source_host=current_host.host.name,
                        target=target,
                        bind_ip=target.bind_ip,
                        listen_port=port,
                        dynamic_mode="remote",
                    )
                    if spec.key in specs:
                        raise ConfigError(f"duplicate tunnel definition: {spec.name}")
                    specs[spec.key] = spec
        else:
            for target in cluster.foreign_hosts:
                site = target.site
                if not site:
                    raise ConfigError(
                        f"{CONFIG_FILE}: foreign host {target.name} is missing site"
                    )
                ports = cluster.domestic_to_foreign_ports_by_site.get(site)
                if not ports:
                    continue
                    
                foreign_hosts_in_site = [h for h in cluster.foreign_hosts if h.site == site]
                try:
                    host_idx = foreign_hosts_in_site.index(target)
                except ValueError:
                    host_idx = 0
                    
                if host_idx < len(ports):
                    my_ports = [ports[host_idx]]
                else:
                    my_ports = []
                    
                for port in my_ports:
                    spec = TunnelSpec(
                        cluster_name=cluster.name,
                        direction="domestic_to_foreign",
                        site=site,
                        source_host=current_host.host.name,
                        target=target,
                        bind_ip=current_host.host.bind_ip,
                        listen_port=port,
                        dynamic_mode="local",
                    )
                    if spec.key in specs:
                        raise ConfigError(f"duplicate tunnel definition: {spec.name}")
                    specs[spec.key] = spec

    validate_tunnel_conflicts(specs)
    return specs


def reconcile_workers(
    workers: Dict[Tuple[str, str, str, str, int], TunnelWorker],
    desired_specs: Dict[Tuple[str, str, str, str, int], TunnelSpec],
    subio_ssh_bin: Optional[str],
    ssh_bin: str,
) -> Tuple[int, int, int]:
    added = 0
    removed = 0
    restarted = 0

    for key, worker in list(workers.items()):
        desired = desired_specs.get(key)
        if desired is None:
            log(f"[manager] removing tunnel {worker.spec.name}")
            worker.stop_and_join()
            del workers[key]
            removed += 1
            continue
        if worker.spec != desired:
            log(f"[manager] reloading tunnel {worker.spec.name}")
            worker.stop_and_join()
            del workers[key]
            restarted += 1

    for key, spec in desired_specs.items():
        if key in workers:
            continue
        log(f"[manager] starting tunnel {spec.name}")
        worker = TunnelWorker(spec=spec, subio_ssh_bin=subio_ssh_bin, ssh_bin=ssh_bin)
        workers[key] = worker
        worker.start()
        added += 1
        time.sleep(START_DELAY)

    return added, removed, restarted


def stop_workers(workers: Dict[Tuple[str, str, str, str, int], TunnelWorker]) -> None:
    for worker in workers.values():
        worker.stop()
    for worker in workers.values():
        worker.thread.join(timeout=10.0)


def load_live_runtime() -> Tuple[RuntimeConfig, str, Tuple[ActiveCluster, ...], Dict[Tuple[str, str, str, str, int], TunnelSpec]]:
    config = load_runtime_config()
    current_token, active_clusters = resolve_active_clusters(config)
    if not active_clusters:
        raise ConfigError(f"{CONFIG_FILE}: no active clusters matched current host token {current_token!r}")

    ensure_local_tunnel_auth(active_clusters)

    global IDENTITY_FILE
    IDENTITY_FILE = select_identity_file()
    if not IDENTITY_FILE:
        raise ConfigError("no usable SSH identity file found after tunnel-key provisioning")

    write_known_hosts_file(active_clusters)
    specs = build_tunnel_specs(active_clusters)
    return config, current_token, active_clusters, specs


def summarize_active_clusters(active_clusters: Tuple[ActiveCluster, ...]) -> str:
    parts = []
    for active in active_clusters:
        cluster = active.cluster
        current_host = active.current_host
        summary = f"{cluster.name}:{current_host.role}:{current_host.host.name}"
        if current_host.host.site:
            summary += f"({current_host.host.site})"
        parts.append(summary)
    return ", ".join(parts)


def parse_cli(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="subio-manager.py",
        description="SubIO/OpenSSH reverse SOCKS tunnel manager and tester",
    )
    subparsers = parser.add_subparsers(dest="command")

    test_parser = subparsers.add_parser(
        "test",
        help="test local SOCKS speed through one or more ports",
    )
    test_parser.add_argument(
        "ports",
        metavar="PORT",
        nargs="+",
        type=int,
        help="local SOCKS port to test, for example 10810",
    )
    test_parser.add_argument(
        "--bind-ip",
        default="127.0.0.1",
        help="SOCKS bind IP to test against (default: 127.0.0.1)",
    )
    test_parser.add_argument(
        "--url",
        default=TEST_URL,
        help=f"download URL for the speed test (default: {TEST_URL})",
    )
    test_parser.add_argument(
        "--connect-timeout",
        type=float,
        default=TEST_CONNECT_TIMEOUT,
        help=f"curl connect timeout in seconds (default: {TEST_CONNECT_TIMEOUT:g})",
    )
    test_parser.add_argument(
        "--port-probe-timeout",
        type=float,
        default=TEST_PORT_PROBE_TIMEOUT,
        help=f"local port probe timeout in seconds (default: {TEST_PORT_PROBE_TIMEOUT:g})",
    )
    test_parser.add_argument(
        "--max-time",
        type=float,
        default=TEST_MAX_TIME,
        help=(
            "overall curl max-time in seconds; 0 disables it "
            f"(default: {TEST_MAX_TIME:g})"
        ),
    )
    test_parser.add_argument(
        "--curl-bin",
        default=os.environ.get("CURL_BIN", "curl"),
        help="curl binary or path to use (default: curl)",
    )
    return parser.parse_args(argv)


def resolve_binary(path_or_name: str) -> str:
    if os.path.sep in path_or_name:
        if os.access(path_or_name, os.X_OK):
            return path_or_name
        fatal(f"binary is not executable: {path_or_name}")
    found = which(path_or_name)
    if found:
        return found
    fatal(f"required binary not found: {path_or_name}")


def probe_local_port(bind_ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((bind_ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_socks_port(
    curl_bin: str,
    bind_ip: str,
    port: int,
    url: str,
    connect_timeout: float,
    max_time: float,
) -> Tuple[bool, str]:
    curl_cmd = [
        curl_bin,
        "--silent",
        "--show-error",
        "--location",
        "--output",
        "/dev/null",
        "--write-out",
        "%{speed_download}",
        "--proxy",
        f"socks5h://{bind_ip}:{port}",
        "--connect-timeout",
        str(connect_timeout),
        url,
    ]
    if max_time > 0:
        curl_cmd.extend(["--max-time", str(max_time)])

    proc = subprocess.run(
        curl_cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or f"curl exited with code {proc.returncode}"
        return False, message

    raw_speed = proc.stdout.strip()
    try:
        speed_bytes_per_second = float(raw_speed)
    except ValueError:
        return False, f"unexpected curl speed output: {raw_speed!r}"

    speed_megabytes = speed_bytes_per_second / 1_000_000
    speed_megabits = speed_bytes_per_second * 8 / 1_000_000
    return True, f"Speed: {speed_megabytes:.2f} MB/s = {speed_megabits:.2f} Mbps"


def run_test_mode(args: argparse.Namespace) -> int:
    curl_bin = resolve_binary(args.curl_bin)
    exit_code = 0
    for port in args.ports:
        label = f"{args.bind_ip}:{port}"
        if not probe_local_port(args.bind_ip, port, args.port_probe_timeout):
            print(f"[{label}] DOWN: local SOCKS port is not listening", flush=True)
            exit_code = 1
            continue
        ok, message = test_socks_port(
            curl_bin=curl_bin,
            bind_ip=args.bind_ip,
            port=port,
            url=args.url,
            connect_timeout=args.connect_timeout,
            max_time=args.max_time,
        )
        prefix = "OK" if ok else "FAIL"
        print(f"[{label}] {prefix}: {message}", flush=True)
        if not ok:
            exit_code = 1
    return exit_code


def main() -> int:
    args = parse_cli(sys.argv[1:])
    if args.command == "test":
        return run_test_mode(args)

    signal.signal(signal.SIGTERM, handle_stop_signal)
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGHUP, handle_reload_signal)

    try:
        _config, current_token, active_clusters, desired_specs = load_live_runtime()
    except ConfigError as exc:
        fatal(str(exc))

    subio_ssh_bin = find_binary(SUBIO_SSH_BIN, "subio-ssh", required=False)
    ssh_bin = find_binary(SSH_BIN, "ssh", required=True)
    workers: Dict[Tuple[str, str, str, str, int], TunnelWorker] = {}

    log("Starting SubIO tunnel manager")
    log(f"Config:         {CONFIG_FILE}")
    log(f"Current token:  {current_token}")
    log(f"Current host id:{CURRENT_HOST_FILE}")
    log(f"Active clusters:{summarize_active_clusters(active_clusters)}")
    log(f"subio-ssh:         {subio_ssh_bin or 'not found; normal ssh only'}")
    log(f"ssh:            {ssh_bin}")
    log(f"Identity:       {IDENTITY_FILE}")
    log(f"Cipher:         {CIPHER or 'default'}")
    log(f"Tunnels:        {len(desired_specs)}")

    added, removed, restarted = reconcile_workers(
        workers=workers,
        desired_specs=desired_specs,
        subio_ssh_bin=subio_ssh_bin,
        ssh_bin=ssh_bin,
    )
    log(
        f"[manager] initial reconcile complete: "
        f"added={added} removed={removed} restarted={restarted} active={len(workers)}"
    )

    while not service_stop_event.is_set():
        reload_event.wait(timeout=1.0)
        if service_stop_event.is_set():
            break
        if reload_event.is_set():
            reload_event.clear()

            try:
                _next_config, next_token, next_active_clusters, next_specs = load_live_runtime()
            except ConfigError as exc:
                log(f"[manager] reload rejected: {exc}")
                continue

            added, removed, restarted = reconcile_workers(
                workers=workers,
                desired_specs=next_specs,
                subio_ssh_bin=subio_ssh_bin,
                ssh_bin=ssh_bin,
            )
            current_token = next_token
            active_clusters = next_active_clusters
            desired_specs = next_specs
            log(
                f"[manager] reload applied: "
                f"added={added} removed={removed} restarted={restarted} active={len(workers)}"
            )

        requests = consume_lane_reset_requests()
        if requests:
            reset_count = apply_lane_reset_requests(
                workers=workers,
                requests=requests,
                subio_ssh_bin=subio_ssh_bin,
                ssh_bin=ssh_bin,
            )
            log(
                f"[manager] lane reset requests applied: "
                f"requested={len(requests)} reset={reset_count} active={len(workers)}"
            )

    stop_workers(workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
