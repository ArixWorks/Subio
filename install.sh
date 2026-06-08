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
echo -e "${YELLOW}Step 1: Installing dependencies...${NC}"
if ! apt-get update -y; then
    echo -e "${RED}Error: Failed to update package lists. Check your internet connection.${NC}"
    exit 1
fi

if ! apt-get install -y curl jq python3 git; then
    echo -e "${RED}Error: Failed to install required packages. Check your internet connection or APT sources.${NC}"
    exit 1
fi

SUBIO_DIR="/opt/subio"

if [ -d "$SUBIO_DIR/.git" ]; then
    echo -e "\n${YELLOW}Step 2: SubIO is already installed. Updating from GitHub...${NC}"
    cd $SUBIO_DIR
    git reset --hard HEAD
    if ! git pull; then
        echo -e "${RED}Error: Failed to pull updates from GitHub.${NC}"
        exit 1
    fi
else
    echo -e "\n${YELLOW}Step 2: Downloading SubIO files from GitHub...${NC}"
    rm -rf $SUBIO_DIR
    if ! git clone https://github.com/ArixWorks/Subio.git $SUBIO_DIR; then
        echo -e "${RED}Error: Failed to download repository. Please check your internet connection.${NC}"
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
