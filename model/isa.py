"""
Especificación de la Micro-ISA v0.1 para el Protocol Emulator ASIC.
Define opcodes, submodos, anchos de registros y funciones de codificación/decodificación.
"""

from enum import IntEnum

# Máscaras estrictas de hardware (evitan enteros infinitos en Python)
MASK_1B  = 0x1
MASK_2B  = 0x3
MASK_3B  = 0x7
MASK_4B  = 0xF
MASK_5B  = 0x1F
MASK_8B  = 0xFF
MASK_16B = 0xFFFF

MEM_DEPTH = 32  # 32 palabras de 16 bits
FIFO_DEPTH = 4  # 4 bytes de profundidad en TX_FIFO y RX_FIFO


class Opcode(IntEnum):
    """
    Códigos de operación de la Micro-ISA (4 bits).
    Cada instrucción es de 16 bits: [opcode:4][delay:5][submode:2][operand:5].
    """
    JMP  = 0x0
    WAIT = 0x1
    SET  = 0x2
    PIN  = 0x3
    OUT  = 0x4
    IN   = 0x5
    PULL = 0x6
    PUSH = 0x7
    CRC  = 0x8


class JmpCond(IntEnum):
    """
    Condiciones de salto para la instrucción JMP (2 bits).
    - ALWAYS: Salta siempre.
    - X_NZ: Salta si el registro X no es cero.
    - Y_NZ: Salta si el registro Y no es cero.
    - PIN_0: Salta si el pin de entrada ui_in[0] es 0.
    """
    ALWAYS = 0b00
    X_NZ   = 0b01
    Y_NZ   = 0b10
    PIN_0  = 0b11


class WaitPol(IntEnum):
    """
    Polaridad de espera para la instrucción WAIT (2 bits).
    - LOW: Espera hasta que el pin de entrada ui_in[0] sea 0.
    - HIGH: Espera hasta que el pin de entrada ui_in[0] sea 1.
    """
    LOW  = 0b00
    HIGH = 0b01


class SetDest(IntEnum):
    """
    Destino del registro para la instrucción SET (2 bits).
    - X: Establece el registro X.
    - Y: Establece el registro Y.
    - PINS: Establece los pines de salida ui_out[7:0].
    - OE: Establece el registro de habilitación de salida ui_oe[7:0].
    """
    X    = 0b00
    Y    = 0b01
    PINS = 0b10
    OE   = 0b11


class PinMode(IntEnum):
    """
    Modo de operación del pin para la instrucción PIN (2 bits).
    - CLR: Fuerza 0 activo (ui_out=0, ui_oe=1).
    - SET: Fuerza 1 activo (ui_out=1, ui_oe=1).
    - HIZ: Alta impedancia / Open-Drain Z (ui_out=0, ui_oe=0).
    - TOG: Conmuta el estado del pin (útil para reloj SCK).
    """
    CLR = 0b00  # Fuerza 0 activo (uio_out=0, uio_oe=1)
    SET = 0b01  # Fuerza 1 activo (uio_out=1, uio_oe=1)
    HIZ = 0b10  # Alta impedancia / Open-Drain Z (uio_out=0, uio_oe=0)
    TOG = 0b11  # Conmuta el estado del pin (útil para reloj SCK)


class OutMode(IntEnum):
    """
    Modo de salida para la instrucción OUT (2 bits).
    - LSB_PIN: Saca OSR[0] push-pull y desplaza OSR >>= 1 (UART).
    - MSB_PIN: Saca OSR[7] push-pull y desplaza OSR <<= 1 (SPI).
    - OD_LSB: Saca OSR[0] en Open-Drain y desplaza OSR >>= 1 (1-Wire).
    - OD_MSB: Saca OSR[7] en Open-Drain y desplaza OSR <<= 1 (I2C).
    """
    LSB_PIN = 0b00  # Saca OSR[0] push-pull y desplaza OSR >>= 1 (UART)
    MSB_PIN = 0b01  # Saca OSR[7] push-pull y desplaza OSR <<= 1 (SPI)
    OD_LSB  = 0b10  # Saca OSR[0] en Open-Drain y desplaza OSR >>= 1 (1-Wire)
    OD_MSB  = 0b11  # Saca OSR[7] en Open-Drain y desplaza OSR <<= 1 (I2C)


class InMode(IntEnum):
    """
    Modo de entrada para la instrucción IN (2 bits).
    - SHIFT_L: Desplaza ISR <<= 1 y captura ui_in[0] (MSB-first: SPI, I2C).
    - SHIFT_R: Desplaza ISR >>= 1 y captura ui_in[0] en ISR[7] (LSB-first: UART RX).
    """
    SHIFT_L = 0b00  # ISR = (ISR << 1) | pin (MSB-first: SPI, I2C)
    SHIFT_R = 0b01  # ISR = (ISR >> 1) | (pin << 7) (LSB-first: UART RX)


class FifoMode(IntEnum):
    """
    Modo de operación del FIFO para las instrucciones PULL y PUSH (2 bits).
    - BLOCK: Bloquea si el FIFO está vacío (PULL) o lleno (PUSH).
    - NOBLOCK: No bloquea; PULL devuelve 0 si el FIFO está vacío, y PUSH descarta el byte si el FIFO está lleno.
    """
    BLOCK   = 0b00
    NOBLOCK = 0b01


class CrcMode(IntEnum):
    """
    Modo de operación del CRC para las instrucciones CRC (2 bits).
    - MOV_OSR: Mueve el CRC a OSR (para enviar el byte de CRC al final del paquete).
    - MOV_ISR: Mueve el CRC a ISR.
    - RESET: Reinicia el CRC a 0x00.
    """
    MOV_OSR = 0b00  # OSR = CRC (para enviar el byte de CRC al final del paquete)
    MOV_ISR = 0b01  # ISR = CRC
    RESET   = 0b10  # CRC = 0x00


def encode_instr(opcode: int, submode: int = 0, operand: int = 0, delay: int = 0) -> int:
    """Empaqueta una instrucción en una palabra de 16 bits exacta."""
    op_b  = (opcode  & MASK_4B) << 12
    del_b = (delay   & MASK_5B) << 7
    sub_b = (submode & MASK_2B) << 5
    arg_b = (operand & MASK_5B)
    return (op_b | del_b | sub_b | arg_b) & MASK_16B


def decode_instr(word: int) -> tuple[int, int, int, int]:
    """Desempaqueta una palabra de 16 bits en (opcode, delay, submode, operand)."""
    word    = word & MASK_16B
    opcode  = (word >> 12) & MASK_4B
    delay   = (word >> 7)  & MASK_5B
    submode = (word >> 5)  & MASK_2B
    operand = word & MASK_5B
    return opcode, delay, submode, operand


# Asignación de pines de entrada dedicados (ui_in[7:0]) para la interfaz host
UI_HOST_NIB_MASK = MASK_4B
UI_RX_POP_BIT    = 4
UI_BIST_EN_BIT   = 5
UI_RUN_EN_BIT    = 6
UI_WR_STB_BIT    = 7


def pack_ui_in(
    wr_stb: int = 0,
    run_en: int = 0,
    bist_en: int = 0,
    rx_pop: int = 0,
    host_nib: int = 0,
) -> int:
    """Empaqueta las señales de control y el nibble host en un byte ui_in[7:0]."""
    return (
        ((wr_stb  & MASK_1B) << UI_WR_STB_BIT)
        | ((run_en  & MASK_1B) << UI_RUN_EN_BIT)
        | ((bist_en & MASK_1B) << UI_BIST_EN_BIT)
        | ((rx_pop  & MASK_1B) << UI_RX_POP_BIT)
        | (host_nib & UI_HOST_NIB_MASK)
    ) & MASK_8B


# Micro-ROM BIST: UART TX 8N1 a 4 ciclos/bit (idéntica a firmware/uart_tx.asm)
BIST_BYTES = (ord("O"), ord("K"), ord("\n"))
BIST_PROGRAM = (
    encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=0),  # 0: TX idle = 1
    encode_instr(Opcode.PULL, FifoMode.BLOCK,  operand=0, delay=0),  # 1: TX_FIFO -> OSR
    encode_instr(Opcode.SET,  SetDest.X,       operand=7, delay=0),  # 2: 8 bits
    encode_instr(Opcode.PIN,  PinMode.CLR,     operand=0, delay=3),  # 3: START (4c)
    encode_instr(Opcode.OUT,  OutMode.LSB_PIN, operand=0, delay=2),  # 4: Bit LSB (3c + JMP = 4c)
    encode_instr(Opcode.JMP,  JmpCond.X_NZ,    operand=4, delay=0),  # 5: Loop 8 bits
    encode_instr(Opcode.PIN,  PinMode.SET,     operand=0, delay=3),  # 6: STOP (4c)
    encode_instr(Opcode.JMP,  JmpCond.ALWAYS,  operand=1, delay=0),  # 7: Siguiente byte
)
