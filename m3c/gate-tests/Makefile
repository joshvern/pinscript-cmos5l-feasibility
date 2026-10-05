# M3C functional gate tests (exported as m3c/gate-tests/Makefile).
# Standard upstream tools only; no dependency on local WSL/runtime paths.
# GATES=yes includes the PDK's sg13cmos5l_udp.v primitive models (M3B fix).
TEST_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
SIM ?= icarus
TOPLEVEL_LANG := verilog
export PYTHONDONTWRITEBYTECODE := 1
export PYTHONPATH := $(TEST_DIR):$(PYTHONPATH)
TOPLEVEL := tb
COCOTB_TEST_MODULES := test,test_gl_engine
ifeq ($(GATES),yes)
SIM_BUILD := sim_build/gl
COMPILE_ARGS += -DGL_TEST -DFUNCTIONAL -DSIM
VERILOG_SOURCES := $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v \
                   $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v \
                   $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v \
                   $(TEST_DIR)/gate_level_netlist.v
else
SIM_BUILD := sim_build/rtl
VERILOG_SOURCES := $(addprefix $(TEST_DIR)/../src/,project.v pinscript_spi_cfg.v pinscript_core.v \
                   pinscript_registers.v pinscript_program_store.v pinscript_fifo.v \
                   pinscript_engine.v pinscript_decode.v)
endif
VERILOG_SOURCES += $(TEST_DIR)/tb.v
include $(shell cocotb-config --makefiles)/Makefile.sim
