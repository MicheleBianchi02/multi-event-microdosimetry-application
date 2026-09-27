"""
Protocol of the ramp-sample sink handed to an acquisition driver.

The driver never touches files: in ramp (or markers) timing mode it
normalizes its ADC readings to 1-D blocks of uint32 (raw counts, zero
padded — see the AQR1 format in services/ramp_storage.py) and pushes them
to the sink, which owns storage and window extraction.  All methods may be
called from the driver's reader thread.
"""
from typing import Optional, Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class IRampSink(Protocol):
    def write_block(
        self, channel: int, samples: np.ndarray, abs_index: Optional[int] = None
    ) -> None:
        """Append one contiguous block of samples of `channel` (0 = ch1,
        1 = ch2), in acquisition order.

        `abs_index` is the absolute sample index of the block's first
        sample; index 0 is the first sample of integration window 0 (the
        driver drops any leading partial window).  None = the block
        continues contiguously after the previous one.  A jump between the
        expected and the reported index is recorded as a gap — alignment
        never shifts silently.
        """
        ...

    def report_gap(self, channel: int, abs_index: int, missing: int) -> None:
        """The driver detected a hole in its stream: `missing` samples are
        lost starting at `abs_index` (e.g. a transport resync)."""
        ...
