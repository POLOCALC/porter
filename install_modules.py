import subprocess
import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
failures = []

def step(name, cmd):
    print(f"Installing {name}...")
    try:
        subprocess.run(cmd, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        failures.append(name)
        print(f"   ERROR: {name} failed (exit {e.returncode})")

# use the environment PIP, if it does not exist, create it
VENV = "/home/polocalc/porter_venv"
PIP = f"{VENV}/bin/pip"
if not os.path.exists(PIP):
    print(f"Creating virtual environment for porter in {VENV}...")
    subprocess.run(f"python3 -m venv {VENV}", shell=True, check=True)


print("Installing packages...")

step("numpy", f"{PIP} install numpy==2.2.4")
step("scipy", f"{PIP} install scipy==1.15.3")
step("opencv", f"{PIP} install opencv-python-headless==4.10.0.84")
step("VmbPy", f"{PIP} install {current_dir}/modules/vmbpy-1.2.1-py3-none-any.whl")
step("Pyalvium", f"{PIP} install -e {current_dir}/modules/Alvium-Camera-Module-Python/.")
step("digi-xbee", f"{PIP} install digi-xbee==1.5.0")
step("ina228", f"{PIP} install adafruit-circuitpython-ina228")
step("pyubx2", f"{PIP} install pyubx2")
step("mcp4725", f"{PIP} install adafruit-circuitpython-mcp4725")
step("pyusb", f"{PIP} install pyusb")
step("pyyaml", f"{PIP} install pyyaml")
step("lager", f"{PIP} install -e {current_dir}/modules/lager/.")
step("sourcore", f"{PIP} install -e {current_dir}/modules/sour_core/.")
step("Alvium (Starspec)", f"cd {current_dir}/modules/Alvium-Camera-Module && ./build.sh")
step("ADS1015", f"cd {current_dir}/modules/ADS1015-ADC-Module && ./build_for_pi.sh")
step("Inertial-Sensors-Module", f"cd {current_dir}/modules/Inertial-Sensors-Module && ./build_for_pi.sh")
step("LM76-Temperature-Sensor", f"cd {current_dir}/modules/LM76-Temperature-Sensor && ./build_for_pi.sh")


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
    failures.append("IMX5 link")
    print(f"Error linking IMX5SensorModule: {e}")

if failures:
    print("\nThe following packages failed to install:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("Done installing packages.")


print("Installing systemd services...")

step("copy unit", f"sudo cp {current_dir}/services/telemd.service /etc/systemd/system/")
#step("copy unit", f"sudo cp {current_dir}/services/powerd.service /etc/systemd/system/")
step("copy unit", f"sudo cp {current_dir}/services/porter@.service /etc/systemd/system/")
step("daemon-reload", "sudo systemctl daemon-reload")

step("enable telemd", "sudo systemctl enable telemd.service")
step("restart telemd", "sudo systemctl restart telemd.service")

#step("enable powerd", "sudo systemctl enable powerd.service")
#step("restart powerd", "sudo systemctl restart powerd.service")

if failures:
    print(f"Failed steps: {', '.join(failures)}")
    sys.exit(1)
print("Install complete.")
