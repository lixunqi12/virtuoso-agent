# Hierarchical schematic: GENERIC_PDK/full_opamp
Depth reached: 0 (cap: 49)

## Root (ROOT, depth=0)
### Instances
- **M58** (GENERIC_PDK/NMOS):
  Connections: G->clk_sc, D->net072, S->net056, B->net056
- **M56** (GENERIC_PDK/NMOS):
  Connections: G->clk_n_sc, D->net084, S->net047, B->net047
- **M48** (GENERIC_PDK/NMOS):
  Connections: G->clk_sc, D->net047, S->net043, B->net043
- **M57** (GENERIC_PDK/NMOS):
  Connections: G->clk_n_sc, D->net086, S->net072, B->net072
- **M33** (GENERIC_PDK/NMOS): w=n_width_sec*finger_n_sec, multi=1, m=1, fingers=finger_n_sec, l=200n
  Connections: G->cmfb_2, D->Vop, S->gnd!, B->gnd!
- **M35** (GENERIC_PDK/NMOS): l=200n, fingers=finger_n_sec, m=1, multi=1, w=n_width_sec*finger_n_sec
  Connections: G->cmfb_2, D->Von, S->gnd!, B->gnd!
- **M29** (GENERIC_PDK/NMOS):
  Connections: G->clk, D->Vo1p, S->net041, B->net041
- **M28** (GENERIC_PDK/NMOS):
  Connections: G->clk, D->net024, S->Vo1n, B->Vo1n
- **M26** (GENERIC_PDK/NMOS):
  Connections: G->clk_n, D->Vbias, S->net032, B->net032
- **M23** (GENERIC_PDK/NMOS):
  Connections: G->clk_n, D->Vcm, S->net041, B->net041
- **M22** (GENERIC_PDK/NMOS):
  Connections: G->clk_n, D->net024, S->Vcm, B->Vcm
- **M21** (GENERIC_PDK/NMOS):
  Connections: G->clk, D->cmfb_1, S->net032, B->net032
- **M16** (GENERIC_PDK/NMOS):
  Connections: G->clk, D->cmfb_2, S->net19, B->net19
- **M14** (GENERIC_PDK/NMOS):
  Connections: G->clk_n, D->Vbias_2, S->net19, B->net19
- **M12** (GENERIC_PDK/NMOS):
  Connections: G->clk_n, D->Vcm, S->net24, B->net24
- **M10** (GENERIC_PDK/NMOS):
  Connections: G->clk_n, D->net16, S->Vcm, B->Vcm
- **M8** (GENERIC_PDK/NMOS):
  Connections: G->clk, D->Vop, S->net24, B->net24
- **M6** (GENERIC_PDK/NMOS):
  Connections: G->clk, D->net16, S->Von, B->Von
- **M5** (GENERIC_PDK/NMOS): fingers=finger_ref, l=200n, m=1, multi=1, w=ref_Width*finger_ref
  Connections: G->net7, D->net7, S->gnd!, B->gnd!
- **M4** (GENERIC_PDK/NMOS): l=200n, fingers=finger_base, m=1, multi=1, w=ref_Width*finger_base
  Connections: G->net7, D->net11, S->gnd!, B->gnd!
- **M1** (GENERIC_PDK/NMOS): l=200n, fingers=finger_n, m=1, multi=1, w=n_width*finger_n
  Connections: G->Vin_n, D->Vo1p, S->net11, B->net11
- **M0** (GENERIC_PDK/NMOS): l=200n, fingers=finger_n, m=1, multi=1, w=n_width*finger_n
  Connections: G->Vin_p, D->Vo1n, S->net11, B->net11
- **M32** (GENERIC_PDK/PMOS): w=p_width*finger_p_sec, multi=1, m=1, fingers=finger_p_sec, l=200n
  Connections: D->Vop, G->Vo1n, B->vdd, S->vdd
- **M34** (GENERIC_PDK/PMOS): l=200n, fingers=finger_p_sec, m=1, multi=1, w=p_width*finger_p_sec
  Connections: D->Von, G->Vo1p, B->vdd, S->vdd
- **M27** (GENERIC_PDK/PMOS):
  Connections: D->Vbias, G->clk, B->net032, S->net032
- **M25** (GENERIC_PDK/PMOS):
  Connections: D->Vo1p, G->clk_n, B->net041, S->net041
- **M24** (GENERIC_PDK/PMOS):
  Connections: D->net024, G->clk_n, B->Vo1n, S->Vo1n
- **M20** (GENERIC_PDK/PMOS):
  Connections: D->cmfb_1, G->clk_n, B->net032, S->net032
- **M19** (GENERIC_PDK/PMOS):
  Connections: D->Vcm, G->clk, B->net041, S->net041
- **M18** (GENERIC_PDK/PMOS):
  Connections: D->net024, G->clk, B->Vcm, S->Vcm
- **M17** (GENERIC_PDK/PMOS):
  Connections: D->cmfb_2, G->clk_n, B->net19, S->net19
- **M15** (GENERIC_PDK/PMOS):
  Connections: D->Vbias_2, G->clk, B->net19, S->net19
- **M13** (GENERIC_PDK/PMOS):
  Connections: D->Vcm, G->clk, B->net24, S->net24
- **M11** (GENERIC_PDK/PMOS):
  Connections: D->net16, G->clk, B->Vcm, S->Vcm
- **M9** (GENERIC_PDK/PMOS):
  Connections: D->Vop, G->clk_n, B->net24, S->net24
- **M7** (GENERIC_PDK/PMOS):
  Connections: D->net16, G->clk_n, B->Von, S->Von
- **M3** (GENERIC_PDK/PMOS): l=200n, fingers=finger_p, m=1, multi=1, w=p_width*finger_p
  Connections: D->Vo1n, G->cmfb_1, B->vdd, S->vdd
- **M2** (GENERIC_PDK/PMOS): l=200n, fingers=finger_p, m=1, multi=1, w=p_width*finger_p
  Connections: D->Vo1p, G->cmfb_1, B->vdd, S->vdd
- **I0** (GENERIC_PDK/ISRC): idc=offset_current
  Connections: PLUS->vdd, MINUS->net7
- **C17** (GENERIC_PDK/IDEAL_CAP):
  Connections: MINUS->net072, PLUS->net085
- **C15** (GENERIC_PDK/IDEAL_CAP):
  Connections: MINUS->net047, PLUS->net083
- **C18** (GENERIC_PDK/IDEAL_CAP): c=1n
  Connections: MINUS->Vin_p, PLUS->Vo1n
- **C19** (GENERIC_PDK/IDEAL_CAP): c=1n
  Connections: MINUS->Vin_n, PLUS->Vo1p
- **C20** (GENERIC_PDK/IDEAL_CAP): c=1n
  Connections: MINUS->net082, PLUS->Vop
- **C21** (GENERIC_PDK/IDEAL_CAP): c=1n
  Connections: MINUS->net081, PLUS->Von
- **C9** (GENERIC_PDK/IDEAL_CAP): c=miller_cap
  Connections: MINUS->net063, PLUS->Von
- **C6** (GENERIC_PDK/IDEAL_CAP):
  Connections: MINUS->gnd!, PLUS->Vop
- **C5** (GENERIC_PDK/IDEAL_CAP): c=100.0f
  Connections: MINUS->net024, PLUS->net032
- **C4** (GENERIC_PDK/IDEAL_CAP): c=100.0f
  Connections: MINUS->net041, PLUS->net032
- **C8** (GENERIC_PDK/IDEAL_CAP): c=miller_cap
  Connections: MINUS->net064, PLUS->Vop
- **C7** (GENERIC_PDK/IDEAL_CAP):
  Connections: MINUS->gnd!, PLUS->Von
- **C1** (GENERIC_PDK/IDEAL_CAP): c=100.0f
  Connections: MINUS->net24, PLUS->net19
- **C0** (GENERIC_PDK/IDEAL_CAP): c=100.0f
  Connections: MINUS->net16, PLUS->net19
- **V10** (GENERIC_PDK/VSRC):
  Connections: PLUS->clk_n_sc, MINUS->gnd!
- **V9** (GENERIC_PDK/VSRC):
  Connections: PLUS->clk_sc, MINUS->gnd!
- **V1** (GENERIC_PDK/VSRC):
  Connections: PLUS->clk_n, MINUS->gnd!
- **V0** (GENERIC_PDK/VSRC):
  Connections: PLUS->clk, MINUS->gnd!
- **V8** (GENERIC_PDK/VSRC): vdc=bias_voltage_2
  Connections: PLUS->Vbias_2, MINUS->gnd!
- **V7** (GENERIC_PDK/VSRC): vdc=cmfb_1
  Connections: PLUS->cmfb_1, MINUS->gnd!
- **V6** (GENERIC_PDK/VSRC): vdc=cmfb_2
  Connections: PLUS->cmfb_2, MINUS->gnd!
- **V5** (GENERIC_PDK/VSRC): vdc=Vdd
  Connections: PLUS->vdd, MINUS->gnd!
- **V4** (GENERIC_PDK/VSRC): vdc=bias_voltage
  Connections: PLUS->Vbias, MINUS->gnd!
- **V2** (GENERIC_PDK/VSRC): vdc=Input_offset
  Connections: PLUS->Vcm, MINUS->gnd!
- **E1** (GENERIC_PDK/GENERIC_DEVICE):
  Connections: NC+->net3, NC-->gnd!, PLUS->Vin+, MINUS->Vcm
- **E0** (GENERIC_PDK/GENERIC_DEVICE):
  Connections: NC+->net3, NC-->gnd!, PLUS->Vin-, MINUS->Vcm
- **V3** (GENERIC_PDK/VSRC):
  Connections: PLUS->net3, MINUS->gnd!
- **R6** (GENERIC_PDK/RESISTOR): r=15.9K
  Connections: MINUS->Vin_p, PLUS->Vin+
- **R7** (GENERIC_PDK/RESISTOR): r=15.9K
  Connections: MINUS->Vin_n, PLUS->Vin-
- **R8** (GENERIC_PDK/RESISTOR): r=15.12K
  Connections: MINUS->Vo1n, PLUS->Vin_p
- **R9** (GENERIC_PDK/RESISTOR): r=15.12K
  Connections: MINUS->Vo1p, PLUS->Vin_n
- **R10** (GENERIC_PDK/RESISTOR): r=15.9K
  Connections: MINUS->Von, PLUS->Vin_p
- **R11** (GENERIC_PDK/RESISTOR): r=15.9K
  Connections: MINUS->net082, PLUS->Vo1n
- **R12** (GENERIC_PDK/RESISTOR): r=15.9K
  Connections: MINUS->net081, PLUS->Vo1p
- **R13** (GENERIC_PDK/RESISTOR): r=15.9K
  Connections: MINUS->Vop, PLUS->Vin_n
- **R1** (GENERIC_PDK/RESISTOR): r=1.000m
  Connections: MINUS->Vo1p, PLUS->net063
- **R0** (GENERIC_PDK/RESISTOR): r=1.000m
  Connections: MINUS->Vo1n, PLUS->net064
