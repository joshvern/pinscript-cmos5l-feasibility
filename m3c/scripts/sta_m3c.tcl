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
        # get_cells -of_objects on a top port returns its connected cells,
        # not an owning cell. Top-level pad ports have no hierarchy separator.
        if {[string first / [get_property $pin full_name]] < 0} {return ""}
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
            pc {(^|[./])engine[./]pc(\[[0-9]+\])?$} \
            memory {(^|[./])memory\[[0-9]+\]\[[0-9]+\]$} \
            pad_out {(^|[./])uio_out\[[0-9]+\]$} \
            pad_oe {(^|[./])uio_oe\[[0-9]+\]$} \
            pin_intent {(^|[./])engine[./](out|oe|od|g)(\[[0-9]+\])?$|^core[./]drive_gate$} \
            shift {(^|[./])engine[./](reg_sr|sr|r0|r1)(\[[0-9]+\])?$} \
            timer {(^|[./])engine[./](t|c)(\[[0-9]+\])?$|^core[./]elapsed\[[0-9]+\]$} \
            record {(^|[./])engine[./](run|reason)(\[[0-9]+\])?$|^core[./]diag_in\[[0-9]+\]$} \
            sync2 {(^|[./])engine[./]sync2\[[0-9]+\]$} \
            fifo {(^|[./])(tx_fifo|rx_fifo)[./](occupancy|read_pointer|slot[0-3])(\[[0-9]+\])?$|(^|[./])engine[./](tx_count|rx_count)\[[0-9]+\]$} \
            store_control {(^|[./])(program_valid|loading|loaded_count|expected_length)(\[[0-9]+\])?$} \
            host_address {(^|[./])read_index\[[0-9]+\]$} \
            host_capture {(^|[./])transmit_shift\[[0-9]+\]$}]
        set result [dict create]
        dict for {group expression} $expressions {
            dict set result $group q {}
            dict set result $group d {}
            dict set result $group cells {}
        }
        set data_by_cell [dict create]
        foreach pin [all_registers -data_pins] {
            # OpenSTA includes RESET_B here for these cells; select actual D only.
            if {![regexp {/D$} [name_of $pin]]} {continue}
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
        foreach property {q d cells} {
            dict set result pads $property [concat [dict get $result pad_out $property] [dict get $result pad_oe $property]]
        }
        dict set result registers q [all_registers -output_pins]
        dict set result registers cells [all_registers -cells]
        dict set result registers d {}
        dict for {cell pins} $data_by_cell {
            dict set result registers d [concat [dict get $result registers d] $pins]
        }
        dict set result top_pins q {}
        dict set result top_pins d [all_outputs]
        dict set result top_pins cells {}
        puts "\nGROUP_COUNTS (physical registers; actual D only, reset/clock excluded)"
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
            puts "pin\tcell\tarrival_ns\tapi_required_ns\tdelta_ns\tarc_kind"
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
            puts "Point API required values may be unset (zero); the full report's data required time is authoritative."
        }
    }

    proc object_names {objects} {
        set names {}
        foreach object $objects {lappend names [name_of $object]}
        return [lsort -unique $names]
    }

    proc report_pair {label from_pins to_pins {via_nets {}} {requirement REQUIRED}} {
        foreach delay {max min} {
            puts "%OL_CREATE_REPORT m3c-${label}-${delay}.rpt"
            puts "schema=pinscript-timing-report/2"
            puts "corner=$::env(_CURRENT_CORNER_NAME) delay=$delay launch_Q=[llength $from_pins] capture_D=[llength $to_pins]"
            puts "requirement=$requirement"
            puts "clock_period_ns=[get_property [get_clocks clk] period]"
            puts "units=time:ns (verify m3c-units.rpt)"
            foreach object [object_names $from_pins] {puts "LAUNCH\t$object"}
            foreach object [object_names $to_pins] {puts "CAPTURE\t$object"}
            foreach object [object_names $via_nets] {puts "VIA\t$object"}
            if {![llength $from_pins] || ![llength $to_pins] || ($label eq "pc-via-fetch-to-pc" && [llength $via_nets] != 23)} {
                puts "status=INCOMPLETE path_count=0 reason=missing_selection"
                puts "%OL_END_REPORT"
                continue
            }
            # Through Q includes FF launch clock/clk-to-Q, excludes input paths.
            # D-only endpoints exclude ports, reset/recovery and clock pins.
            set selectors [list -through $from_pins]
            if {[llength $via_nets]} {lappend selectors -through $via_nets}
            lappend selectors -to $to_pins -path_delay $delay \
                -sort_by_slack -endpoint_path_count 2 -corner $::env(_CURRENT_CORNER_NAME)
            if {[catch {set paths [find_timing_paths {*}$selectors -group_path_count 8]} error]} {
                puts "status=INCOMPLETE path_count=0 reason=$error"
                puts "%OL_END_REPORT"
                continue
            }
            set count [llength $paths]
            if {$requirement eq "NOT_APPLICABLE"} {
                if {$count} {
                    puts "status=INCOMPLETE path_count=$count reason=unexpected_optional_connection"
                } else {
                    puts "status=NOT_APPLICABLE path_count=0"
                    puts "explanation=OUT/OE/OD/G feed the D inputs of registered uio_out/uio_oe; those registers break the combinational path to top ports. Confirmed independently in both retained mapped/routed netlists."
                }
            } elseif {!$count} {
                puts "status=INCOMPLETE path_count=0 reason=no_constrained_path"
            } else {
                puts "status=AVAILABLE path_count=$count"
            }
            # report_checks and find_timing_paths use the same selectors/count.
            report_checks {*}$selectors -group_path_count 8 \
                -fields {slew cap input net fanout} -format full_clock_expanded \
                -digits 6
            puts "%OL_END_REPORT"
            puts "%OL_CREATE_REPORT m3c-${label}-${delay}-points.rpt"
            puts "schema=pinscript-timing-points/2 corner=$::env(_CURRENT_CORNER_NAME) delay=$delay path_count=$count"
            if {[catch {points $paths} error]} {puts "INCOMPLETE: path-point API: $error"}
            puts "%OL_END_REPORT"
        }
    }

    proc run {} {
        puts "%OL_CREATE_REPORT m3c-units.rpt"
        report_units
        foreach clock [get_clocks *] {
            puts "CLOCK\t[name_of $clock]\tperiod=[get_property $clock period]"
        }
        puts "%OL_END_REPORT"
        set selected [groups]
        cell_inventory
        foreach {source destination} {
            registers registers
            pc pc
            memory pc
            store_control pc
            timer pc
            sync2 pc
            fifo pc
            pc pads
            memory pads
            pc pad_out
            pc pad_oe
            memory pad_out
            memory pad_oe
            pc pin_intent
            memory pin_intent
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
            registers top_pins
            pads top_pins
        } {
            report_pair ${source}-to-${destination} \
                [dict get $selected $source q] [dict get $selected $destination d]
        }
        report_pair pin_intent-to-top_pins \
            [dict get $selected pin_intent q] [dict get $selected top_pins d] {} NOT_APPLICABLE
        # Connectivity-derived fetch cut, not guessed RTL names. Each is the
        # FIRST convergence of all 64 storage words for one instruction bit;
        # seven columns retain both polarities. Verified unchanged in both
        # retained final netlists and mapped SHA256 5b693f3a749b986f79f6915dbf7b450f7cb72ea68e53348c5a2e95036e03c3a0.
        # The post-run timing_evidence.py independently rediscovers this cut and
        # validates paths through it; any candidate mismatch is INCOMPLETE.
        set fetch_names {_01779_ _01742_ _01630_ _01588_ _01672_ _01673_ _01708_ _01547_ _02140_ _02141_ _01936_ _01937_ _01895_ _01896_ _01980_ _01981_ _02022_ _02023_ _01857_ _01821_ _02101_ _02064_ _02065_}
        set fetch_nets {}
        foreach net [get_nets -hierarchical *] {
            if {[name_of $net] in $fetch_names} {lappend fetch_nets $net}
        }
        report_pair pc-via-fetch-to-pc [dict get $selected pc q] \
            [dict get $selected pc d] $fetch_nets
    }
}

if {[catch {m3c::run} m3c_error]} {
    # Reporting failures remain visible but must not abort implementation or
    # artifact retention. The separate strict project gate rejects INCOMPLETE.
    puts "%OL_END_REPORT"
    puts "%OL_CREATE_REPORT m3c-reporting-error.rpt"
    puts "INCOMPLETE: $m3c_error"
    puts $::errorInfo
    puts "%OL_END_REPORT"
}
