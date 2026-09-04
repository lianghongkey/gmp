# 通过 JTAG 烧 bitstream（易失，掉电丢失）。bit 由环境变量 BIT 指定。
#   BIT=build/top_jtag.bit vivado -mode batch -source program.tcl   (= make prog-board)
# 需要：Xilinx 7 系列 JTAG 接到板上，hw_server 在跑（vivado 会自己起本地的）。
# ⚠ 烧录期间 vivado 独占 JTAG；烧完 close_hw_target 释放，之后 host/soc_web.py 那条
#   pyjtag 通路才连得上。两者不能同时开着。
set bit [expr {[info exists ::env(BIT)] ? $::env(BIT) : "build/top_jtag.bit"}]
if {![file exists $bit]} { puts "ERROR: 找不到 $bit —— 先 make bit-board"; return }
puts "INFO: 即将烧录 $bit  （bit 生成于 [clock format [file mtime $bit] -format {%Y-%m-%d %H:%M:%S}]）"

open_hw_manager
connect_hw_server
open_hw_target
set dev [lindex [get_hw_devices *xc7k480t*] 0]
if {$dev eq ""} { set dev [lindex [get_hw_devices] 0] }
current_hw_device $dev
refresh_hw_device -update_hw_probes false $dev
set_property PROGRAM.FILE $bit $dev
program_hw_devices $dev
puts "INFO: 已烧录 $bit 到 $dev"
close_hw_target
close_hw_manager
