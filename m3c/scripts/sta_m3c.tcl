# Report-only hook for LibreLane 3.1.0.dev3 MultiCornerSTA, M3C integrated engine.
# Adapted from the M3B probe hook. The workflow copies m3c/scripts to scripts-m3c and
# src/config.json sets STA_EXTRA_CORNER_TCL_FILE to "dir::../scripts-m3c/sta_m3c.tcl".
# No clocks, constraints, timing exceptions, parasitics, or design are changed.
# corner.tcl sources this after read_spefs and selecting the current corner.
if {![info exists ::env(_CURRENT_CORNER_NAME)] || ![info exists corner_name]} {
    return
}

namespace eval m3c {
    proc cell_of {pin} {
        set cells [get_cells -quiet -of_objects $pin]
        if {[llength $cells]} {return [lindex $cells 0]}
        return ""
    }

    proc name_of {object} {
        if {$object eq ""} {return ""}
        return [get_property $object full_name]
    }

    # Match signal nets, not synthesis-generated flip-flop instance names.
    # Retain the complete Q/net inventory so a failed name match is diagnosable.
    proc groups {} {
        set expressions [dict create \
            pc {(^|[./])engine[./]pc_q(\[[0-9]+\])?$} \
            memory {(^|[./])memory\[[0-9]+\](\[[0-9]+\])?$} \
            pads {(^|[./])engine[./](uio_out|uio_oe)(\[[0-9]+\])?$} \
            pin_intent {(^|[./])engine[./](out|oe|od|g)(\[[0-9]+\])?$} \
            shift {(^|[./])engine[./](sr|r0|r1)(\[[0-9]+\])?$} \
            timer {(^|[./])engine[./](t|c|elapsed_q)(\[[0-9]+\])?$} \
            record {(^|[./])engine[./](run|reason|diag)(\[[0-9]+\])?$} \
            sync2 {(^|[./])engine[./]sync2(\[[0-9]+\])?$} \
            fifo {(^|[./])(tx_fifo|rx_fifo)[./](occupancy|read_pointer|slot[0-3])(\[[0-9]+\])?$} \
            store_control {(^|[./])(program_valid|loading|count|length)(\[[0-9]+\])?$} \
            host_address {(^|[./])read_index(\[[0-9]+\])?$} \
            host_capture {(^|[./])transmit_shift(\[[0-9]+\])?$}]
        set result [dict create]
        dict for {group expression} $expressions {
            dict set result $group q {}
            dict set result $group d {}
            dict set result $group cells {}
        }
        set data_by_cell [dict create]
        foreach pin [all_registers -data_pins] {
            dict lappend data_by_cell [name_of [cell_of $pin]] $pin
        }
        puts "%OL_CREATE_REPORT m3c-register-inventory.rpt"
        puts "corner=$::env(_CURRENT_CORNER_NAME)"
        puts "Q_pin\tcell\tnet\tmatched_groups"
        foreach pin [all_registers -output_pins] {
            set cell [cell_of $pin]
            set cell_name [name_of $cell]
            foreach net [get_nets -quiet -of_objects $pin] {
                set net_name [name_of $net]
                set matched {}
                dict for {group expression} $expressions {
                    if {[regexp $expression $net_name]} {
                        lappend matched $group
                        dict set result $group q [concat [dict get $result $group q] [list $pin]]
                        dict set result $group cells [concat [dict get $result $group cells] [list $cell]]
                        if {[dict exists $data_by_cell $cell_name]} {
                            foreach data_pin [dict get $data_by_cell $cell_name] {
                                dict set result $group d [concat [dict get $result $group d] [list $data_pin]]
                            }
                        }
                    }
                }
                puts "[name_of $pin]\t$cell_name\t$net_name\t$matched"
            }
        }
        puts "\nGROUP_COUNTS (name-based structural retention; not functional reachability)"
        dict for {group properties} $result {
            foreach property {q d cells} {
                dict set result $group $property [lsort -unique [dict get $properties $property]]
            }
            puts "$group\tQ=[llength [dict get $result $group q]]\tD=[llength [dict get $result $group d]]\tcells=[llength [dict get $result $group cells]]"
        }
        puts "%OL_END_REPORT"
        return $result
    }

    proc cell_inventory {} {
        set sequential [dict create]
        foreach cell [all_registers -cells] {dict set sequential [name_of $cell] 1}
        set counts [dict create]
        set areas [dict create]
        set unknown 0
        set seq_area 0.0
        set other_area 0.0
        puts "%OL_CREATE_REPORT m3c-mapped-cell-inventory.rpt"
        puts "corner=$::env(_CURRENT_CORNER_NAME)"
        puts "Area is Liberty area at THIS flow stage. Nonsequential includes any physical-only cells represented in STA; use synthesis stat.rpt for synthesis area."
        foreach cell [get_cells -hierarchical *] {
            if {[catch {
                set ref [get_property $cell ref_name]
                set libcell [get_property $cell liberty_cell]
                set area [get_property $libcell area]
            } error]} {
                incr unknown
                puts "UNAVAILABLE\t[name_of $cell]\t$error"
                continue
            }
            dict incr counts $ref
            if {![dict exists $areas $ref]} {dict set areas $ref 0.0}
            dict set areas $ref [expr {[dict get $areas $ref] + $area}]
            if {[dict exists $sequential [name_of $cell]]} {
                set seq_area [expr {$seq_area + $area}]
            } else {
                set other_area [expr {$other_area + $area}]
            }
        }
        puts "reference\tcount\tliberty_area_sum"
        foreach ref [lsort [dict keys $counts]] {
            puts "$ref\t[dict get $counts $ref]\t[dict get $areas $ref]"
        }
        puts "sequential_area=$seq_area nonsequential_area=$other_area unavailable_cells=$unknown"
        puts "%OL_END_REPORT"
    }

    proc points {paths} {
        # get_property time values use the command time unit selected by the
        # flow (ns). Full reports remain authoritative if an API is unavailable.
        set index 0
        foreach path $paths {
            incr index
            puts "PATH\t$index\tstart=[name_of [get_property $path startpoint]]\tend=[name_of [get_property $path endpoint]]\tslack_ns=[get_property $path slack]"
            puts "pin\tcell\tarrival_ns\trequired_ns\tdelta_ns\tarc_kind"
            set previous_cell ""
            set previous_time ""
            set cell_delay 0.0
            set net_delay 0.0
            set cell_arcs 0
            set net_arcs 0
            foreach point [get_property $path points] {
                set pin [get_property $point pin]
                set cell [name_of [cell_of $pin]]
                set arrival [get_property $point arrival]
                set required [get_property $point required]
                set delta 0.0
                set kind first_point
                if {$previous_time ne ""} {
                    set delta [expr {$arrival - $previous_time}]
                    if {$cell ne "" && $cell eq $previous_cell} {
                        set kind cell
                        incr cell_arcs
                        set cell_delay [expr {$cell_delay + $delta}]
                    } else {
                        set kind net
                        incr net_arcs
                        set net_delay [expr {$net_delay + $delta}]
                    }
                }
                puts "[name_of $pin]\t$cell\t$arrival\t$required\t$delta\t$kind"
                set previous_cell $cell
                set previous_time $arrival
            }
            puts "DATA_INTERVAL_SUM\tcell_ns=$cell_delay\tnet_ns=$net_delay\tcell_arcs=$cell_arcs\tnet_arcs=$net_arcs"
            puts "Sum covers intervals between returned data-path points; excludes initial launch arrival, capture-clock path, and endpoint check. See full_clock_expanded report."
        }
    }

    proc report_pair {label from_pins to_pins {via_nets {}} {require_via 0}} {
        foreach delay {max min} {
            puts "%OL_CREATE_REPORT m3c-${label}-${delay}.rpt"
            puts "corner=$::env(_CURRENT_CORNER_NAME) delay=$delay launch_Q=[llength $from_pins] capture_D=[llength $to_pins]"
            if {![llength $from_pins] || ![llength $to_pins] || ($require_via && ![llength $via_nets])} {
                puts "UNAVAILABLE: source or endpoint group absent; expected for engine-only groups in baseline. Inspect register inventory."
                puts "%OL_END_REPORT"
                continue
            }
            # Q pins are through-points on paths launched by their owning FFs.
            # This includes real launch clock and clk-to-Q timing, with D endpoints.
            set selectors [list -through $from_pins]
            if {[llength $via_nets]} {lappend selectors -through $via_nets}
            lappend selectors -to $to_pins -path_delay $delay \
                -sort_by_slack -endpoint_path_count 2 -corner $::env(_CURRENT_CORNER_NAME)
            report_checks {*}$selectors -group_path_count 32 \
                -fields {slew cap input net fanout} -format full_clock_expanded \
                -digits 6
            puts "%OL_END_REPORT"
            puts "%OL_CREATE_REPORT m3c-${label}-${delay}-points.rpt"
            if {[catch {
                set paths [find_timing_paths {*}$selectors -group_path_count 8]
                if {![llength $paths]} {puts "UNAVAILABLE: no constrained timing paths for selected groups"}
                points $paths
            } error]} {puts "UNAVAILABLE: path-point API: $error"}
            puts "%OL_END_REPORT"
        }
    }

    proc run {} {
        set selected [groups]
        cell_inventory
        foreach {source destination} {
            pc pc
            memory pc
            store_control pc
            timer pc
            sync2 pc
            fifo pc
            pc pads
            memory pads
            pc shift
            memory shift
            fifo shift
            sync2 shift
            pc timer
            memory timer
            pc record
            memory record
            pc fifo
            host_address host_capture
            memory host_capture
            pc host_capture
        } {
            report_pair ${source}-to-${destination} \
                [dict get $selected $source q] [dict get $selected $destination d]
        }
    }
}

if {[catch {m3c::run} m3c_error]} {
    # A reporting limitation must stay visible without changing implementation
    # behavior or hiding the standard flow's timing/checker failure status.
    puts "%OL_END_REPORT"
    puts "%OL_CREATE_REPORT m3c-reporting-error.rpt"
    puts "UNAVAILABLE: $m3c_error"
    puts $::errorInfo
    puts "%OL_END_REPORT"
}
