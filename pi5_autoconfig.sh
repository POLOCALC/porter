#!/bin/bash
# =============================================================
# Raspberry Pi 5 Setup Script
# Script: pi5_autoconfig.sh
# Run as: bash pi5_autoconfig.sh
# Prerequisites: Pi already flashed with RPi OS Lite 64-bit,
#   SSH enabled, logged in as polocalc
# =============================================================

set -e  # exit on error

# folder containing this script (and config.txt), wherever it is run from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================"
echo " RPi Setup Script Starting..."
echo "============================================"

# -------------------------------------------------------------
# 1. STATIC IP via nmcli
# -------------------------------------------------------------
echo "[1/10] Configuring static IP..."

CONN="Wired connection 1"

sudo nmcli connection modify "$CONN" ipv4.method manual ipv4.addresses 192.168.4.57/24
sudo nmcli connection modify "$CONN" ipv4.gateway 192.168.4.1
sudo nmcli connection modify "$CONN" ipv4.dns "1.1.1.1 8.8.8.8"
sudo nmcli connection modify "$CONN" ipv6.method disabled

echo "    Static IP configured: 192.168.4.57/24"

# -------------------------------------------------------------
# 2. DISABLE SUDO PASSWORD for polocalc
# -------------------------------------------------------------
echo "[2/10] Disabling sudo password for polocalc..."

echo "polocalc ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_polocalc-nopasswd > /dev/null
sudo chmod 0440 /etc/sudoers.d/010_polocalc-nopasswd

# -------------------------------------------------------------
# 3. ENABLE AUTO LOGIN (console)
# -------------------------------------------------------------
echo "[3/10] Enabling auto login for polocalc..."

sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin polocalc --noclear %I \$TERM
EOF

# -------------------------------------------------------------
# 4. DISABLE UNUSED SERVICES
# -------------------------------------------------------------
echo "[4/10] Disabling unused services..."

sudo systemctl disable bluetooth.service
sudo systemctl stop bluetooth.service
sudo systemctl disable wpa_supplicant.service
sudo systemctl disable avahi-daemon.service
sudo systemctl stop avahi-daemon.service
sudo apt purge -y cloud-init

echo "    Services disabled."

# -------------------------------------------------------------
# 5. INSTALL PACKAGES
# -------------------------------------------------------------
echo "[5/10] Installing packages..."

sudo apt update -y
sudo apt install -y screen git vim cmake libtool build-essential libusb-1.0-0-dev libaio-dev automake gpsd gpsd-clients pps-tools python3-gps chrony

wget https://downloads.xiph.org/releases/opus/opus-1.6.1.tar.gz
tar -xzvf opus-1.6.1.tar.gz
cd opus-1.6.1
autoreconf -f -i
./configure
make -j4
sudo make install
cd ..
rm -rf opus-1.6.1 opus-1.6.1.tar.gz

echo "    Packages installed."

# -------------------------------------------------------------
# 6. INSTALL config.txt (from the repository, next to this script)
# -------------------------------------------------------------
echo "[6/10] Installing /boot/firmware/config.txt..."

CONFIG_SRC="$SCRIPT_DIR/config.txt"
CONFIG_DST="/boot/firmware/config.txt"

if [ ! -f "$CONFIG_SRC" ]; then
    echo "    ERROR: $CONFIG_SRC not found, config.txt not changed."
    exit 1
fi

# keep the current file, so it can be restored from the SD card if the new one does not boot
if [ -f "$CONFIG_DST" ]; then
    sudo cp "$CONFIG_DST" "$CONFIG_DST.bak"
    echo "    Previous config.txt saved as $CONFIG_DST.bak"
fi

sudo install -m 644 "$CONFIG_SRC" "$CONFIG_DST"

echo "    config.txt installed from $CONFIG_SRC."

# -------------------------------------------------------------
# 7. GPS (gpsd + PPS) AND TIME SYNC (chrony)
#    (gpsd, gps_serial.rules and chrony.conf are in the repository, next to this script)
# -------------------------------------------------------------
echo "[7/10] Configuring gpsd and chrony..."

for f in gpsd gps_serial.rules chrony.conf; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        echo "    ERROR: $SCRIPT_DIR/$f not found, GPS and time sync not configured."
        exit 1
    fi
done

# gpsd
sudo install -m 644 "$SCRIPT_DIR/gpsd" /etc/default/gpsd
echo "    /etc/default/gpsd installed."

# polocalc must be in dialout to open the serial ports (porter, ttyAMA0) and /dev/pps0
sudo usermod -a -G dialout "$USER"

# udev rule: /dev/pps0 owned by root:dialout at every boot (a chown in /dev is lost at reboot)
sudo install -m 644 "$SCRIPT_DIR/gps_serial.rules" /etc/udev/rules.d/gps_serial.rules
sudo udevadm control --reload-rules
if [ -e /dev/pps0 ]; then
    sudo udevadm trigger --action=change /dev/pps0     # apply it now, no reboot needed
    echo "    /dev/pps0: $(stat -c '%U:%G %a' /dev/pps0)"
else
    echo "    /dev/pps0 not present yet (pps-gpio is active after the reboot): the rule applies then"
fi
echo "    /etc/udev/rules.d/gps_serial.rules installed."

sudo systemctl restart gpsd
sudo systemctl enable gpsd
echo "    gpsd enabled and restarted."

# chrony
sudo mkdir -p /etc/chrony
sudo install -m 644 "$SCRIPT_DIR/chrony.conf" /etc/chrony/chrony.conf
echo "    /etc/chrony/chrony.conf installed (used from the next chrony restart / reboot)."

# -------------------------------------------------------------
# 8. EDIT .bashrc
# -------------------------------------------------------------
echo "[8/10] Updating .bashrc..."

BASHRC="/home/polocalc/.bashrc"

if ! grep -qs "Project PATH additions" "$BASHRC"; then
    cat >> "$BASHRC" <<'EOF'
    # --- Project PATH additions ---
    alias vimbax='/opt/VimbaX_2026-1/bin/VimbaXViewer'
    export PATH="$HOME/.local/bin:$PATH"
EOF
fi

echo "    .bashrc updated."

# -------------------------------------------------------------
# 9. REACTIVATE NETWORK + VERIFY
# -------------------------------------------------------------
echo "[9/10] Reactivating network connection..."

sudo nmcli connection down "$CONN" || true
sudo nmcli connection up "$CONN" || true

# -------------------------------------------------------------
# 10. PORTER DEPENDENCIES
# -------------------------------------------------------------
echo "[10/10] Installing PORTER dependencies..."

if ! bash "$SCRIPT_DIR/install_porter.sh"; then
    echo ""
    echo "PORTER dependency install FAILED - not rebooting, so you can read the errors."
    echo "Fix the problem, then run: bash $SCRIPT_DIR/install_porter.sh"
    exit 1
fi


echo ""
echo "============================================"
echo " Setup complete! Rebooting in 5 seconds..."
echo " Reconnect via: ssh polocalc@192.168.4.57"
echo "============================================"
sleep 5
sudo reboot now