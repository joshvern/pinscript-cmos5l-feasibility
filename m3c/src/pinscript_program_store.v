/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// A sequentially written, bounded program image. Supported project depths are
// 32 and 64. The array is deliberately neither reset nor initialized. Every
// command is rejected (BUSY) while the engine runs, independently of the
// register decoder's interlock.
module pinscript_program_store #(
    parameter integer PROGRAM_DEPTH = 64
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        engine_busy,
    input  wire        command_valid,
    input  wire [2:0]  command,
    input  wire [15:0] command_data,
    output wire [15:0] expected_length,
    output wire [15:0] loaded_count,
    output reg         loading,
    output reg         program_valid,
    input  wire [15:0] read_address,
    output reg  [15:0] read_data,
    output wire        read_valid,
    output reg         error_valid,
    output reg  [3:0]  error_code
);
    localparam integer ADDRESS_WIDTH = (PROGRAM_DEPTH > 1) ? $clog2(PROGRAM_DEPTH) : 1;
    localparam integer COUNT_WIDTH = $clog2(PROGRAM_DEPTH + 1);
    localparam [15:0] DEPTH_VALUE = PROGRAM_DEPTH;
    localparam [2:0] SET_LENGTH = 3'd0, BEGIN_LOAD = 3'd1,
                     APPEND = 3'd2, COMMIT = 3'd3, ABORT = 3'd4;
    reg [15:0] memory [0:PROGRAM_DEPTH-1];
    reg [COUNT_WIDTH-1:0] length;
    reg [COUNT_WIDTH-1:0] count;

    assign expected_length = {{(16-COUNT_WIDTH){1'b0}}, length};
    assign loaded_count = {{(16-COUNT_WIDTH){1'b0}}, count};

    // One shared asynchronous read path (ADR 005/006). The caller selects the
    // address: the engine PC while RUN, the host READ_ADDRESS otherwise. The
    // output is masked before the memory is read, so words beyond the accepted
    // count (stale or never written) read as zero with read_valid low. While
    // RUN loading is impossible, so read_valid then means "PC < committed
    // count". Every state element derived from read_data is registered on clk.
    assign read_valid = (loading || program_valid) && read_address < loaded_count;
    always @* begin
        read_data = 16'b0;
        if (read_valid)
            read_data = memory[read_address[ADDRESS_WIDTH-1:0]];
    end

    // Error response describes the currently presented command, before its
    // acceptance edge. Callers may sample it on the same edge as the command.
    always @* begin
        error_valid = 1'b0;
        error_code = 4'b0;
        if (command_valid) begin
            if (engine_busy)
                error_code = 4'd9;
            else begin
                case (command)
                    SET_LENGTH: begin
                        if (loading) error_code = 4'd6; // LENGTH_LOCKED
                        else if (command_data == 0 || command_data > DEPTH_VALUE)
                            error_code = 4'd4; // INVALID_LENGTH
                    end
                    BEGIN_LOAD: begin
                        if (loading) error_code = 4'd5; // LOAD_STATE
                        else if (length == 0) error_code = 4'd4;
                    end
                    APPEND: begin
                        if (!loading) error_code = 4'd5;
                        else if (count >= length) error_code = 4'd8; // OVERRUN
                    end
                    COMMIT: begin
                        if (!loading) error_code = 4'd5;
                        else if (count != length) error_code = 4'd7; // INCOMPLETE
                    end
                    ABORT: error_code = 4'd0;
                    default: error_code = 4'd12; // BAD_VALUE
                endcase
            end
            error_valid = (error_code != 0);
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            length <= {COUNT_WIDTH{1'b0}};
            count <= {COUNT_WIDTH{1'b0}};
            loading <= 1'b0;
            program_valid <= 1'b0;
        end else if (command_valid && !error_valid) begin
            case (command)
                SET_LENGTH: length <= command_data[COUNT_WIDTH-1:0];
                BEGIN_LOAD: begin
                    count <= {COUNT_WIDTH{1'b0}};
                    loading <= 1'b1;
                    program_valid <= 1'b0;
                end
                APPEND: begin
                    memory[count[ADDRESS_WIDTH-1:0]] <= command_data;
                    count <= count + {{(COUNT_WIDTH-1){1'b0}}, 1'b1};
                end
                COMMIT: begin
                    loading <= 1'b0;
                    program_valid <= 1'b1;
                end
                ABORT: begin
                    count <= {COUNT_WIDTH{1'b0}};
                    loading <= 1'b0;
                    program_valid <= 1'b0;
                end
                default: begin end
            endcase
        end
    end
endmodule

`default_nettype wire
