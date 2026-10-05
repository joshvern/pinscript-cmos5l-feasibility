/*
 * Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_joshua_vernazza_pinscript (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  wire miso;
  wire write_strobe, read_strobe;
  wire [7:0] reg_address;
  wire [15:0] write_data, read_data;
  wire transport_error_valid;
  wire [3:0] transport_error_code;
  wire error_active;
  wire program_valid, loading;
  wire [15:0] unused_fetch_data;

  pinscript_spi_cfg spi_cfg (
      .clk(clk), .rst_n(rst_n),
      .spi_sclk(ui_in[0]), .spi_mosi(ui_in[1]), .spi_cs_n(ui_in[2]),
      .miso(miso), .write_strobe(write_strobe), .read_strobe(read_strobe),
      .reg_address(reg_address), .write_data(write_data), .read_data(read_data),
      .error_valid(transport_error_valid), .error_code(transport_error_code)
  );

  pinscript_registers #(.PROGRAM_DEPTH(64)) registers (
      .clk(clk), .rst_n(rst_n), .engine_busy(1'b0),
      .write_strobe(write_strobe), .read_strobe(read_strobe),
      .reg_address(reg_address), .write_data(write_data), .read_data(read_data),
      .transport_error_valid(transport_error_valid),
      .transport_error_code(transport_error_code), .error_active(error_active),
      .program_valid(program_valid), .loading(loading),
      .fetch_address(16'b0), .fetch_data(unused_fetch_data)
  );

  // No execution engine exists in M1. All future protocol pins are released.
  assign uo_out = {4'b0, 1'b0, error_active, 1'b0, miso};
  assign uio_out = 8'b0;
  assign uio_oe = 8'b0;
  wire _unused = &{ena, ui_in[7:3], uio_in, program_valid, loading,
                   unused_fetch_data, 1'b0};

endmodule

`default_nettype wire
