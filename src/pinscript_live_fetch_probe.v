/* Copyright (c) 2026 Joshua Vernazza
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

// Isolated M3B timing experiment. This is deliberately NOT a production engine.
// See ../RTL.md for the strict subset, observability and synthetic FIFO contract.
module pinscript_probe_consumer (
    input wire clk,
    input wire rst_n,
    input wire start,
    input wire stop,
    input wire [15:0] fetch_word,
    input wire fetch_valid,
    input wire [7:0] sampled_inputs,
    input wire [2:0] tx_count,
    input wire [2:0] rx_count,
    output reg run,
    output reg [6:0] pc,
    output reg [3:0] reason,
    output reg [7:0] output_state,
    output reg [7:0] r0,
    output reg [7:0] r1,
    output reg [15:0] timer,
    output reg delay_active,
    output reg [15:0] captured_word,
    output reg [6:0] captured_pc,
    output reg captured_valid,
    output reg [6:0] captured_next_pc,
    output reg [15:0] captured_control
);
    wire [3:0] opcode = fetch_word[15:12];
    wire [4:0] condition = fetch_word[11:7];
    wire condition_legal = (condition <= 5'd5) || condition[4];
    reg condition_true;
    reg instruction_legal;
    reg [6:0] next_pc;
    reg [7:0] next_output, next_r0, next_r1;
    reg [15:0] next_timer;
    reg next_delay_active;
    reg stop_cycle;
    reg [3:0] stop_reason, outcome;
    wire [7:0] decremented_counter = (fetch_word[11] ? r1 : r0) - 8'd1;

    // The condition sources are registers, not unconstrained combinational
    // test inputs. SR conditions are omitted and fault, never approximated.
    always @* begin
        condition_true = 1'b0;
        case (condition)
            5'd0: condition_true = 1'b1;
            5'd1: condition_true = 1'b0;
            5'd2: condition_true = (tx_count == 0);
            5'd3: condition_true = (tx_count != 0);
            5'd4: condition_true = (rx_count == 4);
            5'd5: condition_true = (rx_count != 4);
            default: begin
                if (condition[4])
                    condition_true = (sampled_inputs[condition[2:0]] == condition[3]);
            end
        endcase
    end

    // Every bit outside the supported operands is checked. Valid v0.1 words
    // outside this experiment's subset also fault ILLEGAL_INSTRUCTION.
    always @* begin
        instruction_legal = 1'b0;
        case (opcode)
            4'h0: begin
                case (fetch_word[11:8])
                    4'h1: instruction_legal = (fetch_word[7:0] == 8'h00);
                    4'h2, 4'h4, 4'h5:
                        instruction_legal = (fetch_word[7:1] == 7'b0);
                    4'h6: instruction_legal = (fetch_word[7:4] == 4'b0);
                    default: instruction_legal = 1'b0;
                endcase
            end
            4'h1: instruction_legal = !fetch_word[8] && (fetch_word[11:9] <= 3'd2);
            4'h3: instruction_legal = (fetch_word[11:10] <= 2'd1) &&
                                       (fetch_word[9:8] == 2'b11);
            4'h4: instruction_legal = condition_legal && !fetch_word[6];
            4'h5: instruction_legal = (fetch_word[10:6] == 5'b0);
            4'h6, 4'h7: instruction_legal = 1'b1;
            4'h8: instruction_legal = condition_legal &&
                                       (!fetch_word[6] || fetch_word[5:0] == 6'b0);
            default: instruction_legal = 1'b0;
        endcase
    end

    always @* begin
        next_pc = pc;
        next_output = output_state;
        next_r0 = r0;
        next_r1 = r1;
        next_timer = timer;
        next_delay_active = delay_active;
        stop_cycle = 1'b0;
        stop_reason = 4'b0;
        outcome = 4'b0;
        if (run) begin
            if (stop) begin
                stop_cycle = 1'b1;
                stop_reason = 4'd2;
                outcome = 4'd13;
            end else if (!fetch_valid) begin
                stop_cycle = 1'b1;
                stop_reason = 4'd4;
                outcome = 4'd12;
            end else if (!instruction_legal) begin
                stop_cycle = 1'b1;
                stop_reason = 4'd3;
                outcome = 4'd11;
            end else begin
                next_pc = pc + 7'd1;
                outcome = 4'd1;
                case (opcode)
                    4'h0: begin
                        case (fetch_word[11:8])
                            4'h2: begin
                                stop_cycle = 1'b1;
                                stop_reason = 4'd1;
                                outcome = 4'd10;
                            end
                            4'h4: begin
                                if (tx_count == 0) begin
                                    if (fetch_word[0]) begin
                                        stop_cycle = 1'b1;
                                        stop_reason = 4'd6;
                                    end else begin
                                        next_pc = pc;
                                    end
                                    outcome = 4'd9;
                                end else outcome = 4'd15;
                            end
                            4'h5: begin
                                if (rx_count == 4) begin
                                    if (fetch_word[0]) begin
                                        stop_cycle = 1'b1;
                                        stop_reason = 4'd7;
                                    end else begin
                                        next_pc = pc;
                                    end
                                    outcome = 4'd9;
                                end else outcome = 4'd15;
                            end
                            4'h6: next_timer = {fetch_word[3:0], timer[11:0]};
                            default: begin end // NOP
                        endcase
                    end
                    4'h1: begin
                        case (fetch_word[11:9])
                            3'd0: next_output = output_state | fetch_word[7:0];
                            3'd1: next_output = output_state & ~fetch_word[7:0];
                            3'd2: next_output = output_state ^ fetch_word[7:0];
                            default: begin end
                        endcase
                    end
                    4'h3: begin
                        if (fetch_word[10]) next_r1 = fetch_word[7:0];
                        else next_r0 = fetch_word[7:0];
                    end
                    4'h4: begin
                        if (condition_true) begin
                            next_pc = {1'b0, fetch_word[5:0]};
                            outcome = 4'd2;
                        end else outcome = 4'd3;
                    end
                    4'h5: begin
                        if (fetch_word[11]) next_r1 = decremented_counter;
                        else next_r0 = decremented_counter;
                        if (decremented_counter != 0) begin
                            next_pc = {1'b0, fetch_word[5:0]};
                            outcome = 4'd4;
                        end else outcome = 4'd5;
                    end
                    4'h6: begin
                        if (!delay_active) begin
                            next_timer = {4'b0, fetch_word[11:0]};
                            next_delay_active = (fetch_word[11:0] != 0);
                            if (fetch_word[11:0] != 0) begin
                                next_pc = pc;
                                outcome = 4'd6;
                            end
                        end else if (timer == 16'd1) begin
                            next_timer = 16'b0;
                            next_delay_active = 1'b0;
                        end else begin
                            next_timer = timer - 16'd1;
                            next_pc = pc;
                            outcome = 4'd6;
                        end
                    end
                    4'h7: next_timer = {4'b0, fetch_word[11:0]};
                    4'h8: begin
                        if (condition_true) outcome = 4'd14;
                        else if (timer == 0) begin
                            outcome = 4'd8;
                            if (fetch_word[6]) begin
                                stop_cycle = 1'b1;
                                stop_reason = 4'd5;
                            end else next_pc = {1'b0, fetch_word[5:0]};
                        end else begin
                            next_timer = timer - 16'd1;
                            next_pc = pc;
                            outcome = 4'd7;
                        end
                    end
                    default: begin end
                endcase
                if (stop_cycle) next_pc = pc;
            end
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            run <= 1'b0;
            pc <= 7'b0;
            reason <= 4'b0;
            output_state <= 8'b0;
            r0 <= 8'b0;
            r1 <= 8'b0;
            timer <= 16'b0;
            delay_active <= 1'b0;
            captured_word <= 16'b0;
            captured_pc <= 7'b0;
            captured_valid <= 1'b0;
            captured_next_pc <= 7'b0;
            captured_control <= 16'b0;
        end else if (stop) begin
            // STOP also wins over a simultaneous testbench START; the SPI
            // wrapper presents at most one host event per core clock.
            if (run) begin
                run <= 1'b0;
                reason <= 4'd2;
                captured_word <= fetch_word;
                captured_pc <= pc;
                captured_valid <= fetch_valid;
                captured_next_pc <= pc;
                captured_control <= {opcode, outcome, condition_true,
                    instruction_legal, delay_active, 1'b1, 4'd2};
            end
        end else if (start && !run) begin
            run <= 1'b1;
            pc <= 7'b0;
            reason <= 4'b0;
            output_state <= 8'b0;
            r0 <= 8'b0;
            r1 <= 8'b0;
            timer <= 16'b0;
            delay_active <= 1'b0;
            captured_word <= 16'b0;
            captured_pc <= 7'b0;
            captured_valid <= 1'b0;
            captured_next_pc <= 7'b0;
            captured_control <= 16'b0;
        end else if (run) begin
            captured_word <= fetch_word;
            captured_pc <= pc;
            captured_valid <= fetch_valid;
            captured_next_pc <= next_pc;
            captured_control <= {opcode, outcome, condition_true,
                instruction_legal, delay_active, stop_cycle, stop_reason};
            if (stop_cycle) begin
                run <= 1'b0;
                reason <= stop_reason;
            end else begin
                pc <= next_pc;
                output_state <= next_output;
                r0 <= next_r0;
                r1 <= next_r1;
                timer <= next_timer;
                delay_active <= next_delay_active;
            end
        end
    end
endmodule

module pinscript_live_fetch_probe #(
    parameter integer SHARED_READ_PORT = 0
) (
    input wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input wire ena,
    input wire clk,
    input wire rst_n
);
    wire miso, write_strobe, read_strobe, transport_error_valid;
    wire [7:0] reg_address;
    wire [15:0] write_data;
    reg [15:0] read_data;
    wire [3:0] transport_error_code;
    reg [3:0] sticky_error;
    reg [15:0] scratch, host_read_address;
    reg [2:0] tx_count, rx_count;
    (* async_reg = "true" *) reg [7:0] sync1, sync2;
    wire run;
    wire [6:0] pc;
    wire [3:0] reason;
    wire [7:0] output_state, r0, r1;
    wire [15:0] timer, captured_word, captured_control;
    wire delay_active, captured_valid;
    wire [6:0] captured_pc, captured_next_pc;
    wire [15:0] expected_length, loaded_count, store_read_data, store_fetch_data;
    wire loading, program_valid, store_error_valid;
    wire [3:0] store_error_code;
    reg store_command_valid;
    reg [2:0] store_command;
    reg [3:0] access_error;
    reg clear_error, set_scratch, set_read_address, set_conditions;
    wire start_request = write_strobe && reg_address == 8'h04 && write_data == 16'd5;
    wire start_accept = start_request && !run && program_valid && !loading;
    wire stop_request = write_strobe && reg_address == 8'h04 && write_data == 16'd6;
    wire fetch_valid = program_valid && ({9'b0, pc} < loaded_count);
    wire [15:0] fetch_word = SHARED_READ_PORT ? store_read_data : store_fetch_data;
    wire [15:0] memory_read_address = (SHARED_READ_PORT && run) ? {9'b0, pc} : host_read_address;
    wire [15:0] host_read_data = (SHARED_READ_PORT && run) ? 16'b0 : store_read_data;
    wire known_address = (reg_address <= 8'h0c) ||
                         (reg_address >= 8'h10 && reg_address <= 8'h16);

    pinscript_spi_cfg spi_cfg (
        .clk(clk), .rst_n(rst_n), .spi_sclk(ui_in[0]),
        .spi_mosi(ui_in[1]), .spi_cs_n(ui_in[2]), .miso(miso),
        .write_strobe(write_strobe), .read_strobe(read_strobe),
        .reg_address(reg_address), .write_data(write_data), .read_data(read_data),
        .error_valid(transport_error_valid), .error_code(transport_error_code)
    );

    // Both candidates instantiate the unchanged production store. In the
    // shared case the unused fetch result is disconnected and optimized away.
    // run || start_accept guards the START edge as well as every RUN cycle.
    pinscript_program_store #(.PROGRAM_DEPTH(64)) store (
        .clk(clk), .rst_n(rst_n), .engine_busy(run || start_accept),
        .command_valid(store_command_valid && !start_accept),
        .command(store_command), .command_data(write_data),
        .expected_length(expected_length), .loaded_count(loaded_count),
        .loading(loading), .program_valid(program_valid),
        .read_address(memory_read_address), .read_data(store_read_data),
        .fetch_address(SHARED_READ_PORT ? 16'b0 : {9'b0, pc}),
        .fetch_data(store_fetch_data),
        .error_valid(store_error_valid), .error_code(store_error_code)
    );

    pinscript_probe_consumer consumer (
        .clk(clk), .rst_n(rst_n), .start(start_accept), .stop(stop_request),
        .fetch_word(fetch_word), .fetch_valid(fetch_valid), .sampled_inputs(sync2),
        .tx_count(tx_count), .rx_count(rx_count), .run(run), .pc(pc), .reason(reason),
        .output_state(output_state), .r0(r0), .r1(r1), .timer(timer),
        .delay_active(delay_active), .captured_word(captured_word),
        .captured_pc(captured_pc), .captured_valid(captured_valid),
        .captured_next_pc(captured_next_pc), .captured_control(captured_control)
    );

    always @* begin
        store_command_valid = 1'b0;
        store_command = 3'b0;
        access_error = 4'b0;
        clear_error = 1'b0;
        set_scratch = 1'b0;
        set_read_address = 1'b0;
        set_conditions = 1'b0;
        if (read_strobe && !known_address) access_error = 4'd2;
        if (write_strobe) begin
            if (reg_address == 8'h04 && write_data == 16'd4) clear_error = 1'b1;
            else if (stop_request) begin end
            else if (reg_address == 8'h0c) begin
                if ((write_data & 16'hff88) != 0 || write_data[2:0] > 3'd4 ||
                    write_data[6:4] > 3'd4) access_error = 4'd12;
                else set_conditions = 1'b1;
            end else if (run) access_error = 4'd9;
            else begin
                case (reg_address)
                    8'h03: set_scratch = 1'b1;
                    8'h04: begin
                        case (write_data)
                            16'd1: begin store_command_valid = 1'b1; store_command = 3'd1; end
                            16'd2: begin store_command_valid = 1'b1; store_command = 3'd3; end
                            16'd3: begin store_command_valid = 1'b1; store_command = 3'd4; end
                            16'd5: if (!program_valid || loading) access_error = 4'd13;
                            default: access_error = 4'd12;
                        endcase
                    end
                    8'h06: begin store_command_valid = 1'b1; store_command = 3'd0; end
                    8'h08: begin store_command_valid = 1'b1; store_command = 3'd2; end
                    8'h09: begin
                        if (write_data >= 16'd64) access_error = 4'd12;
                        else set_read_address = 1'b1;
                    end
                    default: access_error = known_address ? 4'd3 : 4'd2;
                endcase
            end
        end
    end

    always @* begin
        read_data = 16'b0;
        case (reg_address)
            8'h00: read_data = 16'h4c46; // LF: deliberately not M1 DEVICE_ID
            8'h01: read_data = SHARED_READ_PORT ? 16'hb002 : 16'hb001;
            8'h02: read_data = {12'b0, (sticky_error != 0), run, loading, program_valid};
            8'h03: read_data = scratch;
            8'h05: read_data = {12'b0, sticky_error};
            8'h06: read_data = expected_length;
            8'h07: read_data = loaded_count;
            8'h09: read_data = host_read_address;
            8'h0a: read_data = host_read_data;
            8'h0b: read_data = 16'd64;
            8'h0c: read_data = {9'b0, rx_count, 1'b0, tx_count};
            8'h10: read_data = {4'b0, reason, run, pc};
            8'h11: read_data = captured_word;
            8'h12: read_data = {1'b0, captured_valid, captured_next_pc, captured_pc};
            8'h13: read_data = captured_control;
            8'h14: read_data = {output_state, sync2};
            8'h15: read_data = {r0, r1};
            8'h16: read_data = timer;
            default: begin end
        endcase
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            sticky_error <= 4'b0;
            scratch <= 16'b0;
            host_read_address <= 16'b0;
            tx_count <= 3'b0;
            rx_count <= 3'b0;
            sync1 <= 8'b0;
            sync2 <= 8'b0;
        end else begin
            sync1 <= uio_in;
            sync2 <= sync1;
            if (set_scratch) scratch <= write_data;
            if (set_read_address) host_read_address <= write_data;
            if (set_conditions) begin
                tx_count <= write_data[2:0];
                rx_count <= write_data[6:4];
            end
            if (clear_error) sticky_error <= 4'b0;
            else if (sticky_error == 0) begin
                if (transport_error_valid) sticky_error <= transport_error_code;
                else if (access_error != 0) sticky_error <= access_error;
                else if (store_error_valid) sticky_error <= store_error_code;
            end
        end
    end

    assign uo_out = {2'b0, (!run && reason >= 4'd3), loading,
                     program_valid, (sticky_error != 0), run, miso};
    assign uio_out = output_state;
    assign uio_oe = 8'b0;
    wire _unused = &{ena, ui_in[7:3], delay_active, 1'b0};
endmodule

module tt_um_pinscript_probe_separate (
    input wire [7:0] ui_in, output wire [7:0] uo_out,
    input wire [7:0] uio_in, output wire [7:0] uio_out,
    output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n
);
    pinscript_live_fetch_probe #(.SHARED_READ_PORT(0)) probe (
        .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out),
        .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n)
    );
endmodule

module tt_um_pinscript_probe_shared (
    input wire [7:0] ui_in, output wire [7:0] uo_out,
    input wire [7:0] uio_in, output wire [7:0] uio_out,
    output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n
);
    pinscript_live_fetch_probe #(.SHARED_READ_PORT(1)) probe (
        .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out),
        .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n)
    );
endmodule

`default_nettype wire
