import os
import signal
from typing import Type

import cv2
import numpy as np
import pyfakewebcam
import requests
from multiprocessing import Queue

from .types import QueueDict

FHD = (1080, 1920)
HD = (720, 1280)
NTSC = (480, 720)


def get_mask(frame, bodypix_url='http://localhost:13165'):
    _, data = cv2.imencode(".jpg", frame)
    r = requests.post(
        url=bodypix_url,
        data=data.tobytes(),
        headers={"Content-Type": "application/octet-stream"})
    mask = np.frombuffer(r.content, dtype=np.uint8)
    mask = mask.reshape((frame.shape[0], frame.shape[1]))
    return mask


def post_process_mask(mask):
    mask = cv2.dilate(mask, np.ones((10, 10), np.uint8), iterations=1)
    mask = cv2.blur(cv2.UMat(mask.get().astype(np.float32)), (30, 30))
    return mask


def shift_image(img, dx, dy):
    img = np.roll(img, dy, axis=0)
    img = np.roll(img, dx, axis=1)
    if dy > 0:
        img[:dy, :] = 0
    elif dy < 0:
        img[dy:, :] = 0
    if dx > 0:
        img[:, :dx] = 0
    elif dx < 0:
        img[:, dx:] = 0
    return img


def hologram_effect(img):
    # add a blue tint
    holo = cv2.applyColorMap(img.get(), cv2.COLORMAP_WINTER)
    # add a halftone effect
    band_length, band_gap = 2, 3
    for y in range(holo.shape[0]):
        if y % (band_length + band_gap) < band_length:
            holo[y, :, :] = holo[y, :, :] * np.random.uniform(0.1, 0.3)
    # add some ghosting
    holo_blur = cv2.addWeighted(holo, 0.2, shift_image(holo.copy(), 5, 5), 0.8, 0)
    holo_blur = cv2.addWeighted(holo_blur, 0.4, shift_image(holo.copy(), -5, -5), 0.6, 0)
    # combine with the original color, oversaturated
    out = cv2.addWeighted(img, 0.5, holo_blur, 0.6, 0)
    return out


def get_frame(cap: object, background: object = None, use_hologram: bool = False) -> object:
    _ret_, frame = cap.read()
    # fetch the mask with retries (the app needs to warmup and we're lazy)
    # e v e n t u a l l y c o n s i s t e n t
    mask = None
    while mask is None:
        try:
            mask = get_mask(frame)
        except:
            pass

    # post-process mask and frame
    mask = cv2.UMat(mask)
    mask = post_process_mask(mask)

    frame = cv2.UMat(frame)
    if background is None:
        background = cv2.GaussianBlur(frame, (221, 221), sigmaX=20, sigmaY=20)

    if use_hologram:
        frame = hologram_effect(frame)

    # composite the foreground and background
    frame = frame.get().astype(np.uint8)
    mask = mask.get().astype(np.float32)
    background = background.get().astype(np.uint8)
    inv_mask = 1 - mask
    for c in range(frame.shape[2]):
        frame[:, :, c] = frame[:, :, c] * mask + background[:, :, c] * inv_mask
    return frame


def start(queue: "Queue[QueueDict]" = None, camera: str = "/dev/video0", background: str = None,
          use_hologram: bool = False, use_mirror: bool = False, resolution: str = None):
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # setup access to the *real* webcam
    cap = cv2.VideoCapture(camera, cv2.CAP_V4L2)

    orig_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if resolution is not None:
        # resolution is supplied by user in width followed by height order
        width, height = resolution[0], resolution[1]
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    else:
        width, height = orig_width, orig_height

    # for (height, width) in [FHD, HD, NTSC, (orig_height, orig_width)]:
    #     # Attempt to set the camera resolution via brute force
    #     cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    #     cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    #     if cap.get(cv2.CAP_PROP_FRAME_HEIGHT) == height:
    #         break

    # setup the fake camera
    fake = pyfakewebcam.FakeWebcam("/dev/video20", width, height)

    # load the virtual background
    background_scaled = None
    if background is not None:
        background_data = cv2.UMat(cv2.imread(background))
        background_scaled = cv2.resize(background_data, (width, height))

    # frames forever
    while True:
        frame = get_frame(cap, background=background_scaled, use_hologram=use_hologram)
        if use_mirror is True:
            frame = cv2.flip(frame, 1)
        # fake webcam expects RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        fake.schedule_frame(frame)
        if queue is not None and not queue.empty():
            data = queue.get(False)

            if data["background"] is None:
                background_scaled = None
            else:
                background = data["background"]
                background_data = cv2.UMat(cv2.imread(background))
                background_scaled = cv2.resize(background_data, (width, height))

            use_hologram = data["hologram"]
            use_mirror = data["mirror"]

def start_bodypix():
    appjsdir = None
    if os.environ["SNAP"]:
        appjsdir = os.environ["SNAP"]
    else:
        project = os.path.dirname(os.path.dirname(__file__))
        appjsdir = os.path.join(project, "bodypix")
    os.execlp("node", "node", os.path.join(os.environ["SNAP"], "app.js"))
