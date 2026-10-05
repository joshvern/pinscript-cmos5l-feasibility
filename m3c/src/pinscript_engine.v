/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// PinScript execution engine, ISA v0.1 (docs/isa.md, docs/timing.md).
//
// One 16-bit word per core cycle. PC addresses the shared asynchronous store
// read port (ADR 005); the word is decoded in the same cycle and every effect
// is registered at the edge ending the cycle. There is no instruction register,
// prefetch phase or branch bubble. All state is clocked by clk; uio_in is
// sampled data through a two-stage synchronizer.
//
// Precedence within one running cycle (isa.md section 8): reset > host STOP >
// PC_RANGE > illegal instruction > instruction stop. A stop cycle commits only
// the run state, REASON, DIAG_IN and (except HALT HOLD) G.
//
// uio_out/uio_oe are registered from the next-state OUT/OE/OD/G values, so they
// equal the isa.md section 4 functions of the registered state in every cycle
// (no added output cycle) and come directly from flip-flops.
module pinscript_engine #(
    parameter integer PROGRAM_DEPTH = 64,
    parameter integer ELAPSED_BITS = 24
) (
    input  wire        clk,
    input  wire        rst_n,
    // Shared program-store read port: valid only while RUN owns it. PC is
    // clog2(depth+1) bits (7 at depth 64), zero-extended on this port.
    output wire [6:0]  pc,
    input  wire [15:0] fetch_word,
    input  wire        fetch_valid,
    // Host events, already accepted by the register decoder (one per cycle).
    input  wire        host_start,
    input  wire        host_stop,
    // FIFOs: decisions use the occupancy registered at the start of the cycle.
    input  wire [2:0]  tx_count,
    input  wire [7:0]  tx_head,
    output wire        tx_pop,
    input  wire [2:0]  rx_count,
    output wire        rx_push,
    output wire [7:0]  rx_push_data,
    // Programmable pins.
    input  wire [7:0]  uio_in,
    output reg  [7:0]  uio_out,
    output reg  [7:0]  uio_oe,
    // Host-visible state (frozen while not RUN).
    output wire        running,
    output wire        drive_gate,
    output wire [3:0]  stop_reason,
    output wire [7:0]  pin_out,
    output wire [7:0]  pin_oe,
    output wire [7:0]  pin_od,
    output wire [7:0]  diag_in,
    output wire [7:0]  reg_r0,
    output wire [7:0]  reg_r1,
    output wire [7:0]  reg_sr,
    output wire [15:0] timer,
    output wire [23:0] elapsed
);
    localparam integer PC_BITS = $clog2(PROGRAM_DEPTH + 1);
    localparam [3:0] HALT = 4'd1, HOST_STOP = 4'd2, ILLEGAL = 4'd3, PC_RANGE = 4'd4,
                     WAIT_TIMEOUT = 4'd5, TX_UNDERFLOW = 4'd6, RX_OVERFLOW = 4'd7;
    localparam [ELAPSED_BITS-1:0] ELAPSED_MAX = {ELAPSED_BITS{1'b1}};

    // ------------------------------------------------------------------ state
    reg run;
    reg [PC_BITS-1:0] pc_q;
    reg [7:0] r0, r1, sr;
    reg [15:0] t;
    reg c;
    reg [7:0] out, oe, od;
    reg g;
    reg [ELAPSED_BITS-1:0] elapsed_q;
    reg [3:0] reason;
    reg [7:0] diag;
    (* async_reg = "true" *) reg [7:0] sync1;
    (* async_reg = "true" *) reg [7:0] sync2;

    assign pc = pc_q;                    // zero-extends at depth 32
    assign running = run;
    assign drive_gate = g;
    assign stop_reason = reason;
    assign pin_out = out;
    assign pin_oe = oe;
    assign pin_od = od;
    assign diag_in = diag;
    assign reg_r0 = r0;
    assign reg_r1 = r1;
    assign reg_sr = sr;
    assign timer = t;
    assign elapsed = elapsed_q;          // zero-extends a reduced test width

    // ------------------------------------------------------------------ decode
    wire legal, op_nop, op_halt, op_fault, op_pull, op_push, op_ldth, op_pin, op_shift,
         op_ld, op_mov, op_jmp, op_djnz, op_delay, op_ldt, op_wait;
    pinscript_decode decode (
        .word(fetch_word), .legal(legal), .op_nop(op_nop), .op_halt(op_halt),
        .op_fault(op_fault), .op_pull(op_pull), .op_push(op_push), .op_ldth(op_ldth),
        .op_pin(op_pin), .op_shift(op_shift), .op_ld(op_ld), .op_mov(op_mov),
        .op_jmp(op_jmp), .op_djnz(op_djnz), .op_delay(op_delay), .op_ldt(op_ldt),
        .op_wait(op_wait)
    );

    wire [4:0] cond = fetch_word[11:7];
    wire [6:0] target7 = {1'b0, fetch_word[5:0]};
    wire [PC_BITS-1:0] target = target7[PC_BITS-1:0];
    wire [PC_BITS-1:0] pc_plus_one = pc_q + 1'b1;   // PC can hold depth: no wrap
    wire [11:0] imm12 = fetch_word[11:0];
    wire [7:0] mask = fetch_word[7:0];
    wire flag0 = fetch_word[0];          // HALT HOLD, PULL/PUSH FAULT
    wire wait_fault = fetch_word[6];     // WAIT ..., FAULT
    wire tx_empty = (tx_count == 3'd0);
    wire rx_full = (rx_count == 3'd4);
    wire t_zero = (t == 16'd0);

    // Condition table (isa.md section 2) on state during the cycle.
    reg cond_true;
    always @* begin
        if (cond[4])
            cond_true = (sync2[cond[2:0]] == cond[3]);
        else begin
            case (cond[3:0])
                4'd0: cond_true = 1'b1;          // ALWAYS
                4'd2: cond_true = tx_empty;      // TXE
                4'd3: cond_true = !tx_empty;     // TXNE
                4'd4: cond_true = rx_full;       // RXF
                4'd5: cond_true = !rx_full;      // RXNF
                4'd6: cond_true = !sr[0];        // LO(SR0)
                4'd7: cond_true = sr[0];         // HI(SR0)
                4'd8: cond_true = !sr[7];        // LO(SR7)
                4'd9: cond_true = sr[7];         // HI(SR7)
                default: cond_true = 1'b0;       // NEVER; reserved codes never decode
            endcase
        end
    end

    // Instruction-caused stops.
    wire inst_stop = op_halt || op_fault ||
                     (op_pull && tx_empty && flag0) ||
                     (op_push && rx_full && flag0) ||
                     (op_wait && !cond_true && t_zero && wait_fault);
    wire [3:0] inst_reason = op_halt ? HALT :
                             op_fault ? {1'b1, fetch_word[2:0]} :
                             op_pull ? TX_UNDERFLOW :
                             op_push ? RX_OVERFLOW : WAIT_TIMEOUT;
    wire stop = run && (host_stop || !fetch_valid || !legal || inst_stop);
    wire [3:0] stop_code = host_stop ? HOST_STOP :
                           !fetch_valid ? PC_RANGE :
                           !legal ? ILLEGAL : inst_reason;
    wire stop_holds = !host_stop && fetch_valid && op_halt && flag0;
    wire exec = run && !stop;

    // Shift datapath: the outgoing bit is taken before this instruction's shift.
    wire shift_msb = fetch_word[11];
    wire outgoing = shift_msb ? sr[7] : sr[0];
    wire incoming = fetch_word[8] ? sync2[fetch_word[4:2]] : 1'b0;
    wire [7:0] out_bit = 8'd1 << fetch_word[7:5];
    wire [7:0] shifted = shift_msb ? {sr[6:0], incoming} : {incoming, sr[7:1]};

    // LD/MOV source and DJNZ decrement.
    wire [7:0] mov_source = (fetch_word[9:8] == 2'd3) ? fetch_word[7:0] :
                            (fetch_word[9:8] == 2'd0) ? r0 :
                            (fetch_word[9:8] == 2'd1) ? r1 : sr;
    wire [7:0] djnz_value = (fetch_word[11] ? r1 : r0) - 8'd1;

    // ------------------------------------------------------------------ next state
    reg n_run, n_c, n_g;
    reg [PC_BITS-1:0] n_pc;
    reg [7:0] n_r0, n_r1, n_sr, n_out, n_oe, n_od, n_diag;
    reg [15:0] n_t;
    reg [ELAPSED_BITS-1:0] n_elapsed;
    reg [3:0] n_reason;
    reg n_tx_pop, n_rx_push;

    always @* begin
        n_run = run; n_pc = pc_q; n_r0 = r0; n_r1 = r1; n_sr = sr; n_t = t; n_c = c;
        n_out = out; n_oe = oe; n_od = od; n_g = g; n_elapsed = elapsed_q;
        n_reason = reason; n_diag = diag;
        n_tx_pop = 1'b0; n_rx_push = 1'b0;
        if (exec) begin
            n_pc = pc_plus_one;
            n_elapsed = (elapsed_q == ELAPSED_MAX) ? elapsed_q : elapsed_q + 1'b1;
            if (op_pull) begin
                if (!tx_empty) begin
                    n_sr = tx_head;
                    n_tx_pop = 1'b1;
                end else
                    n_pc = pc_q;                         // stall (PULL FAULT stopped above)
            end
            if (op_push) begin
                if (!rx_full) n_rx_push = 1'b1;
                else n_pc = pc_q;
            end
            if (op_ldth) n_t = {fetch_word[3:0], t[11:0]};
            if (op_pin) begin
                case (fetch_word[11:9])
                    3'd0: n_out = out | mask;
                    3'd1: n_out = out & ~mask;
                    3'd2: n_out = out ^ mask;
                    3'd3: n_oe = oe | mask;
                    3'd4: n_oe = oe & ~mask;
                    3'd5: n_od = od | mask;
                    default: n_od = od & ~mask;          // 6; 7 never decodes
                endcase
            end
            if (op_shift) begin
                if (fetch_word[10]) n_out = outgoing ? (out | out_bit) : (out & ~out_bit);
                if (fetch_word[9]) n_sr = shifted;
            end
            if (op_ld || op_mov) begin
                case (fetch_word[11:10])
                    2'd0: n_r0 = mov_source;
                    2'd1: n_r1 = mov_source;
                    default: n_sr = mov_source;          // 2; 3 never decodes
                endcase
            end
            if (op_jmp && cond_true) n_pc = target;
            if (op_djnz) begin
                if (fetch_word[11]) n_r1 = djnz_value;
                else n_r0 = djnz_value;
                if (djnz_value != 8'd0) n_pc = target;
            end
            if (op_delay) begin
                if (!c) begin
                    n_t = {4'd0, imm12};
                    if (imm12 != 12'd0) begin
                        n_c = 1'b1;
                        n_pc = pc_q;
                    end
                end else if (t == 16'd1) begin
                    n_t = 16'd0;
                    n_c = 1'b0;
                end else begin
                    n_t = t - 16'd1;
                    n_pc = pc_q;
                end
            end
            if (op_ldt) n_t = {4'd0, imm12};
            if (op_wait && !cond_true) begin
                if (t_zero) n_pc = target;                // WAIT ..., FAULT stopped above
                else begin
                    n_t = t - 16'd1;
                    n_pc = pc_q;
                end
            end
        end else if (stop) begin
            n_run = 1'b0;
            n_reason = stop_code;
            n_diag = sync2;
            if (!stop_holds) n_g = 1'b0;
        end else if (!run) begin
            if (host_start) begin
                n_run = 1'b1; n_pc = {PC_BITS{1'b0}};
                n_r0 = 8'd0; n_r1 = 8'd0; n_sr = 8'd0; n_t = 16'd0; n_c = 1'b0;
                n_out = 8'd0; n_oe = 8'd0; n_od = 8'd0; n_g = 1'b1;
                n_elapsed = {ELAPSED_BITS{1'b0}}; n_reason = 4'd0; n_diag = 8'd0;
            end else if (host_stop)
                n_g = 1'b0;                                // only clears G when not RUN
        end
    end

    assign tx_pop = n_tx_pop;
    assign rx_push = n_rx_push;
    assign rx_push_data = sr;

    always @(posedge clk) begin
        if (!rst_n) begin
            run <= 1'b0; pc_q <= {PC_BITS{1'b0}};
            r0 <= 8'd0; r1 <= 8'd0; sr <= 8'd0; t <= 16'd0; c <= 1'b0;
            out <= 8'd0; oe <= 8'd0; od <= 8'd0; g <= 1'b0;
            elapsed_q <= {ELAPSED_BITS{1'b0}}; reason <= 4'd0; diag <= 8'd0;
            sync1 <= 8'd0; sync2 <= 8'd0;
            uio_out <= 8'd0; uio_oe <= 8'd0;
        end else begin
            run <= n_run; pc_q <= n_pc;
            r0 <= n_r0; r1 <= n_r1; sr <= n_sr; t <= n_t; c <= n_c;
            out <= n_out; oe <= n_oe; od <= n_od; g <= n_g;
            elapsed_q <= n_elapsed; reason <= n_reason; diag <= n_diag;
            sync1 <= uio_in; sync2 <= sync1;
            // isa.md section 4: an open-drain pin can never be driven high.
            uio_out <= n_out & ~n_od;
            uio_oe <= {8{n_g}} & n_oe & (~n_od | ~n_out);
        end
    end
endmodule

`default_nettype wire
