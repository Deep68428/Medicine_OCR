#!/usr/bin/env python3
"""
trigger_capture.py
Reusable hardware-triggered camera capture class using mvIMPACT Acquire.
Import this into any script and pass a callback to handle each captured frame.
"""

import time
import threading
import ctypes
from loguru import logger
from typing import Callable

try:
    from mvIMPACT import acquire as mvIA

    logger.info("mvIMPACT is installed")

except ImportError:
    logger.error("mvIMPACT is not installed")

    raise ImportError(
        "mvIMPACT Acquire Python bindings not found. "
        "On warehouse machines install the ImpactAcquire package. "
        "In dev/test mode the hardware camera is disabled and POST /trigger is used instead."
    )

try:
    import cv2
    import numpy as np

    USE_DISPLAY = True
except ImportError:
    USE_DISPLAY = False


class TriggerCapture:
    """
    Hardware-triggered continuous camera capture using mvIMPACT Acquire.

    Usage:
        def my_callback(frame: np.ndarray, frame_index: int, timestamp_ms: int):
            # do whatever you want with the frame
            cv2.imwrite(f"frame_{frame_index}.jpg", frame)

        cam = TriggerCapture(on_frame=my_callback)
        cam.start()          # blocking — runs until Ctrl+C or cam.stop()
        # OR
        cam.start_async()    # non-blocking — runs in background thread
        ...
        cam.stop()
    """

    def __init__(
        self,
        on_frame: Callable[[np.ndarray, int, int], None],
        camera_serial: str | None = None,
        trigger_source: str = "Line4",
        trigger_activation: str = "RisingEdge",
        trigger_selector: str = "FrameStart",
        prefill: int = 4,
        timeout_ms: int = -1,
        exposure_time_us: float = 40000.0,
    ):
        """
        Parameters
        ----------
        on_frame        : Callback called for every captured frame.
                          Signature: on_frame(frame: np.ndarray, frame_index: int, timestamp_ms: int)
        camera_serial   : Camera serial number to select. If None, auto-selects when only
                          one camera is present, otherwise prompts the user.
        trigger_source  : GenICam TriggerSource value  (default: "Line4")
        trigger_activation : GenICam TriggerActivation (default: "RisingEdge")
        trigger_selector: GenICam TriggerSelector      (default: "FrameStart")
        prefill         : Number of requests to pre-queue in driver pipeline.
        timeout_ms      : Wait timeout per frame in ms. -1 = wait forever.
        exposure_time_us: Camera exposure time in microseconds (default: 300000 = 300 ms).
        """
        self.on_frame = on_frame
        self.camera_serial = camera_serial.strip() if camera_serial else None
        self.trigger_source = trigger_source
        self.trigger_activation = trigger_activation
        self.trigger_selector = trigger_selector
        self.prefill = prefill
        self.timeout_ms = timeout_ms
        self.exposure_time_us = exposure_time_us

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Stats (read after stop)
        self.frame_index = 0
        self.total_errors = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        """Blocking. Runs capture loop in the calling thread. Returns on stop() or Ctrl+C."""
        self._run()

    def start_async(self):
        """Non-blocking. Runs capture loop in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the capture loop to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _get_device(self, dev_mgr: mvIA.DeviceManager) -> mvIA.Device | None:
        device_count = dev_mgr.deviceCount()
        if device_count == 0:
            logger.info("No devices found!")
            return None

        logger.info(f"\nFound {device_count} device(s):")
        for i in range(device_count):
            dev = dev_mgr.getDevice(i)
            logger.info(
                f"  [{i}] {dev.serial.read()} - {dev.product.read()} ({dev.family.read()})"
            )

        if self.camera_serial is not None:
            for i in range(device_count):
                dev = dev_mgr.getDevice(i)
                if dev.serial.read() == self.camera_serial:
                    logger.info(
                        f"Selected device by serial {self.camera_serial!r} at index {i}."
                    )
                    return dev
            logger.info(f"No device found with serial={self.camera_serial!r}.")
            return None

        if device_count == 1:
            logger.info("Auto-selecting the only available device.")
            return dev_mgr.getDevice(0)

        logger.info(
            "Multiple devices found and no camera_serial configured — cannot auto-select."
        )
        return None

    def _configure_trigger(self, pDev: mvIA.Device):
        logger.info("⚙  Configuring trigger via GenICam node map...")
        try:
            node_map = mvIA.GenICam(pDev)
            node_map.triggerSelector.writeS(self.trigger_selector)
            node_map.triggerMode.writeS("On")
            node_map.triggerSource.writeS(self.trigger_source)
            node_map.triggerActivation.writeS(self.trigger_activation)
            node_map.exposureTime.writeD(self.exposure_time_us)

            logger.info("✅ Trigger configured:")
            logger.info(f"   TriggerSelector   = {node_map.triggerSelector.readS()}")
            logger.info(f"   TriggerMode       = {node_map.triggerMode.readS()}")
            logger.info(f"   TriggerSource     = {node_map.triggerSource.readS()}")
            logger.info(f"   TriggerActivation = {node_map.triggerActivation.readS()}")
            logger.info(f"   ExposureTime (µs) = {node_map.exposureTime.read()}")

        except Exception as e:
            print(
                f"⚠  GenICam config failed: {e}. Trying CameraSettingsBlueCOUGAR fallback..."
            )
            try:
                cs = mvIA.CameraSettingsBlueCOUGAR(pDev)
                cs.triggerMode.write(mvIA.ctmOnRisingEdge)
                cs.triggerSource.write(mvIA.ctsDigIn0)
                cs.expose_us.write(int(self.exposure_time_us))
                print("✅ Trigger configured via CameraSettingsBlueCOUGAR fallback.")
            except Exception as e2:
                print(f"❌ Fallback also failed: {e2}")
                raise

    def _start_acquisition(self, pDev, fi):
        if pDev.acquisitionStartStopBehaviour.read() == mvIA.assbUser:
            result = fi.acquisitionStart()
            if result != mvIA.DMR_NO_ERROR:
                logger.warning(f"WARNING: acquisitionStart() failed with code {result}")

    def _stop_acquisition(self, pDev, fi):
        if pDev.acquisitionStartStopBehaviour.read() == mvIA.assbUser:
            result = fi.acquisitionStop()
            if result != mvIA.DMR_NO_ERROR:
                logger.warning(f"WARNING: acquisitionStop() failed with code {result}")

    def _buffer_to_numpy(self, image_buffer) -> np.ndarray | None:
        if not USE_DISPLAY:
            logger.info("(NumPy/OpenCV not available — cannot convert buffer)")
            return None

        width = image_buffer.iWidth
        height = image_buffer.iHeight
        total_bytes = image_buffer.iSize
        bytes_per_px = total_bytes / (width * height)

        # Try to read the pixel format from the buffer (name varies by SDK:
        # iPixelType, iFormat, ePixelFormat, etc. — adjust to your SDK).
        pixel_format = getattr(image_buffer, "iPixelType", None)
        if pixel_format is None:
            pixel_format = getattr(image_buffer, "iFormat", None)
        if pixel_format is None:
            pixel_format = getattr(image_buffer, "ePixelFormat", None)

        logger.debug(
            f"[_buffer_to_numpy] {width}x{height}, total_bytes={total_bytes}, "
            f"bytes_per_px={bytes_per_px:.3f}, pixel_format={pixel_format}"
        )

        ptr = ctypes.cast(int(image_buffer.vpData), ctypes.POINTER(ctypes.c_ubyte))
        arr = np.ctypeslib.as_array(ptr, shape=(total_bytes,)).copy()

        # ---- Bayer 8-bit formats (1 byte per pixel, needs demosaicing) ----
        # Check this BEFORE plain Mono8, because Bayer is also ~1.0 bytes/px.
        bayer_codes = {
            # Common string-style format names
            "BayerRG8": cv2.COLOR_BayerRG2BGR,
            "BayerGR8": cv2.COLOR_BayerGR2BGR,
            "BayerGB8": cv2.COLOR_BayerGB2BGR,
            "BayerBG8": cv2.COLOR_BayerBG2BGR,
            # Add numeric enum values from your SDK here, e.g.:
            # 17301513: cv2.COLOR_BayerRG2BGR,   # PFNC BayerRG8
        }

        if pixel_format in bayer_codes and abs(bytes_per_px - 1.0) < 0.01:
            logger.debug(
                f"  -> branch: Bayer 8-bit ({pixel_format}) — demosaicing to BGR"
            )
            raw = arr.reshape((height, width))
            return cv2.cvtColor(raw, bayer_codes[pixel_format])

        # 3-channel (BGR) image
        if abs(bytes_per_px - 3.0) < 0.01:
            logger.debug("  -> branch: 3-channel BGR (3.0 bytes/px)")
            return arr.reshape((height, width, 3))

        # ---- 4-channel RGBx888 (common in some cameras) ----
        if abs(bytes_per_px - 4.0) < 0.01:
            logger.debug("  -> branch: 4-channel RGBx888 / BGRx888 (4.0 bytes/px)")
            arr_reshaped = arr.reshape((height, width, 4))

            # Extract first three channels
            rgb_or_bgr = arr_reshaped[:, :, :3]

            # OpenCV expects BGR order, so convert RGB -> BGR
            bgr = rgb_or_bgr[..., ::-1]  # reverse last axis (R<->B)

            # Convert to mono8 then back to 3-channel so downstream code stays uniform
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

        # 1-channel / mono image (Mono8)
        elif abs(bytes_per_px - 1.0) < 0.01:
            logger.debug("  -> branch: Mono8 (1.0 bytes/px)")
            return arr.reshape((height, width))

        # Packed 10-bit mono — 4 px in 5 bytes
        elif abs(bytes_per_px - 1.25) < 0.01:
            logger.debug("  -> branch: Packed 10-bit mono (1.25 bytes/px)")
            arr16 = arr.view(np.uint16)
            arr16 = (arr16 & 0x03FF)[: height * width]
            return arr16.reshape((height, width))

        # Packed 12-bit mono — 2 px in 3 bytes
        elif abs(bytes_per_px - 1.5) < 0.01:
            logger.debug("  -> branch: Packed 12-bit mono (1.5 bytes/px)")
            arr16 = arr.view(np.uint16)
            arr16 = (arr16 & 0x0FFF)[: height * width]
            return arr16.reshape((height, width))

        # 2 bytes per pixel — Mono10/Mono12/Mono16 unpacked
        elif abs(bytes_per_px - 2.0) < 0.01:
            logger.debug("  -> branch: Mono10/12/16 unpacked (2.0 bytes/px)")
            arr16 = arr.view(np.uint16)[: height * width]
            return arr16.reshape((height, width))

        else:
            logger.warning(
                f"  -> branch: FALLBACK — unhandled format: {total_bytes} bytes for "
                f"{width}x{height} ({bytes_per_px:.3f} bytes/px), pixel_format={pixel_format} "
                f"— returning raw Mono8 best-effort"
            )
            return arr[: height * width].reshape((height, width))

    def _run(self):
        dev_mgr = mvIA.DeviceManager()

        pDev = self._get_device(dev_mgr)
        if pDev is None:
            print("❌ No device available. Exiting.")
            return

        try:
            pDev.open()
        except mvIA.ImpactAcquireException as e:
            print(f"❌ Failed to open device: {e.getErrorCode()}")
            return

        try:
            self._configure_trigger(pDev)
        except Exception as e:
            print(f"❌ Trigger configuration failed: {e}")
            return

        fi = mvIA.FunctionInterface(pDev)

        print(f"\n🔄 Pre-queuing {self.prefill} requests...")
        for _ in range(self.prefill):
            fi.imageRequestSingle()

        self._start_acquisition(pDev, fi)

        print(
            f"\n📷 Waiting for hardware triggers on {self.trigger_source} ({self.trigger_activation})..."
        )
        print("   Call stop() or press Ctrl+C to end.\n")

        self.frame_index = 0
        self.total_errors = 0

        # When timeout_ms is -1 (wait forever), poll in short intervals so
        # _stop_event is checked and stop() / Ctrl+C can exit cleanly.
        _poll_ms = 500 if self.timeout_ms == -1 else self.timeout_ms

        try:
            while not self._stop_event.is_set():
                request_nr = fi.imageRequestWaitFor(_poll_ms)

                if not fi.isRequestNrValid(request_nr):
                    # No frame yet (poll timeout) — re-queue and check stop flag.
                    fi.imageRequestSingle()
                    continue

                pRequest = fi.getRequest(request_nr)

                if pRequest.isOK:
                    if pRequest.payloadType.read() != mvIA.pt2DImage:
                        print(
                            f"  ⚠  [{self.frame_index:05d}] Unsupported payload, skipping."
                        )
                    else:
                        image_buffer = pRequest.getImageBufferDesc().getBuffer()
                        if image_buffer:
                            frame = self._buffer_to_numpy(image_buffer)
                            if frame is not None:
                                timestamp_ms = int(time.time() * 1000)
                                try:
                                    self.on_frame(frame, self.frame_index, timestamp_ms)
                                except Exception as cb_err:
                                    print(
                                        f"  ⚠  Callback error on frame {self.frame_index}: {cb_err}"
                                    )
                            pixel_format = pRequest.imagePixelFormat.readS()
                            w = image_buffer.iWidth
                            h = image_buffer.iHeight
                            print(
                                f"  ✅ [{self.frame_index:05d}] {pixel_format} {w}x{h}  ts={timestamp_ms}"
                            )
                        else:
                            print(f"  ❌ [{self.frame_index:05d}] Empty buffer.")
                            self.total_errors += 1
                else:
                    print(
                        f"  ❌ [{self.frame_index:05d}] {pRequest.requestResult.readS()}"
                    )
                    self.total_errors += 1

                fi.imageRequestUnlock(request_nr)
                fi.imageRequestSingle()
                self.frame_index += 1

        except KeyboardInterrupt:
            print("\n🛑 Stopped by user.")

        finally:
            self._stop_acquisition(pDev, fi)
            print("\n📊 Session summary:")
            print(f"   Triggers received : {self.frame_index}")
            print(f"   Errors            : {self.total_errors}")
