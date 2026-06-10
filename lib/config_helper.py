#!/usr/bin/env python3
import json
import os
import sys
import argparse

CONFIG_FILE = "/etc/subio-manager.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return []

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def init_config():
    print("Initializing new SubIO configuration...")
    # Initialize basic structure if empty
    config = load_config()
    if not config:
        cluster = {
            "name": "subio_cluster",
            "foreign_hosts": [],
            "domestic_hosts": [],
            "foreign_to_domestic_ports_by_site": {},
            "domestic_to_foreign_ports_by_site": {}
        }
        config.append(cluster)
        save_config(config)
        print("Config initialized.")
    else:
        print("Config already exists.")

def add_node(node_type, ip, name, key, ssh_port, subio_port, site="XX", socks_port=10810):
    config = load_config()
    if not config:
        print("Error: Config not initialized. Run init first.")
        sys.exit(1)
        
    cluster = config[0]
    host_entry = {
        "name": name,
        "ipv4": ip,
        "hostkey_ed25519_ssh": key,
        "hostkey_ed25519_hpn": key,
        "ssh_port": int(ssh_port),
        "subio_port": int(subio_port)
    }
    
    
    for h in cluster.get("domestic_hosts", []) + cluster.get("foreign_hosts", []):
        if h.get("name") == name:
            print(f"Error: Node with name '{name}' already exists.")
            sys.exit(1)
        if h.get("ipv4") == ip:
            print(f"Error: Node with IP '{ip}' already exists.")
            sys.exit(1)

    if node_type.lower() == 'iran':
        cluster["domestic_hosts"].append(host_entry)
        # Assuming port 10810 for the tunnel if first node
        if not cluster["foreign_to_domestic_ports_by_site"]:
            cluster["foreign_to_domestic_ports_by_site"]["XX"] = [10810]
    else:
        host_entry["site"] = site
        cluster["foreign_hosts"].append(host_entry)
        if "foreign_to_domestic_ports_by_site" not in cluster:
            cluster["foreign_to_domestic_ports_by_site"] = {}
        if site not in cluster["foreign_to_domestic_ports_by_site"]:
            cluster["foreign_to_domestic_ports_by_site"][site] = []
        if socks_port not in cluster["foreign_to_domestic_ports_by_site"][site]:
            cluster["foreign_to_domestic_ports_by_site"][site].append(int(socks_port))
        
    save_config(config)
    print(f"Node {name} added successfully.")

def remove_node(name):
    config = load_config()
    if not config:
        print("Error: Config not initialized.")
        sys.exit(1)
        
    cluster = config[0]
    removed = False
    
    # Check foreign
    initial_len = len(cluster.get("foreign_hosts", []))
    cluster["foreign_hosts"] = [h for h in cluster.get("foreign_hosts", []) if h["name"] != name]
    if len(cluster["foreign_hosts"]) < initial_len:
        removed = True
        
    # Check domestic
    initial_len = len(cluster.get("domestic_hosts", []))
    cluster["domestic_hosts"] = [h for h in cluster.get("domestic_hosts", []) if h["name"] != name]
    if len(cluster["domestic_hosts"]) < initial_len:
        removed = True
        
    if removed:
        save_config(config)
        print(f"Node {name} removed successfully.")
    else:
        print(f"Node {name} not found.")

def list_nodes(ping=False):
    config = load_config()
    if not config:
        print("No nodes configured.")
        return
        
    cluster = config[0]
    foreign = cluster.get("foreign_hosts", [])
    domestic = cluster.get("domestic_hosts", [])
    
    print("\nForeign Servers (Kharej):")
    if not foreign:
        print("  (None)")
    for h in foreign:
        status = ""
        if ping:
            import subprocess
            cmd = ["nc", "-z", "-w", "1", h["ipv4"], str(h.get("subio_port", 2222))]
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status = "\033[0;32m[Online]\033[0m" if result.returncode == 0 else "\033[0;31m[Offline]\033[0m"
        print(f"  - {h['name']} ({h['ipv4']}) {status}")
        
    print("\nDomestic Servers (Iran):")
    if not domestic:
        print("  (None)")
    for h in domestic:
        status = ""
        if ping:
            import subprocess
            cmd = ["nc", "-z", "-w", "1", h["ipv4"], str(h.get("subio_port", 2222))]
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status = "\033[0;32m[Online]\033[0m" if result.returncode == 0 else "\033[0;31m[Offline]\033[0m"
        print(f"  - {h['name']} ({h['ipv4']}) {status}")
    print("")

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    
    init_parser = subparsers.add_parser("init")
    
    add_parser = subparsers.add_parser("add-node")
    add_parser.add_argument("--type", required=True, choices=["iran", "foreign"])
    add_parser.add_argument("--ip", required=True)
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--key", required=True)
    add_parser.add_argument("--ssh-port", default="22")
    add_parser.add_argument("--subio-port", default="2222")
    add_parser.add_argument("--site", default="XX")
    add_parser.add_argument("--socks-port", default="10810")
    
    rm_parser = subparsers.add_parser("remove-node")
    rm_parser.add_argument("--name", required=True)
    
    list_parser = subparsers.add_parser("list-nodes")
    list_ping_parser = subparsers.add_parser("list-nodes-ping")
    
    args = parser.parse_args()
    
    if args.command == "init":
        init_config()
    elif args.command == "add-node":
        add_node(args.type, args.ip, args.name, args.key, args.ssh_port, args.subio_port, getattr(args, 'site', 'XX'), int(getattr(args, 'socks_port', 10810)))
    elif args.command == "remove-node":
        remove_node(args.name)
    elif args.command == "list-nodes":
        list_nodes(ping=False)
    elif args.command == "list-nodes-ping":
        list_nodes(ping=True)

if __name__ == "__main__":
    main()
