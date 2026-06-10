#!/bin/bash
# SubIO Tunnel Management Script

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SUBIO_DIR="/opt/subio"
CONFIG_FILE="/etc/subio-manager.json"
SERVICE_NAME="subio-manager.service"

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}" 
   exit 1
fi

function read_ascii() {
    local prompt="$1"
    local var_name="$2"
    local default_val="$3"
    local tmp_val
    while true; do
        if [[ -n "$default_val" ]]; then
            read -p "$(echo -e "${prompt} [${default_val}]: ")" tmp_val
            tmp_val=${tmp_val:-$default_val}
        else
            read -p "$(echo -e "${prompt}: ")" tmp_val
        fi
        
        if [[ -z "$tmp_val" ]]; then
            echo -e "${RED}This field is required.${NC}"
            continue
        fi
        
        if LC_ALL=C echo "$tmp_val" | grep -q '[^ -~]'; then
            echo -e "${RED}Error: Only English characters and numbers are allowed. Please try again.${NC}"
            continue
        fi
        
        eval "$var_name=\"\$tmp_val\""
        break
    done
}

function show_header() {
    clear
    echo -e "${CYAN}╔────────────────────────────────────────────────╗${NC}"
    echo -e "${CYAN}│${NC}   ${BOLD}SubIO Tunnel Management Script${NC}               ${CYAN}│${NC}"
    echo -e "${CYAN}│${NC}   0. Exit                                      ${CYAN}│${NC}"
    echo -e "${CYAN}│────────────────────────────────────────────────│${NC}"
    echo -e "${CYAN}│   1. Install / Initial Setup                   │${NC}"
    echo -e "${CYAN}│   2. Update SubIO                              │${NC}"
    echo -e "${CYAN}│   3. Uninstall SubIO                           │${NC}"
    echo -e "${CYAN}│────────────────────────────────────────────────│${NC}"
    echo -e "${CYAN}│   4. Node Management (Add/Remove Servers)      │${NC}"
    echo -e "${CYAN}│   5. Key Management (Show My Key / Add Key)    │${NC}"
    echo -e "${CYAN}│   6. View Current Configuration                │${NC}"
    echo -e "${CYAN}│────────────────────────────────────────────────│${NC}"
    echo -e "${CYAN}│   7. Start SubIO                               │${NC}"
    echo -e "${CYAN}│   8. Stop SubIO                                │${NC}"
    echo -e "${CYAN}│   9. Restart SubIO                             │${NC}"
    echo -e "${CYAN}│  10. Check Status (Health Diagnostics)         │${NC}"
    echo -e "${CYAN}│  11. View Logs (SubIO & Connection Logs)       │${NC}"
    echo -e "${CYAN}│────────────────────────────────────────────────│${NC}"
    echo -e "${CYAN}│  12. Speed Test & Connection Check             │${NC}"
    echo -e "${CYAN}╚────────────────────────────────────────────────╝${NC}"
    echo ""
    show_status_footer
    echo ""
}

function show_status_footer() {
    local service_status="Stopped"
    local autostart="No"
    
    if systemctl is-active --quiet $SERVICE_NAME; then
        service_status="${GREEN}Running${NC}"
    else
        service_status="${RED}Stopped${NC}"
    fi

    if systemctl is-enabled --quiet $SERVICE_NAME 2>/dev/null; then
        autostart="${GREEN}Yes${NC}"
    else
        autostart="${RED}No${NC}"
    fi

    # System Monitor
    local cpu_usage=$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1}' || echo "0")
    local ram_usage=$(free -m | awk 'NR==2{printf "%.1f%%", $3*100/$2}' || echo "0%")
    
    # Network Traffic
    local iface=$(ip route 2>/dev/null | awk '/default/ {print $5}' | head -n1)
    if [ -n "$iface" ] && [ -d "/sys/class/net/$iface/statistics" ]; then
        local rx1=$(cat /sys/class/net/$iface/statistics/rx_bytes)
        local tx1=$(cat /sys/class/net/$iface/statistics/tx_bytes)
        sleep 0.5
        local rx2=$(cat /sys/class/net/$iface/statistics/rx_bytes)
        local tx2=$(cat /sys/class/net/$iface/statistics/tx_bytes)
        local rx_kbps=$(echo "scale=1; ($rx2 - $rx1) / 1024 / 0.5" | bc 2>/dev/null || echo "0")
        local tx_kbps=$(echo "scale=1; ($tx2 - $tx1) / 1024 / 0.5" | bc 2>/dev/null || echo "0")
    else
        local rx_kbps="0"
        local tx_kbps="0"
    fi

    echo -e "Panel state: ${service_status} | Autostart: ${autostart}"
    echo -e "CPU: ${YELLOW}${cpu_usage}%${NC} | RAM: ${YELLOW}${ram_usage}${NC} | Net: DL ${YELLOW}${rx_kbps} KB/s${NC} / UL ${YELLOW}${tx_kbps} KB/s${NC}"
}

function show_spinner() {
    local pid=$1
    local delay=0.1
    local spinstr='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    while [ "$(ps a | awk '{print $1}' | grep $pid)" ]; do
        local temp=${spinstr#?}
        printf " [%c]  " "$spinstr"
        local spinstr=$temp${spinstr%"$temp"}
        sleep $delay
        printf "\b\b\b\b\b\b"
    done
    printf "    \b\b\b\b"
}

function check_status() {
    echo -e "${YELLOW}Running Health Diagnostics...${NC}"
    bash "$SUBIO_DIR/setup.sh" status
    read -p "Press Enter to continue..."
}

function view_logs() {
    echo -e "${YELLOW}Tailing SubIO logs... (Press Ctrl+C to stop)${NC}"
    journalctl -u $SERVICE_NAME -f
}

function speed_test() {
    echo -e "${YELLOW}--- Speed Test ---${NC}"
    read -p "Enter local SOCKS port to test [10810]: " TEST_PORT
    TEST_PORT=${TEST_PORT:-10810}
    echo -e "${CYAN}Testing connection to Hetzner...${NC}"
    curl -x socks5h://127.0.0.1:$TEST_PORT -o /dev/null -s -L -w '%{speed_download}' https://fsn1-speed.hetzner.com/100MB.bin > /tmp/speed_result.txt &
    local curl_pid=$!
    show_spinner $curl_pid
    wait $curl_pid
    local curl_status=$?
    speed=$(cat /tmp/speed_result.txt 2>/dev/null)
    
    if [ $curl_status -eq 0 ] && [ -n "$speed" ]; then
        awk -v s="$speed" 'BEGIN { printf "Speed: %.2f MB/s = %.2f Mbps\n", s/1000000, s*8/1000000 }'
    else
        echo -e "${RED}Test failed or timed out.${NC}"
        echo -e "${YELLOW}Troubleshooting:${NC}"
        echo -e "  1. Is the tunnel running? Check Option 10 (Status)."
        echo -e "  2. Is Datacenter Firewall blocking the connection? Check Hetzner/AWS panel."
        echo -e "  3. Is port $TEST_PORT correct?"
    fi
    read -p "Press Enter to continue..."
}

function manage_nodes() {
    echo -e "${CYAN}--- Node Management ---${NC}"
    echo "1. Add new Server (Iran or Foreign)"
    echo "2. Remove Server"
    echo "3. List all configured Servers"
    echo "0. Back to Main Menu"
    read -p "Select [0-3]: " node_choice
    
    if [[ "$node_choice" == "1" ]]; then
        echo -e "${CYAN}What type of server are you adding?${NC}"
        echo "1. Iran (Domestic)"
        echo "2. Foreign (Kharej)"
        read -p "Select [1-2]: " s_type_num
        
        if [[ "$s_type_num" == "1" ]]; then
            s_type="iran"
        elif [[ "$s_type_num" == "2" ]]; then
            s_type="foreign"
        else
            echo -e "${RED}Invalid selection. Returning to menu.${NC}"
            return
        fi
        read_ascii "Enter IP address of the server" s_ip ""
        read_ascii "Enter a short name (e.g. ir2, de1)" s_name ""
        read_ascii "Enter SubIO-SSH Port" s_subio_port "2222"
        read_ascii "Enter Standard SSH Port (fallback)" s_ssh_port "22"
        
        local s_site="XX"
        local s_socks_port="10810"
        if [[ "$s_type" == "foreign" ]]; then
            read_ascii "Enter Site Code (e.g. DE, UK)" s_site "XX"
            s_site=$(echo "$s_site" | tr '[:lower:]' '[:upper:]')
            read_ascii "Enter SOCKS port for this server" s_socks_port "10811"
        fi
        
        echo -e "${YELLOW}Configuring Local Firewall (if active)...${NC}"
        if command -v ufw >/dev/null 2>&1; then
            ufw allow $s_subio_port/tcp >/dev/null 2>&1
            ufw allow 10810/tcp >/dev/null 2>&1
            echo -e "${GREEN}UFW configured.${NC}"
        elif command -v iptables >/dev/null 2>&1; then
            iptables -I INPUT -p tcp --dport $s_subio_port -j ACCEPT >/dev/null 2>&1
            iptables -I INPUT -p tcp --dport 10810 -j ACCEPT >/dev/null 2>&1
            echo -e "${GREEN}iptables configured.${NC}"
        fi
        
        echo -e "${YELLOW}Fetching Host Key automatically via ssh-keyscan on port $s_subio_port...${NC}"
        ssh-keyscan -p $s_subio_port $s_ip 2>/dev/null | grep ssh-ed25519 | awk '{print $3}' | head -n1 > /tmp/ssh_key_result.txt &
        local ssh_pid=$!
        show_spinner $ssh_pid
        wait $ssh_pid
        KEY=$(cat /tmp/ssh_key_result.txt 2>/dev/null)
        if [ -z "$KEY" ]; then
            echo -e "${RED}Failed to fetch Host Key from $s_ip:$s_subio_port.${NC}"
            echo -e "${YELLOW}WARNING: Connection timed out or refused! This is usually because:${NC}"
            echo -e "  1. Datacenter Firewall (Hetzner, AWS Security Group, Iran DC) is blocking port $s_subio_port."
            echo -e "  2. You must open port $s_subio_port in your provider's control panel."
            echo -e "  3. The server IP is incorrect or the server is offline."
            read -p "Press Enter to continue..."
            return
        fi
        echo -e "${GREEN}Successfully fetched key: $KEY${NC}"
        
        # Call config_helper.py to add the node
        python3 $SUBIO_DIR/lib/config_helper.py add-node --type "$s_type" --ip "$s_ip" --name "$s_name" --key "$KEY" --ssh-port "$s_ssh_port" --subio-port "$s_subio_port" --site "$s_site" --socks-port "$s_socks_port"
        systemctl restart $SERVICE_NAME
        echo -e "${GREEN}Node added and service restarted.${NC}"
        read -p "Press Enter to continue..."
    elif [[ "$node_choice" == "2" ]]; then
        echo -e "${CYAN}Available Nodes:${NC}"
        python3 $SUBIO_DIR/lib/config_helper.py list-nodes
        read_ascii "Enter the Name of the server to remove" s_name ""
        if [ -n "$s_name" ]; then
            python3 $SUBIO_DIR/lib/config_helper.py remove-node --name "$s_name"
            systemctl restart $SERVICE_NAME
            echo -e "${GREEN}Node $s_name removed and service restarted.${NC}"
        fi
        read -p "Press Enter to continue..."
    elif [[ "$node_choice" == "3" ]]; then
        echo -e "${CYAN}Available Nodes:${NC}"
        python3 $SUBIO_DIR/lib/config_helper.py list-nodes-ping
        read -p "Press Enter to continue..."
    fi
}

function key_management() {
    echo -e "${CYAN}--- Key Management ---${NC}"
    echo "1. Show My Public Key (Copy this to add to another server)"
    echo "2. Add Remote Public Key (Paste key from another server here)"
    echo "0. Back to Main Menu"
    read -p "Select [0-2]: " key_choice
    
    if [[ "$key_choice" == "1" ]]; then
        echo -e "\n${GREEN}Your Public Key:${NC}"
        cat /root/.ssh/tunnel_key.pub
        echo -e "\n${YELLOW}Copy the entire line above and paste it in the other server's 'Add Remote Public Key' menu.${NC}"
        read -p "Press Enter to continue..."
    elif [[ "$key_choice" == "2" ]]; then
        echo -e "${CYAN}Paste the Remote Public Key below and press Enter:${NC}"
        read_ascii "Remote Key" remote_key ""
        if [[ $remote_key == ssh-* ]]; then
            echo "$remote_key" >> /root/.ssh/authorized_keys
            echo -e "${GREEN}Key successfully added to authorized_keys!${NC}"
        else
            echo -e "${RED}Invalid key format. It should start with ssh-ed25519 or ssh-rsa.${NC}"
        fi
        read -p "Press Enter to continue..."
    fi
}

function initial_setup() {
    echo -e "${YELLOW}Running Initial Setup...${NC}"
    bash $SUBIO_DIR/setup.sh
    read -p "Press Enter to return to main menu..."
}

function uninstall_subio() {
    echo -e "${RED}WARNING: This will completely remove SubIO Tunnel and all its configurations!${NC}"
    read -p "Are you sure you want to proceed? [y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${GREEN}Uninstallation aborted.${NC}"
        read -p "Press Enter to return to main menu..."
        return
    fi

    echo -e "${YELLOW}Stopping and disabling services...${NC}"
    systemctl stop subio.service subio-ssh.service subio-manager.service subio-lane-guard.service 2>/dev/null
    systemctl disable subio.service subio-ssh.service subio-manager.service subio-lane-guard.service 2>/dev/null

    echo -e "${YELLOW}Removing systemd service files...${NC}"
    rm -f /etc/systemd/system/subio.service
    rm -f /etc/systemd/system/subio-ssh.service
    rm -f /etc/systemd/system/subio-manager.service
    rm -f /etc/systemd/system/subio-lane-guard.service
    systemctl daemon-reload

    echo -e "${YELLOW}Removing configuration files...${NC}"
    rm -rf /etc/subio
    rm -rf /etc/subio-ssh
    rm -rf /var/lib/subio
    rm -f /etc/subio-manager.json
    rm -f /etc/default/subio*
    rm -f /etc/subio-manager-current-host.txt

    echo -e "${YELLOW}Removing installed binaries and scripts...${NC}"
    rm -f /usr/local/bin/subio-manager.py
    rm -f /usr/local/bin/subio-lane-guard.py
    rm -rf /opt/subio-ssh
    rm -rf /opt/hpnssh
    rm -rf /opt/subio

    echo -e "${YELLOW}Attempting to remove hpnssh package if installed via APT...${NC}"
    apt-get remove --purge -y hpnssh 2>/dev/null || true
    
    echo -e "${YELLOW}Removing SubIO CLI command...${NC}"
    rm -f /usr/local/bin/subio
    rm -f /usr/bin/subio

    echo -e "${GREEN}SubIO Tunnel has been completely uninstalled.${NC}"
    echo -e "${YELLOW}You will be disconnected from this menu upon pressing Enter.${NC}"
    read -p "Press Enter to exit..."
    exit 0
}

function menu() {
    while true; do
        show_header
        read -p "Please enter your selection [0-12]: " choice
        case $choice in
            0) exit 0 ;;
            1) initial_setup ;;
            2) bash <(curl -Ls https://raw.githubusercontent.com/ArixWorks/Subio/main/install.sh); read -p "Press Enter..." ;;
            3) uninstall_subio ;;
            4) manage_nodes ;;
            5) key_management ;;
            6) cat $CONFIG_FILE | jq .; read -p "Press Enter..." ;;
            7) systemctl start subio-ssh.service subio-manager.service; echo "Started."; sleep 1 ;;
            8) systemctl stop subio-ssh.service subio-manager.service; echo "Stopped."; sleep 1 ;;
            9) systemctl restart subio-ssh.service subio-manager.service; echo "Restarted."; sleep 1 ;;
            10) check_status ;;
            11) view_logs ;;
            12) speed_test ;;
            *) echo -e "${RED}Invalid selection${NC}"; sleep 1 ;;
        esac
    done
}

menu
