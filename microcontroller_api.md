# Raspberry Pi Pico 2 - DAQ Firmware API Protocol

This document outlines the ASCII serial communication protocol used to control the Raspberry Pi Pico 2 DAQ firmware over USB CDC (Virtual COM Port).

## 1. Hardware Pin Definitions
* **PIN_RESET (GPIO 14):** Integrator Reset Line.
* **PIN_CONV_START (GPIO 15):** Ramp Mode External Trigger.
* **PIN_EVENT_MODE (GPIO 16):** Event Mode State (High/Low).
* **PIN_CAP_0 (GPIO 17):** Capacitor Bank Bit 0.
* **PIN_CAP_1 (GPIO 18):** Capacitor Bank Bit 1.
* **PIN_CAP_2 (GPIO 19):** Capacitor Bank Bit 2.
* **ADC_INPUT (GPIO 26):** Internal ADC Input.

## 2. Command Protocol
All commands are sent as ASCII strings. Standard configuration commands reply with `D` (Done) or `E` (Error). 

### V - Verify Connection
* **Sent:** `V`
* **Response:** `V`
* **Description:** Used to verify the device is alive and communicating.

### R - Set Event Mode
* **Sent:** `RS` (Single) or `RM` (Multi)
* **Response:** `D` or `E`
* **Description:** Changes the static state of `PIN_EVENT_MODE`. `RS` drives the pin HIGH, `RM` drives it LOW.

### C - Set Capacitor/Gain Bits
* **Sent:** `C` followed by exactly three binary digits (e.g., `C101`)
* **Response:** `D` or `E`
* **Description:** Sets the state of `PIN_CAP_0`, `PIN_CAP_1`, and `PIN_CAP_2` respectively. `1` = HIGH, `0` = LOW.

### X - Set Operation Mode
* **Sent:** `XR` (Ramp Mode) or `XN` (No-Ramp Mode)
* **Response:** `D` or `E`
* **Description:** 
  * `XR` selects PIO-driven waveform generation (No ADC reading).
  * `XN` selects DWT-cycle-driven ADC sampling.

### T - Configure Timing (No-Ramp Mode)
* **Sent:** `T <integration_time_us>,<reset_time_us>\n` (e.g., `T 30.0,5.0\n`)
* **Response:** `D` or `E`
* **Description:** Sets the timing parameters in microseconds. The firmware automatically converts these into 150MHz CPU cycles.

### P - Configure Ramp Step (Ramp Mode)
* **Sent:** `P <ramp_dt_us>\n` (e.g., `P 1.0\n`)
* **Response:** `D` or `E`
* **Description:** Sets the width of the conversion trigger pulse in microseconds. This dynamically updates the PIO clock divider.

### S - Start Measurement
* **Sent:** `S`
* **Response:** `D`
* **Description:** Arms the active flag. 
  * In No-Ramp mode, the device will immediately begin streaming ADC data formatted as `sample1,sample2\n` over the serial port.
  * In Ramp mode, the device will initialize and start the PIO state machine.

### E - Stop Measurement
* **Sent:** `E`
* **Response:** `D`
* **Description:** Halts all execution, disables PIO state machines, and returns all control GPIOs (Reset and Conv_Start) to a safe LOW state.

## 3. Data Streaming Format
When started in **No-Ramp Mode (XN)**, the Pico streams data asynchronously as ASCII strings over the serial port.
* **Format:** `[uint16],[uint16]\n`
* **Example:** `2048,3192\n`
