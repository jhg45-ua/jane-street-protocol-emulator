from collections import deque
from random import Random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

FIFO_DEPTH = 4


class RefFifo:
    """Reference model matching model/golden_model.py FIFO semantics."""

    def __init__(self, depth: int = FIFO_DEPTH):
        self.depth = depth
        self.q = deque()

    def reset(self):
        self.q.clear()

    def step(self, rst_n: int, push: int, din: int, pop: int):
        if not rst_n:
            self.q.clear()
            return
        pre_full = len(self.q) >= self.depth
        pre_empty = len(self.q) == 0
        do_push = bool(push) and not pre_full
        do_pop = bool(pop) and not pre_empty
        if do_pop:
            self.q.popleft()
        if do_push:
            self.q.append(din & 0xFF)

    @property
    def empty(self) -> int:
        return 1 if len(self.q) == 0 else 0

    @property
    def full(self) -> int:
        return 1 if len(self.q) >= self.depth else 0

    @property
    def dout(self) -> int:
        return (self.q[0] & 0xFF) if len(self.q) > 0 else 0x00


async def init_and_reset(dut, cycles: int = 2) -> RefFifo:
    clock = Clock(dut.clk, 20, unit="ns")
    cocotb.start_soon(clock.start())

    dut.rst_n.value = 0
    dut.push.value = 0
    dut.din.value = 0
    dut.pop.value = 0

    ref = RefFifo()
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        ref.step(0, 0, 0, 0)
        check_outputs(dut, ref)

    dut.rst_n.value = 1
    await Timer(1, unit="ns")
    return ref


def check_outputs(dut, ref: RefFifo, context: str = ""):
    prefix = f"[{context}] " if context else ""
    assert int(dut.empty.value) == ref.empty, (
        f"{prefix}empty mismatch: got {int(dut.empty.value)}, expected {ref.empty}"
    )
    assert int(dut.full.value) == ref.full, (
        f"{prefix}full mismatch: got {int(dut.full.value)}, expected {ref.full}"
    )
    assert int(dut.dout.value) == ref.dout, (
        f"{prefix}dout mismatch: got 0x{int(dut.dout.value):02X}, expected 0x{ref.dout:02X}"
    )


async def step_cycle(dut, ref: RefFifo, rst_n: int, push: int, din: int, pop: int, context: str = ""):
    dut.rst_n.value = rst_n
    dut.push.value = push
    dut.din.value = din & 0xFF
    dut.pop.value = pop
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    ref.step(rst_n, push, din, pop)
    check_outputs(dut, ref, context=context)


@cocotb.test()
async def test_reset_state(dut):
    """After synchronous rst_n=0, FIFO is empty=1, full=0, dout=0x00."""
    ref = await init_and_reset(dut, cycles=3)
    check_outputs(dut, ref, context="post_reset")


@cocotb.test()
async def test_fill_overflow_drain_underflow(dut):
    """Fill to 4, attempt overflow pushes, drain in FIFO order, attempt underflow pops, recover."""
    ref = await init_and_reset(dut)

    # Fill 4 bytes
    for idx, val in enumerate([0x11, 0x22, 0x33, 0x44]):
        await step_cycle(dut, ref, 1, 1, val, 0, context=f"fill_{idx}")
        assert int(dut.dout.value) == 0x11

    assert int(dut.full.value) == 1
    assert int(dut.empty.value) == 0

    # 2 overflow pushes while full
    for idx, val in enumerate([0xAA, 0xBB]):
        await step_cycle(dut, ref, 1, 1, val, 0, context=f"overflow_{idx}")

    # Drain all 4 bytes in FIFO order
    for idx in range(FIFO_DEPTH):
        await step_cycle(dut, ref, 1, 0, 0x00, 1, context=f"drain_{idx}")

    assert int(dut.empty.value) == 1
    assert int(dut.full.value) == 0
    assert int(dut.dout.value) == 0x00

    # 2 underflow pops while empty
    for idx in range(2):
        await step_cycle(dut, ref, 1, 0, 0x00, 1, context=f"underflow_{idx}")

    # Recover with 1 push and 1 pop
    await step_cycle(dut, ref, 1, 1, 0x5A, 0, context="recover_push")
    assert int(dut.dout.value) == 0x5A
    await step_cycle(dut, ref, 1, 0, 0x00, 1, context="recover_pop")
    assert int(dut.empty.value) == 1


@cocotb.test()
async def test_simultaneous_push_pop(dut):
    """Simultaneous push+pop in empty, intermediate (1..3), and full regimes."""
    ref = await init_and_reset(dut)

    # 1. Empty regime: simultaneous push+pop -> only push takes effect
    await step_cycle(dut, ref, 1, 1, 0xA1, 1, context="simul_empty")
    assert len(ref.q) == 1
    assert int(dut.dout.value) == 0xA1

    # 2. Intermediate regime (count=1..3): both push & pop take effect, count stays constant
    await step_cycle(dut, ref, 1, 1, 0xB2, 0, context="add_second")  # q = [A1, B2]
    await step_cycle(dut, ref, 1, 1, 0xC3, 1, context="simul_inter_c2")  # q = [B2, C3]
    assert int(dut.dout.value) == 0xB2

    await step_cycle(dut, ref, 1, 1, 0xD4, 0, context="add_third")  # q = [B2, C3, D4]
    await step_cycle(dut, ref, 1, 1, 0xE5, 1, context="simul_inter_c3")  # q = [C3, D4, E5]
    assert int(dut.dout.value) == 0xC3

    # 3. Full regime (count=4): no combinational bypass -> only pop takes effect, din discarded
    await step_cycle(dut, ref, 1, 1, 0xF6, 0, context="fill_to_4")  # q = [C3, D4, E5, F6]
    assert int(dut.full.value) == 1
    await step_cycle(dut, ref, 1, 1, 0x99, 1, context="simul_full")  # q = [D4, E5, F6]
    assert int(dut.full.value) == 0
    assert int(dut.dout.value) == 0xD4

    # Drain remaining and verify 0x99 was never inserted
    for idx in range(3):
        await step_cycle(dut, ref, 1, 0, 0x00, 1, context=f"drain_rem_{idx}")
    assert int(dut.empty.value) == 1


@cocotb.test()
async def test_wraparound_and_randomized(dut):
    """250 randomized cycles covering wrap-around and mid-stream synchronous reset."""
    ref = await init_and_reset(dut)
    rng = Random(42)

    for cycle in range(250):
        # Exercise a mid-stream synchronous reset around cycle 125
        if cycle in (125, 126):
            rst_n = 0
        else:
            rst_n = 1
        push = rng.randint(0, 1)
        pop = rng.randint(0, 1)
        din = rng.randint(0, 255)
        await step_cycle(dut, ref, rst_n, push, din, pop, context=f"rand_{cycle}")
