** Generated for: sample test fixture (synthetic)
** Generated on: 2026-04-28
** Design library name: DEMO_LIB
** Design cell name: demo_chain
** Design view name: schematic


** Library name: DEMO_LIB
** Cell name: TIEH
** View name: schematic
.subckt TIEH z vdd vss
xmm8 net10 net10 vss vss <redacted> l=30e-9 w=280e-9 multi=1 nf=1 sd=100e-9 ad=21e-15 as=21e-15 pd=710e-9 ps=710e-9 nrd=1.382006 nrs=1.382006
xmm5 z net10 vdd vdd <redacted> l=30e-9 w=340e-9 multi=1 nf=1 sd=100e-9 ad=25.5e-15 as=25.5e-15 pd=830e-9 ps=830e-9 nrd=500.473e-3 nrs=500.473e-3
.ends TIEH
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: TIEL
** View name: schematic
.subckt TIEL z vdd vss
xmm0 z net1 vss vss <redacted> l=30e-9 w=200e-9 multi=1 nf=1
xmm1 net1 net1 vdd vdd <redacted> l=30e-9 w=240e-9 multi=1 nf=1
.ends TIEL
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: INV1X
** View name: schematic
.subckt INV1X a zn vdd vss
xmm0 zn a vss vss <redacted> l=30e-9 w=140e-9 multi=1 nf=1
xmm1 zn a vdd vdd <redacted> l=30e-9 w=170e-9 multi=1 nf=1
.ends INV1X
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: INV2X
** View name: schematic
.subckt INV2X a zn vdd vss
xmm0 zn a vss vss <redacted> l=30e-9 w=280e-9 multi=1 nf=2
xmm1 zn a vdd vdd <redacted> l=30e-9 w=340e-9 multi=1 nf=2
.ends INV2X
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: NAND1X
** View name: schematic
.subckt NAND1X a1 a2 zn vdd vss
xmm0 zn a1 net1 vss <redacted> l=30e-9 w=140e-9 multi=1 nf=1
xmm1 net1 a2 vss vss <redacted> l=30e-9 w=140e-9 multi=1 nf=1
xmm2 zn a1 vdd vdd <redacted> l=30e-9 w=170e-9 multi=1 nf=1
xmm3 zn a2 vdd vdd <redacted> l=30e-9 w=170e-9 multi=1 nf=1
.ends NAND1X
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: NOR1X
** View name: schematic
.subckt NOR1X a1 a2 zn vdd vss
xmm9 zn a1 vss vss <redacted> l=30e-9 w=140e-9 multi=1 nf=1 sd=100e-9 ad=10.5e-15 as=10.5e-15 pd=430e-9 ps=430e-9 nrd=2.677526 nrs=2.677526
xmm8 zn a2 vss vss <redacted> l=30e-9 w=140e-9 multi=1 nf=1 sd=100e-9 ad=10.5e-15 as=10.5e-15 pd=430e-9 ps=430e-9 nrd=2.677526 nrs=2.677526
xmm3 zn a1 net21 vdd <redacted> l=30e-9 w=170e-9 multi=1 nf=1
xmm7 net21 a2 vdd vdd <redacted> l=30e-9 w=170e-9 multi=1 nf=1
.ends NOR1X
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: TG1X
** View name: schematic
.subckt TG1X a en enb zn vdd vss
xmm0 a en zn vdd <redacted> l=30e-9 w=170e-9
xmm1 a enb zn vss <redacted> l=30e-9 w=140e-9
.ends TG1X
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: LOGIC_BLOCK
** View name: schematic
.subckt LOGIC_BLOCK a b c zn vdd vss
xi0 a b net1 vdd vss NAND1X
xi1 net1 c net2 vdd vss NAND1X
xi2 net2 zn vdd vss INV1X
.ends LOGIC_BLOCK
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: STAGE
** View name: schematic
.subckt STAGE h_in v_in h_out v_out vdd vss
xi0 h_in h_mid_int vdd vss INV1X
xi1 h_mid_int h_out vdd vss INV1X
xi2 v_in v_mid_int vdd vss INV1X
xi3 v_mid_int v_out vdd vss INV1X
.ends STAGE
** End of subcircuit definition.

** Library name: DEMO_LIB
** Cell name: demo_chain
** View name: schematic
xs0 h_in h_in_mid v_in v_in_mid vdd vss STAGE
xs1 h_in_mid h_mid2 v_in_mid v_mid2 vdd vss STAGE
xs2 h_mid2 h_mid3 v_mid2 v_mid3 vdd vss STAGE
xs3 h_mid3 h_out_mid v_mid3 v_out_mid vdd vss STAGE
xs4 h_out_mid h_out v_out_mid v_out vdd vss STAGE
.END
