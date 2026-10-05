# Export as gate-tests/{separate,shared}/Makefile with candidate.mk beside it.
# Standard upstream tools only; no dependency on local WSL/runtime paths.
TEST_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
include $(TEST_DIR)/candidate.mk
SIM ?= icarus
TOPLEVEL_LANG := verilog
TOPLEVEL := tt_um_pinscript_probe_$(CANDIDATE)
export PINSCRIPT_PROBE_CANDIDATE := $(CANDIDATE)
export PYTHONDONTWRITEBYTECODE := 1
export PYTHONPATH := $(TEST_DIR):$(PYTHONPATH)
COCOTB_TEST_MODULES := test_probe_pins
ifeq ($(GATES),yes)
SIM_BUILD := sim_build/gl
COMPILE_ARGS += -DGL_TEST -DFUNCTIONAL -DSIM
VERILOG_SOURCES := $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v \
                   $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v \
                   $(TEST_DIR)/gate_level_netlist.v
else
SIM_BUILD := sim_build/rtl
VERILOG_SOURCES := $(TEST_DIR)/../src/pinscript_spi_cfg.v \
                   $(TEST_DIR)/../src/pinscript_program_store.v \
                   $(TEST_DIR)/../src/pinscript_live_fetch_probe.v
endif
include $(shell cocotb-config --makefiles)/Makefile.sim
