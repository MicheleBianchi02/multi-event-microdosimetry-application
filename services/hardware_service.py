import logging
import threading
import time
import numpy as np

from core.constants import DEFAULT_ADC_BITS, overflow_sentinel
from models.data_model import DataModel
from models.session_model import SessionModel
from hardware.core.hardware_manager import HardwareManager

logger = logging.getLogger(__name__)

class HardwareService:
    """
    Manages the hardware device and simulation logic.
    Delegates the actual serial communication and background threads
    to the specific HardwareManager's acquisition capability.
    """

    def __init__(
        self, hardware_manager: HardwareManager, data_model: DataModel, session_model: SessionModel
    ):
        self.hardware_manager = hardware_manager
        self.data_model = data_model
        self.session = session_model

        self._stop_event = threading.Event()
        self._stop_event.set()
        self._error = False
        self._thread = None
        self._ramp_sink = None

    def start_receiving(self, simulate: bool = False, rate: int = 5, ramp_sink=None) -> bool:
        self._stop_event.clear()
        self._error = False
        self._ramp_sink = ramp_sink

        if not simulate:
            if not self.hardware_manager.acquisition:
                logger.error("No acquisition hardware assigned")
                self._error = True
                return False

            # ORDER MATTERS: arm the acquisition FIRST, start the timing
            # engine SECOND.  The NI task is clocked by the timing device's
            # CONV_START edges and ignores them while not armed — if the
            # edges began first, the first converted sample would land at
            # an arbitrary phase inside a window and every start/stop pair
            # would be misaligned.  Armed first, the first edge after the
            # timing 'S' is the first sample of window 0 (the sink's
            # alignment contract).
            success = self.hardware_manager.acquisition.start_measurement(
                self.session, self.data_model, ramp_sink=ramp_sink
            )
            if not success:
                self._error = True
                return False

            # A timing device that does not hold the acquisition role (e.g.
            # virtual acquisition on the bench, or a µC timing while the NI
            # acquires) starts its engine here — after the consumer is armed.
            if not self.hardware_manager.start_timing_signal():
                logger.error("Starting the timing engine failed")
                # stop the armed acquisition again: nothing was timed
                try:
                    self.hardware_manager.acquisition.stop_measurement()
                except Exception:
                    logger.exception("Stopping the acquisition after a timing start failure failed")
                self._error = True
                return False
        else:
            if self._thread is not None and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=2.0)
            target = self._simulate_ramp if ramp_sink is not None else self._simulate_data
            self._thread = threading.Thread(
                target=target, args=(rate,), daemon=True
            )
            self._thread.start()

        return True

    def stop_receiving(self, send_stop: bool = True) -> bool:
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        if send_stop:
            ok = True
            # Mirror of the B24 start order (acquisition armed first, then
            # the timing 'S'): stop the timing engine FIRST — the Pico
            # finishes its current cycle and the edge train ends — THEN
            # stop the acquisition.  Its reader then drains a STATIC
            # buffer and task.stop() discards nothing; stopping the
            # acquisition first would keep the edges flowing during the
            # drain (which never empties, expires the join and strands
            # the buffered tail).
            if not self.hardware_manager.stop_timing_signal():
                ok = False
            if self.hardware_manager.acquisition:
                if not self.hardware_manager.acquisition.stop_measurement():
                    ok = False
            else:
                ok = False
            return ok
        return True

    def has_error(self) -> bool:
        return self._error

    def clear_error(self):
        self._error = False

    def _simulate_data(self, rate: int):
        bits = getattr(self.session, "adc_bits", DEFAULT_ADC_BITS)
        missing = overflow_sentinel(bits)
        while not self._stop_event.is_set():
            start = time.perf_counter()
            start_value, stop_value, diff_value = self._generate_random_val(bits)

            if not self.session.debug_mode:
                if self.session.multiple_channel:
                    self.data_model.append_data((diff_value[0], diff_value[1]))
                else:
                    self.data_model.append_data((diff_value[0], missing))
            else:
                if self.session.multiple_channel:
                    self.data_model.append_data(
                        (
                            (start_value[0], stop_value[0]),
                            (start_value[1], stop_value[1]),
                        )
                    )
                else:
                    self.data_model.append_data(
                        (
                            (start_value[0], stop_value[0]),
                            (missing, missing),
                        )
                    )

            tm_sleep = 10**-3 / rate
            elapsed = time.perf_counter() - start
            if tm_sleep - elapsed > 0:
                time.sleep(tm_sleep - elapsed)

    def _generate_random_val(self, bits: int = DEFAULT_ADC_BITS):
        # Synthetic ramps scaled to the active ADC's full scale, with the
        # same shape as the classic 12-bit simulator.
        full_scale = 2**bits - 1
        start_value_ch1 = int(np.random.normal(loc=full_scale * 0.244, scale=full_scale * 0.0122))
        stop_value_ch1 = int(np.random.normal(loc=full_scale * 0.903, scale=full_scale * 0.0366))
        start_value_ch2 = int(np.random.normal(loc=full_scale * 0.073, scale=full_scale * 0.0122))
        stop_value_ch2 = int(np.random.normal(loc=full_scale * 0.854, scale=full_scale * 0.0366))

        start_value = np.array([start_value_ch1, start_value_ch2])
        stop_value = np.array([stop_value_ch1, stop_value_ch2])
        diff_val = stop_value - start_value

        sentinel = overflow_sentinel(bits)
        if stop_value_ch1 < start_value_ch1 or stop_value_ch1 >= full_scale:
            stop_value[0] = sentinel
            diff_val[0] = sentinel

        if stop_value_ch2 < start_value_ch2 or stop_value_ch2 >= full_scale:
            stop_value[1] = sentinel
            diff_val[1] = sentinel

        return start_value, stop_value, diff_val

    def _simulate_ramp(self, rate: int):
        """Synthetic ramp-sampling source: feeds the ramp sink with full
        integration windows of uint32 samples (linear ramp + noise, scaled
        to the active ADC).  The window extraction — and through it the
        classic start/stop pipeline — is the same as for real hardware.

        Wall-clock speed is the simulated measurement rate (one window per
        period), not the ADC's sample rate: the sample COUNTS per window are
        exact, the wall time is compressed."""
        import numpy as np

        sink = self._ramp_sink
        session = self.session
        bits = getattr(session, "adc_bits", DEFAULT_ADC_BITS)
        full_scale = 2**bits - 1
        dt_s = getattr(session, "ramp_dt_s", 0.0) or (
            session.int_time if session.timing_mode == "markers" else 1e-5
        )
        n_per_window = int(round(session.int_time / dt_s)) + 1 if dt_s > 0 else 2
        period_s = session.period_s or (session.int_time + session.reset_time)
        channels = [0] + ([1] if session.multiple_channel else [])
        rng = np.random.default_rng()

        while not self._stop_event.is_set():
            start = time.perf_counter()
            for ch in channels:
                # ramp from ~25% to ~90% of full scale, with noise
                base = np.linspace(
                    full_scale * (0.25 + 0.02 * ch),
                    full_scale * (0.90 - 0.05 * ch),
                    n_per_window,
                )
                samples = np.clip(
                    base + rng.normal(0, full_scale * 0.002, n_per_window),
                    0,
                    full_scale,
                ).astype(np.uint32)
                # one block per window keeps indices trivially aligned
                sink.write_block(ch, samples, abs_index=None)
            # one window per simulated period (same wall-clock pacing as
            # _simulate_data: rate = 1e-3 / period)
            tm_sleep = (10**-3 / rate) if rate else period_s
            elapsed = time.perf_counter() - start
            if tm_sleep - elapsed > 0:
                time.sleep(tm_sleep - elapsed)

class SimulatedDevice:
    """Placeholder to check if device is simulated when sending stop command."""
    pass
