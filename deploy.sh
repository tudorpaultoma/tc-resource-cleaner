#!/bin/bash
# Deployment script for Tencent Cloud SCF

set -e

FUNCTION_NAME="tc-resource-cleaner"
PACKAGE_NAME="scf-resource-cleaner.zip"

echo "=========================================="
echo "Building ${FUNCTION_NAME} deployment package"
echo "=========================================="

# Clean previous builds
rm -f ${PACKAGE_NAME}
rm -f scf-clb-cleaner.zip
rm -rf package

# Create package directory
mkdir -p package

# Install dependencies
echo "Installing dependencies..."
pip3 install -r requirements.txt -t package/

# Copy source code
echo "Copying source code..."
cp index.py package/
cp -r services package/

# Create zip package
echo "Creating deployment package..."
cd package
zip -r ../${PACKAGE_NAME} . -q
cd ..

# Cleanup
rm -rf package

echo "=========================================="
echo "Deployment package created: ${PACKAGE_NAME}"
echo "Package size: $(du -h ${PACKAGE_NAME} | cut -f1)"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Upload ${PACKAGE_NAME} to SCF console"
echo "2. Set handler: index.main_handler"
echo "3. Configure environment variables:"
echo "   - DEFAULT_TTL_DAYS=7"
echo "   - DRY_RUN=false"
echo "   - ENABLE_CLB=true"
echo "   - ENABLE_CBS=true"
echo "   - ENABLE_EIP=true"
echo "   - ENABLE_ENI=true"
echo "   - ENABLE_HAVIP=true"
echo "   - REGIONS=ap-singapore,ap-hongkong (optional)"
echo "4. Set trigger: Timer (Cron: 0 2 * * * - daily at 2 AM)"
echo "5. Configure CAM role with required permissions"
echo "=========================================="
