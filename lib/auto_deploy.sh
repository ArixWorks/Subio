#!/bin/bash

# Zero-Touch Auto-Deployment for Foreign Servers

function auto_deploy_foreign() {
    echo -e "${CYAN}--- Auto-Deploy New Foreign Server ---${NC}"
    echo -e "${YELLOW}This will completely configure a new foreign server and add it to your network.${NC}"
    echo -e "${YELLOW}Note: This requires a server that allows SSH Root Password authentication.${NC}"
    
    # 1. Install sshpass locally if missing
    if ! command -v sshpass &>/dev/null; then
        echo -e "${YELLOW}Installing sshpass locally for automated deployment...${NC}"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update >/dev/null 2>&1
        apt-get install -y sshpass >/dev/null 2>&1
    fi
    
    # 2. Ask for IP and Password
    read_ascii "Enter the IP address of the NEW foreign server" new_ip ""
    read -sp "Enter the ROOT PASSWORD of the new server: " new_pass
    echo ""
    
    # 3. Auto-detect Country Code
    echo -e "${CYAN}Auto-detecting server location...${NC}"
    local site_code=$(curl -s --connect-timeout 3 http://ip-api.com/json/$new_ip | jq -r '.countryCode' 2>/dev/null)
    if [[ -z "$site_code" || "$site_code" == "null" ]]; then
        site_code="XX"
    fi
    read_ascii "Site Code (Country, e.g. TR, DE)" confirm_site "$site_code"
    site_code=$(echo "$confirm_site" | tr '[:lower:]' '[:upper:]')
    
    # 4. Auto-generate Short Name
    local existing_count=$(python3 -c "
import json
try:
    c = json.load(open('/etc/subio-manager.json'))[0]
    nodes = c.get('foreign_hosts', [])
    count = sum(1 for n in nodes if n['name'].startswith('${site_code}'.lower()))
    print(count + 1)
except:
    print('1')
")
    local default_name="${site_code,,}${existing_count}"
    read_ascii "Short name for this server" s_name "$default_name"
    
    # 5. SOCKS Port suggestion
    local max_port=$(python3 -c "
import json
try:
    c = json.load(open('/etc/subio-manager.json'))[0]
    ports = []
    for site, ps in c.get('foreign_to_domestic_ports_by_site', {}).items():
        if isinstance(ps, list):
            ports.extend([int(p) for p in ps])
        else:
            ports.append(int(ps))
    print(max(ports) + 10 if ports else 10810)
except:
    print('10810')
")
    read_ascii "SOCKS Port (Press enter to use suggested next port)" s_socks_port "$max_port"
    
    local s_subio_port="2222"
    local s_ssh_port="22"
    
    # 6. Begin Deployment
    echo -e "${YELLOW}Starting Zero-Touch Deployment to $new_ip...${NC}"
    
    local pub_key=$(cat /root/.ssh/tunnel_key.pub 2>/dev/null)
    if [ -z "$pub_key" ]; then
        echo -e "${RED}Error: Local tunnel public key not found! Run Initial Setup first.${NC}"
        read -p "Press Enter to continue..."
        return
    fi
    
    echo -e "${CYAN}Step 1: Injecting SSH Keys...${NC}"
    sshpass -p "$new_pass" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p $s_ssh_port root@$new_ip "mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo '$pub_key' >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo -e "${RED}Failed to connect via password. Please check the IP and Root Password.${NC}"
        echo -e "${YELLOW}Note: Some providers (like AWS) disable password login by default. If so, you must add the server manually.${NC}"
        read -p "Press Enter to continue..."
        return
    fi
    echo -e "${GREEN}SSH keys injected successfully.${NC}"
    
    echo -e "${CYAN}Step 2: Downloading SubIO on remote server...${NC}"
    sshpass -p "$new_pass" ssh -o StrictHostKeyChecking=no -p $s_ssh_port root@$new_ip "export DEBIAN_FRONTEND=noninteractive && curl -sL https://raw.githubusercontent.com/ArixWorks/Subio/main/install.sh | bash" >/dev/null 2>&1
    
    echo -e "${CYAN}Step 3: Bootstrapping remote configuration...${NC}"
    cat << 'EOF' > /tmp/bootstrap_subio.sh
#!/bin/bash
export DEBIAN_FRONTEND=noninteractive
cd /opt/subio
source lib/common.sh
source lib/install_subio_ssh.sh
source lib/configure_tunnel.sh
source lib/setup_services.sh

install_prerequisites >/dev/null 2>&1
install_subio_ssh >/dev/null 2>&1
export HPN_PORT=2222
configure_subio_ssh_daemon >/dev/null 2>&1
configure_default_env >/dev/null 2>&1
install_python_scripts >/dev/null 2>&1
install_systemd_services >/dev/null 2>&1

export SERVER_ROLE="kharej"
enable_and_start_services >/dev/null 2>&1
EOF
    sshpass -p "$new_pass" scp -o StrictHostKeyChecking=no -P $s_ssh_port /tmp/bootstrap_subio.sh root@$new_ip:/tmp/bootstrap_subio.sh
    sshpass -p "$new_pass" ssh -o StrictHostKeyChecking=no -p $s_ssh_port root@$new_ip "bash /tmp/bootstrap_subio.sh"
    
    echo -e "${CYAN}Step 4: Fetching new Host Key from remote server...${NC}"
    ssh-keyscan -p $s_subio_port $new_ip 2>/dev/null | grep ssh-ed25519 | awk '{print $3}' | head -n1 > /tmp/ssh_key_result.txt &
    local ssh_pid=$!
    show_spinner $ssh_pid
    wait $ssh_pid
    KEY=$(cat /tmp/ssh_key_result.txt 2>/dev/null)
    if [ -z "$KEY" ]; then
        echo -e "${RED}Failed to fetch Host Key from $new_ip:$s_subio_port. The remote installation might have failed.${NC}"
        read -p "Press Enter to continue..."
        return
    fi
    echo -e "${GREEN}Successfully fetched key: $KEY${NC}"
    
    echo -e "${CYAN}Step 5: Updating local and remote configurations...${NC}"
    python3 $SUBIO_DIR/lib/config_helper.py add-node --type "foreign" --ip "$new_ip" --name "$s_name" --key "$KEY" --ssh-port "$s_ssh_port" --subio-port "$s_subio_port" --site "$site_code" --socks-port "$s_socks_port"
    
    # SCP the updated subio-manager.json to the remote server
    sshpass -p "$new_pass" scp -o StrictHostKeyChecking=no -P $s_ssh_port /etc/subio-manager.json root@$new_ip:/etc/subio-manager.json
    # Write the remote server's IP into its current-host.txt and restart manager
    sshpass -p "$new_pass" ssh -o StrictHostKeyChecking=no -p $s_ssh_port root@$new_ip "echo '$new_ip' > /etc/subio-manager-current-host.txt && systemctl restart subio-manager"
    
    systemctl restart $SERVICE_NAME
    echo -e "${GREEN}Deployment complete! The new server '$s_name' is now fully configured and connected.${NC}"
    read -p "Press Enter to continue..."
}
