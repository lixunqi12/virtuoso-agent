** Synthetic testbench fixture for tests/test_netlist_reader.py.
.TEMP 25
.OPTION INGOLD=2 ARTIST=2 PSF=2 MEASOUT=1 PARHIER=LOCAL PROBE=0 MARCH=2 ACCURACY=1 POST RUNLVL=5 probe=1
.INCLUDE netlist.sp
.LIB "<path>" TOP_TT

.PARAM delay = 50p
+ PROSIGN = 0V
+ LSB = 0V
+ LSB2 = 0V
+ MSB = 0V
+ SIGN = 0V
+ hinvoltage = 0

V1 vdd 0 0.9V
V2 vss 0 0V
V3 in_a 0 0V
V4 in_b 0 0V
V5 in_c 0 0V
V6 in_d 0 0V
V7 stim<1> 0 0V PWL (0 0 5n 0 5.01n SIGN 6n SIGN 6.01n 0)
V8 stim<2> 0 0V PWL (0 0 5n 0 5.01n LSB 6n LSB 6.01n 0)
V9 stim<3> 0 0V PWL (0 0 5n 0 5.01n LSB2 6n LSB2 6.01n 0)
V10 stim<4> 0 0V PWL (0 0 5n 0 5.01n MSB 6n MSB 6.01n 0)
V11 stim<5> 0 0V
V12 stim<6> 0 0V
V13 stim<7> 0 0V
V14 stim<8> 0 0V
V15 ctrl<1> 0 0V
V16 ctrl<2> 0 0V
V17 ctrl<3> 0 0V
V18 ctrl<4> 0 0V
V19 ctrl<5> 0 0V
V20 ctrl<6> 0 0V
V21 H_IN 0 0.9V PWL (0 '0.9-hinvoltage' 7.0n '0.9-hinvoltage' 7.01n 'hinvoltage' 9n 'hinvoltage' 9.01n '0.9-hinvoltage')
V22 V_IN 0 0.9V PWL (0 '0.9-hinvoltage' '7.0n+delay' '0.9-hinvoltage' '7.01n+delay' 'hinvoltage' '9n+delay' 'hinvoltage' '9.01n+delay' '0.9-hinvoltage')
V23 aux<1> 0 0V
V24 aux<2> 0 0V
V25 aux<3> 0 0V
V26 aux<4> 0 0V
V27 aux<5> 0 0V
V28 aux<6> 0 0V
V29 aux<7> 0 0V
V30 aux<8> 0 0V
V31 ena 0 0V
V32 enb 0 0.9V

.tran 5p 10ns SWEEP delay -150p 150p 50p
.MEASURE tran h_tphl trig V(H_IN) val=0.45 fall=1 targ V(h_out_mid) val=0.45 fall=1
.MEASURE tran v_tphl trig V(V_IN) val=0.45 fall=1 targ V(v_out_mid) val=0.45 fall=1
.MEASURE tran h_tplh trig V(H_IN) val=0.45 rise=1 targ V(h_out_mid) val=0.45 rise=1
.MEASURE tran v_tplh trig V(V_IN) val=0.45 rise=1 targ V(v_out_mid) val=0.45 rise=1

.alter ** -3
.PARAM delay = 50p
+ PROSIGN = 0V
+ SIGN = 0V
+ hinvoltage = 0

.alter ** -2
.PARAM delay = 50p
+ PROSIGN = 0V
+ SIGN = 0V
+ hinvoltage = 0

.alter ** -1
.PARAM delay = 50p
+ PROSIGN = 0V
+ SIGN = 0V
+ hinvoltage = 0

.alter ** +0
.PARAM delay = 50p
+ PROSIGN = 0.9V
+ SIGN = 0.9V
+ hinvoltage = 0.9

.alter ** +1
.PARAM delay = 50p
+ PROSIGN = 0.9V
+ SIGN = 0.9V
+ hinvoltage = 0.9

.alter ** +2
.PARAM delay = 50p
+ PROSIGN = 0.9V
+ SIGN = 0.9V
+ hinvoltage = 0.9

.alter ** +3
.PARAM delay = 50p
+ PROSIGN = 0.9V
+ SIGN = 0.9V
+ hinvoltage = 0.9

.END
