/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// Strict ISA v0.1 decode (docs/isa.md section 2). Every field not listed as an
// operand must hold its stated value; any other word is illegal. Legality
// depends only on the 16-bit word. Exactly one op_* output is high for a legal
// word and none for an illegal one. Operands are taken from the word directly
// by the engine; this module only classifies.
module pinscript_decode (
    input  wire [15:0] word,
    output wire        legal,
    output wire        op_nop,
    output wire        op_halt,
    output wire        op_fault,
    output wire        op_pull,
    output wire        op_push,
    output wire        op_ldth,
    output wire        op_pin,
    output wire        op_shift,
    output wire        op_ld,
    output wire        op_mov,
    output wire        op_jmp,
    output wire        op_djnz,
    output wire        op_delay,
    output wire        op_ldt,
    output wire        op_wait
);
    wire [3:0] major = word[15:12];
    wire [3:0] sub = word[11:8];
    wire [3:0] arg = word[3:0];
    wire [4:0] cond = word[11:7];
    // 01010..01111 are reserved condition codes.
    wire cond_reserved = !cond[4] && cond[3] && (cond[2] || cond[1]);

    wire sys = (major == 4'h0) && (word[7:4] == 4'h0);
    assign op_nop = sys && (sub == 4'd1) && (arg == 4'd0);
    assign op_halt = sys && (sub == 4'd2) && (arg[3:1] == 3'd0);
    assign op_fault = sys && (sub == 4'd3) && !arg[3];
    assign op_pull = sys && (sub == 4'd4) && (arg[3:1] == 3'd0);
    assign op_push = sys && (sub == 4'd5) && (arg[3:1] == 3'd0);
    assign op_ldth = sys && (sub == 4'd6);

    assign op_pin = (major == 4'h1) && (word[11:9] != 3'd7) && !word[8];

    // SHIFT: d [11], o [10], s [9], i [8], a [7:5], b [4:2], [1:0] = 0.
    // Legal (o,s,i): 100, 110, 111, 010, 011. a = 0 unless o; b = 0 unless i.
    wire sh_o = word[10], sh_s = word[9], sh_i = word[8];
    wire sh_form = sh_o ? (sh_s || !sh_i) : sh_s;
    assign op_shift = (major == 4'h2) && sh_form && (word[1:0] == 2'd0) &&
                      (sh_o || (word[7:5] == 3'd0)) && (sh_i || (word[4:2] == 3'd0));

    wire ldmov = (major == 4'h3) && (word[11:10] != 2'd3);
    assign op_ld = ldmov && (word[9:8] == 2'd3);
    assign op_mov = ldmov && (word[9:8] != 2'd3) && (word[7:0] == 8'd0);

    assign op_jmp = (major == 4'h4) && !cond_reserved && !word[6];
    assign op_djnz = (major == 4'h5) && (word[10:6] == 5'd0);
    assign op_delay = (major == 4'h6);
    assign op_ldt = (major == 4'h7);
    assign op_wait = (major == 4'h8) && !cond_reserved && (!word[6] || (word[5:0] == 6'd0));

    assign legal = op_nop | op_halt | op_fault | op_pull | op_push | op_ldth |
                   op_pin | op_shift | op_ld | op_mov | op_jmp | op_djnz |
                   op_delay | op_ldt | op_wait;
endmodule

`default_nettype wire
