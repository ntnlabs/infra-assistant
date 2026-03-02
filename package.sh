#!/bin/bash
#
# Package infra-assistant for transfer
# Creates a tarball that can be copied to target machine
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_NAME="infra-assistant-$(date +%Y%m%d).tar.gz"

echo "Packaging infra-assistant..."

cd "${SCRIPT_DIR}/.."

tar -czvf "${PACKAGE_NAME}" \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='venv' \
    --exclude='*.log' \
    --exclude='dify' \
    infra-assistant/

echo ""
echo "Created: ${SCRIPT_DIR}/../${PACKAGE_NAME}"
echo ""
echo "To deploy:"
echo "  1. Copy ${PACKAGE_NAME} to target machine"
echo "  2. Extract: tar -xzvf ${PACKAGE_NAME} -C /opt/"
echo "  3. Run: cd /opt/infra-assistant && sudo ./install.sh"
