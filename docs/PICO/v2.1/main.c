/**
 * ATOMIQA — Pico 2 DAQ firmware (PICO2DAQ 2.1)
 * ============================================================
 * Serial protocol (USB CDC, ASCII commands terminated by \n or \r,
 * single-byte or single-line replies):
 *
 *   V                     -> "V PICO2DAQ 2.1 <16-hex board id> <clk MHz>\n"
 *                            identity / liveness probe (idle only)
 *   Q                     -> "Q run=<0|1> mode=<noramp|ramp> gen=<pio|c>
 *                            int=%.3f reset=%.3f dt=%.3f div=<int.frac>
 *                            pulses=%lu caps=<b0b1b2> event=<ME|SE>
 *                            dropped=%u\n"                       (idle)
 *   RS / RM               -> event mode pin high (SE) / low (ME),      D / E
 *   C b0 b1 b2            -> capacitor bank bits ('0'/'1'),            D / E
 *   XN / XR               -> select no-ramp (ADC) / ramp (PIO) mode,   D / E
 *   T <int_us>,<reset_us> -> timing, floored to whole sys-clock cycles  D / E
 *                            in double precision; T/P re-derive the raw
 *                            ramp parameters (manual-bench path)
 *   P <dt_us>             -> ramp pulse spacing (software generator:      D / E
 *                            bounded by the time range; in PIO mode by
 *                            the 16.8 clock divider, dt <= 65536*10/clk)
 *   O <div_i>,<div_f>,<reset_pio>,<pulses>
 *                         -> program the ramp generator with RAW values  D / E
 *                            (the application path: the host computes the
 *                            numbers, the firmware obeys without rounding)
 *   GP / GC               -> ramp generator: PIO hardware / software on   D / E
 *                            the timing core (bench comparison of both)
 *   S                     -> start: ack D first, then binary data      D
 *   E                     -> stop: stream end-marker 0xFFFFFFFF,       D / E
 *                            then ack (E = engine did not finish)
 *   anything else         -> '?'
 *
 * Replies: D = ok, E = error/bad parameter, '?' = unknown command.
 * While a measurement is running (between S and E) every command except
 * 'E' is SILENTLY IGNORED: its ASCII reply would corrupt the binary
 * stream on the same USB endpoint.
 *
 * Data stream (no-ramp mode only), raw binary, host byte order = little:
 *   uint32 word = (sample1 << 16) | sample2   (two 12-bit ADC readings)
 *   terminated at stop by the word 0xFFFFFFFF (both halves > 4095, so it
 *   can never be valid data).
 *
 * Timing model (no-ramp): the engine samples at t1 and t1+T_int, asserts
 * RESET right after the sample-2 instant, releases it RESET_SETTLE_US
 * before the next sample 1, giving period = T_int + T_rst exactly.
 * T_int/T_rst are floored to whole system clock cycles by 'T'.
 *
 * Timing model (ramp, process v2 — identical structure in both
 * generators, selectable with GP/GC for a bench comparison):
 *   reset LOW -> settle (RESET HIGH, no pulses: 20 PIO cycles in the PIO
 *   generator = 2*dt, fixed 2 us in the software one) -> N = int/dt + 1
 *   pulses with edges at 0, dt, ..., int -> the last pulse's period runs
 *   out -> restart.  Period = reset + settle + N*dt.
 *   PIO: everything is quantized to the dt/10 tick (one pulse = 10 PIO
 *   cycles at clkdiv = clk*dt/10e6); dt is bounded by the 16.8 divider.
 *   Software: dt, reset and settle all quantize to one system clock cycle,
 *   with no dt upper bound; jitter = bus arbitration only (ns).
 *
 * Pin map:
 *   GP14 integrator reset (0 = reset, 1 = integrate)
 *   GP15 ramp-mode external trigger output (PIO)
 *   GP16 event mode (0 = ME, 1 = SE)
 *   GP17 capacitor bank bit 0
 *   GP18 capacitor bank bit 1
 *   GP19 capacitor bank bit 2
 *   GP26 ADC input (channel 0), 12 bit
 *
 * Core layout: core 0 runs main() — USB CDC, command parsing and data
 * streaming; core 1 (launched via multicore_launch_core1) runs the
 * timing engine with interrupts disabled.
 */
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "pico/time.h"
#include "pico/unique_id.h"
#include "hardware/adc.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "hardware/structs/sio.h"

// --- ARM CORTEX-M33 DWT & CoreDebug POLYFILL ---
// Bypasses the need for core_cm33.h by mapping the registers directly
typedef struct {
    volatile uint32_t DHCSR;
    volatile uint32_t DCRSR;
    volatile uint32_t DCRDR;
    volatile uint32_t DEMCR;
} CoreDebug_Type;
#define CoreDebug ((CoreDebug_Type *)0xE000EDF0)
#define CoreDebug_DEMCR_TRCENA_Msk (1UL << 24)

typedef struct {
    volatile uint32_t CTRL;
    volatile uint32_t CYCCNT;
} DWT_Type;
#define DWT ((DWT_Type *)0xE0001000)
#define DWT_CTRL_CYCCNTENA_Msk (1UL << 0)
// -----------------------------------------------

// Include auto-generated PIO header
#include "ramp_control.pio.h"

// --- FIRMWARE IDENTITY ---
#define FW_NAME       "PICO2DAQ"
#define FW_VERSION    "2.1"

// --- HARDWARE PIN ASSIGNMENTS ---
#define PIN_RESET       14  // Integrator Reset Line
#define PIN_CONV_START  15  // Ramp Mode External Trigger Signal

// --- STATIC CONFIGURATION PINS ---
#define PIN_EVENT_MODE  16  // For 'R' command (Single/Multi event)
#define PIN_CAP_0       17  // For 'C' command (Capacitor bank bit 0)
#define PIN_CAP_1       18  // For 'C' command (Capacitor bank bit 1)
#define PIN_CAP_2       19  // For 'C' command (Capacitor bank bit 2)

#define ADC_GPIO_PIN    26  // ADC Input Pin (GPIO 26 = ADC Channel 0)
#define ADC_CHANNEL     0

// --- VALIDATED TIMING LIMITS (mirrored by the host driver) ---
#define INT_TIME_MIN_US     5.0f    // ADC conversion (~2 us) + margin
// Ramp process v2 has the settle AFTER the reset phase, so 5 us LOW is
// meaningful there.  In no-ramp mode the 5 us settle is INSIDE the window:
// at exactly 5 us the reset line never asserts LOW (degenerate, allowed).
#define RESET_TIME_MIN_US   5.0f
#define TIME_MAX_US         10000000.0f

// RESET is released this long before sample 1 so the integrator output
// transient settles; it is part of the reset window.
#define RESET_SETTLE_US     5.0f

// NOP delay between starting an ADC conversion and acting on the pin state:
// keeps the reset assertion out of the SAR acquisition window. Applied
// symmetrically to both samples so its latency cancels in the difference.
#define ADC_ACQ_GUARD_NOPS  10

// Default timing at boot (must satisfy the limits above)
#define DEFAULT_INT_US      30.0f
#define DEFAULT_RESET_US    20.0f
#define DEFAULT_RAMP_DT_US  10.0f

// Ramp process v2: settle between the reset release and the first
// CONV_START pulse.  PIO generator: 20 PIO cycles (scales with the PIO
// clock: 2*dt at 50% duty).  Software generator: fixed 2 us.
#define RAMP_SETTLE_PIO_CYCLES 20
#define RAMP_SETTLE_SOFT_US    2.0f

// --- DATA STREAM ---
#define STREAM_MARKER   0xFFFFFFFFu  // both halves 65535: impossible as data
#define RING_WORDS      1024         // power of two; absorbs USB stalls so the
                                     // timing loop never blocks on the host

// --- SYSTEM CONFIGURATION STRUCTURE ---
// The ramp generator's raw PIO parameters (divider, reset cycles, pulse
// count) are the single source of truth for run_ramp_pio_mode: they are
// either computed by recompute_ramp_params() from T/P (manual bench use)
// or programmed directly by the host with 'O' (the application computes
// them and sends raw counts — no firmware-side rounding can then disagree
// with what the host previewed).
typedef struct {
    volatile bool is_ramp_mode;     // true = Ramp Mode (PIO), false = No-Ramp Mode (ADC)
    volatile bool is_running;       // Active acquisition flag
    volatile bool run_active;       // A run started and was not fully stopped yet
    volatile bool engine_done;      // Timing engine finished (idle)
    volatile uint32_t clk_hz;       // Actual system clock (not assumed 150 MHz)
    volatile uint32_t reset_cycles; // Reset duration in sys-clock cycles
    volatile uint32_t int_cycles;   // Integration duration in sys-clock cycles
    volatile float ramp_dt_us;      // Effective pulse step (Ramp mode, for Q)
    volatile uint32_t ramp_div_int; // PIO clock divider, integer part
    volatile uint32_t ramp_div_frac8; // PIO clock divider, 1/256 fractional part
    volatile uint32_t ramp_reset_pio; // Reset phase length in PIO cycles
    volatile uint32_t ramp_pulses;  // CONV_START edges programmed into the PIO
    volatile bool ramp_gen_pio;     // Ramp generator: true = PIO, false = software
} SystemConfig;

volatile SystemConfig sys_cfg = {
    .is_ramp_mode = false,
    .is_running = false,
    .run_active = false,
    .engine_done = true,
    .clk_hz = 0,
    .reset_cycles = 0,
    .int_cycles = 0,
    .ramp_dt_us = DEFAULT_RAMP_DT_US,
    .ramp_gen_pio = true
};

// Global handles for PIO state machine
PIO pio_hw = pio0;
uint sm_id = 0;
uint pio_offset = 0;

// --- SPSC RING BUFFER (producer: timing engine core, consumer: USB core) ---
static volatile uint32_t stream_ring[RING_WORDS];
static volatile uint32_t ring_head;      // written only by the producer
static volatile uint32_t ring_tail;      // written only by the consumer
static volatile uint32_t dropped_words;  // samples lost to a full ring

static inline void ring_reset(void) {
    ring_head = 0;
    ring_tail = 0;
    dropped_words = 0;
    __compiler_memory_barrier();
}

// Called from the timing loop: never blocks, never stalls the period.
static inline bool ring_push(uint32_t value) {
    uint32_t h = ring_head;
    uint32_t next = (h + 1) & (RING_WORDS - 1);
    if (next == ring_tail) {
        dropped_words++;
        return false;
    }
    stream_ring[h] = value;
    __dmb();
    ring_head = next;
    return true;
}

static inline bool ring_pop(uint32_t *value) {
    uint32_t t = ring_tail;
    if (t == ring_head) {
        return false;
    }
    *value = stream_ring[t];
    __dmb();
    ring_tail = (t + 1) & (RING_WORDS - 1);
    return true;
}

// --- DWT CYCLE COUNTER HELPERS ---
static inline void enable_cycle_counter(void) {
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CYCCNT = 0;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

static inline void wait_until_cycle(uint32_t target_cycle) {
    while ((DWT->CYCCNT - target_cycle) > (1UL << 31)) {
        __asm volatile ("nop");
    }
}

// --- NON-BLOCKING ADC READ (same register sequence as the SDK's adc_read) ---
static inline void adc_start_once(void) {
    hw_set_bits(&adc_hw->cs, ADC_CS_START_ONCE_BITS);
}

static inline uint16_t adc_finish_read(void) {
    while (!(adc_hw->cs & ADC_CS_READY_BITS)) {
        tight_loop_contents();
    }
    return (uint16_t)adc_hw->result;
}

// ============================================================================
// CORE 1: TIME-CRITICAL EXECUTION ENGINE (Runs strictly from SRAM)
// ============================================================================

// NO-RAMP MODE: Absolute sample-to-sample period tracking
void __not_in_flash_func(run_noramp_adc_mode)(void) {
    uint32_t irq_status = save_and_disable_interrupts();

    sys_cfg.ramp_pulses = 0;  // no PIO pulse train in this mode

    const uint32_t settle_cycles =
        (uint32_t)(RESET_SETTLE_US * (float)sys_cfg.clk_hz / 1e6f);

    // The integrator is held in reset while idle (pin low), so there is no
    // residual charge. Release it, let the output settle, then take the
    // very first sample: the first cycle is as clean as the following ones.
    sio_hw->gpio_set = (1 << PIN_RESET);
    uint32_t t1_target = DWT->CYCCNT + settle_cycles;
    wait_until_cycle(t1_target);

    while (sys_cfg.is_running) {
        // --- STEP 1: SAMPLE 1 ---
        // Split read: the conversion (and with it the sampling instant)
        // starts at t1. The acquisition guard below matches sample 2's, so
        // the software latency of the two sampling instents is identical
        // and the differential timing is unbiased.
        wait_until_cycle(t1_target);
        adc_start_once();
        for (int i = 0; i < ADC_ACQ_GUARD_NOPS; i++) {
            __asm volatile ("nop");
        }
        uint16_t sample1 = adc_finish_read();

        // Calculate exact absolute cycle target for Sample 2
        uint32_t t2_target = t1_target + sys_cfg.int_cycles;

        // --- STEP 2: SAMPLE 2 ---
        // The RESET assertion must not land inside the SAR's acquisition
        // window: give it the same guard delay as sample 1 (the offset is
        // common to both samples and cancels in the difference).
        wait_until_cycle(t2_target);
        adc_start_once();
        for (int i = 0; i < ADC_ACQ_GUARD_NOPS; i++) {
            __asm volatile ("nop");
        }

        // --- STEP 3: RESET PHASE (starts right after the acquisition) ---
        sio_hw->gpio_clr = (1 << PIN_RESET);

        // DATA PACKING & RING PUSH
        // Handled during the reset window — absorbs the execution overhead.
        uint16_t sample2 = adc_finish_read();
        uint32_t packed_payload = ((uint32_t)sample1 << 16) | sample2;
        ring_push(packed_payload);

        // Calculate exact absolute cycle target for Sample 1 of the NEXT loop
        // Total Period = int_cycles + reset_cycles
        uint32_t t1_next_target = t2_target + sys_cfg.reset_cycles;

        // PIN_RESET must go HIGH exactly RESET_SETTLE_US BEFORE the next
        // Sample 1, to allow the transient to finish
        uint32_t reset_high_target = t1_next_target - settle_cycles;

        wait_until_cycle(reset_high_target);
        sio_hw->gpio_set = (1 << PIN_RESET);

        // Advance anchor to next loop target
        t1_target = t1_next_target;
    }

    // Force RESET pin LOW when stopped (safe state: capacitor discharged)
    sio_hw->gpio_clr = (1 << PIN_RESET);

    // End-of-stream marker: tells the consumer exactly where the data stops
    // (and, with it, that the engine has finished its last cycle).
    ring_push(STREAM_MARKER);
    __dmb();
    sys_cfg.engine_done = true;
    restore_interrupts(irq_status);
}

// Derive the raw ramp-generator parameters from the current T/P values
// (the manual-bench path; the app normally programs them via 'O').
// Double precision throughout: every intermediate fits float64 exactly,
// where the old float32 code lost pulses (4.5e9 stored as 4499999744).
//
// Ramp process v2: reset LOW -> settle (RESET HIGH, no pulses) ->
// N = int/dt + 1 pulses with edges at 0, dt, ..., int -> last pulse's
// period runs out -> restart.  In PIO cycles: reset_pio = 10*reset/dt
// (the LOW duration itself; everything is quantized to the dt/10 tick),
// pulses = int/dt + 1, settle = 20 fixed cycles.
static void recompute_ramp_params(void) {
    double clk = (double)sys_cfg.clk_hz;
    double dt = (double)sys_cfg.ramp_dt_us;

    double div = clk * dt / 10.0e6;          // ideal 16.8 divider
    if (div < 1.0) div = 1.0;
    if (div > 65535.99609375) div = 65535.99609375;  // (65535 + 255/256)
    sys_cfg.ramp_div_int = (uint32_t)div;
    sys_cfg.ramp_div_frac8 = (uint32_t)((div - (double)sys_cfg.ramp_div_int) * 256.0);
    double div_actual = (double)sys_cfg.ramp_div_int + (double)sys_cfg.ramp_div_frac8 / 256.0;

    // Pulse count: int/dt intervals, edges on BOTH endpoints.
    // Minimum integration = dt (2 pulses): clamp to one interval.
    uint32_t intervals = (uint32_t)((double)sys_cfg.int_cycles * 1.0e6 / (clk * dt));
    if (intervals < 1) intervals = 1;
    sys_cfg.ramp_pulses = intervals + 1;

    // Reset phase (LOW duration) in PIO cycles, quantized to the dt/10
    // tick (clamped: a too-short reset degenerates to one tick).
    int64_t rp = (int64_t)((double)sys_cfg.reset_cycles * 10.0e6 / (clk * dt));
    if (rp < 1) rp = 1;
    sys_cfg.ramp_reset_pio = (uint32_t)rp;

    // Effective dt for Q display: PIO -> 16.8-quantized; software ->
    // floored to whole system-clock cycles (what the engine will use).
    // int_cycles/reset_cycles are NOT touched: they hold the exact
    // T-command floors (source of truth for no-ramp and software).
    if (sys_cfg.ramp_gen_pio) {
        sys_cfg.ramp_dt_us = (float)(div_actual * 10.0e6 / clk);
    } else {
        uint32_t dt_cycles = (uint32_t)((double)sys_cfg.ramp_dt_us * clk / 1.0e6);
        if (dt_cycles == 0) dt_cycles = 1;
        sys_cfg.ramp_dt_us = (float)((double)dt_cycles * 1.0e6 / clk);
    }
}

// RAMP MODE: Hardware-driven PIO State Machine Setup & Execution.
// No parameter math here: it programs exactly the raw values stored in
// sys_cfg (set by 'O' or derived from T/P by recompute_ramp_params).
void run_ramp_pio_mode(void) {
    // 1. Configure PIO State Machine
    pio_sm_config c = ramp_control_program_get_default_config(pio_offset);

    // Map PIN_CONV_START to SET pins, PIN_RESET to SIDE-SET pin
    sm_config_set_set_pins(&c, PIN_CONV_START, 1);
    sm_config_set_sideset_pins(&c, PIN_RESET);
    sm_config_set_clkdiv_int_frac8(&c, (uint32_t)sys_cfg.ramp_div_int,
                                   (uint8_t)sys_cfg.ramp_div_frac8);

    // Claim the pins for the PIO *only while ramp mode runs* (claiming them
    // at boot would steal them from the SIO writes of the no-ramp engine),
    // and make the PIO actually drive them: the program never sets pindirs,
    // so without this both pins would stay Hi-Z.
    pio_gpio_init(pio_hw, PIN_RESET);
    pio_gpio_init(pio_hw, PIN_CONV_START);
    pio_sm_init(pio_hw, sm_id, pio_offset, &c);
    // RESET first, CONV_START last: pio_sm_set_consecutive_pindirs leaves
    // the SET base on its last argument, which must match the program's
    // `set pins` group (PIN_CONV_START).
    pio_sm_set_consecutive_pindirs(pio_hw, sm_id, PIN_RESET, 1, true);
    pio_sm_set_consecutive_pindirs(pio_hw, sm_id, PIN_CONV_START, 1, true);

    // 4. Load loop counters into PIO FIFO (Reset count - 1, Pulse count - 1)
    pio_sm_put_blocking(pio_hw, sm_id, sys_cfg.ramp_reset_pio - 1);
    pio_sm_put_blocking(pio_hw, sm_id, sys_cfg.ramp_pulses - 1);

    // 5. Enable PIO State Machine (Runs continuously in hardware with 0 CPU load)
    pio_sm_set_enabled(pio_hw, sm_id, true);

    // Wait until stop command is issued by user via USB
    while (sys_cfg.is_running) {
        tight_loop_contents();
    }

    // Stop PIO and hand the pins back to the SIO with safe idle levels
    pio_sm_set_enabled(pio_hw, sm_id, false);
    gpio_set_function(PIN_RESET, GPIO_FUNC_SIO);
    gpio_set_dir(PIN_RESET, GPIO_OUT);
    gpio_put(PIN_RESET, 0);
    gpio_set_function(PIN_CONV_START, GPIO_FUNC_SIO);
    gpio_set_dir(PIN_CONV_START, GPIO_OUT);
    gpio_put(PIN_CONV_START, 0);

    // Same end-of-stream handshake as the no-ramp engine
    ring_push(STREAM_MARKER);
    __dmb();
    sys_cfg.engine_done = true;
}

// RAMP MODE, SOFTWARE GENERATOR (selected with 'G C'): the same process as
// the PIO program, driven from the timing core with DWT-absolute anchors —
// reset LOW -> settle (2 us fixed, reset HIGH) -> N = int/dt + 1 pulses
// with edges at k*dt (50% duty, like the PIO) -> last pulse's period runs
// out -> restart.  Runs strictly from SRAM with interrupts disabled; the
// only jitter source is bus arbitration from core 0 (single-digit ns,
// non-accumulating: every edge re-syncs to the absolute timeline).
// Resolution is one system clock cycle (6.67 ns at 150 MHz) for dt, reset
// and settle alike — no dt-coupled quantization, no dt upper bound.
void __not_in_flash_func(run_ramp_soft_mode)(void) {
    uint32_t irq_status = save_and_disable_interrupts();

    const uint32_t clk = sys_cfg.clk_hz;
    uint32_t dt_cycles = (uint32_t)((double)sys_cfg.ramp_dt_us * clk / 1.0e6);
    if (dt_cycles == 0) dt_cycles = 1;
    uint32_t half_cycles = dt_cycles / 2;
    if (half_cycles == 0) half_cycles = 1;
    const uint32_t settle_cycles = (uint32_t)(RAMP_SETTLE_SOFT_US * clk / 1.0e6);
    // Pulses: int/dt intervals, edges on BOTH endpoints (cycle-domain floor,
    // exact like everywhere else).  Minimum integration = dt (2 pulses).
    uint32_t intervals = sys_cfg.int_cycles / dt_cycles;
    if (intervals < 1) intervals = 1;
    const uint32_t pulses = intervals + 1;
    sys_cfg.ramp_pulses = pulses;
    sys_cfg.ramp_dt_us = (float)((double)dt_cycles * 1.0e6 / clk);
    const uint32_t reset_mask = 1u << PIN_RESET;
    const uint32_t conv_mask = 1u << PIN_CONV_START;

    uint32_t t = DWT->CYCCNT;
    while (sys_cfg.is_running) {
        // --- PHASE 1: RESET (both lines LOW) ---
        sio_hw->gpio_clr = reset_mask | conv_mask;
        t += sys_cfg.reset_cycles;
        wait_until_cycle(t);

        // --- PHASE 2: SETTLE (reset released, no pulses yet) ---
        sio_hw->gpio_set = reset_mask;
        t += settle_cycles;
        wait_until_cycle(t);

        // --- PHASE 3: PULSE TRAIN (edges at k*dt, 50% duty) ---
        for (uint32_t k = 0; k < pulses && sys_cfg.is_running; k++) {
            sio_hw->gpio_set = conv_mask;
            uint32_t t_low = t + half_cycles;
            t += dt_cycles;
            wait_until_cycle(t_low);
            sio_hw->gpio_clr = conv_mask;
            wait_until_cycle(t);
        }
        // The train spans pulses*dt: the last pulse's low half runs out
        // before the cycle wraps (same structure as the PIO program).
    }

    sio_hw->gpio_clr = reset_mask | conv_mask;
    ring_push(STREAM_MARKER);
    __dmb();
    sys_cfg.engine_done = true;
    restore_interrupts(irq_status);
}

// Timing engine entry point — runs on CORE 1 (main()/USB runs on core 0)
void timing_engine_main(void) {
    enable_cycle_counter();

    while (true) {
        if (!sys_cfg.is_running) {
            tight_loop_contents();
            continue;
        }

        if (sys_cfg.is_ramp_mode) {
            if (sys_cfg.ramp_gen_pio) {
                run_ramp_pio_mode();
            } else {
                run_ramp_soft_mode();
            }
        } else {
            run_noramp_adc_mode();
        }
    }
}

// ============================================================================
// CORE 0: COMMUNICATION & USB SERIAL ENGINE
// ============================================================================

// Wait (bounded) for the engine to be fully idle; returns false on timeout.
static bool engine_wait_idle(uint32_t timeout_ms) {
    absolute_time_t deadline = make_timeout_time_ms(timeout_ms);
    while (!sys_cfg.engine_done) {
        if (time_reached(deadline)) {
            return false;
        }
        tight_loop_contents();
    }
    return true;
}

// Stop the run: stream out everything still pending, up to and including the
// end-of-stream marker, then acknowledge. 'D' = clean stop, 'E' = the engine
// did not finish within the timeout.
static void stop_measurement(void) {
    if (!sys_cfg.is_running && !sys_cfg.run_active) {
        putchar_raw('D');
        fflush(stdout);
        return;
    }
    sys_cfg.is_running = false;

    // The engine notices the stop within one period; the marker follows.
    uint32_t period_ms = (uint32_t)(((uint64_t)sys_cfg.int_cycles +
                                     sys_cfg.reset_cycles) * 1000ull /
                                    sys_cfg.clk_hz) + 200;
    absolute_time_t deadline = make_timeout_time_ms(period_ms);

    bool marker_seen = false;
    uint32_t word;
    while (!time_reached(deadline)) {
        while (ring_pop(&word)) {
            // Forward everything, INCLUDING the marker: the host uses it to
            // detect the clean end of the stream (and to discard any
            // trailing bytes before the ack).
            fwrite(&word, sizeof(uint32_t), 1, stdout);
            if (word == STREAM_MARKER) {
                marker_seen = true;
                break;
            }
        }
        if (marker_seen) {
            break;
        }
        tight_loop_contents();
    }

    // Flush the buffered binary data BEFORE the raw ack byte: putchar_raw
    // bypasses the stdio buffer and would otherwise overtake it.
    fflush(stdout);
    sys_cfg.run_active = false;
    putchar_raw(marker_seen ? 'D' : 'E');
    fflush(stdout);
}

void process_usb_command(char *line) {
    char cmd = line[0];

    // While a measurement is running the endpoint carries raw binary data:
    // any ASCII reply (other than the stop handshake) would corrupt the
    // stream. Ignore everything but 'E'.
    if (sys_cfg.is_running && cmd != 'E') {
        return;
    }

    switch (cmd) {
        case 'V': { // Identity: firmware, unique board id, system clock
            char id_str[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
            pico_get_unique_board_id_string(id_str, sizeof(id_str));
            printf("V %s %s %s %lu\n", FW_NAME, FW_VERSION, id_str,
                   (unsigned long)(sys_cfg.clk_hz / 1000000));
            fflush(stdout);
            break;
        }

        case 'Q': { // Status / diagnostics (actual quantized timing, drops)
            // caps/event are read back from the PADS (gpio_get), not from a
            // stored copy: a line dragged low by the outside world shows up
            // here as 0 even after the command that set it.
            double clk = (double)sys_cfg.clk_hz;
            double int_us = (double)sys_cfg.int_cycles * 1e6 / clk;
            double reset_us = (double)sys_cfg.reset_cycles * 1e6 / clk;
            if (sys_cfg.is_ramp_mode && sys_cfg.ramp_gen_pio) {
                // what the PIO generator will actually do (dt/10-quantized)
                double div_actual = (double)sys_cfg.ramp_div_int +
                                    (double)sys_cfg.ramp_div_frac8 / 256.0;
                int_us = (double)(sys_cfg.ramp_pulses - 1) * 10.0 * div_actual * 1e6 / clk;
                reset_us = (double)sys_cfg.ramp_reset_pio * div_actual * 1e6 / clk;
            } else if (sys_cfg.is_ramp_mode) {
                // software generator: pulses-derived int, cycle-floored dt
                double dt_us = (double)((uint32_t)((double)sys_cfg.ramp_dt_us * clk / 1.0e6)) * 1e6 / clk;
                int_us = (double)(sys_cfg.ramp_pulses - 1) * dt_us;
            }
            printf("Q run=%d mode=%s gen=%s int=%.3f reset=%.3f dt=%.3f div=%lu.%03lu pulses=%lu caps=%d%d%d event=%s dropped=%lu\n",
                   sys_cfg.is_running ? 1 : 0,
                   sys_cfg.is_ramp_mode ? "ramp" : "noramp",
                   sys_cfg.ramp_gen_pio ? "pio" : "c",
                   int_us,
                   reset_us,
                   (double)sys_cfg.ramp_dt_us,
                   (unsigned long)sys_cfg.ramp_div_int,
                   (unsigned long)sys_cfg.ramp_div_frac8,
                   (unsigned long)sys_cfg.ramp_pulses,
                   gpio_get(PIN_CAP_0), gpio_get(PIN_CAP_1), gpio_get(PIN_CAP_2),
                   gpio_get(PIN_EVENT_MODE) ? "SE" : "ME",
                   (unsigned long)dropped_words);
            fflush(stdout);
            break;
        }

        case 'R': // Set Event Mode (RS = Single, RM = Multi)
            if (line[1] == 'S') {
                gpio_put(PIN_EVENT_MODE, 1);
                putchar_raw('D');
            } else if (line[1] == 'M') {
                gpio_put(PIN_EVENT_MODE, 0);
                putchar_raw('D');
            } else {
                putchar_raw('E');
            }
            fflush(stdout);
            break;

        case 'C': // Set Capacitor/Gain bits (e.g., C101)
            // Validate that we received exactly three '0' or '1' characters
            if ((line[1] == '0' || line[1] == '1') &&
                (line[2] == '0' || line[2] == '1') &&
                (line[3] == '0' || line[3] == '1')) {

                gpio_put(PIN_CAP_0, line[1] == '1' ? 1 : 0);
                gpio_put(PIN_CAP_1, line[2] == '1' ? 1 : 0);
                gpio_put(PIN_CAP_2, line[3] == '1' ? 1 : 0);

                putchar_raw('D');
            } else {
                putchar_raw('E'); // Error: Invalid format
            }
            fflush(stdout);
            break;

        case 'X': // Mode selection: XR (Ramp/PIO) or XN (No-Ramp/ADC)
            if (line[1] == 'R') {
                sys_cfg.is_ramp_mode = true;
                putchar_raw('D');
            } else if (line[1] == 'N') {
                sys_cfg.is_ramp_mode = false;
                putchar_raw('D');
            } else {
                putchar_raw('E');
            }
            fflush(stdout);
            break;

        case 'T': { // Timing configuration: T <int_us>,<reset_us>
            // Validated against the same limits the host driver enforces;
            // NaN/inf fail the range test (all comparisons false).  The
            // cycles are floored in DOUBLE precision so the quantization
            // matches the host's floor(us * 150) exactly (float32 products
            // drift at large values).
            float int_us = 0.0f, reset_us = 0.0f;
            if (sscanf(line + 1, "%f,%f", &int_us, &reset_us) == 2 &&
                int_us >= INT_TIME_MIN_US && int_us <= TIME_MAX_US &&
                reset_us >= RESET_TIME_MIN_US && reset_us <= TIME_MAX_US) {
                double clk_mhz = (double)sys_cfg.clk_hz / 1e6;
                sys_cfg.int_cycles = (uint32_t)((double)int_us * clk_mhz);      // floor
                sys_cfg.reset_cycles = (uint32_t)((double)reset_us * clk_mhz);  // floor
                recompute_ramp_params();
                putchar_raw('D');
            } else {
                putchar_raw('E');
            }
            fflush(stdout);
            break;
        }

        case 'P': { // Ramp step parameter: P <ramp_dt_us> (software generator)
            // The app only sends 'P' after 'GC': the software engine drives
            // both pins from the timing core with one-clock-cycle
            // resolution, so dt is bounded by the general time range only
            // (as of 2.1 — 2.0 wrongly applied the PIO divider bound here).
            // In PIO mode the 16.8 clock divider still holds (dt <=
            // 65536 * 10 sys-clocks): 'O' programs that engine, but keep
            // manual use honest too.
            float dt_us = 0.0f;
            float dt_max_us = (sys_cfg.ramp_gen_pio)
                                  ? 65536.0f * 10.0e6f / (float)sys_cfg.clk_hz
                                  : TIME_MAX_US;
            if (sscanf(line + 1, "%f", &dt_us) == 1 &&
                dt_us > 0.0f && dt_us <= dt_max_us) {
                sys_cfg.ramp_dt_us = dt_us;
                recompute_ramp_params();
                putchar_raw('D');
            } else {
                putchar_raw('E');
            }
            fflush(stdout);
            break;
        }

        case 'O': { // Raw ramp-generator programming (used by the app):
            // O <div_int>,<div_frac8>,<reset_pio>,<pulses>
            // The host computes everything with its own exact arithmetic and
            // the firmware programs exactly these numbers — no firmware-side
            // rounding can disagree with what the host previewed.  The
            // effective timing fields are refreshed so Q stays truthful.
            // NOTE: the app-side math still follows the OLD process (reset
            // gap compensation); it will be updated once the PIO-vs-C
            // generator comparison (see 'G') is decided.
            unsigned long di = 0, fr = 0, rp = 0, pl = 0;
            if (sscanf(line + 1, "%lu,%lu,%lu,%lu", &di, &fr, &rp, &pl) == 4 &&
                di >= 1 && di <= 65535 && fr <= 255 && rp >= 1 && pl >= 1) {
                sys_cfg.ramp_div_int = (uint32_t)di;
                sys_cfg.ramp_div_frac8 = (uint32_t)fr;
                sys_cfg.ramp_reset_pio = (uint32_t)rp;
                sys_cfg.ramp_pulses = (uint32_t)pl;
                double clk = (double)sys_cfg.clk_hz;
                double div_actual = (double)di + (double)fr / 256.0;
                sys_cfg.ramp_dt_us = (float)(div_actual * 10.0e6 / clk);
                sys_cfg.int_cycles = (uint32_t)((double)(pl - 1) * 10.0 * div_actual);
                sys_cfg.reset_cycles = (uint32_t)((double)rp * div_actual);
                putchar_raw('D');
            } else {
                putchar_raw('E');
            }
            fflush(stdout);
            break;
        }

        case 'G': // Ramp generator selection: GP = PIO hardware, GC = software
            if (line[1] == 'P') {
                sys_cfg.ramp_gen_pio = true;
                putchar_raw('D');
            } else if (line[1] == 'C') {
                sys_cfg.ramp_gen_pio = false;
                putchar_raw('D');
            } else {
                putchar_raw('E');
            }
            fflush(stdout);
            break;

        case 'S': // Start Measurement
            // 1. Make sure a previous run fully finished (E already waited
            //    for the marker; this is a defensive bounded wait).
            if (!engine_wait_idle(1000)) {
                putchar_raw('E');
                fflush(stdout);
                break;
            }
            // 2. Clear stale data and the drop counter
            ring_reset();
            // 3. Flush any lingering bytes, THEN acknowledge and start:
            //    the host begins reading a clean stream right after the 'D'.
            fflush(stdout);
            putchar_raw('D');
            fflush(stdout);
            sys_cfg.run_active = true;
            __compiler_memory_barrier();
            sys_cfg.engine_done = false;
            __compiler_memory_barrier();
            sys_cfg.is_running = true;
            break;

        case 'E': // Stop Measurement
            stop_measurement();
            break;

        default:
            putchar_raw('?');
            fflush(stdout);
            break;
    }
}

int main(void) {
    // Initialize standard SDK I/O (USB CDC)
    stdio_init_all();

    // Read the REAL system clock once: every µs<->cycle conversion and the
    // identity string use it, so a different clock config cannot silently
    // rescale all timings.
    sys_cfg.clk_hz = clock_get_hz(clk_sys);
    float clk_mhz = (float)sys_cfg.clk_hz / 1e6f;
    sys_cfg.int_cycles = (uint32_t)(DEFAULT_INT_US * clk_mhz);
    sys_cfg.reset_cycles = (uint32_t)(DEFAULT_RESET_US * clk_mhz);

    // Initialize timing loop GPIOs (SIO function, safe idle levels)
    gpio_init(PIN_RESET);
    gpio_set_dir(PIN_RESET, GPIO_OUT);
    gpio_put(PIN_RESET, 0);

    gpio_init(PIN_CONV_START);
    gpio_set_dir(PIN_CONV_START, GPIO_OUT);
    gpio_put(PIN_CONV_START, 0);

    // Initialize static configuration GPIOs
    gpio_init(PIN_EVENT_MODE); gpio_set_dir(PIN_EVENT_MODE, GPIO_OUT); gpio_put(PIN_EVENT_MODE, 0);
    gpio_init(PIN_CAP_0);      gpio_set_dir(PIN_CAP_0, GPIO_OUT);      gpio_put(PIN_CAP_0, 0);
    gpio_init(PIN_CAP_1);      gpio_set_dir(PIN_CAP_1, GPIO_OUT);      gpio_put(PIN_CAP_1, 0);
    gpio_init(PIN_CAP_2);      gpio_set_dir(PIN_CAP_2, GPIO_OUT);      gpio_put(PIN_CAP_2, 0);

    // Initialize ADC
    adc_init();
    adc_gpio_init(ADC_GPIO_PIN);
    adc_select_input(ADC_CHANNEL);

    // Load the PIO program into instruction memory only: the pins are
    // claimed for the PIO when (and only while) ramp mode runs.
    pio_offset = pio_add_program(pio_hw, &ramp_control_program);

    // Launch the timing engine on CORE 1 (main()/USB stays on core 0)
    multicore_launch_core1(timing_engine_main);

    // Main loop (core 0): USB CDC command parsing + binary data streaming
    char rx_buffer[64];
    uint rx_pos = 0;

    while (true) {
        // 1. Non-blocking check for incoming serial commands from PC
        int c = getchar_timeout_us(0);
        if (c != PICO_ERROR_TIMEOUT) {
            if (c == '\n' || c == '\r') {
                if (rx_pos > 0) {
                    rx_buffer[rx_pos] = '\0';
                    process_usb_command(rx_buffer);
                    rx_pos = 0;
                }
            } else if (rx_pos < sizeof(rx_buffer) - 1) {
                rx_buffer[rx_pos++] = (char)c;
            }
        }

        // 2. Stream ADC data over USB as RAW BINARY, in small batches so
        //    command parsing keeps running between them.
        if (sys_cfg.is_running && !sys_cfg.is_ramp_mode) {
            uint32_t batch[32];
            uint32_t word;
            int n = 0;
            while (n < (int)(sizeof(batch) / sizeof(batch[0])) && ring_pop(&word)) {
                if (word != STREAM_MARKER) {
                    batch[n++] = word;
                }
            }
            if (n > 0) {
                fwrite(batch, sizeof(uint32_t), n, stdout);
                // The data are flushed automatically by the USB driver
            }
        }
    }

    return 0;
}
