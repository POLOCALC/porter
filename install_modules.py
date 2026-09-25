import os

current_dir = os.path.dirname(os.path.abspath(__file__))

print("Installing required modules in the folder ./modules...")

# VmbPy
print(" Installing VmbPy")
try:
    os.system(f"pip install {current_dir}/modules/vmbpy-1.2.1-py3-none-any.whl")
except:
    print("Error installing VmbPy")

# Pyalvium
print(" Installing Pyalvium")
try:
    os.system(f"pip install -e {current_dir}/modules/Alvium-Camera-Module-Python/.")
except:
    print("Error installing Pyalvium")

# digi-Xbee
print(" Installing digi-xbee")
try:
    os.system(f"pip install digi-xbee==1.5.0")
except:
    print("Error installing digi-xbee")

# ina228 
print(" Installing ina228")
try:
    os.system(f"pip install adafruit-circuitpython-ina228")
except:
    print("Error installing ina228")

# pyubx2
print(" Installing pyubx2")
try:
    os.system(f"pip install pyubx2")
except:
    print("Error installing pyubx2")

# adafruit-circuitpython-mcp4725
print(" Installing adafruit-circuitpython-mcp4725")
try:
    os.system(f"pip install adafruit-circuitpython-mcp4725")
except:
    print("Error installing adafruit-circuitpython-mcp4725")

# pyUSB
print(" Installing pyusb")
try:
    os.system(f"pip install pyusb")
except:
    print("Error installing pyusb")

# pyyaml
print(" Installing pyyaml")
try:
    os.system(f"pip install pyyaml")
except:
    print("Error installing pyyaml")

# lager
print(" Installing lager")
try:
    os.system(f"pip install -e {current_dir}/modules/lager/.")
except:
    print("Error installing lager")

# SourCore
print(" Installing SourCore")
try:
    os.system(f"pip install -e {current_dir}/modules/sour_core/.")
except:
    print("Error installing SourCore")

# Alvium (Starspec module)
print(" Installing Alvium (Starspec module)")
try:
    os.system(f"cd {current_dir}/modules/Alvium-Camera-Module && ./build.sh")
except:
    print("Error installing Alvium (Starspec module)")

# ADS1015
print(" Installing ADS1015")
try:
    # cd to the build_for_pi.sh script to the current directory and run it
    os.system(f"cd {current_dir}/modules/ADS1015-ADC-Module && ./build_for_pi.sh")
except:
    print("Error installing ADS1015")

# Inertial sensor
print(" Installing Inertial-Sensors-Module")
try:
    os.system(f"cd {current_dir}/modules/Inertial-Sensors-Module && ./build_for_pi.sh")
except:
    print("Error installing Inertial-Sensors-Module")

# LM76
print(" Installing LM76-Temperature-Sensor")
try:
    os.system(f"cd {current_dir}/modules/LM76-Temperature-Sensor && ./build_for_pi.sh")
except:
    print("Error installing LM76-Temperature-Sensor")

# IMX5 (prebuilt binary): symlink it into ~/.local/bin so it runs from anywhere
print(" Linking IMX5SensorModule")
try:
    binary = os.path.join(current_dir, "modules", "IMX-5-Sensor-Module", "bin", "IMX5SensorModule")
    bin_dir = os.path.expanduser("~/.local/bin")
    link = os.path.join(bin_dir, "IMX5SensorModule")

    if not os.path.isfile(binary):
        raise FileNotFoundError(binary)

    os.makedirs(bin_dir, exist_ok=True)
    # replace an existing link (equivalent of ln -sf)
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink(binary, link)
    print(f"  Linked {link} -> {binary}")

    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        bashrc = os.path.expanduser("~/.bashrc")
        with open(bashrc, "a+") as f:
            f.seek(0)
            if "HOME/.local/bin" not in f.read():
                f.write('\nexport PATH="$HOME/.local/bin:$PATH"\n')
                print("  Added ~/.local/bin to PATH in ~/.bashrc (run: source ~/.bashrc)")
except Exception as e:
    print(f"Error linking IMX5SensorModule: {e}")

print("Done installing packages.")

print("Installing systemd services...")
try:
    os.system(f"sudo cp {current_dir}/startup/telemd.service /etc/systemd/system/")
    os.system("sudo systemctl daemon-reload")
    os.system("sudo systemctl enable telemd.service")
    os.system("sudo systemctl start telemd.service")
except:
    print("Error installing systemd services")

try:
    os.system(f"sudo cp {current_dir}/startup/powerd.service /etc/systemd/system/")
    os.system("sudo systemctl daemon-reload")
    os.system("sudo systemctl enable powerd.service")
    os.system("sudo systemctl start powerd.service")
except:
    print("Error installing systemd services")