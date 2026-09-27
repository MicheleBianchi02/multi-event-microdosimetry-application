;*******************************************************;
;    Filename: 						;
;    Date:						;
;    File Version:					;
;    							;
;    Author:						;
;    Company:						;
;    							;
;*******************************************************;
;							;
;    Files required: P18F8723.INC,  188723_g.lkr	;
;							;
;*******************************************************;

	LIST P=18F8723, F=INHX32
	#include <P18F8723.INC>

	#define measure_flag		    flags, 0
	#define type_measure		    flags, 1  ; clear = start, set = stop
	#define discard_measure_ch1 	flags, 2  ; set = discard, clear = don't
					                          ; used when an interrupt occur while measuring
	#define discard_measure_ch2 	flags, 3  ; set = discard, clear = don't
	#define mode_flag		        flags, 4  ; set = debug mode, clear = production mode   
    #define double_ch_config        flags, 5  ; set = 2 ch mode, clear = single ch configuration

	#define reset_pin	    LATD, LATD7
    #define reset_pin_2     LATD, LATD1

    #define event_mode_pin  LATD, LATD5     ; change between single event and multi event

	#define signal_start	LATD, LATD0

	; used for debug
	#define ch1_pin		LATD, LATD4
	#define change_gain_pin	 LATD, LATD6    ; change integration capacitor (ie gain)
	
	EXTERN 	temp_wr, d_write
	EXTERN 	LCDLine_1, LCDLine_2, LCDInit

SelectChannel2 MACRO
	; CHS3:CHS0 = bit 5-2
	; CHS3:CHS0 bits of ADCON0 become 0110  (AN6 Analog channel)
	; starting from 1010 (AN10)
    ; AN6 = RF1
	BCF 	ADCON0, 5
	BSF 	ADCON0, 4
	
	ENDM
	

SelectChannel1 MACRO
	; CHS3:CHS0 bits of ADCON0 become 1010  (AN10 Analog channel)
	; starting from 0110 (AN6)
    ; AN10 = RF5
	BSF 	ADCON0, 5
	BCF	    ADCON0, 4
	ENDM
	


;**************************
;Configuration bits

;   Oscillator Selection:
	CONFIG	OSC = HSPLL             ; clock frequency 40Mhz (10MHzx4 con PLL)
	CONFIG	WDT = OFF               ; required by the PICKIT3 debugger
	CONFIG 	LVP = OFF
	CONFIG 	PWRT = OFF
	CONFIG 	LPT1OSC = OFF    	; disable low-power mode for timer oscillator

;******************************************************************************
; Access memeory

		UDATA_ACS

WREG_TEMP	RES	1	; used when receving data

flags 		RES	1

counter		RES	1     	; used when generating delay for the first reset

start_measure_ch1 	RES	2
stop_measure_ch1 	RES	2
measure_ch1		RES	2

start_measure_ch2 	RES	2
stop_measure_ch2 	RES	2
measure_ch2		RES	2

; LCD variables
ptr_pos		RES 	1  
ptr_count	RES 	1

; Delay varaibles (for the LCD)
temp_1		RES 	1
temp_2		RES 	1
temp_3		RES 	1


; Timer 
TMR1U		RES	1  ; new timer register
timerU_int	RES	1  ; control register
timerH_int	RES	1  ; control register
timerL_int	RES	1  ; control register
timerH_reset	RES	1  ; control register
timerL_reset	RES	1  ; control register

;******************************************************************************
; With UDATA the location is decided by the assembler. If the reserved dimension 
; exceed the bank dimension (256 bytes), an error will be raised (it doesn't go 
; to the next bank automatically). Since a bank will be used, to access the variable,
; the BSR should be used. A more easy way is to use a pointer like FSRx 


;**************************
;EEPROM data

;DATA_EEPROM	CODE	0xf00000

		;DE	"Test Data",0,1,2,3,4,5


;**************************
RESET_VECTOR	CODE	0x0000

		GOTO	start

;**************************
HI_INT_VECTOR	CODE	0x0008

		GOTO	HighInt

;**************************
LOW_INT_VECTOR	CODE	0x0018

		GOTO	LowInt

;**************************

Main		CODE

stan_table	; table for standard code
	;       "XXXXXXXXXXXXXXXX"      ; ptr
	data	"Microchip PICDEM"      ; 0
	data	" PIC18 Explorer "	; 16

	data	"                "	; 32

	data 	"     READY      "	; 48

	data 	"   PROCESSING   "	; 64

    data    "     ARMED      "  ; 80
	

;**************************
;High priority interrupt routine
HighInt:

	; Skip if the interrupt came from the timer
	BTFSS 	PIR1, TMR1IF
	GOTO 	isr_rx

	; skip ISR if it's not measuring
	BTFSS  	measure_flag
	GOTO 	iret_timer
	
	; --- ISR for the timer ---
isr_timer

	BTFSC 	type_measure
	BRA 	stop_type

start_type
	; --- start of integration ---

	BCF 	reset_pin
	BSF 	reset_pin_2

    ; NOP to add small delay (skip transient)
    NOP
    NOP
    
    ; BTFSC will already compensate the INCFSZ and BRA at the end of integration

    BTFSC   double_ch_config
	BSF 	ADCON0, GO	; start conversion

	CALL 	delay_5us
	CALL 	delay_5us
	CALL 	delay_1us
	CALL 	SetIntTimer	; this takes 1 us


	SelectChannel2
	; save result
    ; In single channel mode these data will be saved later
	MOVFF 	ADRESL, start_measure_ch1
	MOVFF 	ADRESH, start_measure_ch1+1
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	
	CALL delay_1us
	
	BSF 	ADCON0, GO ; start conversion
	CALL 	wait_adc_conv
	; conversion takes 11.2 us max. Considering polling and RETURN we can assume 
	; a maximum of 11.5 us
	

	SelectChannel1

	; save result
	MOVFF 	ADRESL, start_measure_ch2
	MOVFF 	ADRESH, start_measure_ch2+1

	BSF 	type_measure  ; set to stop
	
	GOTO 	iret_timer



stop_type
	; --- end of integration ---

	; Prescaler. Works as the timer registers TMR1x. At each interrupt, when 
	; integrating, TMR1U is incremented. When the overflow occur we continue
	; the measure. TMR1U is loaded as TMR1* (eg if no increment is required, 
	; TMR1U, and so timerU_int must be 1111 1111)
	INCFSZ 	TMR1U
	GOTO 	iret_timer

    ; NOP to compensate the three NOP at the start of integration
    NOP
    NOP
    NOP
	

    BTFSC   double_ch_config
	BSF 	ADCON0, GO	; start conversion

	CALL 	delay_5us
	CALL 	delay_5us
	CALL 	delay_1us
	CALL 	delay_1us
	
	SelectChannel2
    ; In single channel mode these data will be saved later
	MOVFF 	ADRESL, stop_measure_ch1
	MOVFF 	ADRESH, stop_measure_ch1+1
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	
	CALL delay_1us
	
	BSF 	ADCON0, GO ; start conversion
	
	CALL 	wait_adc_conv 
	
	SelectChannel1

	; save result
	MOVFF 	ADRESL, stop_measure_ch2
	MOVFF 	ADRESH, stop_measure_ch2+1

	BCF 	type_measure  ; set to start
	
	BSF 	reset_pin
	BCF 	reset_pin_2
	

	; --- overflow check ---
	
	; If the value given by the adc is the last bin (maximum) we assume that
	; there was an overflow (i.e. the input tension is bigger than the maximum 
	; detectable tension)
	
	; ========= Channel 1 ============
	MOVLW 	0x0F   ; b'0000 1111'
	CPFSEQ 	stop_measure_ch1+1
	BRA 	check_ch2_over  ; skip if they are not equal

	MOVLW 	0xFF   ; b'1111 1111'
	CPFSEQ 	stop_measure_ch1
	BRA 	check_ch2_over  ; skip if they are not equal

	BSF 	discard_measure_ch1

check_ch2_over
	; ========= Channel 2 ============

	MOVLW 	0x0F 
	CPFSEQ 	stop_measure_ch2+1
	BRA 	calc_diff

	MOVLW 	0xFF
	CPFSEQ 	stop_measure_ch2
	BRA 	calc_diff

	BSF 	discard_measure_ch2
	
	
calc_diff

	; ========= Channel 1 ============
	; --- Calculate the difference ---
	
	MOVF 	start_measure_ch1, W
	SUBWF 	stop_measure_ch1, W  ; result placed in W
	MOVWF 	measure_ch1

	MOVF 	start_measure_ch1+1, W
	SUBWFB 	stop_measure_ch1+1, W  ; result placed in W
	MOVWF 	measure_ch1+1
	
	; --- negative check ---
	
	BTFSC 	STATUS, N
	BSF 	discard_measure_ch1


	; ========= Channel 2 ============
	; --- Calculate the difference ---
	
	MOVF 	start_measure_ch2, W
	SUBWF 	stop_measure_ch2, W
	MOVWF 	measure_ch2

	MOVF 	start_measure_ch2+1, W
	SUBWFB 	stop_measure_ch2+1, W
	MOVWF 	measure_ch2+1
	
	; --- negative check ---
	
	BTFSC 	STATUS, N
	BSF 	discard_measure_ch2
	
	
	; --------------------
	; Data is sent in the ISR because the other interrupt are disabled when 
	; processing this ISR. It means that interrupt from the serial port (received
	; byte) will wait that this process ends, ie all data are sent through the
	; serial port to the PC. Sending data from the main code (outside the ISR) would
	; create some problem if the function for reading data (ReadData) need to 
	; send back byte (like the 'D' or 'V' code).
	
	; don't send if an interrupt occured while measuring since another interrupt
	; must wait that this ISR end, giving wrong values on the elapsed time. Also 
	; discard if the calculated value was negative or an adc overflow occured.
	
	; When a measure need to be discarded, we send the value 2^15, that is not 
	; reachable by the integrated adc
	BTFSS 	discard_measure_ch1  ; set = discard the measure
	BRA 	check_ch2

	CLRF 	measure_ch1   ; 0x00
	MOVLW 	b'10000000'   ; 0x80
	MOVWF 	measure_ch1+1

	; CLRF    stop_measure_ch1  ; 0x00
	; MOVLW   b'10000000'      ; 0x80
	; MOVWF   stop_measure_ch1+1
	

check_ch2
	BTFSS 	discard_measure_ch2  ; set = discard the measure
	BRA 	send_measure

	CLRF 	measure_ch2
	MOVLW 	b'10000000'  ; 0x80
	MOVWF 	measure_ch2+1

	; CLRF    stop_measure_ch2
	; MOVLW   b'10000000'  ; 0x80
	; MOVWF   stop_measure_ch2+1

	
send_measure
	
	CALL 	SendData
	
	BCF 	discard_measure_ch1
	BCF 	discard_measure_ch2

	; 20 us delay
	CALL 	delay_5us
	CALL 	delay_5us
	; CALL 	delay_5us
	; CALL 	delay_5us
	
	CALL 	SetResetTimer

	BRA 	iret_timer
	
	
isr_rx
	; --- ISR receive ---
	
	; Reading received byte is done inside the ISR so no other interrupt can 
	; occur while inside here
	CALL 	ReadData

	BRA 	iret

iret_timer
	; --- Return from timer interrupt ---
	
	BCF 	PIR1, TMR1IF  ; wait for another interrupt from the timer
	
	; --- Return from interrup ---
iret
	RETFIE	FAST

;**************************
;Low priority interrupt routine

LowInt:
		RETFIE

;************************** ROUTINES ******************************
;******************************************************************
;******************************************************************


SetIntTimer:

	MOVFF 	timerU_int, TMR1U
	MOVFF 	timerH_int, TMR1H
	MOVFF 	timerL_int, TMR1L

	RETURN

SetResetTimer:
	
	MOVFF 	timerH_reset, TMR1H
	MOVFF 	timerL_reset, TMR1L

	RETURN

; ---- Read data received in the serial ---------------------
ReadData:

	; Since this function is called from inside the ISR, other interrupt are 
	; disabled. So clearing the rx flag and waiting new bytes is safe
	
	; If the first byte is received while the SendData is being processed, 
	; there may be a problem since before calling this function, we have to wait 
	; that the ISR finish and a new one is entered (to process rx interrupt). So, 
	; if we receive more than 2 byte without reading, one get overwritten. This is 
	; a problem since in that case we may miss some config data (for the timer). 
	; One workaround could be to send a response to the PC (like W) and then let 
	; him send the data only when that specific byte is received

	
	MOVFF 	RCREG1, WREG_TEMP  ; empty the rx register. Clear the flag

	
	MOVLW 	A'S'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	start_byte

	MOVLW 	A'E'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	stop_byte

    MOVLW   A'A'
    SUBWF   WREG_TEMP, W
    BTFSC   STATUS, Z
    BRA     arm_byte

    MOVLW   A'R'
    SUBWF   WREG_TEMP, W
    BTFSC   STATUS, Z
    BRA     change_event_mode

    MOVLW   A'N'
    SUBWF   WREG_TEMP, W
    BTFSC   STATUS, Z
    BRA     num_ch_config
    
    MOVLW   A'C'
    SUBWF   WREG_TEMP, W
    BTFSC   STATUS, Z
    BRA     change_gain
	
	MOVLW 	A'T'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	timer_config_rx

	MOVLW 	A'V'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	verify_byte
	
	MOVLW 	A'M'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	set_mode
	
	; We should not get here. It would happen if the received byte is not on the 
	; list above
	BRA 	end_rx
	
timer_config_rx
	; --- Timer config ---
	
	
	; Send the 'W' code and wait that the PC send the other 4 bytes
	CALL 	wait_tx_empty
	MOVLW 	'W'
	MOVWF 	TXREG1
	
	; wait for the 5 bytes
	CALL 	wait_rx_full
	MOVF 	RCREG1, W
	MOVWF 	timerU_int

	CALL 	wait_rx_full
	MOVF 	RCREG1, W
	MOVWF 	timerH_int
	
	; Since at most 2 byte can be received by the serial without reading, 
	; for safety reason, the sender, should put a small delay here before sending
	; the other two bytes. Since we are reading RCREG1 this should not be a problem, 
	; it is only for additional safety.

	CALL 	wait_rx_full
	MOVF 	RCREG1, W
	MOVWF 	timerL_int

	
	CALL 	wait_rx_full
	MOVF 	RCREG1, W
	MOVWF 	timerH_reset
	
	CALL 	wait_rx_full
	MOVF 	RCREG1, W
	MOVWF 	timerL_reset


	; The measure is discarded if an interrupt occur while measuring
	BTFSS	measure_flag
	BRA	responde_timer

	BSF	discard_measure_ch1
	BSF 	discard_measure_ch2

responde_timer
	; Send the 'D' code (DONE) 
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1
	
	RETURN


arm_byte
    
	; The measure is discarded if an interrupt occur while measuring
	BTFSC 	measure_flag
	BRA 	end_rx
    
	BSF 	reset_pin
	BCF 	reset_pin_2
    
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	; Display: armed
	MOVLW	.80
	MOVWF	ptr_pos
	CALL	stan_char_1

	MOVLW	.32  ; empty line
	MOVWF	ptr_pos
	CALL	stan_char_2

    
    RETURN

num_ch_config
    
	; The measure is discarded if an interrupt occur while measuring
	BTFSC 	measure_flag
	BRA 	end_rx

	; wait for the config
	CALL 	wait_rx_full
	MOVFF 	RCREG1, WREG_TEMP

	MOVLW 	A'Y'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
    BRA     set_double_ch_mode

	MOVLW 	A'N'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
    BRA     set_single_ch_mode
    
	; wrong code
	BRA 	end_rx

set_double_ch_mode
    
    BSF     double_ch_config

	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	
    RETURN
    
set_single_ch_mode
    
    BCF     double_ch_config
    
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	
    RETURN


change_event_mode
    
    ; Stop the measure before changing the event mode
	BTFSC 	measure_flag
	BRA 	end_rx

	; wait for the config
	CALL 	wait_rx_full
	MOVFF 	RCREG1, WREG_TEMP

	MOVLW 	A'S'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
    BRA     single_event_mode

	MOVLW 	A'M'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
    BRA     multi_event_mode
    
	; wrong code
	BRA 	end_rx

single_event_mode
    
    BSF     event_mode_pin

	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	
    RETURN

multi_event_mode
    
    BCF     event_mode_pin

	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	
    RETURN


change_gain
    
	; The measure is discarded if an interrupt occur while measuring
	BTFSC 	measure_flag
	BRA 	end_rx

	; wait for the config
	CALL 	wait_rx_full
	MOVFF 	RCREG1, WREG_TEMP

	MOVLW 	A'Y'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
    BRA     increase_gain

	MOVLW 	A'N'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
    BRA     decrease_gain
    
	; wrong code
	BRA 	end_rx

increase_gain
    
    BSF     change_gain_pin

	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	
    RETURN
    
decrease_gain
    
    BCF     change_gain_pin
    
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	
    RETURN

start_byte
	; --- Start measure ---
	

	; The measure is discarded if an interrupt occur while measuring
	BTFSC 	measure_flag
	BRA 	end_rx

	; Display: processing
	MOVLW	.64
	MOVWF	ptr_pos
	CALL	stan_char_1

	MOVLW	.32  ; empty line
	MOVWF	ptr_pos
	CALL	stan_char_2
	
	; Send the 'D' code (DONE) 
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	BSF 	signal_start  ; turn on led when the program is running
	
	BSF 	measure_flag
	BCF 	discard_measure_ch1
	BCF 	discard_measure_ch2
	; The function below must be called at the end of the ISR since we are turning
	; on the timer. So, we should be out of the ISR as fast as possible
	CALL 	FirstMeasure

	; At the moment the timer is already working, but the 16 bit buffer is not
	; being filled. 
	BSF 	T1CON, TMR1ON   ; start filling the tmr1 registers

	RETURN


stop_byte
	; --- Stop measure ---
	
	BCF 	T1CON, TMR1ON   ; stop filling the timer's register (stop interrupt from it)
	
	; Even if an interrupt occur because the flag was set before disabling the timer,
	; by clearing the measure flag, me are sure that the next ISR (caused by the 
	; timer) will not return any data.
	BCF 	measure_flag
	BCF 	reset_pin  
	BCF 	reset_pin_2

	; Send the 'D' code (DONE) 
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	BCF 	signal_start  ; turn of the led 

	; Display: ready
	MOVLW	.48
	MOVWF	ptr_pos
	CALL	stan_char_1

	MOVLW	.32  ; empty line
	MOVWF	ptr_pos
	CALL	stan_char_2

	RETURN

verify_byte

	BTFSS 	measure_flag
	BRA 	responde_verify
	
	BSF 	discard_measure_ch1
	BSF 	discard_measure_ch2

responde_verify

	; Send the 'V' code (VERIFIED)
	CALL 	wait_tx_empty
	MOVLW 	'V'
	MOVWF 	TXREG1

	RETURN

set_mode
	
	; The measure is discarded if an interrupt occur while measuring
	BTFSC 	measure_flag
	BRA 	end_rx

	; wait for the mode
	CALL 	wait_rx_full
	MOVFF 	RCREG1, WREG_TEMP

	MOVLW 	A'D'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	set_debug_mode

	MOVLW 	A'P'
	SUBWF 	WREG_TEMP, W
	BTFSC 	STATUS, Z
	BRA 	set_production_mode

	; wrong code
	BRA 	end_rx
	
	
set_debug_mode

	BSF 	mode_flag

	; Send the 'D' code (DONE)
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	RETURN

set_production_mode

	BCF 	mode_flag
	
	; Send the 'D' code (DONE) 
	CALL 	wait_tx_empty
	MOVLW 	'D'
	MOVWF 	TXREG1

	RETURN

end_rx
	
	; Enter here if the wrong byte has been sent or 'S', 'A' while the measure is already
	; running
	
	; The measure is discarded if an interrupt occur while measuring. Only if the 
	; received byte is not one of the above
	BTFSS 	measure_flag
	BRA 	responde_fault
	
	BSF 	discard_measure_ch1
	BSF 	discard_measure_ch2

responde_fault
	; Send the 'F' code (FAULT)
	CALL 	wait_tx_empty
	MOVLW 	'F'
	MOVWF 	TXREG1
	
	RETURN

; ---- Send 2 byte to the serial port ------------------------
SendData:
	; Little endian is used (first LSB and then the MSB are sent)

	BTFSC 	mode_flag  ; set = debug_mode
	BRA 	send_debug
	
	; The wait_tx_empty should not be needed since we should start from a 
	; point where the two register are already empty. We leave them just for 
	; safety

    ; Send only ch2 if in single channel configuration
    BTFSS double_ch_config
    BRA send_ch2

	; ========= Channel 1 ============
	CALL 	wait_tx_empty
	MOVFF 	measure_ch1, TXREG1
	
	CALL 	wait_tx_empty
	MOVFF 	measure_ch1+1, TXREG1

send_ch2
	; ========= Channel 2 ============
	CALL 	wait_tx_empty
	MOVFF 	measure_ch2, TXREG1
	
	CALL 	wait_tx_empty
	MOVFF 	measure_ch2+1, TXREG1
	

	; wait both the byte to be sent
	BTFSS 	TXSTA1, TRMT  ; set when TX register is empty
	BRA 	$-2  ; wait that both byte are sent
	
	RETURN

send_debug
	
    ; Send only ch2 if in single channel configuration
    BTFSS double_ch_config
    BRA send_ch2_debug
    
	; ========= Channel 1 ============
	CALL 	wait_tx_empty
	MOVFF 	start_measure_ch1, TXREG1
	
	CALL 	wait_tx_empty
	MOVFF 	start_measure_ch1+1, TXREG1

	CALL 	wait_tx_empty
	MOVFF 	stop_measure_ch1, TXREG1
	
	CALL 	wait_tx_empty
	MOVFF 	stop_measure_ch1+1, TXREG1

send_ch2_debug
	; ========= Channel 2 ============
	CALL 	wait_tx_empty
	MOVFF 	start_measure_ch2, TXREG1
	
	CALL 	wait_tx_empty
	MOVFF 	start_measure_ch2+1, TXREG1

	CALL 	wait_tx_empty
	MOVFF 	stop_measure_ch2, TXREG1
	
	CALL 	wait_tx_empty
	MOVFF 	stop_measure_ch2+1, TXREG1

	; wait both the byte to be sent
	BTFSS 	TXSTA1, TRMT  ; set when TX register is empty
	BRA 	$-2  ; wait that both byte are sent
	
	RETURN


;--------------- 5us Delay -----------------------------------
delay_5us:
	; execute 50 instructions

	; CALL:   2 inst
	; RETURN: 2 inst
	
	; Other 46 instruction are required

	; 14 * 3 + 2 = 44
	MOVLW 	.14	; 1 ints
	MOVWF 	counter	; 1 inst

	; DECFSZ, if followed by a 2 word instruction takes 3 instruction total, even 
	; if skipping
loop_delay
	DECFSZ 	counter
	BRA 	loop_delay

	NOP  ; 1 inst
	NOP  ; 1 inst

	RETURN

;--------------- 1us Delay -----------------------------------
delay_1us:
	; CALL:   2 inst
	; RETURN: 2 inst
	
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP

	RETURN

;---------------- 1s Delay -----------------------------------
; This is not accurate and should be used only to display info on the LCD
delay_1s:
	call	Ldelay
	call	Ldelay
	call	Ldelay
	call 	Ldelay
	call	Ldelay
	call 	Ldelay
	call 	Ldelay
	call 	Ldelay
	call	Ldelay
	call 	Ldelay
	return

Ldelay	; ~100 ms delay at 40 Mhz
	movlw	0xFF
	movwf	temp_1
	movwf	temp_2
	movlw	0x05
	movwf	temp_3
d1l1 	; 255*255*5 * 3 operations
	decfsz	temp_1,F
	bra	d1l1
	decfsz	temp_2,F
	bra	d1l1
	decfsz	temp_3,F
	bra	d1l1
	return	

; Taken from the example on the Microchip website
;----Standard code, Place characters on line-1--------------------------
stan_char_1:
	call	LCDLine_1
	movlw	.16
	movwf	ptr_count
	movlw	UPPER stan_table
	movwf	TBLPTRU
	movlw	HIGH stan_table
	movwf	TBLPTRH
	movlw	LOW stan_table
	movwf	TBLPTRL
	movf	ptr_pos,W
	addwf	TBLPTRL,F
	clrf	WREG
	addwfc	TBLPTRH,F
	addwfc	TBLPTRU,F

stan_next_char_1
	tblrd	*+
	movff	TABLAT,temp_wr			
	call	d_write					;send character to LCD

	decfsz	ptr_count,F				;move pointer to next char
	bra	stan_next_char_1

	return

;----Standard code, Place characters on line-2--------------------------
stan_char_2:	
	call	LCDLine_2
	movlw	.16
	movwf	ptr_count
	movlw	UPPER stan_table
	movwf	TBLPTRU
	movlw	HIGH stan_table
	movwf	TBLPTRH
	movlw	LOW stan_table
	movwf	TBLPTRL
	movf	ptr_pos,W
	addwf	TBLPTRL,F
	clrf	WREG
	addwfc	TBLPTRH,F
	addwfc	TBLPTRU,F

stan_next_char_2:
	tblrd	*+
	movff	TABLAT,temp_wr
	call	d_write					;send character to LCD

	decfsz	ptr_count,F				;move pointer to next char
	bra	stan_next_char_2

	return

;---- Wait until the adc conversion end --------------------------
wait_adc_conv:

	BTFSC 	ADCON0, GO_DONE
	BRA 	wait_adc_conv

	RETURN

	
;----Wait that the transmit buffer is empty -----------------------------
wait_tx_empty:
	BTFSS	PIR1, TX1IF 
	; BTFSS TXSTA1, TRMT  ; only for debug
	
	BRA 	wait_tx_empty

	RETURN

;----Wait that the receiver buffer is filled-----------------------------
wait_rx_full:
	BTFSS 	PIR1, RC1IF 
	BRA 	wait_rx_full

	RETURN


; --- First measure init ----
FirstMeasure:

	BCF 	type_measure  ; set to start

	SelectChannel1
	
	; CALL 	delay_1s
	; CALL 	delay_1s

	; Preset the timer and wait fot the next interrup
	CALL 	SetResetTimer


	RETURN

; ------------------------
; ---- initialization ----
; ------------------------

clean:
	; clear variables
	
	CLRF 	counter

	MOVLW 	0x00
	CLRF 	WREG_TEMP

	CLRF 	flags ; All flags should be 0 by default
	
	CLRF 	ptr_pos		
	CLRF 	ptr_count	

	CLRF 	temp_1		
	CLRF 	temp_2		
	CLRF 	temp_3		


	CLRF 	TMR1U
	CLRF 	timerU_int
	CLRF 	timerH_int
	CLRF 	timerL_int	
	CLRF 	timerH_reset
	CLRF 	timerL_reset
	
	CLRF 	measure_ch1
	CLRF 	measure_ch2

	CLRF 	TMR1H
	CLRF 	TMR1L

	RETURN

init_conf:

	BSF 	RCON, IPEN
	BSF 	INTCON, GIE
	BSF 	INTCON, PEIE

	RETURN

init_port:

    CLRF    PORTE
    ; Set all port to input (also default options)
    SETF    TRISE

	; Configure port
	CLRF 	PORTD
	
	; All port set to output 
	CLRF 	TRISD
	
	BCF 	reset_pin
	BCF 	reset_pin_2

	
	BCF 	signal_start

	RETURN

init_usb:

	; Display: waiting \n connection
	MOVLW	.48
	MOVWF	ptr_pos
	CALL	stan_char_1

	MOVLW	.64
	MOVWF	ptr_pos
	CALL	stan_char_2

	; Configure EUSART1
	; Since we want full duplex mode (can receive and transmit at the same time,
	; kind of), we use the Asynchronous mode.  
	;
	; Configure output
	BSF 	TRISC, TRISC7 ; Set RC7 (RX1) as an input
	BCF 	TRISC, TRISC6 ; Set RC6 (TX1) as an output

	BCF 	TXSTA1, TX9	; set as 8 bit (not 9)
	BSF 	TXSTA1, TXEN    ; transmit enable
	BCF 	TXSTA1, SYNC	; Asynchronous mode
	BSF 	TXSTA1, BRGH	; high speed baud rate
	BCF 	TXSTA1, SENDB   ; Do not Send Sync Break on next transmission

	; Configure input
	BSF 	RCSTA1, SPEN    ; Serial port enabled
	BCF 	RCSTA1, RX9	; set 8 bit input (not 9)
	BSF 	RCSTA1, CREN    ; enable receiver

	BSF 	BAUDCON1, BRG16	; Set baud register to 16 bit

	; Baud rate = 115200 b/s. 
	; From table: SYNC=0; BRGH=1; BRG16=1  =>  SPBRG = 86 (decimal)
	; in hexadecimal 0056h (ie 0x0056)
	MOVLW 	0x00
	MOVWF 	SPBRGH1
	MOVLW 	0x56
	MOVWF 	SPBRG1

	MOVF 	RCREG1, W  ; clean flag and empty register
	
	BSF 	PIE1, RC1IE  ; Enables the EUSART1 receive interrupt
	BSF 	IPR1, RC1IP  ; EUSART1 receive interrupt set to high priority

	RETURN



init_timer:
	; Configure timer
	
	BSF 	PIE1, TMR1IE ; enable interrupt on overflow
	BSF 	IPR1, TMR1IP ; high-priority for TIMER1

	; Here the timer is not enabled yet
	MOVLW 	b'10001010'
	MOVWF 	T1CON

	
	RETURN


init_adc:

	MOVLW 	b'00011001'  ; AN6 as input
	MOVWF 	ADCON0
	MOVLW 	b'00010100'  ; set AN10->AN0 as analog input
	MOVWF 	ADCON1
	MOVLW 	b'10000010'  ; TACQ = 0
	MOVWF 	ADCON2

	RETURN



init_lcd:
	
	CALL 	LCDInit
	MOVLW	.0		;send "Microchip PICDEM" to LCD
	MOVWF	ptr_pos
	CALL	stan_char_1

	MOVLW	.16		;send " HPC Explorer2 " to LCD
	MOVWF	ptr_pos
	CALL	stan_char_2
	
	CALL delay_1s

	RETURN


; --- Wait for the start signal ---
wait_start:
	
	; Display: ready
	MOVLW	.48
	MOVWF	ptr_pos
	CALL	stan_char_1

	MOVLW	.32  ; empty line
	MOVWF	ptr_pos
	CALL	stan_char_2

	RETURN


; #### START ####
start:


	CALL 	clean
	CALL 	init_conf
	CALL 	init_port
	CALL 	init_adc

	CALL 	init_lcd

	CALL 	init_usb

	CALL 	init_timer

	CALL 	wait_start




;**************************
; Infinite cycle waiting for interrupt



idle_sleep

	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	NOP
	
	GOTO 	idle_sleep


;End of program
		END
