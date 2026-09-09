import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from arch.bootstrap import optimal_block_length
from numpy.typing import NDArray


def calculate_results(
    data: NDArray,
    ch1: bool = True,
    ch2: bool = True,
    cov_both_ch: bool = False,
    calc_bootstrap: bool = True,
) -> dict:
    """
    Compute mean, variance, covariance, autocovariance and their uncertainties.

    The function processes each requested channel independently. For each channel it returns:
    - Simple (analytical) estimates of mean, variance, and y_bar_d = var/mean,
      with standard uncertainties (assuming independent Gaussian data).
    - Bootstrap‑based estimates (circular block bootstrap) of mean, variance,
      and y_bar_d = (var - cov)/mean or (var - auto_cov)/mean,
      together with bootstrap standard deviations as uncertainties.

    Parameters
    ----------
    data : NDArray, shape (n_samples, 2)
        Input data with two columns (channel 0 and channel 1). May contain NaNs.
    ch1 : bool, default=True
        If True, process channel 0.
    ch2 : bool, default=True
        If True, process channel 1.
    cov_both_ch : bool, default=False
        If True, compute covariance between the two channels (requires both ch1 and ch2 True).
        Uses full columns (even+odd pairing) instead of interleaved decimation.
    n_bootstraps : int, default=5000
        Number of bootstrap resamples for uncertainty estimation.

    Returns
    -------
    dict
        Nested dictionary with three top‑level keys: "var", "var_cov", "var_autocov".
        Each key maps to a dict containing:
          - "N", "Mean", "Variance", "y_bar_d"
          - additionally "Covariance" (under "var_cov" and "var_doublecov")
            or "AutoCorrelation" (under "var_autocov").
        Each of these holds a list of 4 numbers:
            index 0: value for channel 1 (first channel)
            index 1: uncertainty of that value (standard deviation)
            index 2: value for channel 2 (second channel)
            index 3: uncertainty of that value
        For "var_cov", the "Covariance" list stores [cov_12, unc_cov, cov_12, unc_cov].
        For "var_autocov", the "AutoCorrelation" list stores [γ(1), unc_γ, γ(1), unc_γ],
        where γ(1) is the lag‑1 autocovariance of the channel.

    Notes
    -----
    - When ch1 or ch2 is False, the corresponding slots are filled with zeros.
    - Variance method uncertainties: mean → sqrt(var/n), variance → sqrt(2*var²/(n-1)),
      y_bar_d via error propagation.
    - Bootstrap method uses optimal block length from `arch.bootstrap.optimal_block_length`
      (circular bootstrap). If optimal length is NaN or data are all zero, bootstrap
      uncertainties are set to zero.
    - The function never raises an error about data; it falls back to zeros when computation fails.
    """

    if (not ch1 or not ch2) and cov_both_ch:
        cov_both_ch = False

    def zero_results(result_dict: dict, ii_idx: int, keys_to_zero: list[str]):
        """Helper to cleanly zero out dictionary results."""
        for key in keys_to_zero:
            result_dict[key]["N"][ii_idx] = 0
            result_dict[key]["Mean"][ii_idx] = 0
            result_dict[key]["Variance"][ii_idx] = 0
            result_dict[key]["y_bar_d"][ii_idx] = 0

            result_dict[key]["Mean"][ii_idx + 1] = 0
            result_dict[key]["Variance"][ii_idx + 1] = 0
            result_dict[key]["y_bar_d"][ii_idx + 1] = 0

            if "Covariance" in result_dict[key]:
                result_dict[key]["Covariance"][ii_idx] = 0
                result_dict[key]["Covariance"][ii_idx + 1] = 0

            if "cov_mean_sq" in result_dict[key]:
                result_dict[key]["cov_mean_sq"][ii_idx] = 0
                result_dict[key]["cov_mean_sq"][ii_idx + 1] = 0

            if "AutoCorrelation" in result_dict[key]:
                result_dict[key]["AutoCorrelation"][ii_idx] = 0
                result_dict[key]["AutoCorrelation"][ii_idx + 1] = 0

            if "autocov_mean_sq" in result_dict[key]:
                result_dict[key]["autocov_mean_sq"][ii_idx] = 0
                result_dict[key]["autocov_mean_sq"][ii_idx + 1] = 0

    def get_ch_matrix(idx, both_ch_cov: bool = False):
        if both_ch_cov:
            y = data[:, 0]
            x = data[:, 1]

            length = min(len(x), len(y))
            y = y[:length]
            x = x[:length]

            # Reintroduced NaN filtering for dual channels
            valid_mask = ~np.isnan(x) & ~np.isnan(y)
            return y[valid_mask], x[valid_mask]
        else:
            channel_data = data[:, idx]
            channel_data = channel_data[~np.isnan(channel_data)]
            y = channel_data[1::2]
            # truncate x if len(channel_data) is odd
            x = channel_data[0::2][: len(y)]
            return y, x

    def calc_cov(idx, ii, result: dict, both_ch: bool = False):
        error = False
        y, x = get_ch_matrix(idx, both_ch)

        data_matrix_cov = y if idx == 0 else x
        n = len(data_matrix_cov)

        if n <= 1:
            error = True
        else:
            mean = np.mean(data_matrix_cov)
            var = np.nanvar(data_matrix_cov, ddof=1)
            cov = np.cov(x, y, ddof=1)[0, 1]
            yd = (var - cov) / mean if mean > 0 else 0
            cov_mean_sq = cov / (mean**2) if mean != 0 else 0

            # --- UNCERTAINTY CALCULATION ---

            if calc_bootstrap:
                try:
                    n_bootstraps = max(1, int(np.ceil(n / 10.0)))
                    opt_blocks = optimal_block_length(data_matrix_cov)
                    b_cb_float = opt_blocks["circular"].iloc[0]

                    if np.isnan(b_cb_float):
                        error = True
                    else:
                        optimal_B = max(1, min(int(np.round(b_cb_float)), n // 2))

                        mean_bootstraps = np.zeros(n_bootstraps)
                        var_bootstraps = np.zeros(n_bootstraps)
                        cov_bootstraps = np.zeros(n_bootstraps)

                        def process_batch_cov(i):
                            current_batch_size = min(batch_size, n_bootstraps - i)
                            indices = get_cbb_indices(n, optimal_B, current_batch_size)

                            batch_matrix = data_matrix_cov[indices]
                            mb = np.mean(batch_matrix, axis=1, keepdims=True)
                            mean_res = mb.squeeze()

                            centered = batch_matrix - mb
                            var_res = np.einsum("ij,ij->i", centered, centered) / (
                                batch_matrix.shape[1] - 1
                            )

                            y_boot = y[indices]
                            x_boot = x[indices]

                            mean_X_boot = np.mean(x_boot, axis=1, keepdims=True)
                            mean_Y_boot = np.mean(y_boot, axis=1, keepdims=True)
                            n_pairs = x_boot.shape[1]

                            if n_pairs > 1:
                                cov_res = np.einsum(
                                    "ij,ij->i",
                                    x_boot - mean_X_boot,
                                    y_boot - mean_Y_boot,
                                ) / (n_pairs - 1)
                            else:
                                cov_res = np.zeros(current_batch_size)

                            return i, current_batch_size, mean_res, var_res, cov_res

                        with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
                            futures = [
                                executor.submit(process_batch_cov, i)
                                for i in range(0, n_bootstraps, batch_size)
                            ]
                            for future in futures:
                                i, current_batch_size, mean_res, var_res, cov_res = (
                                    future.result()
                                )
                                mean_bootstraps[i : i + current_batch_size] = mean_res
                                var_bootstraps[i : i + current_batch_size] = var_res
                                cov_bootstraps[i : i + current_batch_size] = cov_res

                        yD_bootstraps = (
                            var_bootstraps - cov_bootstraps
                        ) / mean_bootstraps

                        cov_mean_sq_bootstraps = cov_bootstraps / (
                            mean_bootstraps**2 + 1e-15
                        )

                        unc_mean = np.std(mean_bootstraps, ddof=1)
                        unc_var = np.std(var_bootstraps, ddof=1)
                        unc_cov = np.std(cov_bootstraps, ddof=1)
                        unc_yd = np.std(yD_bootstraps, ddof=1)
                        unc_cov_mean_sq = np.std(cov_mean_sq_bootstraps, ddof=1)
                except Exception:
                    error = True
            else:
                unc_mean = unc_var = unc_cov = unc_yd = 0.0
                unc_cov_mean_sq = 0.0

        if error:
            mean = var = cov = yd = 0.0
            unc_mean = unc_var = unc_cov = unc_yd = 0.0
            cov_mean_sq = 0.0
            unc_cov_mean_sq = 0.0

        target_key = "var_doublecov" if both_ch else "var_cov"

        result[target_key]["N"][ii] = n
        result[target_key]["Mean"][ii] = mean
        result[target_key]["Variance"][ii] = var
        result[target_key]["Covariance"][ii] = cov
        result[target_key]["y_bar_d"][ii] = yd
        result[target_key]["cov_mean_sq"][ii] = cov_mean_sq

        result[target_key]["Mean"][ii + 1] = unc_mean
        result[target_key]["Variance"][ii + 1] = unc_var
        result[target_key]["Covariance"][ii + 1] = unc_cov
        result[target_key]["y_bar_d"][ii + 1] = unc_yd
        result[target_key]["cov_mean_sq"][ii + 1] = unc_cov_mean_sq

    def batch_lagged_auto_cov(batch_matrix, t, ddof=1):
        """
        Compute lagged autocovariance for each row in a batch.

        Parameters
        ----------
        batch_matrix : np.ndarray, shape (n_batch, n)
            Each row is a bootstrap resampled time series (length n).
        t : int
            Lag (non‑negative integer).
        ddof : int, default=1
            Delta degrees of freedom. Denominator = n - ddof.

        Returns
        -------
        np.ndarray, shape (n_batch,)
            Autocovariance at lag t for every row in the batch.

        Notes
        -----
        Uses the same denominator as the original function (n - ddof, here n = number of columns).
        The computation follows the standard definition:
            γ(t) = 1/(n-ddof) * Σ_{i=0}^{n-t-1} (x_i - μ)(x_{i+t} - μ)
        where μ is the sample mean of the whole series (row‑wise).
        """
        n = batch_matrix.shape[1]
        if t >= n:
            return np.zeros(batch_matrix.shape[0])

        means = np.mean(batch_matrix, axis=1, keepdims=True)
        centered = batch_matrix - means

        sum_prod = np.einsum("ij,ij->i", centered[:, : n - t], centered[:, t:])

        denom = n - ddof
        return sum_prod / denom

    def get_cbb_indices(sz, sz_B, n_b):
        """
        Generates n_b sequences of sz cyclic block‑based indices.

        Each sequence is built by:
        - Dividing the range [0, sz-1] into num_blocks = (sz // sz_B) + 1 overlapping blocks.
        - For each block, picking a random starting index.
        - Forming a contiguous block of sz_B indices (wrapping around via modulo sz).
        - Concatenating all blocks and truncating to exactly sz indices.

        Example
        -------
        sz = 10, sz_B = 3, n_b = 2

        Step 1: num_blocks = (10 // 3) + 1 = 3 + 1 = 4

        Step 2: Random start indices (shape = (2, 4))
            start_indices = [[0, 7, 2, 5],
                             [1, 4, 8, 3]]

        Step 3: offsets = [0, 1, 2]

        Step 4: Add offsets (broadcasting) -> blocks of 3 consecutive indices
            For first sequence (row 0):
                Block 0 (start 0): [0,1,2]
                Block 1 (start 7): [7,8,9]
                Block 2 (start 2): [2,3,4]
                Block 3 (start 5): [5,6,7]
            For second sequence (row 1):
                Block 0 (start 1): [1,2,3]
                Block 1 (start 4): [4,5,6]
                Block 2 (start 8): [8,9,10]
                Block 3 (start 3): [3,4,5]

        Step 5: Apply modulo sz = 10
            Only block 2 of second sequence changes: 10 % 10 = 0 → becomes [8,9,0]
            All other indices already in [0,9].

            After modulo, raw concatenated blocks (shape = (2, 4*3) = (2,12)):
                Row0: [0,1,2, 7,8,9, 2,3,4, 5,6,7]
                Row1: [1,2,3, 4,5,6, 8,9,0, 3,4,5]

        Step 6: Truncate to first sz = 10 elements ([:, :10])
                Row0 (output): [0,1,2,7,8,9,2,3,4,5]
                Row1 (output): [1,2,3,4,5,6,8,9,0,3]

        The returned array has shape (n_b, sz) = (2,10), each row being a cyclic,
        wrap‑around sequence of indices built from random contiguous blocks.
        """
        num_blocks = (sz // sz_B) + 1
        out = np.empty((n_b, num_blocks, sz_B), dtype=np.int32)
        start_indices = np.random.randint(0, sz, size=(n_b, num_blocks), dtype=np.int32)

        # PERFORMANCE OPTIMIZATION: Instead of calculating `indices % sz` across millions of
        # items (which triggers expensive integer division on the CPU), we generate indices and
        # add the offsets. Because the maximum offset is < sz, the values can only ever exceed
        # `sz` by at most `sz_B`. Therefore, instead of modulo, we can just subtract `sz` from
        # the out-of-bounds indices using an in-place mask. Subtraction is far cheaper than modulo.
        np.add(start_indices[:, :, None], np.arange(sz_B, dtype=np.int32), out=out)

        mask = out >= sz
        np.subtract(out, sz, out=out, where=mask)

        return out.reshape(n_b, -1)[:, :sz]

    result = {
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
        },
        "var_autocov": {
            "N": [0, 0, 0, 0],
            "Mean": [0, 0, 0, 0],
            "Variance": [0, 0, 0, 0],
            "AutoCorrelation": [0, 0, 0, 0],
            "y_bar_d": [0, 0, 0, 0],
            "autocov_mean_sq": [0, 0, 0, 0],
        },
        "var_doublecov": {
            "N": [0, 0, 0, 0],
            "Mean": [0, 0, 0, 0],
            "Variance": [0, 0, 0, 0],
            "Covariance": [0, 0, 0, 0],
            "y_bar_d": [0, 0, 0, 0],
            "cov_mean_sq": [0, 0, 0, 0],
        },
    }

    batch_size = 2
    all_method_keys = ["var", "var_cov", "var_doublecov", "var_autocov"]

    for idx, ii in [(0, 0), (1, 2)]:
        # If the channel is not meant to be processed, zero it and skip
        if (idx == 0 and not ch1) or (idx == 1 and not ch2):
            zero_results(result, ii, all_method_keys)
            continue

        # Extract and clean data for the current channel once
        data_matrix = data[:, idx]
        data_matrix = data_matrix[~np.isnan(data_matrix)]
        n = len(data_matrix)

        # --- VARIANCE METHOD ---
        if n > 1:
            var = np.var(data_matrix, ddof=1)
            mean = np.mean(data_matrix)
        else:
            var = 0.0
            mean = 0.0

        result["var"]["N"][ii] = n
        result["var"]["Mean"][ii] = mean
        result["var"]["Mean"][ii + 1] = np.sqrt(var / n) if n > 0 else 0

        result["var"]["Variance"][ii] = var
        result["var"]["Variance"][ii + 1] = (
            np.sqrt(2 * var**2 / (n - 1)) if n > 1 else 0
        )

        safe_mean = mean if mean != 0 else 1.0
        result["var"]["y_bar_d"][ii] = var / safe_mean

        if n <= 1:
            result["var"]["y_bar_d"][ii + 1] = 0
        else:
            result["var"]["y_bar_d"][ii + 1] = (var / safe_mean) * np.sqrt(
                2 / (n - 1) + var / (n * safe_mean**2)
            )

        # Check for empty or zero-sum arrays to skip bootstrap
        if n <= 1 or np.ptp(data_matrix) == 0:
            zero_results(result, ii, ["var_cov", "var_doublecov", "var_autocov"])
            continue

        # ==================================
        # --- VARIANCE COVARIANCE METHOD ---
        # ==================================
        calc_cov(idx, ii, result, False)

        # =========================================
        # --- VARIANCE DOUBLE COVARIANCE METHOD ---
        # =========================================
        if cov_both_ch and ch1 and ch2:
            calc_cov(idx, ii, result, True)
        else:
            zero_results(result, ii, ["var_doublecov"])

        # uncertainty calculated later

        # =======================================
        # --- VARIANCE AUTO-COVARIANCE METHOD ---
        # =======================================
        error = False
        auto_cov = 0.0
        unc_auto_cov = 0.0

        # data_matrix is already NaN-filtered from the top of the loop
        if n > 1:
            auto_cov = batch_lagged_auto_cov(data_matrix.reshape(1, -1), 1)[0]
        yd = (var - auto_cov) / safe_mean if safe_mean > 0 else 0.0

        autocov_mean_sq = auto_cov / (safe_mean**2) if mean != 0 else 0.0
        # --- UNCERTAINTY CALCULATION ---
        if calc_bootstrap:
            try:
                n_bootstraps = max(1, int(np.ceil(n / 10.0)))
                opt_blocks = optimal_block_length(data_matrix)
                b_cb_float = opt_blocks["circular"].iloc[0]
                if np.isnan(b_cb_float):
                    error = True
                else:
                    optimal_B = max(1, min(int(np.round(b_cb_float)), n // 2))

                    mean_bootstraps = np.zeros(n_bootstraps)
                    var_bootstraps = np.zeros(n_bootstraps)
                    auto_cov_bootstraps = np.zeros(n_bootstraps)

                    def process_batch_autocov(i):
                        current_batch_size = min(batch_size, n_bootstraps - i)
                        indices = get_cbb_indices(n, optimal_B, current_batch_size)

                        batch_matrix = data_matrix[indices]
                        mb = np.mean(batch_matrix, axis=1, keepdims=True)
                        mean_res = mb.squeeze()

                        centered = batch_matrix - mb
                        var_res = np.einsum("ij,ij->i", centered, centered) / (
                            batch_matrix.shape[1] - 1
                        )

                        auto_cov_res = batch_lagged_auto_cov(batch_matrix, 1)
                        return i, current_batch_size, mean_res, var_res, auto_cov_res

                    with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
                        futures = [
                            executor.submit(process_batch_autocov, i)
                            for i in range(0, n_bootstraps, batch_size)
                        ]
                        for future in futures:
                            i, current_batch_size, mean_res, var_res, auto_cov_res = (
                                future.result()
                            )
                            mean_bootstraps[i : i + current_batch_size] = mean_res
                            var_bootstraps[i : i + current_batch_size] = var_res
                            auto_cov_bootstraps[i : i + current_batch_size] = (
                                auto_cov_res
                            )

                    yD_bootstraps = (
                        var_bootstraps - auto_cov_bootstraps
                    ) / mean_bootstraps

                    autocov_mean_sq_bootstraps = auto_cov_bootstraps / (
                        mean_bootstraps**2 + 1e-15
                    )

                    unc_mean = np.std(mean_bootstraps, ddof=1)
                    unc_var = np.std(var_bootstraps, ddof=1)
                    unc_auto_cov = np.std(auto_cov_bootstraps, ddof=1)
                    unc_yd = np.std(yD_bootstraps, ddof=1)
                    unc_autocov_mean_sq = np.std(autocov_mean_sq_bootstraps, ddof=1)

            except Exception:
                error = True
        else:
            unc_mean = unc_var = unc_auto_cov = unc_yd = 0.0
            unc_autocov_mean_sq = 0.0

        if error:
            mean = var = auto_cov = yd = 0.0
            unc_mean = unc_var = unc_auto_cov = unc_yd = 0.0
            autocov_mean_sq = 0
            unc_autocov_mean_sq = 0.0

        result["var_autocov"]["N"][ii] = n
        result["var_autocov"]["Mean"][ii] = mean
        result["var_autocov"]["Variance"][ii] = var
        result["var_autocov"]["AutoCorrelation"][ii] = auto_cov
        result["var_autocov"]["y_bar_d"][ii] = yd
        result["var_autocov"]["autocov_mean_sq"][ii] = autocov_mean_sq

        result["var_autocov"]["Mean"][ii + 1] = unc_mean
        result["var_autocov"]["Variance"][ii + 1] = unc_var
        result["var_autocov"]["AutoCorrelation"][ii + 1] = unc_auto_cov
        result["var_autocov"]["y_bar_d"][ii + 1] = unc_yd
        result["var_autocov"]["autocov_mean_sq"][ii + 1] = unc_autocov_mean_sq

    return result
