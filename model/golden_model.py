"""
Golden Model (Instruction Set Simulator de ciclo exacto) para el Protocol Emulator ASIC.
Incluye el núcleo programable, la interfaz host, la micro-ROM BIST y la latencia
de sincronización 2-FF de todas las entradas externas (ui_in y uio_in).
Actúa como oráculo de referencia frente al RTL en Verilog (vía Cocotb).
"""

from collections import deque
from isa import (
    MASK_1B, MASK_2B, MASK_3B, MASK_4B, MASK_5B, MASK_8B, MASK_16B,
    MEM_DEPTH, FIFO_DEPTH,
    UI_HOST_NIB_MASK, UI_RX_POP_BIT, UI_BIST_EN_BIT, UI_RUN_EN_BIT, UI_WR_STB_BIT,
    BIST_BYTES, BIST_PROGRAM,
    Opcode, JmpCond, WaitPol, SetDest, PinMode,
    OutMode, InMode, FifoMode, CrcMode,
    encode_instr, decode_instr, pack_ui_in,
)


def crc8_step(crc: int, bit: int, poly: int = 0x07) -> int:
    """Actualiza el acumulador CRC-8 (polinomio estándar 0x07) con 1 bit en 1 ciclo."""
    msb = (crc >> 7) & MASK_1B
    feedback = msb ^ (bit & MASK_1B)
    crc = (crc << 1) & MASK_8B
    if feedback:
        crc ^= poly
    return crc & MASK_8B


class GoldenProtocolEmulator:
    def __init__(self):
        # Memoria de instrucciones (32 palabras de 16 bits)
        self.imem = [0x0000] * MEM_DEPTH
        self.reset()

    def reset(self):
        """Reinicia todos los registros exactamente como rst_n = 0 en Verilog."""
        self.imem        = [0x0000] * MEM_DEPTH
        self.pc          = 0 & MASK_5B
        self.delay_cnt   = 0 & MASK_5B
        self.osr         = 0 & MASK_8B
        self.isr         = 0 & MASK_8B
        self.x           = 0 & MASK_5B
        self.y           = 0 & MASK_5B
        self.crc         = 0 & MASK_8B
        self.last_in_bit = 0 & MASK_1B

        # Salidas registradas del núcleo
        self.core_uo_out = 0 & MASK_8B
        self.uio_out     = 0 & MASK_8B
        self.uio_oe      = 0 & MASK_8B

        # Salida combinacional final en uo_out (mux entre núcleo y RX_FIFO)
        self.uo_out      = 0 & MASK_8B

        # Colas FIFO desacopladas
        self.tx_fifo     = deque(maxlen=FIFO_DEPTH)
        self.rx_fifo     = deque(maxlen=FIFO_DEPTH)

        # Sincronizadores 2-FF y detectores de flanco para entradas asíncronas
        self.ui_sync1    = 0 & MASK_8B
        self.ui_sync2    = 0 & MASK_8B
        self.uio_sync1   = 0 & MASK_8B
        self.uio_sync2   = 0 & MASK_8B
        self.wr_stb_prev = 0 & MASK_1B
        self.rx_pop_prev = 0 & MASK_1B
        self.run_en_prev = 0 & MASK_1B

        # Registros de la interfaz host (ensamblado de nibbles MSB-first) y BIST
        self.imem_addr   = 0 & MASK_5B
        self.nib_cnt     = 0 & MASK_2B
        self.nib_shift   = 0 & MASK_16B
        self.bist_idx    = 0 & MASK_2B

        # Contador global de ciclos simulados
        self.cycle_count = 0

    def load_program(self, program: list[int]):
        """Carga una lista de palabras de 16 bits directamente en la memoria de instrucciones."""
        assert len(program) <= MEM_DEPTH, f"El programa excede {MEM_DEPTH} instrucciones"
        self.imem = [0x0000] * MEM_DEPTH
        for i, word in enumerate(program):
            self.imem[i] = word & MASK_16B

    def host_push_tx(self, byte_val: int) -> bool:
        """El host introduce directamente un byte en la TX_FIFO (atajo para test de núcleo)."""
        if len(self.tx_fifo) < FIFO_DEPTH:
            self.tx_fifo.append(byte_val & MASK_8B)
            return True
        return False

    def host_pop_rx(self) -> int | None:
        """El host extrae directamente un byte recibido de la RX_FIFO (atajo para test de núcleo)."""
        if len(self.rx_fifo) > 0:
            return self.rx_fifo.popleft()
        return None

    def _drive_pin(self, pin: int, val: int, oe: int):
        """Actualiza un único pin en core_uo_out, uio_out y uio_oe respetando las máscaras de 8 bits."""
        pin = pin & MASK_3B
        bit_mask = 1 << pin
        if val & MASK_1B:
            self.core_uo_out = (self.core_uo_out | bit_mask) & MASK_8B
            self.uio_out     = (self.uio_out     | bit_mask) & MASK_8B
        else:
            self.core_uo_out = (self.core_uo_out & ~bit_mask) & MASK_8B
            self.uio_out     = (self.uio_out     & ~bit_mask) & MASK_8B

        if oe & MASK_1B:
            self.uio_oe = (self.uio_oe | bit_mask) & MASK_8B
        else:
            self.uio_oe = (self.uio_oe & ~bit_mask) & MASK_8B

    def _execute_core_cycle(self, uio_sampled: int, instr: int) -> tuple[bool, int | None]:
        """
        Ejecuta 1 ciclo del núcleo (delay -> fetch/decode -> execute -> PC/DELAY_CNT)
        leyendo el estado previo al flanco y devolviendo (tx_pop, rx_push_byte).
        """
        if self.delay_cnt > 0:
            self.delay_cnt = (self.delay_cnt - 1) & MASK_5B
            return False, None

        opcode, delay, submode, operand = decode_instr(instr)
        next_pc = (self.pc + 1) & MASK_5B
        stalled = False
        tx_pop = False
        rx_push_byte = None

        if opcode == Opcode.JMP:
            target = operand & MASK_5B
            if submode == JmpCond.ALWAYS:
                next_pc = target
            elif submode == JmpCond.X_NZ:
                if self.x > 0:
                    self.x = (self.x - 1) & MASK_5B
                    next_pc = target
            elif submode == JmpCond.Y_NZ:
                if self.y > 0:
                    self.y = (self.y - 1) & MASK_5B
                    next_pc = target
            elif submode == JmpCond.PIN_0:
                if self.last_in_bit == 0:
                    next_pc = target

        elif opcode == Opcode.WAIT:
            pin = operand & MASK_3B
            pin_val = (uio_sampled >> pin) & MASK_1B
            expected = 1 if submode == WaitPol.HIGH else 0
            if pin_val != expected:
                stalled = True

        elif opcode == Opcode.SET:
            imm5 = operand & MASK_5B
            if submode == SetDest.X:
                self.x = imm5
            elif submode == SetDest.Y:
                self.y = imm5
            elif submode == SetDest.PINS:
                self.core_uo_out = (self.core_uo_out & 0xE0) | imm5
                self.uio_out     = (self.uio_out     & 0xE0) | imm5
            elif submode == SetDest.OE:
                self.uio_oe      = (self.uio_oe      & 0xE0) | imm5

        elif opcode == Opcode.PIN:
            pin = operand & MASK_3B
            if submode == PinMode.CLR:
                self._drive_pin(pin, val=0, oe=1)
            elif submode == PinMode.SET:
                self._drive_pin(pin, val=1, oe=1)
            elif submode == PinMode.HIZ:
                self._drive_pin(pin, val=0, oe=0)
            elif submode == PinMode.TOG:
                curr_val = (self.core_uo_out >> pin) & MASK_1B
                self._drive_pin(pin, val=curr_val ^ 1, oe=1)

        elif opcode == Opcode.OUT:
            pin = operand & MASK_3B
            if submode in (OutMode.LSB_PIN, OutMode.OD_LSB):
                bit = self.osr & MASK_1B
                self.osr = (self.osr >> 1) & MASK_8B
            else:
                bit = (self.osr >> 7) & MASK_1B
                self.osr = (self.osr << 1) & MASK_8B

            self.crc = crc8_step(self.crc, bit)

            if submode in (OutMode.LSB_PIN, OutMode.MSB_PIN):
                self._drive_pin(pin, val=bit, oe=1)
            else:
                self._drive_pin(pin, val=0, oe=(0 if bit else 1))

        elif opcode == Opcode.IN:
            pin = operand & MASK_3B
            bit = (uio_sampled >> pin) & MASK_1B
            self.last_in_bit = bit
            self.crc = crc8_step(self.crc, bit)

            if submode == InMode.SHIFT_L:
                self.isr = ((self.isr << 1) | bit) & MASK_8B
            else:
                self.isr = ((self.isr >> 1) | (bit << 7)) & MASK_8B

        elif opcode == Opcode.PULL:
            if len(self.tx_fifo) > 0:
                self.osr = self.tx_fifo[0] & MASK_8B
                self.crc = 0x00
                tx_pop = True
            elif submode == FifoMode.BLOCK:
                stalled = True

        elif opcode == Opcode.PUSH:
            if len(self.rx_fifo) < FIFO_DEPTH:
                rx_push_byte = self.isr & MASK_8B
                self.isr = 0x00
            elif submode == FifoMode.BLOCK:
                stalled = True

        elif opcode == Opcode.CRC:
            if submode == CrcMode.MOV_OSR:
                self.osr = self.crc & MASK_8B
            elif submode == CrcMode.MOV_ISR:
                self.isr = self.crc & MASK_8B
            elif submode == CrcMode.RESET:
                self.crc = 0x00

        if not stalled:
            self.pc = next_pc
            self.delay_cnt = delay & MASK_5B

        return tx_pop, rx_push_byte

    def step_core(self, uio_in: int = 0xFF) -> dict:
        """
        Avanza 1 ciclo únicamente del núcleo proto_core (sin sincronizadores 2-FF ni
        pausa por RUN_EN). Útil para pruebas unitarias de protocolos y co-sim de proto_core.v.
        """
        uio_in &= MASK_8B
        self.cycle_count += 1

        tx_pop, rx_push_byte = self._execute_core_cycle(uio_in, self.imem[self.pc])
        if tx_pop:
            self.tx_fifo.popleft()
        if rx_push_byte is not None:
            self.rx_fifo.append(rx_push_byte)

        self.uo_out = self.core_uo_out
        return self.state_snapshot()

    def step(self, ui_in: int = 0x00, uio_in: int = 0xFF) -> dict:
        """
        Avanza 1 ciclo completo de reloj (posedge clk) a nivel de pin del ASIC:
        sincronización 2-FF de ui_in/uio_in -> detección de flancos -> host/BIST + núcleo -> mux uo_out.
        """
        ui_in &= MASK_8B
        uio_in &= MASK_8B
        self.cycle_count += 1

        # 1. Leer salidas de sincronizadores (estado previo al flanco de subida)
        wr_stb   = (self.ui_sync2 >> UI_WR_STB_BIT)  & MASK_1B
        run_en   = (self.ui_sync2 >> UI_RUN_EN_BIT)  & MASK_1B
        bist_en  = (self.ui_sync2 >> UI_BIST_EN_BIT) & MASK_1B
        rx_pop   = (self.ui_sync2 >> UI_RX_POP_BIT)  & MASK_1B
        host_nib = self.ui_sync2 & UI_HOST_NIB_MASK

        wr_rising      = bool(wr_stb and not self.wr_stb_prev)
        rx_pop_rising  = bool(rx_pop and not self.rx_pop_prev)
        run_en_changed = (run_en != self.run_en_prev)
        effective_run  = bool(run_en or bist_en)

        pre_tx_full  = len(self.tx_fifo) >= FIFO_DEPTH
        pre_rx_empty = len(self.rx_fifo) == 0

        # 2. Paso del núcleo (solo si RUN_EN o BIST_EN sincronizados están activos)
        if effective_run:
            instr = BIST_PROGRAM[self.pc & MASK_3B] if bist_en else self.imem[self.pc]
            core_tx_pop, core_rx_push_byte = self._execute_core_cycle(self.uio_sync2, instr)
        else:
            core_tx_pop, core_rx_push_byte = False, None

        # 3. Interfaz host (nibbles MSB-first) y alimentador BIST
        eff_nib_cnt = 0 if run_en_changed else self.nib_cnt
        new_nib_cnt = eff_nib_cnt
        new_bist_idx = self.bist_idx
        bist_push_byte = None
        host_tx_push_byte = None
        host_rx_pop = False

        if bist_en:
            new_nib_cnt = 0
            if self.bist_idx < len(BIST_BYTES) and not pre_tx_full:
                bist_push_byte = BIST_BYTES[self.bist_idx]
                new_bist_idx = (self.bist_idx + 1) & MASK_2B
        elif run_en:
            if wr_rising:
                if eff_nib_cnt == 0:
                    self.nib_shift = host_nib & MASK_16B
                    new_nib_cnt = 1
                else:
                    byte_val = ((self.nib_shift & MASK_4B) << 4) | host_nib
                    if not pre_tx_full:
                        host_tx_push_byte = byte_val & MASK_8B
                    new_nib_cnt = 0
        else:
            if rx_pop_rising and not pre_rx_empty:
                host_rx_pop = True
            if wr_rising:
                next_shift = ((self.nib_shift << 4) | host_nib) & MASK_16B
                self.nib_shift = next_shift
                if eff_nib_cnt == 3:
                    self.imem[self.imem_addr] = next_shift
                    self.imem_addr = (self.imem_addr + 1) & MASK_5B
                    new_nib_cnt = 0
                else:
                    new_nib_cnt = (eff_nib_cnt + 1) & MASK_2B

        # 4. Aplicar actualizaciones de FIFOs (semántica síncrona sin bypass combinacional)
        if core_tx_pop:
            self.tx_fifo.popleft()
        if bist_push_byte is not None:
            self.tx_fifo.append(bist_push_byte)
        elif host_tx_push_byte is not None:
            self.tx_fifo.append(host_tx_push_byte)

        if host_rx_pop:
            self.rx_fifo.popleft()
        if core_rx_push_byte is not None:
            self.rx_fifo.append(core_rx_push_byte)

        # 5. Actualizar registros de interfaz y cadena de sincronizadores 2-FF
        self.nib_cnt     = new_nib_cnt & MASK_2B
        self.bist_idx    = new_bist_idx & MASK_2B
        self.wr_stb_prev = wr_stb
        self.rx_pop_prev = rx_pop
        self.run_en_prev = run_en

        self.ui_sync2  = self.ui_sync1
        self.ui_sync1  = ui_in
        self.uio_sync2 = self.uio_sync1
        self.uio_sync1 = uio_in

        # 6. Mux combinacional de salida uo_out tras el flanco
        post_effective_run = bool(
            ((self.ui_sync2 >> UI_RUN_EN_BIT) & MASK_1B)
            or ((self.ui_sync2 >> UI_BIST_EN_BIT) & MASK_1B)
        )
        rx_head = (self.rx_fifo[0] & MASK_8B) if len(self.rx_fifo) > 0 else 0x00
        self.uo_out = self.core_uo_out if post_effective_run else rx_head

        return self.state_snapshot()

    def state_snapshot(self) -> dict:
        return {
            "cycle": self.cycle_count,
            "pc": self.pc,
            "delay_cnt": self.delay_cnt,
            "osr": self.osr,
            "isr": self.isr,
            "x": self.x,
            "y": self.y,
            "crc": self.crc,
            "last_in_bit": self.last_in_bit,
            "uo_out": self.uo_out,
            "core_uo_out": self.core_uo_out,
            "uio_out": self.uio_out,
            "uio_oe": self.uio_oe,
            "tx_fifo_len": len(self.tx_fifo),
            "rx_fifo_len": len(self.rx_fifo),
            "imem_addr": self.imem_addr,
            "nib_cnt": self.nib_cnt,
            "bist_idx": self.bist_idx,
            "ui_sync2": self.ui_sync2,
            "uio_sync2": self.uio_sync2,
        }


# =====================================================================
# Prueba rápida autónoma: Emisión de un byte ('A' = 0x41) por UART TX
# =====================================================================
if __name__ == "__main__":
    emu = GoldenProtocolEmulator()

    # Cada bit durará 1 + 3 = 4 ciclos de reloj (para ver la traza compacta en terminal)
    BIT_DELAY = 3

    uart_tx_prog = [
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=0),
        encode_instr(Opcode.PULL, FifoMode.BLOCK,  operand=0, delay=0),
        encode_instr(Opcode.SET,  SetDest.X,       operand=7, delay=0),
        encode_instr(Opcode.PIN,  PinMode.CLR,     operand=0, delay=BIT_DELAY),
        encode_instr(Opcode.OUT,  OutMode.LSB_PIN, operand=0, delay=BIT_DELAY),
        encode_instr(Opcode.JMP,  JmpCond.X_NZ,    operand=4, delay=0),
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=BIT_DELAY),
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS,  operand=1, delay=0),
    ]

    emu.load_program(uart_tx_prog)
    emu.host_push_tx(ord("A"))  # 'A' = 0x41 = 0b01000001 (LSB first: 1,0,0,0,0,0,1,0)

    print("Ciclo | PC | Delay | X |  OSR | TX_Pin (uo_out[0]) | CRC8")
    print("-" * 58)
    for _ in range(52):
        st = emu.step_core()
        tx_pin = st["uo_out"] & 1
        print(f" {st['cycle']:03d}  | {st['pc']:02d} |  {st['delay_cnt']:02d}   | {st['x']} | 0x{st['osr']:02X} |         {tx_pin}          | 0x{st['crc']:02X}")
