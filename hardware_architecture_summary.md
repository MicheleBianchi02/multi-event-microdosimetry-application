# Hardware Architecture & Interface Summary

This document summarizes the recent architectural changes made to the application, specifically focusing on the hardware interfaces, gain configurations, UI updates, and device bug fixes. It serves as a checkpoint to easily resume work later.

## 1. Hardware Abstraction & Roles
The monolithic DAQ architecture was refactored into a modular, interface-driven system.
* **Roles:** The application now splits hardware responsibilities into four distinct roles: `acquisition`, `timing`, `gain_ch1`, and `gain_ch2`.
* **JSON Configuration:** Devices and their assigned roles are defined in `hardware_config.json`. This allows the same physical device (like the Pico) to fulfill multiple roles, or splits roles across different devices.
* **Auto-Connection:** We implemented persistent Linux `udev` rules (e.g., `/dev/pico_daq`) so the system connects to known hardware automatically without user intervention, while falling back to a manual mapping screen (`SettingWindow`) if devices aren't found.
* **Interfaces defined:** 
  * `IDataAcquisition`: Handles starting/stopping streams and reading data.
  * `ITimingController`: Handles Start/Stop timing, Ramp timing, and calculating physical hardware limits.
  * `IGainController`: Handles calculating and applying GPIO switch permutations for capacitor banks.

## 2. Gain & Capacitance Workflow
* **UI Relocation:** Gain selection was moved out of the live `RunningWindow` and into the setup phase (`TimeWindow`). Gains are now correctly configured and sent to the hardware *before* the measurement begins.
* **Dynamic Gain Math:** `HardwareManager` parses `hardware_config.json` (which defines `fixed_capacitor_pF` and `switchable_capacitors`) to compute all valid physical combinations. 
* **Session Storage:** When a user selects a gain (e.g., `10x`), the actual physical capacitance (e.g., `100pF`) is retrieved and saved in the global `SessionModel`. All subsequent data scaling and plot rendering dynamically rely on this physical value.

## 3. Raspberry Pi Pico Integration & Bug Fixes
* **Ramp Sampling Support:** 
  * The `ITimingController.configure_ramp` signature was updated to send `ramp_dt`, `int_time`, and `reset_time` all at once.
  * The Pico driver was updated to report `supports_ramp_sampling = True`. When selected in the UI, the Pico successfully receives the `XR` (Ramp Mode), `T` (Integration/Reset), and `P` (Ramp dt) commands.
  * We verified against the Pico C firmware that in Ramp Mode (PIO State Machine), the Pico generates the exact timing triggers needed but purposely does *not* read ADC samples.
* **Critical Gain Pin Bug Fix:** 
  * *The Issue:* The Pico accepts a single global capacitor command (`C<c0><c1><c2>`). Because CH1 has 3 switchable capacitors and CH2 has 1, configuring CH2 immediately after CH1 was incorrectly mapping its bit to `c0` and zeroing out `c1` and `c2`. This caused Pin 17 to stay indefinitely ON and Pins 18/19 to stay OFF.
  * *The Fix:* Implemented a state-caching array (`_cap_state`) in `PicoSerialDAQ`. It dynamically allocates the available physical pins. CH1 consumes the first 3 bits, and CH2 safely skips overwriting them.

## 4. Next Steps & Future Work
When resuming work, the following tasks are up next:

1. **Dynamic Hardware Configuration (`setup.json`):**
   * **Goal:** Automate the generation and configuration of `hardware_config.json`.
   * **Description:** A new `setup.json` file will be introduced to act as the master configuration blueprint. It will explicitly map which physical switchable capacitors (values in pF) are physically wired to which specific microcontroller pins across different channels. 
   * **Impact:** This allows the software to dynamically understand the physical routing of the hardware and automatically select the correct hardware interface controllers and pin masks, removing the need for hardcoded or manual hardware JSON generation.

2. **National Instruments (NI) ADC Implementation:**
   * Since the Pico can generate the ramp timing but cannot read the ramp samples, the next major step is to implement `IDataAcquisition` for the NI device.
   * This device will be registered dynamically via the new configuration flow to handle the `acquisition` role, while the Pico continues to serve the `timing` role.

3. **Hardware Validation:**
   * Probe the Pico's `PIN_CONV_START` (Pin 15) and `PIN_RESET` (Pin 14) with an oscilloscope to verify the exact pulse widths and frequencies match the GUI inputs when using the new Ramp configuration.
