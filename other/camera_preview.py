#!/usr/bin/env python3
"""
Live preview of the Alvium camera (USB via vmbpy, or CSI-2 via media-ctl/v4l2-ctl)
to tune exposure time and gain and to adjust the focus ring.

Usage:
    python3 other/camera_preview.py --backend csi
    python3 other/camera_preview.py --backend usb [--camera-id DEV_1AB22C0707F3]
    python3 other/camera_preview.py --backend csi --config config/default_M400_CSI.yml

Needs a display: run it on the Pi with an HDMI monitor / VNC, or from a laptop
with `ssh -X polocalc@<pi>` (lower --display-width if X forwarding is slow).
Porter must NOT be acquiring at the same time (the camera can only be opened once).

Window controls:
    trackbars        exposure (log scale) and gain
    left click       move the focus ROI (the green box)
    z                toggle 1:1 zoom on the focus ROI (to judge sharpness on real pixels)
    r                reset the peak focus value
    [ / ]            exposure -/+ 10%
    - / =            gain -/+ one step (0.1 dB on USB, 1 driver unit on CSI)
    s                save the current frame (PNG) and settings (txt) in --save-dir
    p                print the settings as a YAML snippet for the porter config
    q / ESC          quit

Focus: the "focus" number is the variance of the Laplacian inside the ROI.
Turn the focus gear slowly and stop where it peaks ("peak" keeps the maximum seen).
Point at a static, high-contrast, far target (the focus is set for infinity in flight)
and lower the exposure until nothing in the ROI is saturated, because clipped pixels
make the focus value unreliable.
"""

import argparse
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import cv2
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# OpenCV names Bayer codes after the 2nd row, so use the explicit sensor-order aliases
BAYER_TO_BGR = {
    'BayerRG8': cv2.COLOR_BayerRGGB2BGR,
    'BayerGR8': cv2.COLOR_BayerGRBG2BGR,
    'BayerGB8': cv2.COLOR_BayerGBRG2BGR,
    'BayerBG8': cv2.COLOR_BayerBGGR2BGR,
}


def to_bgr(arr, pixel_format):
    """Convert a 2D/3D uint8 frame to BGR."""
    if pixel_format in BAYER_TO_BGR:
        return cv2.cvtColor(arr, BAYER_TO_BGR[pixel_format])
    if pixel_format == 'Mono8':
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    if pixel_format == 'RGB8':
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return arr  # BGR8


# --------------------------------------------------------------------------- #
# Backends: each one exposes
#   exposure_range_us, gain_range, gain_step, gain_unit, pixel_format
#   start(), stop(), latest() -> (raw ndarray, frame counter)
#   set_exposure_us(v), set_gain(v), get_exposure_us(), get_gain()
# --------------------------------------------------------------------------- #

class UsbBackend:
    """Alvium USB3 through vmbpy. Frames arrive in a callback; only the newest one is kept."""

    def __init__(self, camera_id, exposure_us, gain, pixel_format, fps, buffers=10):
        import vmbpy
        self.vmbpy = vmbpy
        logging.getLogger('vmbpyLog').propagate = False
        self.camera_id = camera_id
        self.pixel_format = pixel_format
        self._init = dict(exposure_us=exposure_us, gain=gain, fps=fps, buffers=buffers)
        self._lock = threading.Lock()
        self._frame = None
        self._count = 0
        self.gain_step = 0.1
        self.gain_unit = 'dB'

    def start(self):
        vmbpy = self.vmbpy
        self._vmb = vmbpy.VmbSystem.get_instance()
        self._vmb.__enter__()
        # On the Pi, Vimba X also loads its CSI transport layer, so a CSI-attached Alvium
        # shows up here too (and usually first): only auto-pick USB3 Vision cameras.
        cams = self._vmb.get_all_cameras()
        for c in cams:
            try:
                tl = c.get_transport_layer().get_type().name
            except Exception:
                tl = '?'
            print(f'  vmbpy camera: {c.get_id()}  {c.get_model()}  transport={tl}')
        if self.camera_id:
            self.cam = self._vmb.get_camera_by_id(self.camera_id)
        else:
            usb = [c for c in cams
                   if c.get_transport_layer().get_type() == vmbpy.TransportLayerType.U3V]
            if not usb:
                raise RuntimeError('No USB3 Vision camera found by vmbpy (is it connected / is porter running?)')
            self.cam = usb[0]
        self.cam.__enter__()
        print(f'Opened {self.cam.get_id()} - {self.cam.get_model()}')

        def feat(name):
            return self.cam.get_feature_by_name(name)

        for name, value in [('ExposureAuto', 'Off'), ('GainAuto', 'Off'), ('BalanceWhiteAuto', 'Off'),
                            ('Width', feat('WidthMax').get()), ('Height', feat('HeightMax').get()),
                            ('PixelFormat', self.pixel_format),
                            ('AcquisitionFrameRateEnable', True)]:
            try:
                feat(name).set(value)
            except Exception as e:  # not every model has every feature
                print(f'warning: could not set {name}={value}: {e}')

        self.exposure_range_us = feat('ExposureTime').get_range()
        self.gain_range = feat('Gain').get_range()
        self.set_exposure_us(self._init['exposure_us'])
        self.set_gain(self._init['gain'])
        try:
            fr = feat('AcquisitionFrameRate')
            lo, hi = fr.get_range()
            fps = min(max(self._init['fps'], lo), hi)
            if fps != self._init['fps']:
                print(f'warning: {self._init["fps"]} fps not allowed by the camera, using {fps:.2f} '
                      f'(range {lo:.2g}-{hi:.2f})')
            fr.set(fps)
        except Exception as e:
            print(f'warning: could not set AcquisitionFrameRate: {e}')

        # a trigger mode left on (e.g. from VimbaXViewer or a saved user set) means no frames ever arrive
        for name, value in [('TriggerSelector', 'FrameStart'), ('TriggerMode', 'Off'),
                            ('AcquisitionMode', 'Continuous')]:
            try:
                feat(name).set(value)
            except Exception as e:
                print(f'warning: could not set {name}={value}: {e}')
        for name in ['DeviceLinkSpeed', 'DeviceLinkThroughputLimit', 'PayloadSize', 'PixelFormat',
                     'Width', 'Height', 'AcquisitionFrameRate', 'TriggerMode']:
            try:
                print(f'  {name} = {feat(name).get()}')
            except Exception:
                pass

        self.status_counts = {}   # FrameStatus name -> count, shown while waiting for frames
        self.handler_error = None

        def handler(cam, stream, frame):
            # always give the buffer back: if it is not re-queued the stream stalls silently
            try:
                status = frame.get_status()
                self.status_counts[status.name] = self.status_counts.get(status.name, 0) + 1
                if status == self.vmbpy.FrameStatus.Complete:
                    arr = frame.as_numpy_ndarray()
                    arr = arr[:, :, 0] if arr.ndim == 3 and arr.shape[2] == 1 else arr
                    with self._lock:
                        self._frame = arr.copy()
                        self._count += 1
            except Exception as e:
                if self.handler_error is None:
                    traceback.print_exc()
                self.handler_error = repr(e)
            finally:
                cam.queue_frame(frame)

        self.cam.start_streaming(handler=handler, buffer_count=self._init['buffers'])

    def stop(self):
        try:
            if self.cam.is_streaming():
                self.cam.stop_streaming()
        finally:
            self.cam.__exit__(None, None, None)
            self._vmb.__exit__(None, None, None)

    def latest(self):
        with self._lock:
            return self._frame, self._count

    def set_exposure_us(self, v):
        lo, hi = self.exposure_range_us
        self.cam.get_feature_by_name('ExposureTime').set(float(min(max(v, lo), hi)))

    def set_gain(self, v):
        lo, hi = self.gain_range
        self.cam.get_feature_by_name('Gain').set(float(min(max(v, lo), hi)))

    def get_exposure_us(self):
        return self.cam.get_feature_by_name('ExposureTime').get()

    def get_gain(self):
        return self.cam.get_feature_by_name('Gain').get()


class CsiBackend:
    """Alvium CSI-2 on the Pi 5. Reuses pyalvium_csi to configure the media pipeline,
    then streams with `v4l2-ctl --stream-to=-` and keeps only the newest frame."""

    def __init__(self, exposure_us, gain, pixel_format, fps):
        try:
            from pyalvium_csi.Camera import Camera
            from pyalvium_csi import parameters as csi_params
        except ImportError:
            sys.path.insert(0, os.path.join(REPO_ROOT, 'modules', 'Alvium-Camera-Module-CSI'))
            from pyalvium_csi.Camera import Camera
            from pyalvium_csi import parameters as csi_params
        self.params = csi_params
        self.pixel_format = pixel_format
        # pyalvium_csi writes metadata/log files into output_path: keep them out of the data dir
        self._tmp = tempfile.mkdtemp(prefix='alvium_preview_')
        self.cam = Camera(output_path=self._tmp, settings={
            'exposure': exposure_us, 'gain': gain, 'format': pixel_format, 'max_framerate': fps})
        self._lock = threading.Lock()
        self._frame = None
        self._count = 0
        self.proc = None
        self.gain_step = 1
        self.gain_unit = 'drv'

    @staticmethod
    def _range(info, scale=1.0):
        lo = re.search(r'min=(-?\d+)', info)
        hi = re.search(r'max=(-?\d+)', info)
        return int(lo.group(1)) * scale, int(hi.group(1)) * scale

    def start(self):
        self.cam.__enter__()
        f = self.cam.features
        ns = self.params.EXPOSURE_NS_PER_US
        self.exposure_range_us = self._range(f[self.params.EXPOSURE_CONTROL]['info'], 1.0 / ns)
        self.gain_range = self._range(f[self.params.GAIN_CONTROL]['info'])
        self._exposure_us = int(f[self.params.EXPOSURE_CONTROL]['value']) / ns
        self._gain = float(f[self.params.GAIN_CONTROL]['value'])

        cmd = ['v4l2-ctl', '-d', self.cam.video, '--stream-mmap', '--stream-to=-']
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        c = self.cam
        bpp = c.bytes_per_pixel
        while True:
            buf = self.proc.stdout.read(c.size_image)
            if len(buf) < c.size_image:
                break
            arr = np.frombuffer(buf, np.uint8)[:c.height * c.bytes_per_line]
            arr = arr.reshape(c.height, c.bytes_per_line)[:, :c.width * bpp]  # drop row padding
            if bpp == 3:
                arr = arr.reshape(c.height, c.width, 3)
            with self._lock:
                self._frame = arr
                self._count += 1

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        self.cam.__exit__(None, None, None)

    def latest(self):
        with self._lock:
            return self._frame, self._count

    def set_exposure_us(self, v):
        lo, hi = self.exposure_range_us
        v = min(max(v, lo), hi)
        self.cam.set_values({self.params.EXPOSURE_CONTROL: int(round(v * self.params.EXPOSURE_NS_PER_US))})
        self._exposure_us = v

    def set_gain(self, v):
        lo, hi = self.gain_range
        v = int(round(min(max(v, lo), hi)))
        self.cam.set_values({self.params.GAIN_CONTROL: v})
        self._gain = v

    def get_exposure_us(self):
        return self._exposure_us

    def get_gain(self):
        return self._gain


# --------------------------------------------------------------------------- #
# Preview GUI
# --------------------------------------------------------------------------- #

WIN = 'Alvium preview'
EXP_SLIDER_MAX = 1000
GAIN_SLIDER_MAX = 1000


class Preview:
    def __init__(self, cam, args):
        self.cam = cam
        self.args = args
        self.zoom = False
        self.roi_center = None          # full-resolution pixel coords, set on first frame
        self.roi_half = args.roi // 2
        self.peak = 0.0
        self.scale = 1.0                # display px / sensor px
        self.pending_exp = None         # trackbar values applied at most every 100 ms
        self.pending_gain = None
        self.last_apply = 0.0
        self.fps_t = time.time()
        self.fps_n = 0
        self.fps = 0.0
        self._sync = False              # True while the code itself moves the trackbars

    # ---- slider <-> value mapping (log scale for exposure) ----
    def _exp_hi(self):
        return min(self.cam.exposure_range_us[1], self.args.max_exposure)

    def exp_to_slider(self, us):
        lo, hi = max(self.cam.exposure_range_us[0], 1.0), self._exp_hi()
        return int(round((math.log(us) - math.log(lo)) / (math.log(hi) - math.log(lo)) * EXP_SLIDER_MAX))

    def slider_to_exp(self, s):
        lo, hi = max(self.cam.exposure_range_us[0], 1.0), self._exp_hi()
        return math.exp(math.log(lo) + s / EXP_SLIDER_MAX * (math.log(hi) - math.log(lo)))

    def gain_to_slider(self, g):
        lo, hi = self.cam.gain_range
        return int(round((g - lo) / (hi - lo) * GAIN_SLIDER_MAX)) if hi > lo else 0

    def slider_to_gain(self, s):
        lo, hi = self.cam.gain_range
        return lo + s / GAIN_SLIDER_MAX * (hi - lo)

    def _on_exp(self, s):
        if not self._sync:
            self.pending_exp = self.slider_to_exp(max(s, 0))

    def _on_gain(self, s):
        if not self._sync:
            self.pending_gain = self.slider_to_gain(s)

    def _sync_trackbars(self):
        self._sync = True
        cv2.setTrackbarPos('exposure (log)', WIN, self.exp_to_slider(self.cam.get_exposure_us()))
        cv2.setTrackbarPos('gain', WIN, self.gain_to_slider(self.cam.get_gain()))
        self._sync = False

    def _on_mouse(self, event, x, y, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN and not self.zoom:
            self.roi_center = (int(x / self.scale), int(y / self.scale))
            self.peak = 0.0

    def _apply_pending(self):
        if time.time() - self.last_apply < 0.1:
            return
        if self.pending_exp is not None:
            self.cam.set_exposure_us(self.pending_exp)
            self.pending_exp = None
        if self.pending_gain is not None:
            self.cam.set_gain(self.pending_gain)
            self.pending_gain = None
        self.last_apply = time.time()

    # ---- drawing ----
    @staticmethod
    def _histogram(gray, w=256, h=100):
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
        hist = np.log1p(hist)
        hist = (hist / hist.max() * (h - 1)).astype(int) if hist.max() > 0 else hist.astype(int)
        img = np.full((h, w, 3), 30, np.uint8)
        for x, v in enumerate(hist):
            cv2.line(img, (x, h - 1), (x, h - 1 - v), (0, 0, 255) if x >= 250 else (200, 200, 200))
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), (120, 120, 120))
        return img

    @staticmethod
    def _text(img, lines, origin=(10, 25)):
        x, y = origin
        for line in lines:
            cv2.putText(img, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(img, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)
            y += 24

    def settings_text(self):
        return (f'  exposure: {self.cam.get_exposure_us():.0f}    # us\n'
                f'  gain: {self.cam.get_gain():.1f}    # {self.cam.gain_unit}\n'
                f'  format: {self.cam.pixel_format}')

    def save(self, bgr):
        os.makedirs(self.args.save_dir, exist_ok=True)
        stem = os.path.join(self.args.save_dir, time.strftime('preview_%Y%m%d_%H%M%S'))
        cv2.imwrite(stem + '.png', bgr)
        with open(stem + '.txt', 'w') as f:
            f.write(self.settings_text() + f'\n  focus_peak: {self.peak:.1f}\n')
        print(f'saved {stem}.png / .txt')

    def run(self):
        cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
        cv2.createTrackbar('exposure (log)', WIN, 0, EXP_SLIDER_MAX, self._on_exp)
        cv2.createTrackbar('gain', WIN, 0, GAIN_SLIDER_MAX, self._on_gain)
        cv2.setMouseCallback(WIN, self._on_mouse)
        self._sync_trackbars()

        last_count = -1
        bgr = None
        while True:
            self._apply_pending()
            raw, count = self.cam.latest()
            if raw is not None and count != last_count:
                last_count = count
                self.fps_n += 1
                bgr = to_bgr(raw, self.cam.pixel_format)
                self.render(bgr)
            elif raw is None:
                waiting = np.zeros((200, 600, 3), np.uint8)
                lines = ['waiting for frames...']
                counts = getattr(self.cam, 'status_counts', None)
                if counts is not None:
                    lines.append(f'callbacks by status: {counts or "none yet"}')
                if getattr(self.cam, 'handler_error', None):
                    lines.append(f'handler error: {self.cam.handler_error}'[:70])
                self._text(waiting, lines)
                cv2.imshow(WIN, waiting)

            now = time.time()
            if now - self.fps_t >= 1.0:
                self.fps = self.fps_n / (now - self.fps_t)
                self.fps_t, self.fps_n = now, 0

            key = cv2.waitKey(15) & 0xFF
            if key in (ord('q'), 27) or cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
            elif key == ord('z'):
                self.zoom = not self.zoom
            elif key == ord('r'):
                self.peak = 0.0
            elif key in (ord('['), ord(']')):
                self.cam.set_exposure_us(self.cam.get_exposure_us() * (0.9 if key == ord('[') else 1.1))
                self._sync_trackbars()
            elif key in (ord('-'), ord('=')):
                step = self.cam.gain_step if key == ord('=') else -self.cam.gain_step
                self.cam.set_gain(self.cam.get_gain() + step)
                self._sync_trackbars()
            elif key == ord('s') and bgr is not None:
                self.save(bgr)
            elif key == ord('p'):
                print('camera:\n' + self.settings_text())

    def render(self, bgr):
        h, w = bgr.shape[:2]
        if self.roi_center is None:
            self.roi_center = (w // 2, h // 2)
        cx = min(max(self.roi_center[0], self.roi_half), w - self.roi_half)
        cy = min(max(self.roi_center[1], self.roi_half), h - self.roi_half)
        x0, y0, x1, y1 = cx - self.roi_half, cy - self.roi_half, cx + self.roi_half, cy + self.roi_half

        # statistics on full-resolution data
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        roi = gray[y0:y1, x0:x1]
        focus = cv2.Laplacian(roi, cv2.CV_64F).var()
        self.peak = max(self.peak, focus)
        sat_all = 100.0 * np.count_nonzero(gray >= 250) / gray.size
        sat_roi = 100.0 * np.count_nonzero(roi >= 250) / roi.size

        if self.zoom:
            view = bgr[y0:y1, x0:x1].copy()
            disp_scale = max(1, self.args.display_width // view.shape[1])
            view = cv2.resize(view, None, fx=disp_scale, fy=disp_scale, interpolation=cv2.INTER_NEAREST)
        else:
            self.scale = min(1.0, self.args.display_width / w)
            view = cv2.resize(bgr, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
            s = self.scale
            cv2.rectangle(view, (int(x0 * s), int(y0 * s)), (int(x1 * s), int(y1 * s)), (0, 255, 0), 2)

        # focus bar relative to peak
        ratio = focus / self.peak if self.peak > 0 else 0
        bar_w = int((view.shape[1] - 20) * ratio)
        cv2.rectangle(view, (10, view.shape[0] - 25), (10 + bar_w, view.shape[0] - 10),
                      (0, 255, 0) if ratio > 0.95 else (0, 165, 255), -1)

        self._text(view, [
            f'exp {self.cam.get_exposure_us():.0f} us   gain {self.cam.get_gain():.1f} {self.cam.gain_unit}   '
            f'{self.fps:.1f} fps   {w}x{h}',
            f'focus {focus:.1f}   peak {self.peak:.1f}   ({100 * ratio:.0f}%)',
            f'saturated: frame {sat_all:.2f}%   roi {sat_roi:.2f}%   max {int(gray.max())}',
            'z zoom  click ROI  r reset peak  [ ] exp  - = gain  s save  p print  q quit',
        ])

        hist = self._histogram(cv2.resize(gray, (w // 8, h // 8), interpolation=cv2.INTER_NEAREST))
        hh, hw = hist.shape[:2]
        if view.shape[0] > hh + 140 and view.shape[1] > hw + 20:
            view[view.shape[0] - hh - 35:view.shape[0] - 35, view.shape[1] - hw - 10:view.shape[1] - 10] = hist

        cv2.imshow(WIN, view)


def load_camera_config(path):
    import yaml
    with open(path) as f:
        cfg = yaml.safe_load(f).get('camera', {})
    return cfg


def main():
    p = argparse.ArgumentParser(description='Live preview of the Alvium camera to tune exposure, gain and focus.')
    p.add_argument('--backend', choices=['usb', 'csi'], help='usb (vmbpy) or csi (Pi 5 CSI-2). '
                   'Default: csi if the config camera name contains "CSI", else usb.')
    p.add_argument('--config', help='porter YAML config to take the initial exposure/gain/format/framerate from')
    p.add_argument('--camera-id', default=None, help='USB only: vmbpy camera id (default: first camera found)')
    p.add_argument('--exposure', type=float, help='initial exposure in us (default 10000)')
    p.add_argument('--gain', type=float, help='initial gain (dB on USB, driver units on CSI; default 0)')
    p.add_argument('--format', help='pixel format (default BayerRG8)')
    p.add_argument('--fps', type=float, help='frame rate (default 5)')
    p.add_argument('--max-exposure', type=float, default=1_000_000, help='upper end of the exposure slider in us')
    p.add_argument('--buffers', type=int, default=10, help='USB only: number of vmbpy frame buffers')
    p.add_argument('--roi', type=int, default=400, help='side of the focus ROI in sensor pixels')
    p.add_argument('--display-width', type=int, default=1280, help='width of the preview window in pixels')
    p.add_argument('--save-dir', default=os.path.expanduser('~/camera_preview'), help='where "s" saves frames')
    args = p.parse_args()

    cfg = load_camera_config(args.config) if args.config else {}
    exposure = args.exposure if args.exposure is not None else cfg.get('exposure', 10000)
    gain = args.gain if args.gain is not None else cfg.get('gain', 0.0)
    pixel_format = args.format or cfg.get('format', 'BayerRG8')
    fps = args.fps if args.fps is not None else cfg.get('max_framerate', 5)
    backend = args.backend or ('csi' if 'csi' in str(cfg.get('name', '')).lower() else 'usb')

    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        sys.exit('No display found: run with an HDMI monitor/VNC, or connect with `ssh -X`.')

    gui = re.search(r'^\s*GUI:\s*(\S+)', cv2.getBuildInformation(), re.M)
    if gui is None or gui.group(1).upper() == 'NONE':
        sys.exit(f'This OpenCV ({cv2.__file__}) has no GUI support (probably opencv-python-headless).\n'
                 f'Run with an interpreter whose cv2 has GTK/Qt, or in this environment:\n'
                 f'  {sys.executable} -m pip uninstall opencv-python-headless && '
                 f'{sys.executable} -m pip install opencv-python')

    if backend == 'usb':
        cam = UsbBackend(args.camera_id, exposure, gain, pixel_format, fps, args.buffers)
    else:
        cam = CsiBackend(exposure, gain, pixel_format, fps)

    print(f'backend={backend} exposure={exposure}us gain={gain} format={pixel_format} fps={fps}')
    cam.start()
    print(f'exposure range: {cam.exposure_range_us[0]:.0f}-{cam.exposure_range_us[1]:.0f} us, '
          f'gain range: {cam.gain_range[0]}-{cam.gain_range[1]} {cam.gain_unit}')
    try:
        Preview(cam, args).run()
    finally:
        cam.stop()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
        print('final settings:\ncamera:')
        print(f'  exposure: {cam.get_exposure_us():.0f}\n  gain: {cam.get_gain():.1f}')


if __name__ == '__main__':
    main()
