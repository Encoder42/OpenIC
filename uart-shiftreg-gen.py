#!/usr/bin/env python3
import argparse
import json
from typing import Dict, List, Optional

# Wokwi diagram.json basics: version/parts/connections etc. [2](https://docs.wokwi.com/diagram-format)
Conn = List[object]  # [src, dst, color, optional path list]


def uart_frame_bits_8n1(byte_val: int) -> List[int]:
    """UART 8N1: start(0), 8 data bits LSB-first, stop(1)."""
    bits = [0]
    bits += [(byte_val >> i) & 1 for i in range(8)]
    bits.append(1)
    return bits  # length 10


class Diagram:
    def __init__(self, author: str = "uart-shiftreg-gen", editor: str = "wokwi"):
        self.obj: Dict = {
            "version": 1,
            "author": author,
            "editor": editor,
            "parts": [],
            "connections": [],
            "dependencies": {}
        }
        self._ids = set()

    def add_part(self, ptype: str, pid: str, left: float, top: float,
                 rotate: Optional[float] = None, attrs: Optional[Dict] = None):
        if pid in self._ids:
            raise ValueError(f"Duplicate part id: {pid}")
        self._ids.add(pid)
        part = {"type": ptype, "id": pid, "left": left, "top": top, "attrs": attrs or {}}
        if rotate is not None:
            part["rotate"] = rotate
        self.obj["parts"].append(part)

    def add_conn(self, src: str, dst: str, color: str = "black", path: Optional[List[str]] = None):
        c: Conn = [src, dst, color]
        if path is not None:
            c.append(path)
        self.obj["connections"].append(c)


def add_const_source(d: Diagram, bit: int, pid: str, left: float, top: float) -> str:
    """
    Create explicit per-mux constant source (VCC/GND)
    """
    if bit:
        d.add_part("wokwi-vcc", pid, left=left, top=top, attrs={})
        return f"{pid}:VCC"
    else:
        d.add_part("wokwi-gnd", pid, left=left, top=top, rotate=180, attrs={})
        return f"{pid}:GND"


def build(text: str, clock_hz: int = 5, idle_count: int = 10) -> Dict:
    """
    Generates a Wokwi diagram implementing UART TX via a long shift register.
    Inputs:
      - OUT_EN: chip1:IN0 -> mux2:SEL, mux2:A=VCC, mux2:B=TX, mux2:OUT -> UNO RX0 + chip2:OUT0
      - RST_N: chip1:IN1 drives a SEL chain across all bit muxes
      - IN_SEL/EXT_IN: chip1:IN2 -> mux1:SEL, chip1:IN3 -> mux1:B, mux1:A from TX, mux1:OUT injects into idle chain
    End of stream: exactly `idle_count` idle bits (1) provided by the idle chain
    """
    d = Diagram(author="uart-shiftreg-gen (keep-inputs, idle10)", editor="wokwi")

    # ---------------------------------------------------------------------
    # Inputs
    # ---------------------------------------------------------------------
    d.add_part("wokwi-dip-switch-8", "sw1", left=-180.9, top=-138.1, rotate=90, attrs={})
    d.add_part("wokwi-vcc", "pwr1", left=-194.98, top=-211.59, attrs={})
    d.add_part("board-tt-block-input", "chip1", left=-33.6, top=-189.73, attrs={"verilogRole": "input"})
    d.add_part("board-tt-block-output", "chip2", left=830.4, top=-963.58, attrs={"verilogRole": "output"})
    d.add_part("wokwi-clock-generator", "clock1", left=-153.6, top=-336.0, attrs={"frequency": str(clock_hz)})
    d.add_part("wokwi-arduino-uno", "uno1", left=734.6, top=-807.4, rotate=180, attrs={})

    # Labels (optional)
    d.add_part("wokwi-text", "t_outen", left=-336.0, top=-182.4, attrs={"text": "SW1: OUT_EN"})
    d.add_part("wokwi-text", "t_shift", left=-336.0, top=-163.2, attrs={"text": "SW2: RST_N"})
    d.add_part("wokwi-text", "t_insel", left=-336.0, top=-144.0, attrs={"text": "SW3: IN_SEL"})
    d.add_part("wokwi-text", "t_extin", left=-336.0, top=-124.8, attrs={"text": "SW4: EXT_IN"})
    d.add_part("wokwi-text", "t_uart", left=643.2, top=-590.2, attrs={"text": "UART Debug"})

    # DIP 'a' side tied to VCC (all 8)
    for i in range(1, 9):
        d.add_conn("pwr1:VCC", f"sw1:{i}a", "red", ["v0"])

    # Keep EXTIN wiring as in original (EXTIN0..3 to DIP b pins 1..4)
    d.add_conn("chip1:EXTIN0", "sw1:1b", "green", ["h0"])
    d.add_conn("chip1:EXTIN1", "sw1:2b", "green", ["h0"])
    d.add_conn("chip1:EXTIN2", "sw1:3b", "green", ["h0"])
    d.add_conn("chip1:EXTIN3", "sw1:4b", "green", ["h0"])

    # Clock -> EXTCLK as in original
    d.add_conn("clock1:CLK", "chip1:EXTCLK", "green", ["v0"])

    # Output enable mux2 (same role as provided mux2)
    d.add_part("wokwi-mux-2", "mux2", left=518.4, top=-528.0, attrs={})
    d.add_part("wokwi-vcc", "vcc5", left=489.6, top=-560.64, attrs={})
    d.add_conn("vcc5:VCC", "mux2:A", "red", ["v9.6"])
    d.add_conn("chip1:IN0", "mux2:SEL", "green", ["h38.4", "v-315.55", "h432"])
    d.add_conn("mux2:OUT", "uno1:0", "cyan", ["v0"])
    d.add_conn("chip2:OUT0", "mux2:OUT", "green", ["h0"])

    # ---------------------------------------------------------------------
    # UART shift register: one character per column
    # Each bit cell: mux -> flop; mux.A=const bit, mux.B=next flop.Q; mux.SEL part of SHIFT chain
    # ---------------------------------------------------------------------
    x0 = 249.6
    y0 = -336.0
    dx = 556.8
    dy = 96.0

    bytes_list = [ord(c) & 0xFF for c in text]
    blocks = []  # list of dict {mux:[...], ff:[...]}

    for ci, b in enumerate(bytes_list):
        bits = uart_frame_bits_8n1(b)  # 10 bits
        x_mux = x0 + ci * dx
        x_ff = x_mux + 115.2
        x_c = x_mux - 28.8

        d.add_part("wokwi-text", f"char_{ci}_lbl",
                   left=x_mux + 140.0, top=y0 - 120.0,
                   attrs={"text": f"'{chr(b)}' 0x{b:02X}"})

        mux_ids, ff_ids = [], []
        for bi, bit in enumerate(bits):
            mux_id = f"mux_c{ci}_b{bi}"
            ff_id = f"flop_c{ci}_b{bi}"
            c_id = f"c_c{ci}_b{bi}"

            d.add_part("wokwi-mux-2", mux_id, left=x_mux, top=y0 + bi * dy, attrs={})
            d.add_part("wokwi-flip-flop-d", ff_id, left=x_ff, top=(y0 + bi * dy) + 9.6, attrs={})

            const_pin = add_const_source(d, bit, c_id, left=x_c, top=(y0 + bi * dy) + 34.0)
            d.add_conn(const_pin, f"{mux_id}:A", "red" if bit else "black", ["v0"])
            d.add_conn(f"{mux_id}:OUT", f"{ff_id}:D", "black", ["v0"])

            mux_ids.append(mux_id)
            ff_ids.append(ff_id)

        blocks.append({"mux": mux_ids, "ff": ff_ids})

    # ---------------------------------------------------------------------
    # Idle/Injection chain (kept) with exactly idle_count stages (=10)
    # mux1 selects injection source: A=TX, B=EXT_IN, SEL=IN_SEL
    # mux1:OUT injects into last idle mux.B
    # Each idle stage has A=VCC (idle '1'), B=next flop.Q
    # ---------------------------------------------------------------------
    idle_x_mux = x0 + (len(bytes_list) + 1) * dx
    idle_x_ff = idle_x_mux + 115.2
    idle_x_c = idle_x_mux - 28.8

    d.add_part("wokwi-text", "idlechain_lbl",
               left=idle_x_mux + 120.0, top=y0 - 120.0,
               attrs={"text": f"Idle/Injection chain: {idle_count} bits"})

    # mux1 like in provided: IN_SEL/EXT_IN function
    d.add_part("wokwi-mux-2", "mux1", left=idle_x_mux + 105.6, top=624.0, attrs={})
    d.add_conn("chip1:IN2", "mux1:SEL", "green", ["h67.2", "v374.4", "h0", "v441.6", "h537.6"])
    d.add_conn("mux1:B", "chip1:IN3", "green", ["h-528", "v-758.4"])

    # Idle stages
    idle_mux, idle_ff = [], []
    for i in range(idle_count):
        mux_id = f"mux_idle_{i}"
        ff_id = f"flop_idle_{i}"
        c_id = f"c_idle_{i}"

        top_i = y0 + i * dy
        d.add_part("wokwi-mux-2", mux_id, left=idle_x_mux, top=top_i, attrs={})
        d.add_part("wokwi-flip-flop-d", ff_id, left=idle_x_ff, top=top_i + 9.6, attrs={})

        const_pin = add_const_source(d, 1, c_id, left=idle_x_c, top=top_i + 34.0)
        d.add_conn(const_pin, f"{mux_id}:A", "red", ["v0"])
        d.add_conn(f"{mux_id}:OUT", f"{ff_id}:D", "black", ["v0"])

        idle_mux.append(mux_id)
        idle_ff.append(ff_id)

    # Shift wiring inside each UART char block: mux[j].B <- ff[j+1].Q (j=0..8)
    for block in blocks:
        for j in range(9):
            d.add_conn(f"{block['ff'][j+1]}:Q", f"{block['mux'][j]}:B", "black",
                       ["h0", "v38.4", "h211.2"])

    # Chain blocks together at stop stage (bit9):
    # stop mux.B <- next block start ff.Q; last stop mux.B <- idle chain start ff.Q (=> exactly idle_count idles)
    if blocks:
        for ci in range(len(blocks) - 1):
            d.add_conn(f"{blocks[ci+1]['ff'][0]}:Q", f"{blocks[ci]['mux'][9]}:B", "green",
                       ["h0", "v38.4", "h393.6", "v-96.0", "h384.0", "v76.8"])
        d.add_conn(f"{idle_ff[0]}:Q", f"{blocks[-1]['mux'][9]}:B", "green",
                   ["h0", "v38.4", "h320.0"])

    # Idle chain shifting: mux[j].B <- ff[j+1].Q, last idle mux.B <- mux1:OUT injection
    for j in range(idle_count - 1):
        d.add_conn(f"{idle_ff[j+1]}:Q", f"{idle_mux[j]}:B", "black",
                   ["h0", "v38.4", "h211.2"])
    d.add_conn("mux1:OUT", f"{idle_mux[-1]}:B", "green", ["v0", "h403.2", "v-105.6"])

    # SEL chain (SHIFT/LOAD) driven by chip1:IN1 and daisy-chained across all bit muxes
    sel_order = []
    for block in blocks:
        sel_order.extend(block["mux"])
    sel_order.extend(idle_mux)

    if sel_order:
        d.add_conn("chip1:IN1", f"{sel_order[0]}:SEL", "green", ["v-9.6", "h-144", "v-28.8"])
        for a, b in zip(sel_order, sel_order[1:]):
            d.add_conn(f"{a}:SEL", f"{b}:SEL", "white", ["v0"])

    # TX source: first UART start-bit flop.Q (like flop6:Q in provided); if empty text use idle start
    if blocks:
        tx_q = f"{blocks[0]['ff'][0]}:Q"
    else:
        tx_q = f"{idle_ff[0]}:Q"

    # TX to output enable mux and to mux1:A (injection A source)
    d.add_conn(tx_q, "mux2:B", "green", ["v0"])
    d.add_conn("mux1:A", tx_q, "green", ["h-86.4", "v-950.4"])

    # Clock distribution: chip1:CLK drives all flip-flops, chained for readability
    all_ff = []
    for block in blocks:
        all_ff.extend(block["ff"])
    all_ff.extend(idle_ff)

    if all_ff:
        d.add_conn("chip1:CLK", f"{all_ff[0]}:CLK", "green", ["v1.25", "h57.6", "v-134.4"])
        for a, b in zip(all_ff, all_ff[1:]):
            d.add_conn(f"{a}:CLK", f"{b}:CLK", "gray", ["h0"])

    return d.obj


def main():
    ap = argparse.ArgumentParser(
        description="Generate Wokwi diagram.json: UART TX via shift-register, keeping original inputs; idle tail = 10 bits."
    )
    ap.add_argument("text", help="String to send once (ASCII bytes), UART 8N1")
    ap.add_argument("-o", "--out", default="diagram.json", help="Output diagram.json path")
    ap.add_argument("--clock", type=int, default=100, help="Clock frequency in Hz (default 5)")
    ap.add_argument("--idle", type=int, default=10, help="Idle bits at end (default 10)")
    args = ap.parse_args()

    uart_msg = args.text + '\n'
    diagram = build(uart_msg, clock_hz=args.clock, idle_count=args.idle)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(diagram, f, indent=2)
    print(f"Wrote {args.out} for text={args.text!r}, clock={args.clock} Hz, idle={args.idle} bits")


if __name__ == "__main__":
    main()
