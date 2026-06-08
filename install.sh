#!/bin/bash
# SubIO Tunnel One-Line Installer

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}" 
   exit 1
fi

echo -e "${CYAN}=================================================${NC}"
echo -e "${CYAN}            SubIO Tunnel Installer               ${NC}"
echo -e "${CYAN}=================================================${NC}"

# Install dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
apt-get update -y -q > /dev/null 2>&1
apt-get install -y -q curl jq python3 git > /dev/null 2>&1

SUBIO_DIR="/opt/subio"

if [ -d "$SUBIO_DIR" ]; then
    echo -e "${YELLOW}SubIO is already installed. Updating...${NC}"
    cd $SUBIO_DIR
    git pull
    echo -e "${YELLOW}Downloading SubIO files from GitHub...${NC}"
    rm -rf $SUBIO_DIR
    git clone https://github.com/ArixWorks/Subio.git $SUBIO_DIR > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        echo -e "${RED}Failed to download repository. Please check your internet connection.${NC}"
        exit 1
    fi
fi

echo -e "${YELLOW}Setting up permissions...${NC}"
chmod +x $SUBIO_DIR/subio.sh

# Create the CLI shortcut
echo -e "${YELLOW}Creating 'subio' command...${NC}"
ln -sf $SUBIO_DIR/subio.sh /usr/local/bin/subio

echo -e "${GREEN}Installation Complete!${NC}"
echo -e "You can now run ${CYAN}subio${NC} from anywhere to open the interactive menu."
