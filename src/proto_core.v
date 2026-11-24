`default_nettype none

// =============================================================================
// Proyecto:     Protocol Emulator ASIC (Tiny Tapeout IHP CMOS5L, 8x4)
// Módulo:       proto_core.v
// Descripción:  Micro-núcleo programable de ciclo exacto (S2: PC, DELAY, JMP, SET, PIN)
// Reloj/Reset:  clk (flanco subida) | rst_n (activo bajo, síncrono)
// =============================================================================

module proto_core (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        run_en,
    input  wire [15:0] instr,
    output reg  [ 4:0] pc,
    output reg  [ 7:0] uo_out,
    output reg  [ 7:0] uio_out,
    output reg  [ 7:0] uio_oe
);

  // Opcodes (ISA v0.1, bits [15:12])
  localparam [3:0] OP_JMP = 4'h0;
  localparam [3:0] OP_SET = 4'h2;
  localparam [3:0] OP_PIN = 4'h3;

  // Submodos JMP (bits [6:5])
  localparam [1:0] JMP_ALWAYS = 2'b00;
  localparam [1:0] JMP_X_NZ = 2'b01;
  localparam [1:0] JMP_Y_NZ = 2'b10;
  localparam [1:0] JMP_PIN_0 = 2'b11;

  // Submodos SET (bits [6:5])
  localparam [1:0] SET_X = 2'b00;
  localparam [1:0] SET_Y = 2'b01;
  localparam [1:0] SET_PINS = 2'b10;
  localparam [1:0] SET_OE = 2'b11;

  // Submodos PIN (bits [6:5])
  localparam [1:0] PIN_CLR = 2'b00;
  localparam [1:0] PIN_SET = 2'b01;
  localparam [1:0] PIN_HIZ = 2'b10;
  localparam [1:0] PIN_TOG = 2'b11;

  // Decodificación combinacional de la palabra de 16 bits
  wire [3:0] opcode = instr[15:12];
  wire [4:0] delay = instr[11:7];
  wire [1:0] submode = instr[6:5];
  wire [4:0] operand = instr[4:0];
  wire [2:0] pin_idx = operand[2:0];
  // Registros internos del núcleo (S2)
  reg  [4:0] delay_cnt;
  reg  [4:0] x;
  reg  [4:0] y;
  reg        last_in_bit;

  // Ciclo determinista: reset -> run_en -> delay_cnt -> execute -> PC/DELAY_CNT
  always @(posedge clk) begin
    if (!rst_n) begin
      pc          <= 5'd0;
      delay_cnt   <= 5'd0;
      x           <= 5'd0;
      y           <= 5'd0;
      last_in_bit <= 1'b0;
      uo_out      <= 8'h00;
      uio_out     <= 8'h00;
      uio_oe      <= 8'h00;
    end else if (run_en) begin
      if (delay_cnt != 5'd0) begin
        delay_cnt <= delay_cnt - 5'd1;
      end else begin
        // Avance por defecto (si JMP salta, sobreescribe pc más abajo)
        pc        <= pc + 5'd1;
        delay_cnt <= delay;
        case (opcode)
          OP_JMP: begin
            case (submode)
              JMP_ALWAYS: pc <= operand;
              JMP_X_NZ: begin
                if (x != 5'd0) begin
                  x  <= x - 5'd1;
                  pc <= operand;
                end
              end
              JMP_Y_NZ: begin
                if (y != 5'd0) begin
                  y  <= y - 5'd1;
                  pc <= operand;
                end
              end
              JMP_PIN_0: begin
                if (!last_in_bit) begin
                  pc <= operand;
                end
              end
            endcase
          end

          OP_SET: begin
            case (submode)
              SET_X: x <= operand;
              SET_Y: y <= operand;
              SET_PINS: begin
                uo_out[4:0]  <= operand;
                uio_out[4:0] <= operand;
              end
              SET_OE: begin
                uio_oe[4:0] <= operand;
              end
            endcase
          end

          OP_PIN: begin
            case (submode)
              PIN_CLR: begin
                uo_out[pin_idx]  <= 1'b0;
                uio_out[pin_idx] <= 1'b0;
                uio_oe[pin_idx]  <= 1'b1;
              end
              PIN_SET: begin
                uo_out[pin_idx]  <= 1'b1;
                uio_out[pin_idx] <= 1'b1;
                uio_oe[pin_idx]  <= 1'b1;
              end
              PIN_HIZ: begin
                uo_out[pin_idx]  <= 1'b0;
                uio_out[pin_idx] <= 1'b0;
                uio_oe[pin_idx]  <= 1'b0;
              end
              PIN_TOG: begin
                uo_out[pin_idx]  <= ~uo_out[pin_idx];
                uio_out[pin_idx] <= ~uo_out[pin_idx];
                uio_oe[pin_idx]  <= 1'b1;
              end
            endcase
          end

          default: ;  // Opcodes de S3 (WAIT/OUT/IN/PULL/PUSH/CRC) y reservados
        endcase
      end
    end
  end

endmodule
