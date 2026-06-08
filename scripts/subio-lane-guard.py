#!/usr/bin/env python3
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


CONFIG_FILE = Path(os.environ.get("HPN_REVERSE_CONFIG", "/etc/subio-manager.json"))
CURRENT_HOST_FILE = Path(
    os.environ.get("HPN_REVERSE_CURRENT_HOST_FILE", "/etc/subio-manager-current-host.txt")
)
STATE_FILE = Path("/var/lib/subio-lane-guard/state.json")
RESET_REQUEST_DIR = Path(
    os.environ.get("HPN_LANE_RESET_REQUEST_DIR", "/var/lib/subio-manager/reset-requests")
)
PROBE_URL = os.environ.get("HPN_LANE_GUARD_PROBE_URL", "https://api.ipify.org")
CONNECT_TIMEOUT = float(os.environ.get("HPN_LANE_GUARD_CONNECT_TIMEOUT", "5"))
MAX_TIME = float(os.environ.get("HPN_LANE_GUARD_MAX_TIME", "8"))
FAILURE_THRESHOLD = int(os.environ.get("HPN_LANE_GUARD_FAILURE_THRESHOLD", "2"))
RESTART_COOLDOWN = float(os.environ.get("HPN_LANE_GUARD_RESTART_COOLDOWN", "300"))
LANE_RESET_COOLDOWN = float(os.environ.get("HPN_LANE_GUARD_LANE_RESET_COOLDOWN", "180"))
REMOTE_CONNECT_TIMEOUT = int(os.environ.get("HPN_LANE_GUARD_REMOTE_CONNECT_TIMEOUT", "8"))
REMOTE_COMMAND_TIMEOUT = int(os.environ.get("HPN_LANE_GUARD_REMOTE_COMMAND_TIMEOUT", "20"))
SUBIO_SSH_BIN = os.environ.get("SUBIO_SSH_BIN", "/opt/subio-ssh/bin/subio-ssh")
SSH_BIN = os.environ.get("SSH_BIN", "/usr/bin/ssh")
IDENTITY_FILE = os.environ.get("HPN_IDENTITY_FILE", "/root/.ssh/tunnel_key")
HPN_CIPHER = (os.environ.get("HPN_CIPHER") or "default").strip()


def log(message: str) -> None:
    print(message, flush=True)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_clusters() -> list[dict[str, Any]]:
    if not CONFIG_FILE.is_file():
        raise SystemExit(f"missing config: {CONFIG_FILE}")
    payload = load_json(CONFIG_FILE)
    if isinstance(payload, dict):
        payload = payload.get("clusters")
    if not isinstance(payload, list):
        raise SystemExit(f"{CONFIG_FILE}: root must be a cluster array")
    return [entry for entry in payload if isinstance(entry, dict)]


def current_token() -> str:
    if not CURRENT_HOST_FILE.is_file():
        raise SystemExit(f"missing current-host file: {CURRENT_HOST_FILE}")
    for raw in CURRENT_HOST_FILE.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            return value
    raise SystemExit(f"{CURRENT_HOST_FILE}: no host token found")


def token_matches(host: dict[str, Any], token: str) -> bool:
    return token in {host.get("name"), host.get("ipv4")}


def normalize_ports(raw_ports: Any) -> list[int]:
    if isinstance(raw_ports, int):
        raw_ports = [raw_ports]
    if not isinstance(raw_ports, list):
        return []
    ports: list[int] = []
    for port in raw_ports:
        if isinstance(port, int):
            ports.append(port)
    return ports


def expected_ports_for_domestic_clusters(token: str, clusters: list[dict[str, Any]]) -> dict[str, list[int]]:
    expected: dict[str, list[int]] = {}
    for cluster in clusters:
        cluster_name = cluster.get("name")
        domestic_hosts = cluster.get("domestic_hosts")
        foreign_hosts = cluster.get("foreign_hosts")
        ports_by_site = cluster.get("foreign_to_domestic_ports_by_site")
        if (
            not isinstance(cluster_name, str)
            or not isinstance(domestic_hosts, list)
            or not isinstance(foreign_hosts, list)
            or not isinstance(ports_by_site, dict)
        ):
            continue
        if not any(isinstance(host, dict) and token_matches(host, token) for host in domestic_hosts):
            continue
        ports: set[int] = set()
        for host in foreign_hosts:
            if not isinstance(host, dict):
                continue
            site = host.get("site")
            if not isinstance(site, str) or not site:
                continue
            for port in normalize_ports(ports_by_site.get(site)):
                ports.add(port)
        if ports:
            expected[cluster_name] = sorted(ports)
    return expected


def expected_lanes_for_foreign_clusters(token: str, clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_name = cluster.get("name")
        domestic_hosts = cluster.get("domestic_hosts")
        foreign_hosts = cluster.get("foreign_hosts")
        ports_by_site = cluster.get("foreign_to_domestic_ports_by_site")
        if (
            not isinstance(cluster_name, str)
            or not isinstance(domestic_hosts, list)
            or not isinstance(foreign_hosts, list)
            or not isinstance(ports_by_site, dict)
        ):
            continue
        current_foreign = None
        for host in foreign_hosts:
            if isinstance(host, dict) and token_matches(host, token):
                current_foreign = host
                break
        if current_foreign is None:
            continue
        site = current_foreign.get("site")
        if not isinstance(site, str) or not site:
            continue
        ports = normalize_ports(ports_by_site.get(site))
        if not ports:
            continue
        for host in domestic_hosts:
            if not isinstance(host, dict):
                continue
            user = host.get("user")
            if not isinstance(user, str) or not user:
                user = "root"
            hpn_port = host.get("hpn_port")
            ssh_port = host.get("ssh_port")
            try:
                hpn_port = int(hpn_port) if hpn_port is not None else 2222
                ssh_port = int(ssh_port) if ssh_port is not None else 22
            except Exception:
                hpn_port = 2222
                ssh_port = 22
            for port in ports:
                lanes.append(
                    {
                        "cluster_name": cluster_name,
                        "site": site,
                        "target_name": str(host.get("name") or host.get("ipv4") or ""),
                        "target_ipv4": str(host.get("ipv4") or ""),
                        "target_user": user,
                        "target_hpn_port": hpn_port,
                        "target_ssh_port": ssh_port,
                        "port": port,
                    }
                )
    return lanes


def service_state(name: str) -> str:
    proc = subprocess.run(
        ["systemctl", "is-active", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or proc.stderr.strip() or f"exit={proc.returncode}"


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def curl_available() -> bool:
    proc = subprocess.run(["sh", "-c", "command -v curl >/dev/null 2>&1"], check=False)
    return proc.returncode == 0


def egress_probe(port: int) -> tuple[bool, str]:
    if not curl_available():
        return True, "curl-missing"
    cmd = [
        "curl",
        "--silent",
        "--show-error",
        "--connect-timeout",
        str(CONNECT_TIMEOUT),
        "--max-time",
        str(MAX_TIME),
        "--proxy",
        f"socks5h://127.0.0.1:{port}",
        PROBE_URL,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    detail = proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"
    return False, detail


def build_remote_probe_script(port: int) -> str:
    return f"""python3 - <<'PY'
import json
import socket
import subprocess
import shutil

PORT = {port!r}
PROBE_URL = {PROBE_URL!r}
CONNECT_TIMEOUT = {CONNECT_TIMEOUT!r}
MAX_TIME = {MAX_TIME!r}

def port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False

payload = {{"listening": port_listening(PORT)}}
if payload["listening"]:
    if shutil.which("curl") is None:
        payload["curl"] = "missing"
    else:
        proc = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--connect-timeout",
                str(CONNECT_TIMEOUT),
                "--max-time",
                str(MAX_TIME),
                "--proxy",
                f"socks5h://127.0.0.1:{{PORT}}",
                PROBE_URL,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            payload["egress_ip"] = proc.stdout.strip()
        else:
            payload["egress_error"] = (
                proc.stderr.strip() or proc.stdout.strip() or f"exit={{proc.returncode}}"
            )
print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
PY"""


def build_remote_ssh_command(binary: str, transport_port: int, user: str, host: str, remote_script: str) -> list[str]:
    command = [
        binary,
        "-q",
        "-4",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "AddressFamily=inet",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        f"ConnectTimeout={REMOTE_CONNECT_TIMEOUT}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "PubkeyAcceptedAlgorithms=ssh-ed25519",
        "-o",
        "KexAlgorithms=curve25519-sha256",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "IPQoS=throughput",
        "-i",
        IDENTITY_FILE,
        "-p",
        str(transport_port),
        f"{user}@{host}",
        remote_script,
    ]
    if HPN_CIPHER and HPN_CIPHER.lower() != "default":
        cipher_insert_at = command.index("-i")
        command[cipher_insert_at:cipher_insert_at] = ["-c", HPN_CIPHER]
    return command


def remote_lane_probe(target: dict[str, Any], port: int) -> tuple[bool, str]:
    remote_script = build_remote_probe_script(port)
    candidates: list[tuple[str, str, int]] = []
    if os.path.exists(SUBIO_SSH_BIN):
        candidates.append(("SUBIO", SUBIO_SSH_BIN, int(target["target_hpn_port"])))
    candidates.append(("SSH-SUBIO", SSH_BIN, int(target["target_hpn_port"])))
    candidates.append(("SSH", SSH_BIN, int(target["target_ssh_port"])))

    last_error = "unknown ssh failure"
    for label, binary, transport_port in candidates:
        command = build_remote_ssh_command(
            binary=binary,
            transport_port=transport_port,
            user=str(target["target_user"]),
            host=str(target["target_ipv4"]),
            remote_script=remote_script,
        )
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=REMOTE_COMMAND_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = f"{label} timeout after {REMOTE_COMMAND_TIMEOUT}s"
            continue
        if proc.returncode != 0:
            last_error = proc.stderr.strip() or proc.stdout.strip() or f"{label} exit={proc.returncode}"
            continue
        try:
            payload = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            last_error = f"{label} invalid JSON: {exc}"
            continue
        if not isinstance(payload, dict):
            last_error = f"{label} invalid payload"
            continue
        if not payload.get("listening"):
            return False, "not listening"
        if "egress_ip" in payload:
            return True, str(payload.get("egress_ip") or "")
        if payload.get("curl") == "missing":
            return True, "curl-missing"
        if payload.get("egress_error"):
            return False, f"egress failed: {payload['egress_error']}"
        return True, "listening"
    return False, last_error


def load_state() -> dict[str, Any]:
    default = {"failures": {}, "last_restart_ts": {}, "last_lane_reset_ts": {}}
    if not STATE_FILE.is_file():
        return default
    try:
        payload = load_json(STATE_FILE)
    except Exception:
        return default
    if not isinstance(payload, dict):
        return default
    failures = payload.get("failures")
    if not isinstance(failures, dict):
        failures = {}
    last_restart_ts = payload.get("last_restart_ts")
    if not isinstance(last_restart_ts, dict):
        last_restart_ts = {}
    last_lane_reset_ts = payload.get("last_lane_reset_ts")
    if not isinstance(last_lane_reset_ts, dict):
        last_lane_reset_ts = {}
    legacy_subio_ssh = payload.get("last_subio_ssh_restart_ts")
    if "subio-ssh.service" not in last_restart_ts and isinstance(legacy_subio_ssh, (int, float)):
        last_restart_ts["subio-ssh.service"] = float(legacy_subio_ssh)
    return {
        "failures": failures,
        "last_restart_ts": last_restart_ts,
        "last_lane_reset_ts": last_lane_reset_ts,
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def restart_service(unit: str, reason: str, state: dict[str, Any]) -> None:
    last_restart_ts = state.setdefault("last_restart_ts", {})
    now = time.time()
    last_restart = float(last_restart_ts.get(unit, 0.0) or 0.0)
    if (now - last_restart) < RESTART_COOLDOWN:
        wait_for = RESTART_COOLDOWN - (now - last_restart)
        log(f"[guard] restart of {unit} suppressed by cooldown ({wait_for:.1f}s left): {reason}")
        return
    log(f"[guard] restarting {unit}: {reason}")
    proc = subprocess.run(
        ["systemctl", "restart", unit],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"
        log(f"[guard] {unit} restart failed: {detail}")
        return
    last_restart_ts[unit] = now


def lane_reset_identity(lane: dict[str, Any]) -> str:
    return (
        f"{lane['cluster_name']}|foreign_to_domestic|{lane['site']}|"
        f"{lane['target_name']}|{lane['port']}"
    )


def lane_reset_request_path(lane_identity: str) -> Path:
    digest = hashlib.sha256(lane_identity.encode("utf-8")).hexdigest()
    return RESET_REQUEST_DIR / f"{digest}.json"


def request_lane_reset(lane: dict[str, Any], reason: str, state: dict[str, Any]) -> bool:
    lane_identity = lane_reset_identity(lane)
    now = time.time()
    last_lane_reset_ts = state.setdefault("last_lane_reset_ts", {})
    last_reset = float(last_lane_reset_ts.get(lane_identity, 0.0) or 0.0)
    if (now - last_reset) < LANE_RESET_COOLDOWN:
        wait_for = LANE_RESET_COOLDOWN - (now - last_reset)
        log(
            f"[guard] reset of lane {lane_identity} suppressed by cooldown "
            f"({wait_for:.1f}s left): {reason}"
        )
        return False

    payload = {
        "cluster_name": str(lane["cluster_name"]),
        "direction": "foreign_to_domestic",
        "site": str(lane["site"]),
        "target_name": str(lane["target_name"]),
        "listen_port": int(lane["port"]),
        "reason": reason,
        "requested_at": now,
    }
    request_path = lane_reset_request_path(lane_identity)
    RESET_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = request_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    tmp_path.replace(request_path)
    last_lane_reset_ts[lane_identity] = now
    log(f"[guard] queued lane reset for {lane_identity}: {reason}")
    return True


def main() -> int:
    token = current_token()
    clusters = load_clusters()
    expected_domestic = expected_ports_for_domestic_clusters(token, clusters)
    expected_foreign = expected_lanes_for_foreign_clusters(token, clusters)
    if not expected_domestic and not expected_foreign:
        log("[guard] no active lanes on this host; nothing to do")
        return 0

    state = load_state()
    previous_failures = state.get("failures", {})
    failures: dict[str, int] = {}

    subio_ssh_state = service_state("subio-ssh")
    reverse_state = service_state("subio-manager")
    log(f"[guard] subio-ssh={subio_ssh_state} subio-manager={reverse_state}")

    domestic_problem = False
    foreign_problem = False
    domestic_worst = 0
    foreign_worst = 0
    foreign_failures: list[tuple[dict[str, Any], int, str]] = []

    for cluster_name, ports in expected_domestic.items():
        for port in ports:
            key = f"domestic:{cluster_name}:{port}"
            listening = port_listening(port)
            if not listening:
                failures[key] = int(previous_failures.get(key, 0)) + 1
                domestic_problem = True
                domestic_worst = max(domestic_worst, failures[key])
                log(f"[guard] domestic {cluster_name} port {port}: down (failure {failures[key]})")
                continue
            ok, detail = egress_probe(port)
            if ok:
                failures[key] = 0
                if detail == "curl-missing":
                    log(f"[guard] domestic {cluster_name} port {port}: up (curl missing; listener-only check)")
                else:
                    log(f"[guard] domestic {cluster_name} port {port}: up egress={detail}")
            else:
                failures[key] = int(previous_failures.get(key, 0)) + 1
                domestic_problem = True
                domestic_worst = max(domestic_worst, failures[key])
                log(
                    f"[guard] domestic {cluster_name} port {port}: "
                    f"egress failed (failure {failures[key]}): {detail}"
                )

    for lane in expected_foreign:
        key = f"foreign:{lane['cluster_name']}:{lane['target_ipv4']}:{lane['port']}"
        ok, detail = remote_lane_probe(lane, int(lane["port"]))
        if ok:
            failures[key] = 0
            log(
                f"[guard] foreign {lane['cluster_name']} {lane['target_name']} "
                f"port {lane['port']}: up egress={detail}"
            )
        else:
            failures[key] = int(previous_failures.get(key, 0)) + 1
            foreign_problem = True
            foreign_worst = max(foreign_worst, failures[key])
            foreign_failures.append((lane, failures[key], detail))
            log(
                f"[guard] foreign {lane['cluster_name']} {lane['target_name']} "
                f"port {lane['port']}: failed (failure {failures[key]}): {detail}"
            )

    state["failures"] = failures

    if expected_domestic:
        if subio_ssh_state != "active":
            restart_service("subio-ssh.service", f"subio-ssh is {subio_ssh_state!r}", state)
        elif domestic_problem and domestic_worst >= FAILURE_THRESHOLD:
            log(
                "[guard] domestic reverse-lane failures reached threshold "
                f"{FAILURE_THRESHOLD}; not restarting subio-ssh because those lanes are "
                "foreign-owned and an subio-ssh restart would drop healthy sessions"
            )

    if expected_foreign:
        if reverse_state != "active":
            restart_service("subio-manager.service", f"subio-manager is {reverse_state!r}", state)
        elif foreign_problem and foreign_worst >= FAILURE_THRESHOLD:
            reset_count = 0
            for lane, failure_count, detail in foreign_failures:
                if failure_count < FAILURE_THRESHOLD:
                    continue
                if request_lane_reset(
                    lane,
                    (
                        f"foreign lane failures reached threshold {FAILURE_THRESHOLD}; "
                        f"last probe: {detail}"
                    ),
                    state,
                ):
                    failures[
                        f"foreign:{lane['cluster_name']}:{lane['target_ipv4']}:{lane['port']}"
                    ] = 0
                    reset_count += 1
            if reset_count == 0:
                log(
                    "[guard] foreign lane failures reached threshold but all matching "
                    "lane resets are currently suppressed by cooldown"
                )
        if subio_ssh_state != "active":
            restart_service("subio-ssh.service", f"subio-ssh is {subio_ssh_state!r}", state)

    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
