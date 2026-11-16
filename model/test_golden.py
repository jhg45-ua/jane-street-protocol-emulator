# model/test_golden.py
import os
import sys
import pytest
from vcd import VCDWriter

# Permite importar isa y golden_model ejecutando pytest desde cualquier carpeta
sys.path.insert(0, os.path.dirname(__file__))

from isa import (
    Opcode, JmpCond, SetDest, PinMode,
    OutMode, InMode, FifoMode, CrcMode,
    encode_instr
)
from golden_model import GoldenProtocolEmulator


def test_uart_tx_exact_timing():
    """Verifica que UART TX emite 'A' (0x41) con ancho de bit uniforme de 4 ciclos."""
    emu = GoldenProtocolEmulator()
    prog = [
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=0),  # 0: TX idle = 1
        encode_instr(Opcode.PULL, FifoMode.BLOCK,  operand=0, delay=0),  # 1: TX_FIFO -> OSR
        encode_instr(Opcode.SET,  SetDest.X,       operand=7, delay=0),  # 2: 8 bits
        encode_instr(Opcode.PIN,  PinMode.CLR,     operand=0, delay=3),  # 3: START (4 ciclos)
        encode_instr(Opcode.OUT,  OutMode.LSB_PIN, operand=0, delay=2),  # 4: Bit (3c + 1c JMP = 4c)
        encode_instr(Opcode.JMP,  JmpCond.X_NZ,    operand=4, delay=0),  # 5: Loop 8 veces
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=3),  # 6: STOP (4 ciclos)
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS,  operand=1, delay=0),  # 7: Siguiente byte
    ]
    emu.load_program(prog)
    emu.host_push_tx(ord('A'))  # 0x41

    tx_trace = []
    for _ in range(46):
        st = emu.step()
        tx_trace.append(st["uo_out"] & 1)

    # Muestreamos en el centro de cada ventana de 4 ciclos a partir del ciclo 4 (índice 3)
    sampled = [tx_trace[3 + i * 4 + 1] for i in range(10)]
    start_bit = sampled[0]
    data_bits = sampled[1:9]
    stop_bit  = sampled[9]

    reconstructed = sum(bit << idx for idx, bit in enumerate(data_bits))
    assert start_bit == 0, "Fallo en Start Bit"
    assert reconstructed == ord('A'), f"Esperado 0x41, recibido 0x{reconstructed:02X}"
    assert stop_bit == 1, "Fallo en Stop Bit"


def test_spi_master_mode0_full_duplex():
    """Verifica transferencia SPI Mode 0 (MOSI salida MSB-first y MISO entrada simultánea)."""
    emu = GoldenProtocolEmulator()
    # Pines: 0=SCK, 1=MOSI, 2=CS_N, 3=MISO(entrada)
    prog = [
        encode_instr(Opcode.PIN,  PinMode.CLR,     operand=0, delay=0),  # 0: SCK = 0
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=2, delay=0),  # 1: CS_N = 1
        encode_instr(Opcode.PULL, FifoMode.BLOCK,  operand=0, delay=0),  # 2: Cargar byte TX
        encode_instr(Opcode.SET,  SetDest.X,       operand=7, delay=0),  # 3: 8 bits
        encode_instr(Opcode.PIN,  PinMode.CLR,     operand=2, delay=0),  # 4: CS_N = 0 (activo)
        encode_instr(Opcode.OUT,  OutMode.MSB_PIN, operand=1, delay=1),  # 5: Poner bit en MOSI
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=0),  # 6: Flanco subida SCK
        encode_instr(Opcode.IN,   InMode.SHIFT_L,  operand=3, delay=1),  # 7: Leer MISO en ISR
        encode_instr(Opcode.PIN,  PinMode.CLR,     operand=0, delay=0),  # 8: Flanco bajada SCK
        encode_instr(Opcode.JMP,  JmpCond.X_NZ,    operand=5, delay=0),  # 9: Repetir 8 bits
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=2, delay=0),  # 10: CS_N = 1
        encode_instr(Opcode.PUSH, FifoMode.BLOCK,  operand=0, delay=0),  # 11: ISR -> RX_FIFO
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS,  operand=2, delay=0),  # 12: Esperar siguiente
    ]
    emu.load_program(prog)
    tx_byte = 0xA5  # 1010_0101
    slave_response = 0x3C  # 0011_1100
    emu.host_push_tx(tx_byte)

    mosi_captured = []
    prev_sck = 0
    slave_bit_idx = 7

    for _ in range(70):
        miso_bit = (slave_response >> max(slave_bit_idx, 0)) & 1
        st = emu.step(uio_in=(miso_bit << 3))
        sck = (st["uo_out"] >> 0) & 1
        mosi = (st["uo_out"] >> 1) & 1

        # El esclavo muestrea MOSI en flanco de subida de SCK y actualiza MISO en bajada
        if prev_sck == 0 and sck == 1:
            mosi_captured.append(mosi)
        elif prev_sck == 1 and sck == 0:
            slave_bit_idx -= 1
        prev_sck = sck

    received_by_slave = sum(b << (7 - i) for i, b in enumerate(mosi_captured))
    received_by_master = emu.host_pop_rx()

    assert received_by_slave == tx_byte, f"MOSI falló: 0x{received_by_slave:02X}"
    assert received_by_master == slave_response, f"MISO falló: 0x{received_by_master:02X}"


def test_i2c_open_drain_safety_and_vcd():
    """
    Simula START + Byte + ACK + STOP en I2C y verifica formalmente que
    NUNCA se conduce un '1' activo en Open-Drain. Además exporta ondas VCD.
    """
    emu = GoldenProtocolEmulator()
    # Pines: 0=SCL, 1=SDA (ambos Open-Drain)
    prog = [
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=0, delay=0),  # 0: SCL = Z
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=1, delay=0),  # 1: SDA = Z
        encode_instr(Opcode.PULL, FifoMode.BLOCK, operand=0, delay=0),  # 2: Dirección I2C
        encode_instr(Opcode.SET,  SetDest.X,      operand=7, delay=0),  # 3: 8 bits
        encode_instr(Opcode.PIN,  PinMode.CLR,    operand=1, delay=1),  # 4: START (SDA -> 0)
        encode_instr(Opcode.PIN,  PinMode.CLR,    operand=0, delay=1),  # 5: SCL -> 0
        encode_instr(Opcode.OUT,  OutMode.OD_MSB, operand=1, delay=1),  # 6: Bit SDA Open-Drain
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=0, delay=2),  # 7: SCL -> Z (alto)
        encode_instr(Opcode.PIN,  PinMode.CLR,    operand=0, delay=1),  # 8: SCL -> 0
        encode_instr(Opcode.JMP,  JmpCond.X_NZ,   operand=6, delay=0),  # 9: Loop 8 bits
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=1, delay=1),  # 10: Soltar SDA para ACK
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=0, delay=1),  # 11: SCL -> Z (reloj ACK)
        encode_instr(Opcode.IN,   InMode.SHIFT_L, operand=1, delay=1),  # 12: Muestrear ACK (0)
        encode_instr(Opcode.PIN,  PinMode.CLR,    operand=0, delay=1),  # 13: SCL -> 0
        encode_instr(Opcode.PIN,  PinMode.CLR,    operand=1, delay=1),  # 14: SDA -> 0
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=0, delay=1),  # 15: SCL -> Z
        encode_instr(Opcode.PIN,  PinMode.HIZ,    operand=1, delay=2),  # 16: STOP (SDA -> Z)
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS, operand=2, delay=0),  # 17: Fin
    ]
    emu.load_program(prog)
    emu.host_push_tx(0xA0)  # Dirección típica EEPROM 24LC256 (Write)

    with open("golden_i2c.vcd", "w") as f:
        with VCDWriter(f, timescale="10 ns", date="today") as writer:
            v_clk    = writer.register_var("golden", "clk", "wire", size=1)
            v_pc     = writer.register_var("golden", "pc", "wire", size=5)
            v_scl_oe = writer.register_var("golden", "scl_oe", "wire", size=1)
            v_sda_oe = writer.register_var("golden", "sda_oe", "wire", size=1)
            v_sda_line = writer.register_var("golden", "sda_phys_pullup", "wire", size=1)
            v_scl_line = writer.register_var("golden", "scl_phys_pullup", "wire", size=1)

            for cycle in range(85):
                # Simula el esclavo tirando de SDA a 0 durante el pulso de ACK (PC == 11..12)
                slave_ack_low = 0 if emu.pc in (11, 12) else 1
                st = emu.step(uio_in=(slave_ack_low << 1) | 1)

                # PROPIEDAD DE SEGURIDAD OPEN-DRAIN: jamás out=1 y oe=1 a la vez en SCL/SDA
                assert (st["uio_out"] & st["uio_oe"] & 0b11) == 0, (
                    f"¡Cortocircuito Open-Drain en ciclo {cycle}!"
                )

                scl_oe = (st["uio_oe"] >> 0) & 1
                sda_oe = (st["uio_oe"] >> 1) & 1
                # Con resistencia pull-up externa: si oe=1 -> línea=0; si oe=0 -> línea=1 (o ACK)
                scl_phys = 0 if scl_oe else 1
                sda_phys = 0 if sda_oe else slave_ack_low

                t = cycle * 2
                writer.change(v_clk, t, 1)
                writer.change(v_pc, t, st["pc"])
                writer.change(v_scl_oe, t, scl_oe)
                writer.change(v_sda_oe, t, sda_oe)
                writer.change(v_scl_line, t, scl_phys)
                writer.change(v_sda_line, t, sda_phys)
                writer.change(v_clk, t + 1, 0)

    # El bit ACK leído en el ciclo de reloj 9 debe ser 0
    assert (emu.isr & 1) == 0, "No se detectó el ACK (0) del esclavo I2C"
