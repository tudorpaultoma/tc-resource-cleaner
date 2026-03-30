#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

ZIP_NAME="tc-resource-cleaner.zip"

# Clean previous builds
rm -rf package
rm -f "$ZIP_NAME"
mkdir -p package

# Install dependencies
echo "Installing dependencies..."
pip3 install -t package -r requirements.txt --no-cache-dir

# Remove unnecessary files to shrink the package
find package -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find package -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find package -type f -name "*.pyc" -delete 2>/dev/null || true
rm -rf package/bin

# Zip dependencies
echo "Zipping dependencies..."
cd package
zip -r9 "../$ZIP_NAME" . -x "*.DS_Store" -q
cd ..

# Add application code
echo "Adding application code..."
zip -r9 "$ZIP_NAME" index.py services/ -x "*.DS_Store" "*.pyc" "*__pycache__*" -q

# Cleanup
rm -rf package

echo "Done!"
ls -lh "$ZIP_NAME"
