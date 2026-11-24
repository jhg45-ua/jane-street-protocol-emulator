`default_nettype none

// =============================================================================
// Proyecto:     Protocol Emulator ASIC (Tiny Tapeout IHP CMOS5L, 8x4)
// Módulo:       fifo_sync.v
// Descripción:  Cola FIFO síncrona de 4x8 bits (FWFT/peek) sin overflow ni underflow
// Reloj/Reset:  clk (flanco subida) | rst_n (activo bajo, síncrono)
// =============================================================================

module fifo_sync (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       push,
    input  wire [7:0] din,
    input  wire       pop,
    output wire [7:0] dout,
    output wire       full,
    output wire       empty
);

  // Registros internos (39 flip-flops en total: 32 + 2 + 2 + 3)
  reg [7:0] mem[0:3];
  reg [1:0] wptr;
  reg [1:0] rptr;
  reg [2:0] count;

  // Banderas, lectura combinacional en cabeza (peek) y permisos efectivos
  assign empty = (count == 3'd0);
  assign full  = (count == 3'd4);
  assign dout  = empty ? 8'h00 : mem[rptr];

  wire do_push = push & ~full;
  wire do_pop = pop & ~empty;

  // Lógica secuencial: reset síncrono y actualización en flanco de subida
  always @(posedge clk) begin
    if (!rst_n) begin
      mem[0] <= 8'h00;
      mem[1] <= 8'h00;
      mem[2] <= 8'h00;
      mem[3] <= 8'h00;
      wptr   <= 2'd0;
      rptr   <= 2'd0;
      count  <= 3'd0;
    end else begin
      if (do_push) begin
        mem[wptr] <= din;
        wptr      <= wptr + 2'd1;
      end
      if (do_pop) begin
        rptr <= rptr + 2'd1;
      end
      case ({
        do_push, do_pop
      })
        2'b10:   count <= count + 3'd1;
        2'b01:   count <= count - 3'd1;
        default: count <= count;
      endcase
    end
  end

endmodule
