from pathlib import Path
from random import Random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "model"))

from golden_model import GoldenProtocolEmulator
from isa import JmpCond, MEM_DEPTH, Opcode, PinMode, SetDest, encode_instr


def pad_program(program: list[int]) -> list[int]:
    assert len(program) <= MEM_DEPTH
    return list(program) + [0x0000] * (MEM_DEPTH - len(program))


def check_core_state(dut, emu: GoldenProtocolEmulator, context: str = ""):
    st = emu.state_snapshot()
    prefix = f"[{context}] " if context else ""
    checks = [
        ("pc", int(dut.pc.value), st["pc"]),
        ("delay_cnt", int(dut.delay_cnt.value), st["delay_cnt"]),
        ("x", int(dut.x.value), st["x"]),
        ("y", int(dut.y.value), st["y"]),
        ("uo_out", int(dut.uo_out.value), st["core_uo_out"]),
        ("uio_out", int(dut.uio_out.value), st["uio_out"]),
        ("uio_oe", int(dut.uio_oe.value), st["uio_oe"]),
    ]
    for name, got, expected in checks:
        assert got == expected, f"{prefix}{name} mismatch: got 0x{got:02X}, expected 0x{expected:02X}"


async def init_core(dut, program: list[int], reset_cycles: int = 2) -> tuple[GoldenProtocolEmulator, list[int]]:
    imem = pad_program(program)
    clock = Clock(dut.clk, 20, unit="ns")
    cocotb.start_soon(clock.start())

    emu = GoldenProtocolEmulator()
    emu.load_program(imem)

    dut.rst_n.value = 0
    dut.run_en.value = 0
    dut.instr.value = imem[0]

    for _ in range(reset_cycles):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        check_core_state(dut, emu, context="reset")

    dut.rst_n.value = 1
    dut.run_en.value = 1
    await Timer(1, unit="ns")
    return emu, imem


async def step_and_compare(
    dut,
    emu: GoldenProtocolEmulator,
    imem: list[int],
    rst_n: int = 1,
    run_en: int = 1,
    context: str = "",
):
    dut.rst_n.value = rst_n
    dut.run_en.value = run_en
    dut.instr.value = imem[int(dut.pc.value)]

    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")

    if not rst_n:
        emu.reset()
        emu.load_program(imem)
    elif run_en:
        emu.step_core()

    check_core_state(dut, emu, context=context)


@cocotb.test()
async def test_reset_and_pause(dut):
    """Synchronous rst_n=0 clears state; run_en=0 freezes state even with valid instructions."""
    prog = [
        encode_instr(Opcode.SET, SetDest.X, operand=15, delay=4),
        encode_instr(Opcode.SET, SetDest.PINS, operand=0x1F, delay=0),
        encode_instr(Opcode.PIN, PinMode.SET, operand=7, delay=0),
        encode_instr(Opcode.JMP, JmpCond.ALWAYS, operand=10, delay=0),
    ]
    emu, imem = await init_core(dut, prog, reset_cycles=3)

    # Execute 2 active cycles so registers become non-zero
    await step_and_compare(dut, emu, imem, rst_n=1, run_en=1, context="active_0")
    await step_and_compare(dut, emu, imem, rst_n=1, run_en=1, context="active_1_delay")

    # Pause for 6 cycles: state must remain frozen
    for idx in range(6):
        await step_and_compare(dut, emu, imem, rst_n=1, run_en=0, context=f"paused_{idx}")

    # Resume and execute remaining delay + next instructions
    for idx in range(6):
        await step_and_compare(dut, emu, imem, rst_n=1, run_en=1, context=f"resumed_{idx}")

    # Synchronous reset clears everything back to zero
    await step_and_compare(dut, emu, imem, rst_n=0, run_en=1, context="mid_reset")


@cocotb.test()
async def test_delay_exact_timing(dut):
    """Verify exact 1 + delay cycle timing for delay=0, delay=3, and delay=31."""
    prog = [
        encode_instr(Opcode.SET, SetDest.X, operand=1, delay=0),   # PC=0: 1 cycle
        encode_instr(Opcode.SET, SetDest.Y, operand=2, delay=3),   # PC=1: 4 cycles
        encode_instr(Opcode.PIN, PinMode.SET, operand=0, delay=31),  # PC=2: 32 cycles
        encode_instr(Opcode.JMP, JmpCond.ALWAYS, operand=0, delay=0),
    ]
    emu, imem = await init_core(dut, prog)

    # PC=0 -> 1 cycle
    await step_and_compare(dut, emu, imem, context="delay0")
    assert int(dut.pc.value) == 1

    # PC=1 (delay=3) -> 4 cycles total
    for c in range(4):
        await step_and_compare(dut, emu, imem, context=f"delay3_c{c}")
        if c < 3:
            assert int(dut.pc.value) == 2
            assert int(dut.delay_cnt.value) == 3 - c
    assert int(dut.delay_cnt.value) == 0

    # PC=2 (delay=31) -> 32 cycles total
    for c in range(32):
        await step_and_compare(dut, emu, imem, context=f"delay31_c{c}")
        if c < 31:
            assert int(dut.pc.value) == 3
            assert int(dut.delay_cnt.value) == 31 - c
    assert int(dut.delay_cnt.value) == 0

    # PC=3 -> JMP ALWAYS back to 0
    await step_and_compare(dut, emu, imem, context="jmp_back")
    assert int(dut.pc.value) == 0


@cocotb.test()
async def test_jmp_loops_and_conditions(dut):
    """Exercise JMP X_NZ, JMP Y_NZ, JMP ALWAYS, and JMP PIN_0 (last_in_bit=0)."""
    prog = [
        encode_instr(Opcode.SET, SetDest.X, operand=3, delay=0),      # 0: X=3
        encode_instr(Opcode.PIN, PinMode.TOG, operand=0, delay=0),    # 1: toggle pin 0
        encode_instr(Opcode.JMP, JmpCond.X_NZ, operand=1, delay=0),   # 2: loop 4 iterations total
        encode_instr(Opcode.SET, SetDest.Y, operand=2, delay=1),      # 3: Y=2 (2 cycles)
        encode_instr(Opcode.PIN, PinMode.TOG, operand=1, delay=0),    # 4: toggle pin 1
        encode_instr(Opcode.JMP, JmpCond.Y_NZ, operand=4, delay=0),   # 5: loop 3 iterations total
        encode_instr(Opcode.JMP, JmpCond.PIN_0, operand=8, delay=0),  # 6: last_in_bit==0 -> jump to 8
        encode_instr(Opcode.SET, SetDest.X, operand=31, delay=0),     # 7: skipped
        encode_instr(Opcode.JMP, JmpCond.ALWAYS, operand=0, delay=0), # 8: restart
    ]
    emu, imem = await init_core(dut, prog)

    for cycle in range(50):
        await step_and_compare(dut, emu, imem, context=f"jmp_c{cycle}")


@cocotb.test()
async def test_set_and_pin_operations(dut):
    """Verify SET PINS/OE preserve [7:5], and PIN CLR/SET/HIZ/TOG including OD safety."""
    prog = [
        # Set upper pins 5, 6, 7 high (and OE=1)
        encode_instr(Opcode.PIN, PinMode.SET, operand=5, delay=0),   # 0
        encode_instr(Opcode.PIN, PinMode.SET, operand=6, delay=0),   # 1
        encode_instr(Opcode.PIN, PinMode.SET, operand=7, delay=0),   # 2
        # SET PINS / OE only overwrite [4:0], preserving [7:5]
        encode_instr(Opcode.SET, SetDest.PINS, operand=0x15, delay=0),  # 3
        encode_instr(Opcode.SET, SetDest.OE, operand=0x0A, delay=0),    # 4
        # PIN CLR, SET, HIZ, TOG on various pins
        encode_instr(Opcode.PIN, PinMode.CLR, operand=0, delay=0),   # 5
        encode_instr(Opcode.PIN, PinMode.TOG, operand=0, delay=0),   # 6 -> 1
        encode_instr(Opcode.PIN, PinMode.TOG, operand=0, delay=0),   # 7 -> 0
        encode_instr(Opcode.PIN, PinMode.SET, operand=3, delay=0),   # 8 -> out=1, oe=1
        encode_instr(Opcode.PIN, PinMode.HIZ, operand=3, delay=0),   # 9 -> out=0, oe=0
        encode_instr(Opcode.PIN, PinMode.HIZ, operand=6, delay=0),   # 10 -> out=0, oe=0
        encode_instr(Opcode.JMP, JmpCond.ALWAYS, operand=11, delay=0),
    ]
    emu, imem = await init_core(dut, prog)

    for step_idx in range(14):
        await step_and_compare(dut, emu, imem, context=f"set_pin_{step_idx}")
        if step_idx == 4:
            # Verify bits [7:5] were preserved across SET PINS and SET OE
            assert (int(dut.uo_out.value) >> 5) == 0b111
            assert (int(dut.uio_out.value) >> 5) == 0b111
            assert (int(dut.uio_oe.value) >> 5) == 0b111
        if step_idx == 9:
            # Verify Open-Drain HIZ safety on pin 3
            assert ((int(dut.uio_out.value) >> 3) & 1) == 0
            assert ((int(dut.uio_oe.value) >> 3) & 1) == 0
        if step_idx == 10:
            # Verify Open-Drain HIZ safety on pin 6
            assert ((int(dut.uio_out.value) >> 6) & 1) == 0
            assert ((int(dut.uio_oe.value) >> 6) & 1) == 0


@cocotb.test()
async def test_randomized_s2_program(dut):
    """200 cycles of a deterministic random S2 program with pauses and mid-stream reset."""
    rng = Random(123)
    s2_ops = [Opcode.JMP, Opcode.SET, Opcode.PIN]
    prog = []
    for _ in range(MEM_DEPTH):
        op = rng.choice(s2_ops)
        submode = rng.randint(0, 3)
        operand = rng.randint(0, 31)
        delay = rng.randint(0, 7)
        prog.append(encode_instr(op, submode=submode, operand=operand, delay=delay))

    emu, imem = await init_core(dut, prog)

    for cycle in range(200):
        rst_n = 0 if cycle in (100, 101) else 1
        run_en = 0 if rng.random() < 0.15 else 1
        await step_and_compare(dut, emu, imem, rst_n=rst_n, run_en=run_en, context=f"rand_{cycle}")
