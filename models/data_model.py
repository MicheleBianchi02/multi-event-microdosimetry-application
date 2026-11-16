import threading
from typing import List

import numpy as np

from core.constants import DEFAULT_ADC_BITS, overflow_sentinel
from core.data_utils import convert_data, convert_data_debug, filter_data_by_rois

BUFFER_SIZE = 2**17
BUFFER_SIZE_DEBUG = 2**17

# Sanity bound on the dynamic ADC depth (B6): the histogram arrays are
# 2^bits per channel, so bits beyond this would waste memory on bins no
# detector resolution justifies.  (Ramp-monitoring samples never enter the
# DataModel, so 32-bit ramp files are unaffected.)
MAX_ADC_BITS = 20


class DataModel:
    """
    Holds the real-time measurement data.
    Provides thread-safe methods for appending data (from the background hardware thread)
    and retrieving data batches (from the UI thread/Controller).
    Also stores the large numpy arrays and statistical accumulators for the current measurement.
    """

    def __init__(self):
        self._data_buffer: List[tuple] = []
        self._lock = threading.Lock()

        # ADC context (B6): bits/sentinel/bins are dynamic, set from the
        # loaded hardware config at measurement start and from the archive
        # settings at load time; 12 bit is the legacy/simulation fallback.
        self._adc_bits = DEFAULT_ADC_BITS
        self._num_bins = 2**DEFAULT_ADC_BITS

        self.init_variables()

    @property
    def adc_bits(self) -> int:
        return self._adc_bits

    @property
    def num_bins(self) -> int:
        return self._num_bins

    @property
    def overflow_sentinel(self) -> int:
        return overflow_sentinel(self._adc_bits)

    def configure_adc(self, bits: int) -> None:
        """Set the ADC resolution of the upcoming measurement / loaded
        archive: drives the histogram bins and the overflow sentinel.
        Resets the histogram buffers (statistics arrays are untouched)."""
        bits = int(bits)
        if not 1 <= bits <= MAX_ADC_BITS:
            raise ValueError(f"unsupported ADC resolution: {bits} bits")
        self._adc_bits = bits
        self._num_bins = 2**bits
        with self._lock:
            self.bar_data_ch1 = np.zeros(self._num_bins, dtype=int)
            self.bar_data_ch2 = np.zeros(self._num_bins, dtype=int)

    def init_variables(self):
        with self._lock:
            self._data_buffer = []

        self.bar_data_ch1 = np.zeros(self._num_bins, dtype=int)
        self.bar_data_ch2 = np.zeros(self._num_bins, dtype=int)
        self.data = np.zeros((BUFFER_SIZE, 2), dtype=float)
        self.filtered_data = np.full((BUFFER_SIZE, 2), np.nan, dtype=float)
        self.start_data = np.zeros((BUFFER_SIZE_DEBUG, 2), dtype=float)
        self.stop_data = np.zeros((BUFFER_SIZE_DEBUG, 2), dtype=float)

        self.ptr = 0

        # last applied FFT-analysis dialog parameters (excluded_bands,
        # threshold, method, window, nperseg); empty until the user runs the
        # analysis
        self.fft_analysis_params = {}

        self.result = {
            "var": {
                "N": [0, 0, 0, 0],
                "Mean": [0, 0, 0, 0],
                "Variance": [0, 0, 0, 0],
                "y_bar_d": [0, 0, 0, 0],
            },
            "var_cov": {
                "N": [0, 0, 0, 0],
                "Mean": [0, 0, 0, 0],
                "Variance": [0, 0, 0, 0],
                "Covariance": [0, 0, 0, 0],
                "y_bar_d": [0, 0, 0, 0],
                "cov_mean_sq": [0, 0, 0, 0],
                "cov_var_ratio": [0, 0, 0, 0],
            },
            "var_autocov": {
                "N": [0, 0, 0, 0],
                "Mean": [0, 0, 0, 0],
                "Variance": [0, 0, 0, 0],
                "AutoCorrelation": [0, 0, 0, 0],
                "y_bar_d": [0, 0, 0, 0],
                "autocov_mean_sq": [0, 0, 0, 0],
                "autocov_var_ratio": [0, 0, 0, 0],
            },
            "var_doublecov": {
                "N": [0, 0, 0, 0],
                "Mean": [0, 0, 0, 0],
                "Variance": [0, 0, 0, 0],
                "Covariance": [0, 0, 0, 0],
                "y_bar_d": [0, 0, 0, 0],
                "cov_mean_sq": [0, 0, 0, 0],
                "cov_var_ratio": [0, 0, 0, 0],
            },
            "var_fft": {
                "N": [0, 0, 0, 0],
                "M": [0, 0, 0, 0],
                "Mean": [0, 0, 0, 0],
                "Variance": [0, 0, 0, 0],
                "Covariance": [0, 0, 0, 0],
                "y_bar_d": [0, 0, 0, 0],
                "cov_var_ratio": [0, 0, 0, 0],
            },
        }

        self.mean_value_ch1 = 0.0
        self.mean_value_ch2 = 0.0
        self.variance_ch1 = 0.0
        self.variance_ch2 = 0.0
        self.n_overflow_ch1 = 0
        self.n_overflow_ch2 = 0
        self.count = 0
        self.count_ch1 = 0
        self.count_ch2 = 0

        self.count_cov = 0
        self.mean_x_cov = 0.0
        self.mean_y_cov = 0.0
        self.covariance = 0.0

        self.count_succ_ch1 = 0
        self.mean_L_ch1 = 0.0
        self.mean_R_ch1 = 0.0
        self.cov_succ_ch1 = 0.0
        self.leftover_ch1 = None

        self.count_succ_ch2 = 0
        self.mean_L_ch2 = 0.0
        self.mean_R_ch2 = 0.0
        self.cov_succ_ch2 = 0.0
        self.leftover_ch2 = None

    def reset_accumulators(self):
        with self._lock:
            self.bar_data_ch1 = np.zeros(self._num_bins, dtype=int)
            self.bar_data_ch2 = np.zeros(self._num_bins, dtype=int)
            self.mean_value_ch1 = 0.0
            self.mean_value_ch2 = 0.0
            self.variance_ch1 = 0.0
            self.variance_ch2 = 0.0
            self.n_overflow_ch1 = 0
            self.n_overflow_ch2 = 0
            self.count = 0
            self.count_ch1 = 0
            self.count_ch2 = 0

            # a new measurement starts without FFT-analysis input
            self.fft_analysis_params = {}

            self.result = {
                "var": {
                    "N": [0, 0, 0, 0],
                    "Mean": [0, 0, 0, 0],
                    "Variance": [0, 0, 0, 0],
                    "y_bar_d": [0, 0, 0, 0],
                },
                "var_cov": {
                    "N": [0, 0, 0, 0],
                    "Mean": [0, 0, 0, 0],
                    "Variance": [0, 0, 0, 0],
                    "Covariance": [0, 0, 0, 0],
                    "y_bar_d": [0, 0, 0, 0],
                    "cov_mean_sq": [0, 0, 0, 0],
                    "cov_var_ratio": [0, 0, 0, 0],
                },
                "var_autocov": {
                    "N": [0, 0, 0, 0],
                    "Mean": [0, 0, 0, 0],
                    "Variance": [0, 0, 0, 0],
                    "AutoCorrelation": [0, 0, 0, 0],
                    "y_bar_d": [0, 0, 0, 0],
                    "autocov_mean_sq": [0, 0, 0, 0],
                    "autocov_var_ratio": [0, 0, 0, 0],
                },
                "var_doublecov": {
                    "N": [0, 0, 0, 0],
                    "Mean": [0, 0, 0, 0],
                    "Variance": [0, 0, 0, 0],
                    "Covariance": [0, 0, 0, 0],
                    "y_bar_d": [0, 0, 0, 0],
                    "cov_mean_sq": [0, 0, 0, 0],
                    "cov_var_ratio": [0, 0, 0, 0],
                },
                "var_fft": {
                    "N": [0, 0, 0, 0],
                    "M": [0, 0, 0, 0],
                    "Mean": [0, 0, 0, 0],
                    "Variance": [0, 0, 0, 0],
                    "Covariance": [0, 0, 0, 0],
                    "y_bar_d": [0, 0, 0, 0],
                    "cov_var_ratio": [0, 0, 0, 0],
                },
            }

            self.count_cov = 0
            self.mean_x_cov = 0.0
            self.mean_y_cov = 0.0
            self.covariance = 0.0

            self.count_succ_ch1 = 0
            self.mean_L_ch1 = 0.0
            self.mean_R_ch1 = 0.0
            self.cov_succ_ch1 = 0.0
            self.leftover_ch1 = None

            self.count_succ_ch2 = 0
            self.mean_L_ch2 = 0.0
            self.mean_R_ch2 = 0.0
            self.cov_succ_ch2 = 0.0
            self.leftover_ch2 = None

    def append_data(self, data: tuple):
        """
        Thread-safe append of a single measurement data point.

        Parameters
        ----------
        data : tuple
            A tuple containing measurement values.
        """
        with self._lock:
            self._data_buffer.append(data)

    def append_batch(self, batch: List[tuple]):
        """
        Thread-safe append for multiple measurement data points.

        Parameters
        ----------
        batch : List[tuple]
            A list of tuples containing measurement values.
        """
        with self._lock:
            self._data_buffer.extend(batch)

    def get_latest_batch(self) -> List[tuple]:
        """
        O(1) buffer swap to retrieve all accumulated data without blocking the writer for long.

        Returns
        -------
        List[tuple]
            The list of accumulated data tuples since the last call.
        """
        with self._lock:
            if not self._data_buffer:
                return []
            new_data = self._data_buffer
            self._data_buffer = []
            return new_data

    def clear(self):
        self.init_variables()

    def rebuild_accumulators(
        self,
        rois: list | None,
        sample_frequency: float,
        time_rois: list,
    ) -> None:
        """
        Re-filters the entire historical dataset and rebuilds statistics and histograms.

        Parameters
        ----------
        rois : list or None
            A list of Region of Interest (ROI) definitions.
        sample_frequency : float
            The sampling frequency in Hz.
        time_rois : list
            A list of Time Region of Interest (ROI) definitions.
        """

        self.reset_accumulators()
        
        if not time_rois:
            selected_data = self.data[:self.ptr, :].copy()
        else:
            mask = np.zeros(self.ptr, dtype=bool)
            for start_t, end_t in time_rois:
                s_idx = max(0, int(start_t * sample_frequency))
                e_idx = min(self.ptr, int(end_t * sample_frequency))
                if s_idx < e_idx:
                    mask[s_idx:e_idx] = True
            selected_data = self.data[:self.ptr, :].copy()
            selected_data[~mask, :] = np.nan

        # Count overflows before ROI filtering
        self.n_overflow_ch1 += int(np.sum(np.isnan(selected_data[:, 0])))
        self.n_overflow_ch2 += int(np.sum(np.isnan(selected_data[:, 1])))

        filtered_data = filter_data_by_rois(selected_data, rois)
        self.filtered_data[:self.ptr, :] = filtered_data
        self.update_histogram(filtered_data)
        self.update_online_statistics(filtered_data)

    def ingest_raw_batch(
        self,
        raw_batch,
        debug_mode: bool,
        rois: list | None,
        sample_frequency: float,
        time_rois: list,
    ):
        """
        Converts raw batches, stores them, and updates statistics incrementally.

        Parameters
        ----------
        raw_batch : list
            The raw data batch from the hardware service.
        debug_mode : bool
            Whether debug mode is active (includes start/stop values).
        rois : list or None
            A list of Region of Interest (ROI) definitions.
        sample_frequency : float
            The sampling frequency in Hz.
        time_rois : list
            A list of Time Region of Interest (ROI) definitions.

        Returns
        -------
        np.ndarray or tuple
            The converted measurement data. In debug mode, returns a tuple of (start_value, stop_value, new_data).
        """

        if debug_mode:
            start_value, stop_value, new_data = convert_data_debug(
                raw_batch, sentinel=self.overflow_sentinel
            )
            self.append_debug_measurements(start_value, stop_value)
        else:
            new_data = convert_data(raw_batch, sentinel=self.overflow_sentinel)

        self.append_measurements(new_data)


        # Apply time range filtering for new batch
        filtered_batch = new_data.copy()
        if time_rois:
            start_idx = self.ptr - new_data.shape[0]
            end_idx = self.ptr
            
            mask = np.zeros(new_data.shape[0], dtype=bool)
            for start_t, end_t in time_rois:
                t_s_idx = int(start_t * sample_frequency)
                t_e_idx = int(end_t * sample_frequency)
                
                intersect_start = max(start_idx, t_s_idx)
                intersect_end = min(end_idx, t_e_idx)
                
                if intersect_start < intersect_end:
                    slice_start = intersect_start - start_idx
                    slice_end = intersect_end - start_idx
                    mask[slice_start:slice_end] = True
                    
            filtered_batch[~mask, :] = np.nan

        # Count overflows before ROI filtering
        self.n_overflow_ch1 += int(np.sum(np.isnan(filtered_batch[:, 0])))
        self.n_overflow_ch2 += int(np.sum(np.isnan(filtered_batch[:, 1])))

        filtered_batch = filter_data_by_rois(filtered_batch, rois)
        
        new_l = new_data.shape[0]
        self.filtered_data[self.ptr - new_l : self.ptr, :] = filtered_batch
        
        self.update_histogram(filtered_batch)
        self.update_online_statistics(filtered_batch)

        # Return the raw converted samples: the archive must store unfiltered
        # data, with ROIs re-applied from settings.json on load.
        if debug_mode:
            return start_value, stop_value, new_data
        return new_data

    def append_measurements(self, new_data: np.ndarray) -> None:
        """
        Appends new measurement data, dynamically resizing the buffer if needed.

        Parameters
        ----------
        new_data : np.ndarray
            The new numeric data to append, shape (N, 2).
        """
        if new_data.shape[0] <= 0:
            return

        with self._lock:
            if self.ptr + new_data.shape[0] > self.data.shape[0]:
                new_size = self.data.shape[0] * 2
                while self.ptr + new_data.shape[0] > new_size:
                    new_size *= 2

                new_y = np.zeros((new_size, 2), dtype=float)
                new_y[: self.ptr, :] = self.data[: self.ptr, :]
                self.data = new_y
                
                new_f = np.zeros((new_size, 2), dtype=float)
                new_f[: self.ptr, :] = self.filtered_data[: self.ptr, :]
                self.filtered_data = new_f

            self.data[self.ptr : self.ptr + new_data.shape[0], :] = new_data
            self.ptr += new_data.shape[0]

    def append_debug_measurements(
        self, start_data: np.ndarray, stop_data: np.ndarray
    ) -> None:
        """
        Appends new debug data, dynamically resizing buffers if needed.

        Parameters
        ----------
        start_data : np.ndarray
            The array of start values.
        stop_data : np.ndarray
            The array of stop values.
        """
        new_l = start_data.shape[0]
        if new_l <= 0:
            return

        with self._lock:
            if self.ptr + new_l > self.start_data.shape[0]:
                new_size = self.start_data.shape[0] * 2
                while self.ptr + new_l > new_size:
                    new_size *= 2

                new_start = np.zeros((new_size, 2), dtype=float)
                new_stop = np.zeros((new_size, 2), dtype=float)

                new_start[: self.ptr, :] = self.start_data[: self.ptr, :]
                new_stop[: self.ptr, :] = self.stop_data[: self.ptr, :]

                self.start_data = new_start
                self.stop_data = new_stop

            self.start_data[self.ptr : self.ptr + new_l, :] = start_data
            self.stop_data[self.ptr : self.ptr + new_l, :] = stop_data

    def update_online_statistics(self, new_data: np.ndarray):
        """
        Updates rolling mean, variance, and covariance algorithms.

        Parameters
        ----------
        new_data : np.ndarray
            The freshly appended data block, after any necessary filtering.
        """
        if new_data.size == 0:
            return

        ch1 = new_data[:, 0]
        ch2 = new_data[:, 1]

        valid_ch1 = ch1[~np.isnan(ch1)]
        valid_ch2 = ch2[~np.isnan(ch2)]

        n_b_ch1 = len(valid_ch1)
        n_b_ch2 = len(valid_ch2)

        # ==========================================
        # --- Channel 1 Update ---
        # ==========================================
        if n_b_ch1 > 0:
            mean_b_ch1 = np.mean(valid_ch1)
            m2_b_ch1 = np.var(valid_ch1, ddof=0) * n_b_ch1

            n_a_ch1 = self.count_ch1
            n_total_ch1 = n_a_ch1 + n_b_ch1

            delta_ch1 = mean_b_ch1 - self.mean_value_ch1
            self.mean_value_ch1 += delta_ch1 * (n_b_ch1 / n_total_ch1)

            m2_a_ch1 = self.variance_ch1 * (n_a_ch1 - 1) if n_a_ch1 > 1 else 0
            m2_total_ch1 = (
                m2_a_ch1 + m2_b_ch1 + (delta_ch1**2) * (n_a_ch1 * n_b_ch1 / n_total_ch1)
            )

            self.variance_ch1 = (
                m2_total_ch1 / (n_total_ch1 - 1) if n_total_ch1 > 1 else 0
            )
            self.count_ch1 = n_total_ch1

            # --- Ch1 Successive Covariance ---
            if self.leftover_ch1 is not None:
                seq_ch1 = np.concatenate(([self.leftover_ch1], ch1))
            else:
                seq_ch1 = ch1

            n_pairs_ch1_raw = len(seq_ch1) // 2

            if n_pairs_ch1_raw > 0:
                left_ch1_raw = seq_ch1[0 : 2 * n_pairs_ch1_raw : 2]
                right_ch1_raw = seq_ch1[1 : 2 * n_pairs_ch1_raw : 2]

                valid_pairs_ch1 = ~np.isnan(left_ch1_raw) & ~np.isnan(right_ch1_raw)
                left_ch1 = left_ch1_raw[valid_pairs_ch1]
                right_ch1 = right_ch1_raw[valid_pairs_ch1]

                n_pairs_ch1 = len(left_ch1)

                if n_pairs_ch1 > 0:

                    mean_b_L = np.mean(left_ch1)
                    mean_b_R = np.mean(right_ch1)

                    if n_pairs_ch1 > 1:
                        c_LR_b = np.cov(left_ch1, right_ch1, ddof=0)[0, 1] * n_pairs_ch1
                    else:
                        c_LR_b = 0.0

                    n_a_succ = self.count_succ_ch1
                    n_total_succ = n_a_succ + n_pairs_ch1

                    delta_L = mean_b_L - self.mean_L_ch1
                    delta_R = mean_b_R - self.mean_R_ch1

                    self.mean_L_ch1 += delta_L * (n_pairs_ch1 / n_total_succ)
                    self.mean_R_ch1 += delta_R * (n_pairs_ch1 / n_total_succ)

                    c_LR_a = self.cov_succ_ch1 * (n_a_succ - 1) if n_a_succ > 1 else 0
                    c_LR_total = (
                        c_LR_a
                        + c_LR_b
                        + (delta_L * delta_R) * (n_a_succ * n_pairs_ch1 / n_total_succ)
                    )

                    self.cov_succ_ch1 = (
                        c_LR_total / (n_total_succ - 1) if n_total_succ > 1 else 0
                    )
                    self.count_succ_ch1 = n_total_succ

            if len(seq_ch1) % 2 != 0:
                self.leftover_ch1 = seq_ch1[-1]
            else:
                self.leftover_ch1 = None

        # ==========================================
        # --- Channel 2 Update ---
        # ==========================================
        if n_b_ch2 > 0:
            mean_b_ch2 = np.mean(valid_ch2)
            m2_b_ch2 = np.var(valid_ch2, ddof=0) * n_b_ch2

            n_a_ch2 = self.count_ch2
            n_total_ch2 = n_a_ch2 + n_b_ch2

            delta_ch2 = mean_b_ch2 - self.mean_value_ch2
            self.mean_value_ch2 += delta_ch2 * (n_b_ch2 / n_total_ch2)

            m2_a_ch2 = self.variance_ch2 * (n_a_ch2 - 1) if n_a_ch2 > 1 else 0
            m2_total_ch2 = (
                m2_a_ch2 + m2_b_ch2 + (delta_ch2**2) * (n_a_ch2 * n_b_ch2 / n_total_ch2)
            )

            self.variance_ch2 = (
                m2_total_ch2 / (n_total_ch2 - 1) if n_total_ch2 > 1 else 0
            )
            self.count_ch2 = n_total_ch2

            # --- Ch2 Successive Covariance ---
            if self.leftover_ch2 is not None:
                seq_ch2 = np.concatenate(([self.leftover_ch2], ch2))
            else:
                seq_ch2 = ch2

            n_pairs_ch2_raw = len(seq_ch2) // 2

            if n_pairs_ch2_raw > 0:
                left_ch2_raw = seq_ch2[0 : 2 * n_pairs_ch2_raw : 2]
                right_ch2_raw = seq_ch2[1 : 2 * n_pairs_ch2_raw : 2]

                valid_pairs_ch2 = ~np.isnan(left_ch2_raw) & ~np.isnan(right_ch2_raw)
                left_ch2 = left_ch2_raw[valid_pairs_ch2]
                right_ch2 = right_ch2_raw[valid_pairs_ch2]

                n_pairs_ch2 = len(left_ch2)

                if n_pairs_ch2 > 0:

                    mean_b_L = np.mean(left_ch2)
                    mean_b_R = np.mean(right_ch2)

                    if n_pairs_ch2 > 1:
                        c_LR_b = np.cov(left_ch2, right_ch2, ddof=0)[0, 1] * n_pairs_ch2
                    else:
                        c_LR_b = 0.0

                    n_a_succ = self.count_succ_ch2
                    n_total_succ = n_a_succ + n_pairs_ch2

                    delta_L = mean_b_L - self.mean_L_ch2
                    delta_R = mean_b_R - self.mean_R_ch2

                    self.mean_L_ch2 += delta_L * (n_pairs_ch2 / n_total_succ)
                    self.mean_R_ch2 += delta_R * (n_pairs_ch2 / n_total_succ)

                    c_LR_a = self.cov_succ_ch2 * (n_a_succ - 1) if n_a_succ > 1 else 0
                    c_LR_total = (
                        c_LR_a
                        + c_LR_b
                        + (delta_L * delta_R) * (n_a_succ * n_pairs_ch2 / n_total_succ)
                    )

                    self.cov_succ_ch2 = (
                        c_LR_total / (n_total_succ - 1) if n_total_succ > 1 else 0
                    )
                    self.count_succ_ch2 = n_total_succ

            if len(seq_ch2) % 2 != 0:
                self.leftover_ch2 = seq_ch2[-1]
            else:
                self.leftover_ch2 = None

        # ==========================================
        # --- Covariance ---
        # ==========================================
        valid_mask = ~np.isnan(ch1) & ~np.isnan(ch2)
        paired_ch1 = ch1[valid_mask]
        paired_ch2 = ch2[valid_mask]

        n_b_cov = len(paired_ch1)

        if n_b_cov > 0:
            mean_b_x = np.mean(paired_ch1)
            mean_b_y = np.mean(paired_ch2)

            if n_b_cov > 1:
                c_xy_b = np.cov(paired_ch1, paired_ch2, ddof=0)[0, 1] * n_b_cov
            else:
                c_xy_b = 0.0

            n_a_cov = self.count_cov
            n_total_cov = n_a_cov + n_b_cov

            delta_x = mean_b_x - self.mean_x_cov
            delta_y = mean_b_y - self.mean_y_cov

            self.mean_x_cov += delta_x * (n_b_cov / n_total_cov)
            self.mean_y_cov += delta_y * (n_b_cov / n_total_cov)

            c_xy_a = self.covariance * (n_a_cov - 1) if n_a_cov > 1 else 0
            c_xy_total = (
                c_xy_a
                + c_xy_b
                + (delta_x * delta_y) * (n_a_cov * n_b_cov / n_total_cov)
            )

            self.covariance = c_xy_total / (n_total_cov - 1) if n_total_cov > 1 else 0
            self.count_cov = n_total_cov

        self.count += new_data.shape[0]

    def update_histogram(self, new_data: np.ndarray):
        """
        Updates the histogram buffers directly from new data.

        Parameters
        ----------
        new_data : np.ndarray
            The freshly appended data block.
        """
        if new_data.size == 0:
            return

        raw_ch1 = new_data[:, 0]
        raw_ch2 = new_data[:, 1]

        num_bins = self._num_bins
        ch1_indices = raw_ch1[~np.isnan(raw_ch1)].astype(int)
        ch2_indices = raw_ch2[~np.isnan(raw_ch2)].astype(int)

        # Clip to valid range to prevent np.bincount crash on negative/out-of-range values
        ch1_indices = ch1_indices[(ch1_indices >= 0) & (ch1_indices < num_bins)]
        ch2_indices = ch2_indices[(ch2_indices >= 0) & (ch2_indices < num_bins)]

        if len(ch1_indices) > 0:
            self.bar_data_ch1 += np.bincount(ch1_indices, minlength=num_bins)[:num_bins]
        if len(ch2_indices) > 0:
            self.bar_data_ch2 += np.bincount(ch2_indices, minlength=num_bins)[:num_bins]
