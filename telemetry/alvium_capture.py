import argparse
import os
import numpy as np
import cv2
from vmbpy import PixelFormat

try:
    import pyalvium
except ModuleNotFoundError:
    print("No pyalvium module found.")
    pass
except Exception as e:
    print(f"Error importing pyalvium: {e}")
    pass

parser = argparse.ArgumentParser(description="Alvium Camera Capture")
parser.add_argument("--exposure", type=int, default=10000, help="Exposure time in microseconds (default: 10000)")
parser.add_argument("--gain", type=float, default=30.0, help="Gain value (default: 30.0)")

camera_size = (3008, 4128)  # default size, will be updated after camera is opened
resizing_factor = 15  # factor to resize the image for display

def main():
    args = parser.parse_args()

    settings = {
        "exposure": args.exposure,
        "gain": args.gain
    }

    current_directory = os.getcwd()
    
    with pyalvium.Camera(output_path=current_directory, settings=settings) as camera:
        frame = camera.get_frame()
    
    frame = frame.convert_pixel_format(PixelFormat.Bgr8)
    img = frame.as_opencv_image()

    # resize the image
    resized_img = cv2.resize(img, (camera_size[1] // resizing_factor, camera_size[0] // resizing_factor))
    cv2.imwrite("captured_frame.jpg", resized_img)


if __name__ == "__main__":
    main()