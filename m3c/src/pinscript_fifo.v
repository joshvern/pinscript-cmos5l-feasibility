/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// Four 8-bit entries with occupancy 0..4 (docs/isa.md section 7). Push and pop
// are guarded by the occupancy registered at the start of the cycle: a push to
// a full FIFO and a pop from an empty one are ignored, even if the other side
// transfers in the same cycle. There is no same-cycle bypass. Reset and clear
// empty the pointers only; entry storage is never observable while empty, so
// callers must gate `head` with a nonzero count.
module pinscript_fifo (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       clear,
    input  wire       push,
    input  wire [7:0] push_data,
    input  wire       pop,
    output wire [7:0] head,
    output wire [2:0] count
);
    reg [7:0] slot0, slot1, slot2, slot3;
    reg [1:0] read_pointer;
    reg [2:0] occupancy;
    wire do_push = push && (occupancy != 3'd4);
    wire do_pop = pop && (occupancy != 3'd0);
    wire [1:0] write_pointer = read_pointer + occupancy[1:0];

    assign count = occupancy;
    assign head = (read_pointer == 2'd0) ? slot0 :
                  (read_pointer == 2'd1) ? slot1 :
                  (read_pointer == 2'd2) ? slot2 : slot3;

    always @(posedge clk) begin
        if (!rst_n || clear) begin
            read_pointer <= 2'd0;
            occupancy <= 3'd0;
        end else begin
            if (do_pop) read_pointer <= read_pointer + 2'd1;
            if (do_push && !do_pop) occupancy <= occupancy + 3'd1;
            else if (do_pop && !do_push) occupancy <= occupancy - 3'd1;
        end
    end

    // Storage is not reset. With 0 < occupancy < 4 a simultaneous push and pop
    // use different slots, so the popped head is never the slot written here.
    always @(posedge clk) begin
        if (rst_n && !clear && do_push) begin
            case (write_pointer)
                2'd0: slot0 <= push_data;
                2'd1: slot1 <= push_data;
                2'd2: slot2 <= push_data;
                default: slot3 <= push_data;
            endcase
        end
    end
endmodule

`default_nettype wire
