/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// Everything behind the configuration transport: register map and program
// store, TX/RX FIFOs and the execution engine. Its boundary is one decoded
// register operation per strobe, which the SPI receiver supplies at the top.
module pinscript_core #(
    parameter integer PROGRAM_DEPTH = 64,
    parameter integer ELAPSED_BITS = 24
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        write_strobe,
    input  wire        read_strobe,
    input  wire [7:0]  reg_address,
    input  wire [15:0] write_data,
    output wire [15:0] read_data,
    input  wire        transport_error_valid,
    input  wire [3:0]  transport_error_code,
    input  wire [7:0]  uio_in,
    output wire [7:0]  uio_out,
    output wire [7:0]  uio_oe,
    output wire        running,
    output wire        error_active,
    output wire        rx_available,
    output wire        fault_stopped
);
    wire [6:0] engine_pc;
    wire [15:0] fetch_word;
    wire fetch_valid;
    wire host_start, host_stop, host_tx_push, host_rx_pop, host_fifo_clear;
    wire [2:0] tx_count, rx_count;
    wire [7:0] tx_head, rx_head;
    wire tx_pop, rx_push;
    wire [7:0] rx_push_data;
    wire drive_gate;
    wire [3:0] stop_reason;
    wire [7:0] pin_out, pin_oe, pin_od, diag_in, reg_r0, reg_r1, reg_sr;
    wire [15:0] timer;
    wire [23:0] elapsed;

    pinscript_registers #(.PROGRAM_DEPTH(PROGRAM_DEPTH)) registers (
        .clk(clk), .rst_n(rst_n),
        .write_strobe(write_strobe), .read_strobe(read_strobe),
        .reg_address(reg_address), .write_data(write_data), .read_data(read_data),
        .transport_error_valid(transport_error_valid),
        .transport_error_code(transport_error_code), .error_active(error_active),
        .program_valid(), .loading(),
        .engine_busy(running), .engine_pc(engine_pc),
        .fetch_word(fetch_word), .fetch_valid(fetch_valid),
        .host_start(host_start), .host_stop(host_stop), .host_tx_push(host_tx_push),
        .host_rx_pop(host_rx_pop), .host_fifo_clear(host_fifo_clear),
        .tx_count(tx_count), .rx_count(rx_count), .rx_head(rx_head),
        .engine_stopped(stop_reason != 4'd0), .drive_gate(drive_gate),
        .stop_reason(stop_reason), .pin_out(pin_out), .pin_oe(pin_oe), .pin_od(pin_od),
        .diag_in(diag_in), .reg_r0(reg_r0), .reg_r1(reg_r1), .reg_sr(reg_sr),
        .timer(timer), .elapsed(elapsed)
    );

    // TX: host pushes (TX_DATA), engine pops (PULL).
    pinscript_fifo tx_fifo (
        .clk(clk), .rst_n(rst_n), .clear(host_fifo_clear),
        .push(host_tx_push), .push_data(write_data[7:0]), .pop(tx_pop),
        .head(tx_head), .count(tx_count)
    );
    // RX: engine pushes (PUSH), host pops (CONTROL=RX_POP).
    pinscript_fifo rx_fifo (
        .clk(clk), .rst_n(rst_n), .clear(host_fifo_clear),
        .push(rx_push), .push_data(rx_push_data), .pop(host_rx_pop),
        .head(rx_head), .count(rx_count)
    );

    pinscript_engine #(.PROGRAM_DEPTH(PROGRAM_DEPTH), .ELAPSED_BITS(ELAPSED_BITS)) engine (
        .clk(clk), .rst_n(rst_n),
        .pc(engine_pc), .fetch_word(fetch_word), .fetch_valid(fetch_valid),
        .host_start(host_start), .host_stop(host_stop),
        .tx_count(tx_count), .tx_head(tx_head), .tx_pop(tx_pop),
        .rx_count(rx_count), .rx_push(rx_push), .rx_push_data(rx_push_data),
        .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
        .running(running), .drive_gate(drive_gate), .stop_reason(stop_reason),
        .pin_out(pin_out), .pin_oe(pin_oe), .pin_od(pin_od), .diag_in(diag_in),
        .reg_r0(reg_r0), .reg_r1(reg_r1), .reg_sr(reg_sr), .timer(timer),
        .elapsed(elapsed)
    );

    assign rx_available = (rx_count != 3'd0);
    // HALT (1) and HOST_STOP (2) are not faults.
    assign fault_stopped = !running && (stop_reason >= 4'd3);
endmodule

`default_nettype wire
