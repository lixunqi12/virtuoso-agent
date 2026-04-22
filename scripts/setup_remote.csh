#!/bin/csh
# =============================================================
# remote host Server One-Time Setup Script
# Run this on the remote host server to configure the environment
# =============================================================

echo "=== remote host Environment Setup ==="

# Step 1: Switch to Python 3.11
echo ""
echo "[1/4] Setting up Python 3.11..."
module swap apps/python/3.8 apps/python/3.11
python3 --version

# Step 2: Load Cadence IC 23.1
echo ""
echo "[2/4] Loading Cadence IC 23.1..."
module load cadence/ic_23.1
echo "CDS_INST_DIR = $CDS_INST_DIR"
which virtuoso

# Step 3: Install virtuoso-bridge-lite
echo ""
echo "[3/4] Installing virtuoso-bridge-lite..."
pip3 install --user virtuoso-bridge-lite

# Step 4: Verify installation
echo ""
echo "[4/4] Verifying installation..."
python3 -c "import virtuoso_bridge; print('virtuoso-bridge-lite OK')"

echo ""
echo "=== Setup complete ==="
echo "To start the bridge daemon, run:"
echo "  virtuoso-bridge start"
echo ""
echo "To check status:"
echo "  virtuoso-bridge status"
