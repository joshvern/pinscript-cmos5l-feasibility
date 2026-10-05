/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// Host register map, interface version 2 (docs/host-interface.md, ADR 006).
// Decodes one complete transport operation per strobe into register effects,
// loader commands and engine host events, and keeps the sticky first error.
// Owns the program store; the store's single read path is addressed by the
// engine PC while RUN and by READ_ADDRESS otherwise.
module pinscript_registers #(
    parameter integer PROGRAM_DEPTH = 64
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        write_strobe,
    input  wire        read_strobe,
    input  wire [7:0]  reg_address,
    input  wire [15:0] write_data,
    output reg  [15:0] read_data,
    input  wire        transport_error_valid,
    input  wire [3:0]  transport_error_code,
    output wire        error_active,
    output wire        program_valid,
    output wire        loading,
    // Engine side of the shared store read path.
    input  wire        engine_busy,
    input  wire [6:0]  engine_pc,
    output wire [15:0] fetch_word,
    output wire        fetch_valid,
    // Accepted host events, at most one per cycle (the strobe cycle).
    output reg         host_start,
    output reg         host_stop,
    output reg         host_tx_push,
    output reg         host_rx_pop,
    output reg         host_fifo_clear,
    // FIFO and engine state for decisions and read-only registers.
    input  wire [2:0]  tx_count,
    input  wire [2:0]  rx_count,
    input  wire [7:0]  rx_head,
    input  wire        engine_stopped,
    input  wire        drive_gate,
    input  wire [3:0]  stop_reason,
    input  wire [7:0]  pin_out,
    input  wire [7:0]  pin_oe,
    input  wire [7:0]  pin_od,
    input  wire [7:0]  diag_in,
    input  wire [7:0]  reg_r0,
    input  wire [7:0]  reg_r1,
    input  wire [7:0]  reg_sr,
    input  wire [15:0] timer,
    input  wire [23:0] elapsed
);
    localparam [15:0] DEVICE_ID = 16'h5053;
    localparam [15:0] INTERFACE_VERSION = 16'h0002;
    localparam [15:0] DEPTH_VALUE = PROGRAM_DEPTH;
    localparam [7:0] LAST_REGISTER = 8'h15;
    localparam [3:0] E_BAD_ADDRESS = 4'd2, E_READ_ONLY = 4'd3, E_BUSY = 4'd9,
                     E_BAD_VALUE = 4'd12, E_NO_PROGRAM = 4'd13, E_TX_FULL = 4'd14,
                     E_RX_EMPTY = 4'd15;
    localparam [15:0] C_BEGIN_LOAD = 16'd1, C_COMMIT = 16'd2, C_ABORT = 16'd3,
                      C_CLEAR_ERROR = 16'd4, C_START = 16'd5, C_STOP = 16'd6,
                      C_RX_POP = 16'd7, C_FIFO_CLEAR = 16'd8;
    reg [15:0] scratch;
    reg [15:0] read_index;
    reg [3:0] sticky_error;
    reg [3:0] access_error;
    reg clear_error;
    reg scratch_write, index_write;
    reg store_command_valid;
    reg [2:0] store_command;
    wire [15:0] expected_length, loaded_count, store_read_data;
    wire store_read_valid;
    wire store_error_valid;
    wire [3:0] store_error_code;
    assign error_active = (sticky_error != 0);

    // ADR 005: the engine owns the only read path for the whole RUN state,
    // including DELAY, WAIT and FIFO stalls. Host readback is zero while RUN.
    wire [15:0] store_address = engine_busy ? {9'b0, engine_pc} : read_index;

    pinscript_program_store #(.PROGRAM_DEPTH(PROGRAM_DEPTH)) program_store (
        .clk(clk), .rst_n(rst_n), .engine_busy(engine_busy),
        .command_valid(store_command_valid), .command(store_command),
        .command_data(write_data), .expected_length(expected_length),
        .loaded_count(loaded_count), .loading(loading), .program_valid(program_valid),
        .read_address(store_address), .read_data(store_read_data),
        .read_valid(store_read_valid),
        .error_valid(store_error_valid), .error_code(store_error_code)
    );
    assign fetch_word = store_read_data;
    assign fetch_valid = program_valid && store_read_valid;

    wire rx_valid = (rx_count != 3'd0);
    wire [1:0] run_state = engine_busy ? 2'd1 : (engine_stopped ? 2'd2 : 2'd0);

    // Combinational register value; the transport captures it at the edge
    // ending the read_strobe cycle (one coherent 16-bit snapshot per frame).
    always @* begin
        case (reg_address)
            8'h00: read_data = DEVICE_ID;
            8'h01: read_data = INTERFACE_VERSION;
            8'h02: read_data = {12'b0, error_active, engine_busy, loading, program_valid};
            8'h03: read_data = scratch;
            8'h05: read_data = {12'b0, sticky_error};
            8'h06: read_data = expected_length;
            8'h07: read_data = loaded_count;
            8'h09: read_data = read_index;
            8'h0a: read_data = engine_busy ? 16'b0 : store_read_data;
            8'h0b: read_data = DEPTH_VALUE;
            8'h0d: read_data = {7'b0, rx_valid, rx_valid ? rx_head : 8'h00};
            8'h0e: read_data = {run_state, drive_gate, 1'b0, stop_reason, 1'b0, engine_pc};
            8'h0f: read_data = {pin_out, pin_oe};
            8'h10: read_data = {pin_od, diag_in};
            8'h11: read_data = {reg_r0, reg_r1};
            8'h12: read_data = {reg_sr, elapsed[23:16]};
            8'h13: read_data = elapsed[15:0];
            8'h14: read_data = timer;
            8'h15: read_data = {5'b0, tx_count, 5'b0, rx_count};
            default: read_data = 16'b0;   // CONTROL, APPEND_DATA, TX_DATA, unknown
        endcase
    end

    // Writes permitted while RUN (ADR 006 allowed-operation table). Every other
    // write is rejected with BUSY before decoding, as in M1.
    wire control_write = (reg_address == 8'h04);
    wire allowed_while_busy = (control_write && (write_data == C_CLEAR_ERROR ||
                                                 write_data == C_STOP ||
                                                 write_data == C_RX_POP)) ||
                              (reg_address == 8'h0c);

    always @* begin
        access_error = 4'b0;
        clear_error = 1'b0;
        scratch_write = 1'b0;
        index_write = 1'b0;
        store_command_valid = 1'b0;
        store_command = 3'b0;
        host_start = 1'b0;
        host_stop = 1'b0;
        host_tx_push = 1'b0;
        host_rx_pop = 1'b0;
        host_fifo_clear = 1'b0;
        if (read_strobe && reg_address > LAST_REGISTER)
            access_error = E_BAD_ADDRESS;
        if (write_strobe) begin
            if (engine_busy && !allowed_while_busy)
                access_error = E_BUSY;
            else begin
                case (reg_address)
                    8'h03: scratch_write = 1'b1;
                    8'h04: begin
                        case (write_data)
                            C_BEGIN_LOAD: begin store_command_valid = 1'b1; store_command = 3'd1; end
                            C_COMMIT: begin store_command_valid = 1'b1; store_command = 3'd3; end
                            C_ABORT: begin store_command_valid = 1'b1; store_command = 3'd4; end
                            C_CLEAR_ERROR: clear_error = 1'b1;
                            C_START: begin
                                if (program_valid && !loading) host_start = 1'b1;
                                else access_error = E_NO_PROGRAM;
                            end
                            C_STOP: host_stop = 1'b1;
                            C_RX_POP: begin
                                if (rx_valid) host_rx_pop = 1'b1;
                                else access_error = E_RX_EMPTY;
                            end
                            C_FIFO_CLEAR: host_fifo_clear = 1'b1;
                            default: access_error = E_BAD_VALUE;
                        endcase
                    end
                    8'h06: begin store_command_valid = 1'b1; store_command = 3'd0; end
                    8'h08: begin store_command_valid = 1'b1; store_command = 3'd2; end
                    8'h09: begin
                        if (write_data >= DEPTH_VALUE) access_error = E_BAD_VALUE;
                        else index_write = 1'b1;
                    end
                    8'h0c: begin
                        if (write_data[15:8] != 8'd0) access_error = E_BAD_VALUE;
                        else if (tx_count == 3'd4) access_error = E_TX_FULL;
                        else host_tx_push = 1'b1;
                    end
                    8'h00, 8'h01, 8'h02, 8'h05, 8'h07, 8'h0a, 8'h0b, 8'h0d, 8'h0e,
                    8'h0f, 8'h10, 8'h11, 8'h12, 8'h13, 8'h14, 8'h15:
                        access_error = E_READ_ONLY;
                    default: access_error = E_BAD_ADDRESS;
                endcase
            end
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            scratch <= 16'b0;
            read_index <= 16'b0;
            sticky_error <= 4'b0;
        end else begin
            if (scratch_write) scratch <= write_data;
            if (index_write) read_index <= write_data;
            // CLEAR_ERROR has explicit priority. The one-operation SPI path
            // cannot simultaneously clear and raise a different access error.
            if (clear_error) sticky_error <= 4'b0;
            else if (sticky_error == 0) begin
                if (transport_error_valid) sticky_error <= transport_error_code;
                else if (access_error != 0) sticky_error <= access_error;
                else if (store_error_valid) sticky_error <= store_error_code;
            end
        end
    end
endmodule

`default_nettype wire
