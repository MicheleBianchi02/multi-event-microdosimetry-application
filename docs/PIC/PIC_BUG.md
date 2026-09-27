# PIC-1 — integration time diverges from the preview above 2 s

| | |
|---|---|
| **ID** | PIC-1 (this file; not mirrored in the root [BUG.md](../../BUG.md) yet) |
| **Severity** | MEDIUM — dormant for the typical microdosimetry integration times (µs…ms), severe as soon as multi-second integrations are used on the PIC |
| **Affected** | firmware v2.2 (`v2_2/final.asm`) and every earlier revision with the `TMR1U` prescaler (the behaviour is visible in the old code report); driver `pic_serial_v1` (`hardware/pic/pic_daq.py`) and the legacy `core/timing.py` (same math, dead code — B17) |
| **Discovered** | 2026-09-27, while writing [FIRMWARE_AND_DRIVER.md](FIRMWARE_AND_DRIVER.md); numbers below verified empirically against the real driver code |
| **Status** | open — the recommended fix is host-side only (§7) |

## 1. Summary

The driver's timing model assumes every integration "sub-period" lasts as
long as the first one. The firmware instead reloads Timer1 only **once per
integration**: the first sub-period runs from the reload to the overflow,
every further sub-period runs a **full** 65536 ticks (2 s). Whenever the
quantization search needs more than one sub-period (`u_bin > 1`) and the
reload is not 0, the firmware produces a **longer integration than the
application previewed, programmed, and archived** — with no error and no
hint. The preview, the wizard's "actual timing" display and the archive's
`timing_settings` all carry the wrong value.

## 2. Background: the timing model

Timer1 counts rising edges of the 32.768 kHz timebase crystal: one tick ≈
30.52 µs, a full 16-bit period (reload `0x0000`) = 65536 ticks = 2.000 s.
`TMR1U` is a software prescaler: during integration, each Timer1 overflow
increments it, and only when it wraps (`0xFF → 0x00`) is the stop phase
executed. The driver programs three raw values per integration:

- `timerU_int = 256 − u_bin` — the number of sub-periods
- `timerH/L_int = 65536 − tmr_bin` — the first sub-period's reload

## 3. Firmware behaviour (the truth)

`SetIntTimer` — the only write of the integration reload — runs in the
start phase (`final.asm:192`). The stop phase begins with:

```asm
stop_type:                                   ; final.asm:227
    INCFSZ  TMR1U                            ; final.asm:234
    GOTO    iret_timer                       ; final.asm:235   <- no reload!
```

On every non-wrapping overflow the ISR exits **without touching TMR1H/L**,
which have wrapped to 0x0000 and keep counting. The integration therefore
lasts

```
actual_ticks = (65536 − int_reload) + (u_bin − 1) · 65536
             = tmr_bin + (u_bin − 1) · 65536
```

i.e. **one partial period plus (u_bin − 1) full 2 s periods**.

## 4. Driver model (the promise)

`_quantize_timing` (`pic_daq.py:233-261`) searches over `u_bin = 256 … 1`
for the pair minimizing the quantization error **of the product**:

```python
tmr_bin = round(int_time_s · 32768 / u_bin)
candidate = u_bin · tmr_bin / 32768            # every sub-period = the first one
```

and reports this as the actual time (`calculate_actual_timing` /
`calculate_actual_timing_us`), which is what the wizard preview shows and
`DataExporter` writes into `timing_settings.integration_time_s`. The
programmed registers are `u_reg = 256 − u_bin`, `reload = 65536 − tmr_bin`.

## 5. The divergence

```
error = actual − promised = (u_bin − 1) · (65536 − tmr_bin) · 30.52 µs  ≥ 0
```

The firmware is always **equal or longer**; equal iff `u_bin = 1` or
`tmr_bin = 65536` (reload 0). The driver's model and the firmware agree
exactly only when

- **T_int ≤ 2 s** — the search always lands on `u_bin = 1` (a single
  round-to-nearest is optimal; verified: 0.5 s, 1 s, 1.999 s, 2.0 s all
  `u = 1`), or
- **T_int is an exact multiple of 2 s** — the search lands on
  `tmr_bin = 65536`, reload 0, and all sub-periods are full periods
  (verified: 10 s → `u=5, tmr=65536` → 10.000 s; 100 s → `u=50`; 512 s →
  `u=256`).

Everything else above 2 s diverges. Verified against the real driver code
(`pic_serial_v1`, debug mode, two channels):

| Requested | Preview / archive | Firmware actual | Error | (u_bin, tmr_bin) |
|---:|---:|---:|---:|---|
| 2.001 s | 2.0010 s | **12.2859 s** | +10.285 s | (7, 9367) |
| 2.5 s | 2.5000 s | 3.2500 s | +0.750 s | (2, 40960) |
| 3.0 s | 3.0000 s | 3.5000 s | +0.500 s | (2, 49152) |
| 5.0 s | 5.0000 s | 7.2500 s | +2.250 s | (4, 40960) |
| 7.0 s | 7.0000 s | 7.7500 s | +0.750 s | (4, 57344) |
| 9.0 s | 9.0000 s | 11.5000 s | +2.500 s | (6, 49152) |
| 100.5 s | 100.5000 s | 127.5703 s | +27.070 s | (64, 51456) |

The error is not monotonic in the request: it depends on which
`(u_bin, tmr_bin)` pair the search picks (exact divisors of the requested
tick count win ties with small `u_bin`, which keeps the error small; other
requests push the search to large `u_bin` with small `tmr_bin`, where each
extra full period adds 2 s). The relative error reaches ≈ +500 % just above
2 s.

## 6. Impact

- **Timing**: the actual integration (and with it the period and the
  sample frequency) is up to tens of seconds off the request.
- **Provenance**: `settings.json → timing_settings` records the *preview*
  values, so archives of such runs carry a wrong integration time and
  sample frequency — the error propagates to offline analysis and to
  charge conversions calibrated against the recorded timing.
- **Statistics**: the variance–covariance estimators run on whatever
  samples arrive, so they stay internally consistent — but the dose-averaged
  lineal energy and the charge per sample scale with the true integration
  time, so cross-measurement comparisons with correctly-timed runs are
  biased.
- **No detection path**: the PIC has no status query that would reveal the
  applied timing (unlike the Pico's `Q`), and the UI never re-derives it
  from the registers it sent.

## 7. Fix options

1. **Host-side model fix (recommended — no firmware change).** Make the
   search minimize the error against the *firmware's* model
   `tmr_bin + (u_bin − 1)·65536` instead of the product `u_bin·tmr_bin`.
   The programmed registers stay identical in format; for T ≤ 2 s nothing
   changes (`u = 1`), for T > 2 s the search will pick pairs whose actual
   value is what the preview reports (e.g. 3 s → `u = 2, tmr = 32768` →
   exactly 3.0 s). Mirror the same fix in `core/timing.py` or delete it
   (it is already listed as dead code, B17). Add regression tests for the
   > 2 s region (the current suite covers only the `u = 1` regime).
2. **Firmware fix.** Reload `TMR1H/L` in the intermediate-overflow path of
   `stop_type` so every sub-period equals the first — this matches the
   current host model and preserves sub-2 s quantization granularity
   above 2 s, but needs a reflash and re-validation of every PIC board.
3. **Interim mitigation (no code change).** Restrict the campaign's
   declared envelope to integration times ≤ 2 s: the shipped component
   (`hardware_library/device_pic.json`) and the driver's
   `HARDWARE_LIMITS` currently declare 512 s, which is the region where
   the bug lives. (Note: the reset envelope should be tightened to 2 s
   anyway — [FIRMWARE_AND_DRIVER.md §2.2](FIRMWARE_AND_DRIVER.md).)

## 8. Reproducing

```python
from hardware.pic.pic_daq import PICSerialDAQ

d = PICSerialDAQ()
preview, _ = d.calculate_actual_timing_us(2.001e6, 0.5e6,
                                          debug_mode=True, multiple_channel=True)
u_reg, h, l, _, _ = d._last_timer_params
u_bin = 256 - u_reg[0]
tmr_bin = 65536 - ((h[0] << 8) | l[0])
firmware_s = (tmr_bin + (u_bin - 1) * 65536) / 32768
# preview = 2.001 s (what the UI/archive record)
# firmware_s = 12.286 s (what the boards actually do)
```
