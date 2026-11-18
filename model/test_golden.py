# model/test_golden.py
import os
import sys
from pathlib import Path

from vcd import VCDWriter

# Permite importar isa, asm y golden_model ejecutando pytest desde cualquier carpeta
sys.path.insert(0, os.path.dirname(__file__))

from asm import assemble_file
from golden_model import GoldenProtocolEmulator
from isa import (
    CrcMode,
    FifoMode,
    InMode,
    JmpCond,
    Opcode,
    OutMode,
    PinMode,
    SetDest,
    WaitPol,
    encode_instr,
    pack_ui_in,
)


def _send_nibble(emu: GoldenProtocolEmulator, nib: int, run_en: int = 0, uio_in: int = 0xFF):
    """Envía un nibble por WR_STB (1 ciclo en alto + 1 ciclo en bajo) respetando la tasa máxima."""
    emu.step(ui_in=pack_ui_in(wr_stb=1, run_en=run_en, host_nib=nib), uio_in=uio_in)
    emu.step(ui_in=pack_ui_in(wr_stb=0, run_en=run_en, host_nib=nib), uio_in=uio_in)


def _upload_program_pins(emu: GoldenProtocolEmulator, prog: list[int]):
    """Carga un programa en IMEM nibble a nibble (MSB-first) con RUN_EN=0."""
    for word in prog:
        for shift in (12, 8, 4, 0):
            _send_nibble(emu, (word >> shift) & 0xF, run_en=0)
    # Vaciar latencia del sincronizador 2-FF + ciclo de captura antes de subir RUN_EN
    for _ in range(3):
        emu.step(ui_in=pack_ui_in(run_en=0))


def _push_tx_byte_pins(emu: GoldenProtocolEmulator, byte_val: int):
    """Envía un byte a TX_FIFO en 2 nibbles (MSB-first) con RUN_EN=1."""
    _send_nibble(emu, (byte_val >> 4) & 0xF, run_en=1)
    _send_nibble(emu, byte_val & 0xF, run_en=1)


def _decode_uart_8n1(trace: list[int], bit_cycles: int = 4) -> list[int]:
    """Decodifica tramas UART 8N1 (LSB-first) desde una traza ciclo a ciclo."""
    decoded = []
    i = 0
    # Esperar a que la línea entre en reposo (1) primero
    while i < len(trace) and trace[i] == 0:
        i += 1
    while i < len(trace):
        # Buscar flanco de bajada del bit de START
        if trace[i] == 0:
            start_sample = i + bit_cycles // 2
            if start_sample + 9 * bit_cycles >= len(trace):
                break
            assert trace[start_sample] == 0, f"Start bit inválido en índice {i}"
            byte_val = 0
            for bit_idx in range(8):
                b = trace[start_sample + (bit_idx + 1) * bit_cycles]
                byte_val |= b << bit_idx
            stop_bit = trace[start_sample + 9 * bit_cycles]
            assert stop_bit == 1, f"Stop bit inválido en índice {i}"
            decoded.append(byte_val)
            i = start_sample + 9 * bit_cycles
        else:
            i += 1
    return decoded


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
    emu.host_push_tx(ord("A"))  # 0x41

    tx_trace = []
    for _ in range(46):
        st = emu.step_core()
        tx_trace.append(st["uo_out"] & 1)

    # Muestreamos en el centro de cada ventana de 4 ciclos a partir del ciclo 4 (índice 3)
    sampled = [tx_trace[3 + i * 4 + 1] for i in range(10)]
    start_bit = sampled[0]
    data_bits = sampled[1:9]
    stop_bit  = sampled[9]

    reconstructed = sum(bit << idx for idx, bit in enumerate(data_bits))
    assert start_bit == 0, "Fallo en Start Bit"
    assert reconstructed == ord("A"), f"Esperado 0x41, recibido 0x{reconstructed:02X}"
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
    tx_byte = 0xA5
    slave_response = 0x3C
    emu.host_push_tx(tx_byte)

    mosi_captured = []
    prev_sck = 0
    slave_bit_idx = 7

    for _ in range(70):
        miso_bit = (slave_response >> max(slave_bit_idx, 0)) & 1
        st = emu.step_core(uio_in=(miso_bit << 3))
        sck = (st["uo_out"] >> 0) & 1
        mosi = (st["uo_out"] >> 1) & 1

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
    emu.host_push_tx(0xA0)

    with open("golden_i2c.vcd", "w") as f:
        with VCDWriter(f, timescale="10 ns", date="today") as writer:
            v_clk = writer.register_var("golden", "clk", "wire", size=1)
            v_pc = writer.register_var("golden", "pc", "wire", size=5)
            v_scl_oe = writer.register_var("golden", "scl_oe", "wire", size=1)
            v_sda_oe = writer.register_var("golden", "sda_oe", "wire", size=1)
            v_sda_line = writer.register_var("golden", "sda_phys_pullup", "wire", size=1)
            v_scl_line = writer.register_var("golden", "scl_phys_pullup", "wire", size=1)

            for cycle in range(85):
                slave_ack_low = 0 if emu.pc in (11, 12) else 1
                st = emu.step_core(uio_in=(slave_ack_low << 1) | 1)

                assert (st["uio_out"] & st["uio_oe"] & 0b11) == 0, (
                    f"¡Cortocircuito Open-Drain en ciclo {cycle}!"
                )

                scl_oe = (st["uio_oe"] >> 0) & 1
                sda_oe = (st["uio_oe"] >> 1) & 1
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

    assert (emu.isr & 1) == 0, "No se detectó el ACK (0) del esclavo I2C"


def test_assembler_matches_firmware_files():
    """Comprueba que los 3 archivos .asm ensamblan sin errores y ejecutan en el Golden Model."""
    fw_dir = Path(__file__).parent / "firmware"
    uart_prog = assemble_file(fw_dir / "uart_tx.asm")
    spi_prog  = assemble_file(fw_dir / "spi_master.asm")
    i2c_prog  = assemble_file(fw_dir / "i2c_master.asm")

    assert len(uart_prog) == 8
    assert len(spi_prog) == 13
    assert len(i2c_prog) == 18


def test_2ff_sync_latency_ui_and_uio():
    """Verifica latencia exacta de 2 ciclos de sincronización en ui_in y uio_in."""
    emu = GoldenProtocolEmulator()
    prog = [
        encode_instr(Opcode.WAIT, WaitPol.HIGH, operand=0, delay=0),  # 0: esperar uio_in[0]==1
        encode_instr(Opcode.PIN,  PinMode.SET,  operand=1, delay=0),  # 1: activar pin 1
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS, operand=2, delay=0),  # 2: bucle fin
    ]
    emu.load_program(prog)

    run_ui = pack_ui_in(run_en=1)

    # Ciclo 1: ui_in pasa a sync1; sync2 sigue en 0 -> núcleo pausado
    st = emu.step(ui_in=run_ui, uio_in=0x00)
    assert st["ui_sync2"] == 0x00 and st["pc"] == 0

    # Ciclo 2: ui_in llega a sync2 al final del flanco; núcleo aún vio sync2=0 -> pausado
    st = emu.step(ui_in=run_ui, uio_in=0x00)
    assert st["ui_sync2"] == run_ui and st["pc"] == 0

    # Ciclo 3: núcleo arranca y ejecuta WAIT HIGH 0 con uio_sync2=0 (uio_in=1 entra en sync1)
    st = emu.step(ui_in=run_ui, uio_in=0x01)
    assert st["pc"] == 0 and st["uio_sync2"] == 0x00

    # Ciclo 4: WAIT sigue leyendo uio_sync2 previo (0); al final del ciclo uio_sync2 pasa a 1
    st = emu.step(ui_in=run_ui, uio_in=0x01)
    assert st["pc"] == 0 and st["uio_sync2"] == 0x01

    # Ciclo 5: WAIT ve uio_sync2[0]==1 -> avanza a PC=1
    st = emu.step(ui_in=run_ui, uio_in=0x01)
    assert st["pc"] == 1

    # Ciclo 6: ejecuta PIN SET 1
    st = emu.step(ui_in=run_ui, uio_in=0x01)
    assert st["pc"] == 2
    assert (st["uo_out"] & 0x02) == 0x02


def test_host_interface_imem_tx_rx_and_edge_detection():
    """
    Prueba carga IMEM por nibbles (MSB-first), detección de flanco de WR_STB mantenido
    en alto, reset de nib_cnt al cambiar RUN_EN, saturación de TX_FIFO y lectura RX_FIFO.
    """
    emu = GoldenProtocolEmulator()

    # Cargar 2 instrucciones mediante WR_STB mantenido varios ciclos (solo debe contar 1 flanco)
    instr0 = encode_instr(Opcode.SET, SetDest.X, operand=0x15, delay=0)
    instr1 = encode_instr(Opcode.PIN, PinMode.SET, operand=3, delay=0)

    # Nibble parcial previo (2 ciclos en pipeline + 1 ciclo de captura) + cambio de RUN_EN
    _send_nibble(emu, 0xA, run_en=0)
    assert emu.nib_cnt == 0  # Aún cruzando el sincronizador 2-FF
    emu.step(ui_in=pack_ui_in(run_en=0))
    assert emu.nib_cnt == 1
    # Sube RUN_EN=1 y vuelve a bajar RUN_EN=0
    for _ in range(3):
        emu.step(ui_in=pack_ui_in(run_en=1))
    for _ in range(3):
        emu.step(ui_in=pack_ui_in(run_en=0))
    assert emu.nib_cnt == 0
    assert emu.imem_addr == 0

    # Enviar instr0 manteniendo WR_STB=1 durante 4 ciclos por nibble
    for shift in (12, 8, 4, 0):
        nib = (instr0 >> shift) & 0xF
        for _ in range(4):
            emu.step(ui_in=pack_ui_in(wr_stb=1, run_en=0, host_nib=nib))
        for _ in range(3):
            emu.step(ui_in=pack_ui_in(wr_stb=0, run_en=0, host_nib=nib))

    # Enviar instr1 con tasa máxima (1 ciclo alto, 1 ciclo bajo)
    for shift in (12, 8, 4, 0):
        _send_nibble(emu, (instr1 >> shift) & 0xF, run_en=0)
    for _ in range(3):
        emu.step(ui_in=pack_ui_in(run_en=0))

    assert emu.imem[0] == instr0
    assert emu.imem[1] == instr1
    assert emu.imem_addr == 2

    # Probar llenado TX_FIFO (4 bytes + 1 byte descartado por overflow) con core pausado en PULL
    prog_echo = [
        encode_instr(Opcode.IN,   InMode.SHIFT_L, operand=0, delay=0),
        encode_instr(Opcode.IN,   InMode.SHIFT_L, operand=0, delay=0),
        encode_instr(Opcode.PUSH, FifoMode.BLOCK, operand=0, delay=0),
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS, operand=3, delay=0),  # loop inactivo
    ]
    emu.reset()
    _upload_program_pins(emu, prog_echo)
    # Con RUN_EN=1, cargar 5 bytes seguidos cuando el programa no hace PULL
    for val in (0x11, 0x22, 0x33, 0x44, 0x55):
        _push_tx_byte_pins(emu, val)
    for _ in range(3):
        emu.step(ui_in=pack_ui_in(run_en=1))

    assert list(emu.tx_fifo) == [0x11, 0x22, 0x33, 0x44]
    assert emu.nib_cnt == 0

    # Probar RX_FIFO + RX_POP + mux en uo_out
    emu.reset()
    emu.rx_fifo.extend([0xAB, 0xCD])
    # Con RUN_EN=1, RX_POP no debe extraer y uo_out muestra core_uo_out (0x00)
    for _ in range(3):
        emu.step(ui_in=pack_ui_in(run_en=1, rx_pop=1))
    assert list(emu.rx_fifo) == [0xAB, 0xCD]

    # Al pasar a RUN_EN=0, uo_out muestra la cabeza de RX_FIFO (0xAB)
    for _ in range(3):
        st = emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    assert st["uo_out"] == 0xAB

    # Pulso RX_POP -> muestra 0xCD
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=1))
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    st = emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    assert st["uo_out"] == 0xCD
    assert st["rx_fifo_len"] == 1

    # Segundo pulso RX_POP -> FIFO vacía -> uo_out == 0x00
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=1))
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    st = emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    assert st["uo_out"] == 0x00
    assert st["rx_fifo_len"] == 0

    # Tercer pulso en FIFO vacía -> no-op seguro
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=1))
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    st = emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    assert st["uo_out"] == 0x00
    assert st["rx_fifo_len"] == 0


def test_bist_emits_ok_newline_after_reset():
    """Con BIST_EN=1 tras reset, la micro-ROM emite 'OK\\n' por UART sin RUN_EN ni WR_STB."""
    emu = GoldenProtocolEmulator()
    emu.imem[0] = 0xBEEF  # Marca para verificar que BIST no sobrescribe IMEM

    uo_trace = []
    uio_trace = []
    oe_trace = []

    # Mantener BIST_EN=1, RUN_EN=0 durante 160 ciclos
    bist_ui = pack_ui_in(bist_en=1, run_en=0)
    for _ in range(160):
        st = emu.step(ui_in=bist_ui)
        uo_trace.append(st["uo_out"] & 1)
        uio_trace.append(st["uio_out"] & 1)
        oe_trace.append(st["uio_oe"] & 1)

    assert _decode_uart_8n1(uo_trace) == [ord("O"), ord("K"), ord("\n")]
    assert _decode_uart_8n1(uio_trace) == [ord("O"), ord("K"), ord("\n")]
    assert all(oe == 1 for oe in oe_trace[3:]), "Pin UART TX debe permanecer habilitado tras arrancar BIST"
    assert uo_trace[-1] == 1 and uio_trace[-1] == 1, "Línea UART debe quedar en reposo alto"
    assert emu.bist_idx == 3
    assert len(emu.tx_fifo) == 0
    assert emu.imem[0] == 0xBEEF

    # Al bajar BIST_EN=0 (con RUN_EN=0), uo_out vuelve a mostrar RX_FIFO (0x00)
    for _ in range(3):
        st = emu.step(ui_in=pack_ui_in(bist_en=0, run_en=0))
    assert st["uo_out"] == 0x00


def test_pin_level_end_to_end_spi_and_i2c():
    """
    Co-simulación de extremo a extremo a nivel de pines (ui_in, uio_in -> uo_out, uio_out, uio_oe)
    ensamblando los firmwares reales .asm, cargándolos por nibbles y ejecutando con sync 2-FF.
    """
    fw_dir = Path(__file__).parent / "firmware"

    # 1) SPI Master Mode 0 full-duplex a nivel de pines
    emu = GoldenProtocolEmulator()
    spi_prog = assemble_file(fw_dir / "spi_master.asm")
    _upload_program_pins(emu, spi_prog)
    _push_tx_byte_pins(emu, 0xA5)

    slave_response = 0x3C
    mosi_captured = []
    prev_sck = 0
    slave_bit_idx = 7

    for _ in range(85):
        miso_bit = (slave_response >> max(slave_bit_idx, 0)) & 1
        st = emu.step(ui_in=pack_ui_in(run_en=1), uio_in=(miso_bit << 3))
        sck = (st["uio_out"] >> 0) & 1
        mosi = (st["uio_out"] >> 1) & 1
        if prev_sck == 0 and sck == 1:
            mosi_captured.append(mosi)
        elif prev_sck == 1 and sck == 0:
            slave_bit_idx -= 1
        prev_sck = sck

    received_by_slave = sum(b << (7 - i) for i, b in enumerate(mosi_captured))
    assert received_by_slave == 0xA5

    # Pausar core (RUN_EN=0) y leer el byte recibido en RX_FIFO directamente en uo_out
    for _ in range(3):
        st = emu.step(ui_in=pack_ui_in(run_en=0))
    assert st["uo_out"] == slave_response

    # Extraer byte con RX_POP
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=1))
    emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    st = emu.step(ui_in=pack_ui_in(run_en=0, rx_pop=0))
    assert st["uo_out"] == 0x00 and st["rx_fifo_len"] == 0

    # 2) I2C Master a nivel de pines verificando seguridad Open-Drain en TODOS los ciclos
    emu.reset()
    i2c_prog = assemble_file(fw_dir / "i2c_master.asm")
    _upload_program_pins(emu, i2c_prog)
    _push_tx_byte_pins(emu, 0xA0)
    _push_tx_byte_pins(emu, 0x55)

    # Transacción 1: esclavo responde ACK (0); Transacción 2: esclavo responde NACK (1)
    ack_samples = []
    seen_stop = False
    for cycle in range(190):
        # En la 1ª transacción (primeros 95 ciclos) envía ACK=0 durante PC 11..12
        slave_ack_low = 0 if (cycle < 95 and emu.pc in (11, 12)) else 1
        st = emu.step(ui_in=pack_ui_in(run_en=1), uio_in=(slave_ack_low << 1) | 1)
        assert (st["uio_out"] & st["uio_oe"] & 0b11) == 0, (
            f"¡Violación Open-Drain en ciclo {cycle}!"
        )
        if st["pc"] == 13 and st["delay_cnt"] == 1:
            ack_samples.append(st["isr"] & 1)
        if st["pc"] == 17:
            seen_stop = True

    assert seen_stop, "I2C no completó la secuencia hasta STOP"
    assert ack_samples == [0, 1], f"Esperado [ACK=0, NACK=1], obtenido {ack_samples}"
