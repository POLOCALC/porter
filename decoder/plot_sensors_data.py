#!/usr/bin/env python3
"""
plot_sensors_data.py - save PNG plots of every porter sensor in a run folder.

Usage:
  python3 plot_sensors_data.py [run_folder] [--dpi 120] [--no-telemetry]

run_folder defaults to /home/polocalc/data/current. The configuration copy saved by
control.py in the run folder tells which sensors were configured and how.
Plots are written to <run_folder>/sensors_data_plots/. Nothing is displayed.
If the configuration has a lager pointing controller, lager's plot_telemetry.py is also
run on the same folder; its UAV and gimbal plots go to <run_folder>/pointing_controller_plots/.
"""
import argparse
import datetime
import glob
import os
import subprocess
import sys

import matplotlib
matplotlib.use("Agg")                         # files only, no display needed
from matplotlib import pyplot as plt
import numpy as np
import pandas as pd
import yaml

DEFAULT_RUN = "/home/polocalc/data/current"
SENSORS_DIR = "sensors_data"
PLOTS_DIR = "sensors_data_plots"
MAX_POINTS = 400_000                          # thin very long series before plotting (ADC at 1.6 kHz)

ADS1015_FULL_SCALE_V = {1: 4.096, 2: 2.048, 4: 1.024, 8: 0.512, 16: 0.256}   # gain -> full scale, as in porter
IIS2MDC_MG_PER_LSB = 1.5                      # magnetometer sensitivity
IMU_UNITS = {"asm330lhh": ("mg", "mdps"),     # (accelerometer, gyroscope) units written by the inertial binary
             "mpu6050":   ("g",  "rad/s")}
GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc).timestamp()
GPS_LEAP_S = 18                               # GPS - UTC, valid since 2017

# lager's UAV/gimbal plotting script (git submodule)
LAGER_PLOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "modules", "lager", "decoder", "plot_telemetry.py")

parser = argparse.ArgumentParser(description="Save plots of all porter sensors in a run folder")
parser.add_argument("--data_dir", nargs="?", default=DEFAULT_RUN, help="Run folder (contains sensors_data/)")
parser.add_argument("--dpi", type=int, default=120, help="Resolution of the saved PNG files")
parser.add_argument("--no-telemetry", action="store_true",
                    help="Do not run lager's plot_telemetry.py for the UAV and gimbal")


def thin(n):
    """Step to keep at most MAX_POINTS samples."""
    return max(1, n // MAX_POINTS)


def save_series(path, title, t, rows, dpi):
    """One figure, one subplot per row. rows: [(label, unit, values, step)]. t in seconds."""
    rows = [r for r in rows if r[2] is not None and len(r[2]) and np.isfinite(np.asarray(r[2], float)).any()]
    if not rows or len(t) == 0:
        print(f"  skipped {os.path.basename(path)}: no data")
        return False
    k = thin(len(t))
    fig, axs = plt.subplots(len(rows), 1, figsize=(12, 2.6 * len(rows) + 0.8), sharex=True, squeeze=False)
    for ax, (label, unit, values, step) in zip(axs[:, 0], rows):
        ax.plot(np.asarray(t)[::k], np.asarray(values, float)[::k], linewidth=0.7,
                drawstyle="steps-post" if step else "default")
        ax.set_ylabel(f"{label} [{unit}]" if unit else label)
        ax.grid(True, alpha=0.3)
    axs[-1, 0].set_xlabel("Time since run start [s]")
    fig.suptitle(title + (f"  (every {k}th sample shown)" if k > 1 else ""))
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return True


def save_timing(path, title, t, dpi):
    """Sample interval over time and its histogram: shows dropouts and the real rate."""
    t = np.asarray(t, float)
    if len(t) < 3:
        return False
    dt_ms = np.diff(t) * 1e3
    k = thin(len(dt_ms))
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 6))
    a1.plot(t[1:][::k], dt_ms[::k], linewidth=0.5)
    a1.set_xlabel("Time since run start [s]")
    a1.set_ylabel("Interval [ms]")
    a1.grid(True, alpha=0.3)
    a2.hist(dt_ms, bins=200, log=True)
    a2.set_xlabel("Interval between samples [ms]")
    a2.set_ylabel("Count")
    rate = (len(t) - 1) / (t[-1] - t[0]) if t[-1] > t[0] else float("nan")
    fig.suptitle(f"{title}: {len(t)} samples, mean rate {rate:.2f} Hz, max gap {dt_ms.max():.1f} ms")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return True

def load_text(path, names):
    """Whitespace-separated text file; the last line may be incomplete if the binary was killed."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    df = pd.read_csv(path, sep=r"\s+", header=None, names=names, on_bad_lines="skip", engine="python")
    df = df.apply(pd.to_numeric, errors="coerce").dropna()
    return df if len(df) else None


def load_csv(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    df = pd.read_csv(path, on_bad_lines="skip")
    return df if len(df) else None


def load_adc(path):
    df = load_text(path, ["t_us", "raw"])
    if df is not None:
        df["unix"] = df["t_us"] * 1e-6
    return df


def load_inertial(folder):
    out = {}
    for part, cols in (("barometer", ["t_us", "pressure", "temperature"]),
                       ("magnetometer", ["t_us", "x", "y", "z"]),
                       ("gyroscope", ["t_us", "x", "y", "z"]),
                       ("accelerometer", ["t_us", "x", "y", "z"])):
        df = load_text(os.path.join(folder, part + ".bin"), cols)
        if df is not None:
            df["unix"] = df["t_us"] * 1e-6
            out[part] = df
    return out


def load_ns_csv(path):
    df = load_csv(path)
    if df is not None and "timestamp_ns" in df.columns:
        df["unix"] = df["timestamp_ns"] * 1e-9
    return df


def load_gps(path, t_ref):
    """Decode a raw UBX file. iTOW / rcvTow (GPS time of week) is converted to Unix time with t_ref."""
    from pyubx2 import UBXReader, UBX_PROTOCOL
    rows = {"NAV-POSLLH": [], "NAV-STATUS": [], "NAV-CLOCK": [], "RXM-RAWX": []}
    with open(path, "rb") as f:
        for _, msg in UBXReader(f, protfilter=UBX_PROTOCOL, quitonerror=0):
            if msg is None or msg.identity not in rows:
                continue
            if msg.identity == "NAV-POSLLH":
                rows[msg.identity].append(dict(tow=msg.iTOW / 1e3, lat=msg.lat, lon=msg.lon,
                                               hmsl=msg.hMSL / 1e3, hacc=msg.hAcc / 1e3, vacc=msg.vAcc / 1e3))
            elif msg.identity == "NAV-STATUS":
                rows[msg.identity].append(dict(tow=msg.iTOW / 1e3, fix=msg.gpsFix, fix_ok=int(msg.gpsFixOk)))
            elif msg.identity == "NAV-CLOCK":
                rows[msg.identity].append(dict(tow=msg.iTOW / 1e3, bias=msg.clkB, drift=msg.clkD, tacc=msg.tAcc))
            elif msg.identity == "RXM-RAWX":
                best = {}                                       # (gnssId, svId) -> strongest C/N0
                for i in range(1, msg.numMeas + 1):
                    cno = getattr(msg, f"cno_{i:02d}", 0)
                    key = (getattr(msg, f"gnssId_{i:02d}", None), getattr(msg, f"svId_{i:02d}", None))
                    if cno > 0 and cno > best.get(key, 0):
                        best[key] = cno
                top = sorted(best.values(), reverse=True)[:4]
                rows[msg.identity].append(dict(tow=msg.rcvTow, sats=len(best),
                                               cno=sum(top) / len(top) if top else np.nan))
    out = {}
    for ident, r in rows.items():
        if not r:
            continue
        df = pd.DataFrame(r)
        if t_ref is not None:                                   # GPS time of week -> Unix time
            week_start = GPS_EPOCH + np.floor((t_ref + GPS_LEAP_S - GPS_EPOCH) / 604800) * 604800
            df["unix"] = week_start + df["tow"] - GPS_LEAP_S
        else:                                                    # no reference: relative time only
            df["unix"] = df["tow"]
        out[ident] = df
    return out

def plot_adc(name, df, cfg, out, t0, dpi):
    gain = (cfg.get("configuration") or {}).get("gain", 8)
    fs = ADS1015_FULL_SCALE_V.get(gain)
    t = df["unix"] - t0
    if fs is not None:
        rows = [("Voltage", "V", df["raw"] * fs / 2048.0, False)]            # 12-bit, ±full scale
        title = f"{name} ADC (gain {gain}, ±{fs} V)"
    else:
        rows = [("Raw", "counts", df["raw"], False)]
        title = f"{name} ADC (unknown gain {gain}, raw counts)"
    save_series(os.path.join(out, f"{name}_adc.png"), title, t, rows, dpi)
    save_timing(os.path.join(out, f"{name}_timing.png"), f"{name} ADC timing", df["unix"] - t0, dpi)


def plot_inertial(name, parts, cfg, out, t0, dpi):
    model = str((cfg.get("configuration") or {}).get("imu_model", "")).lower()
    acc_u, gyro_u = IMU_UNITS.get(model, ("a.u.", "a.u."))
    if "barometer" in parts:
        d = parts["barometer"]
        save_series(os.path.join(out, f"{name}_barometer.png"), f"{name} barometer", d["unix"] - t0,
                    [("Pressure", "hPa", d["pressure"], False), ("Temperature", "°C", d["temperature"], False)], dpi)
    for part, title, unit, scale in (("magnetometer", "magnetometer", "mG", IIS2MDC_MG_PER_LSB),
                                     ("accelerometer", f"accelerometer ({model or 'unknown IMU'})", acc_u, 1),
                                     ("gyroscope", f"gyroscope ({model or 'unknown IMU'})", gyro_u, 1)):
        if part in parts:
            d = parts[part]
            save_series(os.path.join(out, f"{name}_{part}.png"), f"{name} {title}", d["unix"] - t0,
                        [(f"{a.upper()}", unit, d[a] * scale, False) for a in ("x", "y", "z")], dpi)
    for part, d in parts.items():
        save_timing(os.path.join(out, f"{name}_{part}_timing.png"), f"{name} {part} timing", d["unix"] - t0, dpi)


def plot_imx5(name, files, out, t0, dpi):
    imu, ins, inl2 = files.get("imu"), files.get("ins"), files.get("inl2")
    if imu is not None:
        t = imu["unix"] - t0
        save_series(os.path.join(out, f"{name}_imu_gyroscope.png"), f"{name} IMU gyroscope", t,
                    [(a, "rad/s", imu[c], False) for a, c in (("P", "pqr_P_rad_s"), ("Q", "pqr_Q_rad_s"), ("R", "pqr_R_rad_s"))], dpi)
        save_series(os.path.join(out, f"{name}_imu_accelerometer.png"), f"{name} IMU accelerometer", t,
                    [(a, "m/s²", imu[c], False) for a, c in (("X", "acc_X_m_s2"), ("Y", "acc_Y_m_s2"), ("Z", "acc_Z_m_s2"))], dpi)
        save_timing(os.path.join(out, f"{name}_imu_timing.png"), f"{name} IMU timing", t, dpi)
    if ins is not None:
        t = ins["unix"] - t0
        save_series(os.path.join(out, f"{name}_ins_position.png"), f"{name} INS position", t,
                    [("Latitude", "deg", ins["lat_deg"], False), ("Longitude", "deg", ins["lon_deg"], False),
                     ("Altitude", "m", ins["alt_m"], False)], dpi)
        save_series(os.path.join(out, f"{name}_ins_attitude.png"), f"{name} INS attitude", t,
                    [(a, "deg", np.degrees(ins[c]), False) for a, c in (("Roll", "roll_rad"), ("Pitch", "pitch_rad"), ("Yaw", "yaw_rad"))], dpi)
        save_series(os.path.join(out, f"{name}_ins_velocity.png"), f"{name} INS velocity (body)", t,
                    [(a, "m/s", ins[c], False) for a, c in (("U", "vel_U_m_s"), ("V", "vel_V_m_s"), ("W", "vel_W_m_s"))], dpi)
        save_series(os.path.join(out, f"{name}_ins_ned.png"), f"{name} INS position NED", t,
                    [(a, "m", ins[c], False) for a, c in (("North", "ned_N_m"), ("East", "ned_E_m"), ("Down", "ned_D_m"))], dpi)
        save_series(os.path.join(out, f"{name}_ins_status.png"), f"{name} INS status", t,
                    [("INS status", "", ins["insStatus"], True), ("Hardware status", "", ins["hdwStatus"], True)], dpi)
        save_timing(os.path.join(out, f"{name}_ins_timing.png"), f"{name} INS timing", t, dpi)
    if inl2 is not None:
        t = inl2["unix"] - t0
        save_series(os.path.join(out, f"{name}_inl2_gyro_bias.png"), f"{name} gyroscope bias", t,
                    [(a, "rad/s", inl2[f"biasPqr_{a.lower()}_rad_s"], False) for a in ("X", "Y", "Z")], dpi)
        save_series(os.path.join(out, f"{name}_inl2_acc_bias.png"), f"{name} accelerometer bias", t,
                    [(a, "m/s²", inl2[f"biasAcc_{a.lower()}_m_s2"], False) for a in ("X", "Y", "Z")], dpi)
        save_series(os.path.join(out, f"{name}_inl2_baro_mag.png"), f"{name} baro bias and magnetic field", t,
                    [("Baro bias", "m", inl2["biasBaro_m"], False),
                     ("Mag declination", "deg", np.degrees(inl2["magDec_rad"]), False),
                     ("Mag inclination", "deg", np.degrees(inl2["magInc_rad"]), False)], dpi)


def plot_lm76(name, df, out, t0, dpi):
    t = df["unix"] - t0
    save_series(os.path.join(out, f"{name}_temperature.png"), f"{name} temperature", t,
                [("Temperature", "°C", df["temperature_c"], False),
                 ("Critical", "", df["status_crit"].astype(float), True),
                 ("High", "", df["status_high"].astype(float), True),
                 ("Low", "", df["status_low"].astype(float), True)], dpi)


def plot_gps(name, d, out, t0, dpi):
    if "NAV-POSLLH" in d:
        p = d["NAV-POSLLH"]; t = p["unix"] - t0
        save_series(os.path.join(out, f"{name}_position.png"), f"{name} position", t,
                    [("Latitude", "deg", p["lat"], False), ("Longitude", "deg", p["lon"], False),
                     ("Height MSL", "m", p["hmsl"], False)], dpi)
        save_series(os.path.join(out, f"{name}_accuracy.png"), f"{name} position accuracy", t,
                    [("Horizontal", "m", p["hacc"], False), ("Vertical", "m", p["vacc"], False)], dpi)
        save_timing(os.path.join(out, f"{name}_timing.png"), f"{name} NAV-POSLLH timing", t, dpi)
    if "NAV-STATUS" in d:
        s = d["NAV-STATUS"]
        save_series(os.path.join(out, f"{name}_fix.png"), f"{name} fix", s["unix"] - t0,
                    [("Fix type", "", s["fix"], True), ("Fix OK", "", s["fix_ok"], True)], dpi)
    if "NAV-CLOCK" in d:
        c = d["NAV-CLOCK"]
        save_series(os.path.join(out, f"{name}_clock.png"), f"{name} receiver clock", c["unix"] - t0,
                    [("Bias", "ns", c["bias"], False), ("Drift", "ns/s", c["drift"], False),
                     ("Time accuracy", "ns", c["tacc"], False)], dpi)
    if "RXM-RAWX" in d:
        r = d["RXM-RAWX"]
        save_series(os.path.join(out, f"{name}_signal.png"), f"{name} signal strength", r["unix"] - t0,
                    [("Satellites", "", r["sats"], True), ("Top-4 C/N0", "dB-Hz", r["cno"], False)], dpi)


def find_config(run):
    for f in sorted(os.listdir(run)):
        if f.endswith((".yml", ".yaml")):
            with open(os.path.join(run, f)) as fh:
                return yaml.safe_load(fh) or {}
    return {}


def data_path(sensors_dir, name, suffix=""):
    """File porter wrote for this sensor: <name>_<timestamp><suffix> (first match)."""
    for p in sorted(glob.glob(os.path.join(sensors_dir, f"{glob.escape(name)}_*{suffix}"))):
        if not p.endswith("_stdout.log"):
            return p
    return None


def plot_telemetry(run, config, dpi):
    """Run lager's plot_telemetry.py on the run folder, if a lager pointing controller was configured."""
    pc = config.get("pointing_controller") or {}
    if str(pc.get("name", "")).lower() != "lager":
        print("No lager pointing controller in the configuration, UAV/gimbal plots skipped")
        return
    script = os.path.realpath(LAGER_PLOT_SCRIPT)
    if not os.path.isfile(script):
        print(f"lager plot script not found ({script}), UAV/gimbal plots skipped")
        return
    print("Plotting UAV and gimbal telemetry with lager...")
    # separate process with the same interpreter: lager's main() parses its own command line
    result = subprocess.run([sys.executable, script, "--data-folder", run, "--dpi", str(dpi)])
    if result.returncode != 0:
        print(f"lager plot_telemetry.py failed (exit code {result.returncode})")


def plot_sensors(run, config, dpi):
    sensors_dir = os.path.join(run, SENSORS_DIR)
    if not os.path.isdir(sensors_dir):
        print(f"No {SENSORS_DIR}/ folder in {run}, sensor plots skipped")
        return
    sensors = config.get("sensors") or {}
    if not sensors:
        print(f"No 'sensors' block in the configuration in {run}, sensor plots skipped")
        return
    out = os.path.join(run, PLOTS_DIR)
    os.makedirs(out, exist_ok=True)

    # load everything that has Unix timestamps first, to find the run start
    loaded = []
    for key, cfg in sensors.items():
        name = cfg.get("name", key)
        kind = str((cfg.get("sensor_info") or {}).get("type", "")).lower()
        try:
            if kind == "adc" and (p := data_path(sensors_dir, name, ".bin")):
                loaded.append((kind, name, cfg, load_adc(p)))
            elif kind == "inertial" and (p := data_path(sensors_dir, name, ".bin")) and os.path.isdir(p):
                loaded.append((kind, name, cfg, load_inertial(p)))
            elif kind == "imx5" and (p := data_path(sensors_dir, name, "_imu.csv")):
                base = p[: -len("_imu.csv")]
                loaded.append((kind, name, cfg, {s: load_ns_csv(f"{base}_{s}.csv") for s in ("imu", "ins", "inl2")}))
            elif kind == "lm76" and (p := data_path(sensors_dir, name, ".csv")):
                loaded.append((kind, name, cfg, load_ns_csv(p)))
            elif kind == "gps" and (p := data_path(sensors_dir, name, ".bin")):
                loaded.append((kind, name, cfg, p))             # decoded below, needs the run start
            elif kind in ("dac", "inclinometer"):
                print(f"{name}: {kind} records no data to plot, skipped")
            else:
                print(f"{name}: no data file found for type '{kind}'")
        except Exception as e:
            print(f"{name}: could not load data: {e}")

    def first_times(obj):
        if isinstance(obj, pd.DataFrame):
            return [obj["unix"].min()] if "unix" in obj.columns and len(obj) else []
        if isinstance(obj, dict):
            return [t for v in obj.values() for t in first_times(v)]
        return []
    starts = [t for _, _, _, obj in loaded for t in first_times(obj)]
    t_ref = min(starts) if starts else None
    if t_ref is None:                                            # fall back to the run folder name (local time)
        try:
            t_ref = datetime.datetime.strptime("_".join(os.path.basename(run).split("_")[-2:]),
                                               "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            t_ref = None

    for kind, name, cfg, obj in loaded:
        print(f"Plotting {name} ({kind})...")
        try:
            if kind == "gps":
                gps = load_gps(obj, t_ref)
                t0 = t_ref if t_ref is not None else min(df["unix"].min() for df in gps.values())
                plot_gps(name, gps, out, t0, dpi)
            elif obj is None or (isinstance(obj, dict) and not any(v is not None for v in obj.values())):
                print(f"  {name}: data file is empty")
            elif kind == "adc":
                plot_adc(name, obj, cfg, out, t_ref, dpi)
            elif kind == "inertial":
                plot_inertial(name, obj, cfg, out, t_ref, dpi)
            elif kind == "imx5":
                plot_imx5(name, obj, out, t_ref, dpi)
            elif kind == "lm76":
                plot_lm76(name, obj, out, t_ref, dpi)
        except Exception as e:
            print(f"  {name}: plotting failed: {e}")

    print(f"Sensor plots saved to {out}")


def main():
    args = parser.parse_args()
    run = os.path.realpath(args.data_dir)
    if os.path.basename(run) == SENSORS_DIR:                   # also accept the sensors_data folder itself
        run = os.path.dirname(run)
    if not os.path.isdir(run):
        raise SystemExit(f"Run folder not found: {run}")
    config = find_config(run)
    if not config:
        raise SystemExit(f"No configuration file (.yml) found in {run}")

    plot_sensors(run, config, args.dpi)
    if not args.no_telemetry:
        plot_telemetry(run, config, args.dpi)


if __name__ == "__main__":
    main()
