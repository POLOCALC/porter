#!/bin/bash
# =============================================================
# PORTER dependency installer
# Script: install_porter.sh
# Run as: bash install_porter.sh      (as polocalc, NOT with sudo)
#
# Installs the Python packages into the porter venv, builds the sensor
# binaries from the submodules, links them into ~/.local/bin and installs
# the systemd units. Safe to run again: every step can be repeated.
# Can be run on its own or at the end of pi5_autoconfig.sh.
# =============================================================

set -u                      # undefined variables are errors; no "set -e": failed steps are collected
set -o pipefail

# repository root = folder containing this script, wherever it is run from
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="/home/polocalc/porter_venv"
PIP="$VENV/bin/pip"
BIN_DIR="$HOME/.local/bin"
LOG="$REPO_DIR/install_porter.log"

# keep a copy of all output (useful when run over ssh from pi5_autoconfig.sh)
exec > >(tee -a "$LOG") 2>&1
echo "============================================"
echo " PORTER dependency install - $(date)"
echo " Repository: $REPO_DIR"
echo " Log:        $LOG"
echo "============================================"

# must run as the normal user: the venv and ~/.local/bin belong to polocalc
if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run this script with sudo or as root; run it as polocalc."
    exit 1
fi

FAILURES=()

# step NAME COMMAND...   run a command, record a failure without stopping the script
step() {
    local name="$1"; shift
    echo ""
    echo ">>> $name"
    if "$@"; then
        echo "    OK: $name"
    else
        local rc=$?
        echo "    ERROR: $name failed (exit $rc)"
        FAILURES+=("$name")
    fi
}

# step_in DIR NAME COMMAND...   same, but run inside DIR (without changing this script's directory)
step_in() {
    local dir="$1" name="$2"; shift 2
    step "$name" bash -c "cd \"$dir\" && $*"
}

# -------------------------------------------------------------
# 1. PREREQUISITES
# -------------------------------------------------------------
echo ""
echo "[1/5] Checking prerequisites..."

# git submodules: the build steps need their sources
if [ -d "$REPO_DIR/.git" ]; then
    step "git submodules" git -C "$REPO_DIR" submodule update --init --recursive
fi

# python3-venv: needed to create the venv on Raspberry Pi OS
if ! python3 -m venv --help > /dev/null 2>&1; then
    step "python3-venv (apt)" sudo apt install -y python3-venv
    step "pip upgrade" "$PIP" install --upgrade pip setuptools wheel
fi

# build prerequisites for Python packages without a prebuilt wheel for this Pi
step "build prerequisites (apt)" sudo apt install -y python3-dev build-essential pkg-config libffi-dev swig liblgpio-dev

# Rust toolchain: needed by build_for_pi.sh (ADS1015, Inertial, LM76)
if ! command -v cargo > /dev/null 2>&1 && [ -x "$HOME/.cargo/bin/cargo" ]; then
    export PATH="$HOME/.cargo/bin:$PATH"
fi
if ! command -v cargo > /dev/null 2>&1; then
    echo "    cargo not found, installing the Rust toolchain (rustup)..."
    step "Rust toolchain" bash -c "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal"
    export PATH="$HOME/.cargo/bin:$PATH"
fi

# -------------------------------------------------------------
# 2. PYTHON VIRTUAL ENVIRONMENT
# -------------------------------------------------------------
echo ""
echo "[2/5] Python virtual environment..."

if [ ! -x "$PIP" ]; then
    echo "    Creating virtual environment in $VENV..."
    if ! python3 -m venv "$VENV"; then
        echo "ERROR: could not create $VENV, nothing else can be installed."
        exit 1
    fi
fi
step "pip upgrade" "$PIP" install --upgrade pip

# -------------------------------------------------------------
# 3. PYTHON PACKAGES
# -------------------------------------------------------------
echo ""
echo "[3/5] Installing Python packages..."

step "numpy"     "$PIP" install numpy==2.2.4
step "scipy"     "$PIP" install scipy==1.15.3
step "opencv"    "$PIP" install opencv-python-headless==4.10.0.84
step "VmbPy"     "$PIP" install "$REPO_DIR/modules/vmbpy-1.2.1-py3-none-any.whl"
step "Pyalvium"  "$PIP" install -e "$REPO_DIR/modules/Alvium-Camera-Module-Python"
step "digi-xbee" "$PIP" install digi-xbee==1.5.0
step "ina228"    "$PIP" install adafruit-circuitpython-ina228
step "pyubx2"    "$PIP" install pyubx2
step "mcp4725"   "$PIP" install adafruit-circuitpython-mcp4725
step "pyusb"     "$PIP" install pyusb
step "pyyaml"    "$PIP" install pyyaml
step "lager"     "$PIP" install -e "$REPO_DIR/modules/lager"
step "sourcore"  "$PIP" install -e "$REPO_DIR/modules/sour_core"

# -------------------------------------------------------------
# 4. SENSOR AND CAMERA BINARIES
# -------------------------------------------------------------
echo ""
echo "[4/5] Building binaries..."

# skip the old StarSpec Alvium build, which is not used anymore
#step_in "$REPO_DIR/modules/Alvium-Camera-Module"     "Alvium (Starspec)"       ./build.sh
step_in "$REPO_DIR/modules/ADS1015-ADC-Module"       "ADS1015"                 ./build_for_pi.sh
step_in "$REPO_DIR/modules/Inertial-Sensors-Module"  "Inertial-Sensors-Module" ./build_for_pi.sh
step_in "$REPO_DIR/modules/LM76-Temperature-Sensor"  "LM76-Temperature-Sensor" ./build_for_pi.sh

# IMX5 (prebuilt binary): symlink it into ~/.local/bin so it runs from anywhere
imx5_link() {
    local binary="$REPO_DIR/modules/IMX-5-Sensor-Module/bin/IMX5SensorModule"
    if [ ! -f "$binary" ]; then
        echo "    $binary not found"
        return 1
    fi
    mkdir -p "$BIN_DIR"
    ln -sf "$binary" "$BIN_DIR/IMX5SensorModule"
    echo "    Linked $BIN_DIR/IMX5SensorModule -> $binary"
}
step "IMX5 link" imx5_link

# DJI M400 telemetry bridge (Payload-SDK, a submodule of lager): build psdk_telemetry and link it into ~/.local/bin
PSDK_DIR="$REPO_DIR/modules/lager/modules/Payload-SDK"
psdk_build() {
    local app_info="$PSDK_DIR/samples/sample_c/platform/linux/raspberry_pi/application/dji_sdk_app_info.h"
    if [ ! -f "$app_info" ]; then
        echo "    $PSDK_DIR not found (git submodule update --init --recursive)"
        return 1
    fi
    if grep -q '"your_app_key"' "$app_info"; then
        echo "    DJI App Key/License not set in $app_info: psdk_telemetry not built (see the Payload-SDK README)"
        return 0                                   # not a failure: the M600 setup doesn't need it
    fi
    cmake -S "$PSDK_DIR" -B "$PSDK_DIR/build" -DCMAKE_BUILD_TYPE=Release || return 1
    make -C "$PSDK_DIR/build" -j4 psdk_telemetry || return 1
    mkdir -p "$BIN_DIR"
    ln -sf "$PSDK_DIR/build/bin/psdk_telemetry" "$BIN_DIR/psdk_telemetry"
    echo "    Linked $BIN_DIR/psdk_telemetry -> $PSDK_DIR/build/bin/psdk_telemetry"
}
step "psdk_telemetry" psdk_build


# make sure ~/.local/bin is on PATH for interactive shells (add it only once)
if ! grep -qs 'HOME/.local/bin' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo "    Added ~/.local/bin to PATH in ~/.bashrc"
fi

if [ ${#FAILURES[@]} -gt 0 ]; then
    echo ""
    echo "The following steps failed, systemd units NOT installed:"
    for f in "${FAILURES[@]}"; do echo "  - $f"; done
    echo "Fix the problem and run this script again. Full log: $LOG"
    exit 1
fi
echo ""
echo "All packages and binaries installed."

# -------------------------------------------------------------
# 5. SYSTEMD UNITS
# -------------------------------------------------------------
echo ""
echo "[5/5] Installing systemd units..."

# install the units with this repository's real path, wherever it is cloned
install_porter_units() {
    local unit
    for unit in telemd.service porter@.service psdk-bridge.service; do
        sed "s|/home/polocalc/flight/porter|$REPO_DIR|g" "$REPO_DIR/services/$unit" \
            | sudo tee "/etc/systemd/system/$unit" > /dev/null || return 1
    done
}
step "copy units" install_porter_units

# DJI E-Port USB gadget (Payload-SDK, a submodule of lager): makes the USB-C port a DJI serial device (/dev/ttyGS0)
GADGET_DIR="$REPO_DIR/modules/lager/modules/Payload-SDK/tools/dji_vcom_gadget"
HAVE_GADGET=0
if [ -f "$GADGET_DIR/dji_vcom_gadget.sh" ] && [ -f "$GADGET_DIR/dji-vcom-gadget.service" ]; then
    HAVE_GADGET=1
    # write the unit with the script's real path, so it works wherever the repository is cloned
    install_gadget_unit() {
        sed "s|^ExecStart=.*|ExecStart=/bin/bash $GADGET_DIR/dji_vcom_gadget.sh|" \
            "$GADGET_DIR/dji-vcom-gadget.service" \
            | sudo tee /etc/systemd/system/dji-vcom-gadget.service > /dev/null
    }
    step "copy dji-vcom-gadget unit" install_gadget_unit
else
    echo "    $GADGET_DIR not found: dji-vcom-gadget.service not installed"
fi

step "daemon-reload"  sudo systemctl daemon-reload
step "enable telemd"  sudo systemctl enable telemd.service
step "restart telemd" sudo systemctl restart telemd.service
# powerd is not implemented yet: do not install or enable powerd.service

if [ "$HAVE_GADGET" -eq 1 ]; then
    step "enable dji-vcom-gadget" sudo systemctl enable dji-vcom-gadget.service
    # the gadget needs the USB-C port in device mode (dtoverlay=dwc2,dr_mode=peripheral in config.txt);
    # on a first install that is active only after the next reboot
    if [ -n "$(ls /sys/class/udc 2>/dev/null)" ]; then
        step "start dji-vcom-gadget" sudo systemctl restart dji-vcom-gadget.service
    else
        echo "    USB-C port not in device mode yet: dji-vcom-gadget starts at the next boot"
    fi
fi

install_vcom_udev_rule() {
    echo "KERNEL==\"ttyGS0\", OWNER=\"$USER\", GROUP=\"dialout\", MODE=\"0660\"" \
        | sudo tee /etc/udev/rules.d/99-dji-vcom.rules > /dev/null || return 1
    sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty
}
[ "$HAVE_GADGET" = 1 ] && step "udev rule for /dev/ttyGS0" install_vcom_udev_rule

# PSDK bridge (Matrice 400): drone link from boot, 24 V on the E-Port, RC widgets. Needs psdk_telemetry (built
# above only when the DJI credentials are set). On a Matrice 600 setup: sudo systemctl disable --now psdk-bridge
if [ "$HAVE_GADGET" = 1 ] && [ -x "$BIN_DIR/psdk_telemetry" ]; then
    step "enable psdk-bridge"  sudo systemctl enable psdk-bridge.service
    step "restart psdk-bridge" sudo systemctl restart psdk-bridge.service
else
    echo "    psdk_telemetry not built or USB gadget missing: psdk-bridge.service installed but not enabled"
fi


if [ ${#FAILURES[@]} -gt 0 ]; then
    echo ""
    echo "Failed steps: ${FAILURES[*]}"
    echo "Full log: $LOG"
    exit 1
fi

echo ""
echo "============================================"
echo " PORTER install complete."
echo "============================================"
exit 0
