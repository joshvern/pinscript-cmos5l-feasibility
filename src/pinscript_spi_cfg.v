/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// Oversampled mode-0 SPI. Every external half-period/setup/hold/gap must span
// at least eight clk periods. Only clk clocks state; SCLK is synchronized data.
module pinscript_spi_cfg (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    input  wire        spi_cs_n,
    output wire        miso,
    output reg         write_strobe,
    output reg         read_strobe,
    output reg  [7:0]  reg_address,
    output reg  [15:0] write_data,
    input  wire [15:0] read_data,
    output reg         error_valid,
    output reg  [3:0]  error_code
);
    (* async_reg = "true" *) reg sclk_meta, sclk_sync;
    (* async_reg = "true" *) reg mosi_meta, mosi_sync;
    (* async_reg = "true" *) reg cs_meta, cs_sync;
    reg sclk_previous, cs_previous;
    wire sclk_rise = sclk_sync && !sclk_previous;
    wire sclk_fall = !sclk_sync && sclk_previous;

    reg active, complete;
    reg [5:0] bit_count;
    reg [15:0] receive_shift;
    reg [7:0] command;
    reg [15:0] transmit_shift;
    reg read_pending;
    reg miso_bit;

    // MISO is dedicated (never high impedance). Deselect immediately clamps it.
    assign miso = spi_cs_n ? 1'b0 : miso_bit;

    always @(posedge clk) begin
        if (!rst_n) begin
            sclk_meta <= 1'b0;
            sclk_sync <= 1'b0;
            sclk_previous <= 1'b0;
            mosi_meta <= 1'b0;
            mosi_sync <= 1'b0;
            cs_meta <= 1'b1;
            cs_sync <= 1'b1;
            cs_previous <= 1'b1;
            active <= 1'b0;
            complete <= 1'b0;
            bit_count <= 6'b0;
            receive_shift <= 16'b0;
            command <= 8'b0;
            transmit_shift <= 16'b0;
            read_pending <= 1'b0;
            miso_bit <= 1'b0;
            write_strobe <= 1'b0;
            read_strobe <= 1'b0;
            reg_address <= 8'b0;
            write_data <= 16'b0;
            error_valid <= 1'b0;
            error_code <= 4'b0;
        end else begin
            sclk_meta <= spi_sclk;
            sclk_sync <= sclk_meta;
            sclk_previous <= sclk_sync;
            mosi_meta <= spi_mosi;
            mosi_sync <= mosi_meta;
            cs_meta <= spi_cs_n;
            cs_sync <= cs_meta;
            cs_previous <= cs_sync;
            write_strobe <= 1'b0;
            read_strobe <= 1'b0;
            error_valid <= 1'b0;
            error_code <= 4'b0;

            if (cs_sync) begin
                if (!cs_previous && active && bit_count != 0 && !complete) begin
                    error_valid <= 1'b1;
                    error_code <= 4'd11; // TRUNCATED: no incomplete write
                end
                active <= 1'b0;
                complete <= 1'b0;
                bit_count <= 6'b0;
                receive_shift <= 16'b0;
                transmit_shift <= 16'b0;
                read_pending <= 1'b0;
                miso_bit <= 1'b0;
            end else if (cs_previous) begin
                active <= 1'b1;
                complete <= 1'b0;
                bit_count <= 6'b0;
                receive_shift <= 16'b0;
                command <= 8'b0;
                transmit_shift <= 16'b0;
                read_pending <= 1'b0;
                miso_bit <= 1'b0;
            end else if (active && !complete) begin
                // The address was latched on the previous core edge. Take
                // exactly one coherent 16-bit register value for both bytes.
                if (read_pending) begin
                    transmit_shift <= read_data;
                    read_pending <= 1'b0;
                end
                if (sclk_rise) begin
                    receive_shift <= {receive_shift[14:0], mosi_sync};
                    bit_count <= bit_count + 6'd1;
                    if (bit_count == 6'd7)
                        command <= {receive_shift[6:0], mosi_sync};
                    if (bit_count == 6'd15) begin
                        reg_address <= {receive_shift[6:0], mosi_sync};
                        if (command == 8'h01) begin
                            read_strobe <= 1'b1;
                            read_pending <= 1'b1;
                        end
                    end
                    if (bit_count == 6'd31) begin
                        complete <= 1'b1;
                        if (command == 8'h02) begin
                            write_data <= {receive_shift[14:0], mosi_sync};
                            write_strobe <= 1'b1;
                        end else if (command != 8'h01) begin
                            error_valid <= 1'b1;
                            error_code <= 4'd1; // BAD_COMMAND
                        end
                    end
                end
                if (sclk_fall && bit_count >= 6'd16 && command == 8'h01) begin
                    miso_bit <= transmit_shift[15];
                    transmit_shift <= {transmit_shift[14:0], 1'b0};
                end
            end
        end
    end
endmodule

`default_nettype wire
