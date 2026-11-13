"""
Golden Model (Instruction Set Simulator de ciclo exacto) para el Protocol Emulator ASIC.
Actúa como oráculo de referencia frente al RTL en Verilog (vía Cocotb).
"""

from collections import deque
from isa import (
    MASK_1B, MASK_3B, MASK_5B, MASK_8B, MASK_16B,
    MEM_DEPTH, FIFO_DEPTH,
    Opcode, JmpCond, WaitPol, SetDest, PinMode,
    OutMode, InMode, FifoMode, CrcMode,
    encode_instr, decode_instr
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
        self.pc        = 0 & MASK_5B
        self.delay_cnt = 0 & MASK_5B
        self.osr       = 0 & MASK_8B
        self.isr       = 0 & MASK_8B
        self.x         = 0 & MASK_5B
        self.y         = 0 & MASK_5B
        self.crc       = 0 & MASK_8B
        self.last_in_bit = 0 & MASK_1B

        # Salidas estándar de Tiny Tapeout
        self.uo_out    = 0 & MASK_8B
        self.uio_out   = 0 & MASK_8B
        self.uio_oe    = 0 & MASK_8B

        # Colas FIFO desacopladas
        self.tx_fifo   = deque(maxlen=FIFO_DEPTH)
        self.rx_fifo   = deque(maxlen=FIFO_DEPTH)

        # Contador global de ciclos simulados
        self.cycle_count = 0

    def load_program(self, program: list[int]):
        """Carga una lista de palabras de 16 bits en la memoria de instrucciones."""
        assert len(program) <= MEM_DEPTH, f"El programa excede {MEM_DEPTH} instrucciones"
        self.imem = [0x0000] * MEM_DEPTH
        for i, word in enumerate(program):
            self.imem[i] = word & MASK_16B

    def host_push_tx(self, byte_val: int) -> bool:
        """El host introduce un byte en la TX_FIFO."""
        if len(self.tx_fifo) < FIFO_DEPTH:
            self.tx_fifo.append(byte_val & MASK_8B)
            return True
        return False

    def host_pop_rx(self) -> int | None:
        """El host extrae un byte recibido de la RX_FIFO."""
        if len(self.rx_fifo) > 0:
            return self.rx_fifo.popleft()
        return None

    def _drive_pin(self, pin: int, val: int, oe: int):
        """Actualiza un único pin en uo_out, uio_out y uio_oe respetando las máscaras de 8 bits."""
        pin = pin & MASK_3B
        bit_mask = 1 << pin
        if val & MASK_1B:
            self.uo_out  = (self.uo_out  | bit_mask) & MASK_8B
            self.uio_out = (self.uio_out | bit_mask) & MASK_8B
        else:
            self.uo_out  = (self.uo_out  & ~bit_mask) & MASK_8B
            self.uio_out = (self.uio_out & ~bit_mask) & MASK_8B

        if oe & MASK_1B:
            self.uio_oe = (self.uio_oe | bit_mask) & MASK_8B
        else:
            self.uio_oe = (self.uio_oe & ~bit_mask) & MASK_8B

    def step(self, uio_in: int = 0xFF) -> dict:
        """
        Avanza exactamente 1 ciclo de reloj (posedge clk).
        uio_in representa el estado físico de los 8 pines bidireccionales vistos desde fuera.
        """
        uio_in &= MASK_8B
        self.cycle_count += 1

        # 1. Si hay un retardo sideband activo de la instrucción anterior, consumir 1 ciclo
        if self.delay_cnt > 0:
            self.delay_cnt = (self.delay_cnt - 1) & MASK_5B
            return self.state_snapshot()

        # 2. Fetch y Decode de la instrucción actual
        instr = self.imem[self.pc]
        opcode, delay, submode, operand = decode_instr(instr)

        next_pc = (self.pc + 1) & MASK_5B
        stalled = False

        # 3. Ejecución determinista en 1 ciclo base
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
            pin_val = (uio_in >> pin) & MASK_1B
            expected = 1 if submode == WaitPol.HIGH else 0
            if pin_val != expected:
                stalled = True  # Congela PC y no carga delay_cnt todavía

        elif opcode == Opcode.SET:
            imm5 = operand & MASK_5B
            if submode == SetDest.X:
                self.x = imm5
            elif submode == SetDest.Y:
                self.y = imm5
            elif submode == SetDest.PINS:
                self.uo_out  = (self.uo_out  & 0xE0) | imm5
                self.uio_out = (self.uio_out & 0xE0) | imm5
            elif submode == SetDest.OE:
                self.uio_oe  = (self.uio_oe  & 0xE0) | imm5

        elif opcode == Opcode.PIN:
            pin = operand & MASK_3B
            if submode == PinMode.CLR:
                self._drive_pin(pin, val=0, oe=1)
            elif submode == PinMode.SET:
                self._drive_pin(pin, val=1, oe=1)
            elif submode == PinMode.HIZ:
                # Seguridad Open-Drain: nunca dejamos uio_out=1 cuando uio_oe=0
                self._drive_pin(pin, val=0, oe=0)
            elif submode == PinMode.TOG:
                curr_val = (self.uo_out >> pin) & MASK_1B
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
                # Modo Open-Drain (I2C/1-Wire): bit=0 -> fuerza 0 (oe=1, out=0); bit=1 -> Z (oe=0, out=0)
                self._drive_pin(pin, val=0, oe=(0 if bit else 1))

        elif opcode == Opcode.IN:
            pin = operand & MASK_3B
            bit = (uio_in >> pin) & MASK_1B
            self.last_in_bit = bit
            self.crc = crc8_step(self.crc, bit)

            if submode == InMode.SHIFT_L:
                self.isr = ((self.isr << 1) | bit) & MASK_8B
            else:
                self.isr = ((self.isr >> 1) | (bit << 7)) & MASK_8B

        elif opcode == Opcode.PULL:
            if len(self.tx_fifo) > 0:
                self.osr = self.tx_fifo.popleft() & MASK_8B
                self.crc = 0x00
            elif submode == FifoMode.BLOCK:
                stalled = True

        elif opcode == Opcode.PUSH:
            if len(self.rx_fifo) < FIFO_DEPTH:
                self.rx_fifo.append(self.isr & MASK_8B)
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

        # 4. Actualizar PC y contador de retardo si la instrucción no está bloqueada
        if not stalled:
            self.pc = next_pc
            self.delay_cnt = delay & MASK_5B

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
            "uo_out": self.uo_out,
            "uio_out": self.uio_out,
            "uio_oe": self.uio_oe,
        }


# =====================================================================
# Prueba rápida autónoma: Emisión de un byte ('A' = 0x41) por UART TX
# =====================================================================
if __name__ == "__main__":
    emu = GoldenProtocolEmulator()

    # Cada bit durará 1 + 3 = 4 ciclos de reloj (para ver la traza compacta en terminal)
    BIT_DELAY = 3

    uart_tx_prog = [
        # 0: Estado reposo UART (línea TX en alto = 1)
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=0),
        # 1: Espera bloqueante a que el host deposite un byte en TX_FIFO -> OSR
        encode_instr(Opcode.PULL, FifoMode.BLOCK,  operand=0, delay=0),
        # 2: Cargar contador X = 7 (para iterar 8 bits: 7..0)
        encode_instr(Opcode.SET,  SetDest.X,       operand=7, delay=0),
        # 3: Bit de START (bajar pin 0 a '0' durante 4 ciclos)
        encode_instr(Opcode.PIN,  PinMode.CLR,     operand=0, delay=BIT_DELAY),
        # 4 (loop): Extraer 1 bit LSB del OSR al pin 0 durante 4 ciclos
        encode_instr(Opcode.OUT,  OutMode.LSB_PIN, operand=0, delay=BIT_DELAY),
        # 5: Si X != 0, decrementar X y saltar a la instrucción 4 (0 ciclos extra si salta)
        encode_instr(Opcode.JMP,  JmpCond.X_NZ,    operand=4, delay=0),
        # 6: Bit de STOP (subir pin 0 a '1' durante 4 ciclos)
        encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=BIT_DELAY),
        # 7: Volver a la instrucción 1 a esperar el siguiente byte
        encode_instr(Opcode.JMP,  JmpCond.ALWAYS,  operand=1, delay=0),
    ]

    emu.load_program(uart_tx_prog)
    emu.host_push_tx(ord('A'))  # 'A' = 0x41 = 0b01000001 (LSB first: 1,0,0,0,0,0,1,0)

    print("Ciclo | PC | Delay | X |  OSR | TX_Pin (uo_out[0]) | CRC8")
    print("-" * 58)
    for _ in range(52):
        st = emu.step()
        tx_pin = st["uo_out"] & 1
        print(f" {st['cycle']:03d}  | {st['pc']:02d} |  {st['delay_cnt']:02d}   | {st['x']} | 0x{st['osr']:02X} |         {tx_pin}          | 0x{st['crc']:02X}")
