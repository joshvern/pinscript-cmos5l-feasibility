/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pinscript_registers #(
    parameter integer PROGRAM_DEPTH = 64
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        engine_busy,
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
    input  wire [15:0] fetch_address,
    output wire [15:0] fetch_data
);
    localparam [15:0] DEPTH_VALUE = PROGRAM_DEPTH;
    reg [15:0] scratch;
    reg [15:0] read_index;
    reg [3:0] sticky_error;
    reg [3:0] access_error;
    reg clear_error;
    reg scratch_write, index_write;
    reg store_command_valid;
    reg [2:0] store_command;
    wire [15:0] expected_length, loaded_count, store_read_data;
    wire store_error_valid;
    wire [3:0] store_error_code;
    assign error_active = (sticky_error != 0);

    pinscript_program_store #(.PROGRAM_DEPTH(PROGRAM_DEPTH)) program_store (
        .clk(clk), .rst_n(rst_n), .engine_busy(engine_busy),
        .command_valid(store_command_valid), .command(store_command),
        .command_data(write_data), .expected_length(expected_length),
        .loaded_count(loaded_count), .loading(loading), .program_valid(program_valid),
        .read_address(read_index), .read_data(store_read_data),
        .fetch_address(fetch_address), .fetch_data(fetch_data),
        .error_valid(store_error_valid), .error_code(store_error_code)
    );

    always @* begin
        case (reg_address)
            8'h00: read_data = 16'h5053;
            8'h01: read_data = 16'h0001;
            8'h02: read_data = {12'b0, error_active, engine_busy, loading, program_valid};
            8'h03: read_data = scratch;
            8'h04: read_data = 16'b0;
            8'h05: read_data = {12'b0, sticky_error};
            8'h06: read_data = expected_length;
            8'h07: read_data = loaded_count;
            8'h08: read_data = 16'b0;
            8'h09: read_data = read_index;
            8'h0a: read_data = store_read_data;
            8'h0b: read_data = DEPTH_VALUE;
            default: read_data = 16'b0;
        endcase
    end

    always @* begin
        access_error = 4'b0;
        clear_error = 1'b0;
        scratch_write = 1'b0;
        index_write = 1'b0;
        store_command_valid = 1'b0;
        store_command = 3'b0;
        if (read_strobe && reg_address > 8'h0b)
            access_error = 4'd2; // BAD_ADDRESS
        if (write_strobe) begin
            if (engine_busy && !(reg_address == 8'h04 && write_data == 16'd4))
                access_error = 4'd9;
            else begin
                case (reg_address)
                    8'h03: scratch_write = 1'b1;
                    8'h04: begin
                        case (write_data)
                            16'd1: begin store_command_valid = 1'b1; store_command = 3'd1; end
                            16'd2: begin store_command_valid = 1'b1; store_command = 3'd3; end
                            16'd3: begin store_command_valid = 1'b1; store_command = 3'd4; end
                            16'd4: clear_error = 1'b1;
                            16'd5: access_error = 4'd10; // UNSUPPORTED START
                            default: access_error = 4'd12;
                        endcase
                    end
                    8'h06: begin store_command_valid = 1'b1; store_command = 3'd0; end
                    8'h08: begin store_command_valid = 1'b1; store_command = 3'd2; end
                    8'h09: begin
                        if (write_data >= DEPTH_VALUE) access_error = 4'd12;
                        else index_write = 1'b1;
                    end
                    8'h00, 8'h01, 8'h02, 8'h05, 8'h07, 8'h0a, 8'h0b:
                        access_error = 4'd3; // READ_ONLY
                    default: access_error = 4'd2;
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
