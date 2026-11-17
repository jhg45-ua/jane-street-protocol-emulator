# model/asm.py
"""
Ensamblador de dos pasadas para la Micro-ISA v0.1 del Protocol Emulator ASIC.
Traduce archivos .asm a listas de enteros de 16 bits o archivos .hex para Verilog.
"""

import re
import sys
from pathlib import Path

from isa import (
    MEM_DEPTH, Opcode, JmpCond, WaitPol, SetDest,
    PinMode, OutMode, InMode, FifoMode, CrcMode,
    encode_instr
)

SUBMODE_TABLES = {
    Opcode.JMP:  {"ALWAYS": JmpCond.ALWAYS, "X_NZ": JmpCond.X_NZ, "Y_NZ": JmpCond.Y_NZ, "PIN_0": JmpCond.PIN_0},
    Opcode.WAIT: {"LOW": WaitPol.LOW, "0": WaitPol.LOW, "HIGH": WaitPol.HIGH, "1": WaitPol.HIGH},
    Opcode.SET:  {"X": SetDest.X, "Y": SetDest.Y, "PINS": SetDest.PINS, "OE": SetDest.OE},
    Opcode.PIN:  {"CLR": PinMode.CLR, "SET": PinMode.SET, "HIZ": PinMode.HIZ, "TOG": PinMode.TOG},
    Opcode.OUT:  {"LSB_PIN": OutMode.LSB_PIN, "MSB_PIN": OutMode.MSB_PIN, "OD_LSB": OutMode.OD_LSB, "OD_MSB": OutMode.OD_MSB},
    Opcode.IN:   {"SHIFT_L": InMode.SHIFT_L, "SHIFT_R": InMode.SHIFT_R},
    Opcode.PULL: {"BLOCK": FifoMode.BLOCK, "NOBLOCK": FifoMode.NOBLOCK},
    Opcode.PUSH: {"BLOCK": FifoMode.BLOCK, "NOBLOCK": FifoMode.NOBLOCK},
    Opcode.CRC:  {"MOV_OSR": CrcMode.MOV_OSR, "MOV_ISR": CrcMode.MOV_ISR, "RESET": CrcMode.RESET},
}


class AssemblerError(Exception):
    pass


def _parse_int(token: str, symbols: dict[str, int], line_num: int) -> int:
    token = token.strip()
    if token in symbols:
        return symbols[token]
    try:
        return int(token, 0)
    except ValueError:
        raise AssemblerError(f"Línea {line_num}: símbolo o número desconocido '{token}'")


def assemble(source: str) -> list[int]:
    """Ensambla texto fuente .asm y devuelve la lista de palabras de 16 bits."""
    symbols: dict[str, int] = {}
    cleaned_lines: list[tuple[int, str]] = []

    # Primera pasada: limpiar comentarios, registrar .define y calcular direcciones de etiquetas
    pc = 0
    for line_num, raw_line in enumerate(source.splitlines(), start=1):
        line = re.split(r"[;#]", raw_line, maxsplit=1)[0].strip()
        if not line:
            continue

        # Directiva .define NOMBRE VALOR
        if line.upper().startswith(".DEFINE"):
            parts = line.split()
            if len(parts) != 3:
                raise AssemblerError(f"Línea {line_num}: sintaxis inválida en .define")
            symbols[parts[1]] = _parse_int(parts[2], symbols, line_num)
            continue

        # Extraer etiqueta si existe (ej. "loop:" o "loop: OUT LSB_PIN, 0")
        while ":" in line:
            label, rest = line.split(":", maxsplit=1)
            label = label.strip()
            if not re.match(r"^[A-Za-z_]\w*$", label):
                raise AssemblerError(f"Línea {line_num}: nombre de etiqueta inválido '{label}'")
            if label in symbols:
                raise AssemblerError(f"Línea {line_num}: etiqueta duplicada '{label}'")
            symbols[label] = pc
            line = rest.strip()

        if line:
            if pc >= MEM_DEPTH:
                raise AssemblerError(f"Línea {line_num}: el programa supera las {MEM_DEPTH} instrucciones")
            cleaned_lines.append((line_num, line))
            pc += 1

    # Segunda pasada: codificar cada instrucción en 16 bits
    program: list[int] = []
    for line_num, line in cleaned_lines:
        delay = 0
        delay_match = re.search(r"\[\s*([^\]]+)\s*\]\s*$", line)
        if delay_match:
            delay = _parse_int(delay_match.group(1), symbols, line_num)
            if not (0 <= delay <= 31):
                raise AssemblerError(f"Línea {line_num}: delay [{delay}] fuera de rango (0..31)")
            line = line[:delay_match.start()].strip()

        parts = line.split(maxsplit=1)
        mnemonic = parts[0].upper()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if mnemonic not in Opcode.__members__:
            raise AssemblerError(f"Línea {line_num}: opcode desconocido '{mnemonic}'")
        opcode = Opcode[mnemonic]
        sub_table = SUBMODE_TABLES[opcode]

        args = [a.strip() for a in rest.split(",") if a.strip()] if rest else []

        # Reglas por defecto para instrucciones abreviadas
        if opcode in (Opcode.PULL, Opcode.PUSH) and len(args) == 0:
            submode = FifoMode.BLOCK
            operand = 0
        elif opcode in (Opcode.PULL, Opcode.PUSH, Opcode.CRC) and len(args) == 1:
            sub_key = args[0].upper()
            if sub_key not in sub_table:
                raise AssemblerError(f"Línea {line_num}: submodo inválido '{args[0]}' para {mnemonic}")
            submode = sub_table[sub_key]
            operand = 0
        elif opcode == Opcode.JMP and len(args) == 1:
            submode = JmpCond.ALWAYS
            operand = _parse_int(args[0], symbols, line_num)
        elif len(args) == 2:
            sub_key = args[0].upper()
            if sub_key not in sub_table:
                raise AssemblerError(f"Línea {line_num}: submodo inválido '{args[0]}' para {mnemonic}")
            submode = sub_table[sub_key]
            operand = _parse_int(args[1], symbols, line_num)
        else:
            raise AssemblerError(f"Línea {line_num}: número de operandos inválido en '{line}'")

        if not (0 <= operand <= 31):
            raise AssemblerError(f"Línea {line_num}: operando {operand} fuera de rango (0..31)")

        program.append(encode_instr(opcode, submode, operand, delay))

    return program


def assemble_file(path: str | Path) -> list[int]:
    return assemble(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 model/asm.py <archivo.asm> [salida.hex]")
        sys.exit(1)

    src_path = Path(sys.argv[1])
    words = assemble_file(src_path)

    print(f"[+] Ensamblado '{src_path.name}': {len(words)}/{MEM_DEPTH} instrucciones")
    for addr, w in enumerate(words):
        print(f"  {addr:02d}: 0x{w:04X}  ({w:016b})")

    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2])
        hex_lines = [f"{w:04X}" for w in words] + ["0000"] * (MEM_DEPTH - len(words))
        out_path.write_text("\n".join(hex_lines) + "\n", encoding="utf-8")
        print(f"[+] Archivo hex guardado en: {out_path}")
