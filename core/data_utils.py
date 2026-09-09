import numpy as np
from numpy.typing import NDArray

from core.constants import OVERFLOW_VALUE


def convert_data(data: list[tuple[int, int]]) -> NDArray:
    """
    Converts a list of tuples from the serial device into a float numpy array,
    replacing overflow values with NaN.

    Parameters
    ----------
    data : list of tuple of (int, int)
        The raw measurements.

    Returns
    -------
    NDArray
        The converted array of shape (N, 2).
    """
    arr = np.array(data, dtype=float)
    arr[arr == OVERFLOW_VALUE] = np.nan
    return arr


def convert_data_debug(
    data: list[tuple[tuple[int, int], tuple[int, int]]],
) -> tuple[NDArray, NDArray, NDArray]:
    """
    Converts debug-formatted raw data into start, stop, and difference arrays.

    Parameters
    ----------
    data : list of tuple of (tuple of (int, int), tuple of (int, int))
        The raw debug measurements containing start and stop tuples.

    Returns
    -------
    tuple of (NDArray, NDArray, NDArray)
        A tuple containing (start_values, stop_values, difference).
    """
    start_values = np.array([[a, c] for (a, b), (c, d) in data], dtype=float)
    start_values[start_values == OVERFLOW_VALUE] = np.nan
    stop_values = np.array([[b, d] for (a, b), (c, d) in data], dtype=float)
    stop_values[stop_values == OVERFLOW_VALUE] = np.nan

    new_data = stop_values - start_values
    new_data[new_data < 0] = np.nan
    new_data[new_data == OVERFLOW_VALUE] = np.nan

    return start_values, stop_values, new_data


def filter_data_by_rois(data: NDArray, rois: list | None) -> NDArray:
    """
    Return data filtered by channel-specific ROIs, preserving non-selected channels as NaN.

    Rows where both channels fall completely outside their ROIs are removed.

    Parameters
    ----------
    data : NDArray
        The array of measurements.
    rois : list of tuple or None
        A list of tuples in the format (channel, low, high).

    Returns
    -------
    NDArray
        The filtered data array.
    """
    if rois is None or data.size == 0:
        return data

    mask = np.ones(data.shape, dtype=bool)
    cols_with_rois = set()
    temp_mask = np.zeros(data.shape, dtype=bool)

    for ch, low, high in rois:
        col_idx = ch - 1
        if col_idx < 0 or col_idx >= data.shape[1]:
            continue
        cols_with_rois.add(col_idx)
        temp_mask[:, col_idx] |= (data[:, col_idx] >= low) & (data[:, col_idx] <= high)

    if not cols_with_rois:
        return data

    for col_idx in cols_with_rois:
        mask[:, col_idx] = temp_mask[:, col_idx]

    filtered_data = data.copy()
    filtered_data[~mask] = np.nan
    return filtered_data
