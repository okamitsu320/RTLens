`include "vm_mid_defs.svh"

module vm_mid_top (
    input  logic                   clk,
    input  logic                   en,
    input  logic [`VM_MID_WIDTH-1:0] in0,
    input  logic [`VM_MID_WIDTH-1:0] in1,
    output logic [`VM_MID_WIDTH-1:0] out0
);
    logic [`VM_MID_WIDTH-1:0] lane0_q;
    logic [`VM_MID_WIDTH-1:0] lane1_q;

    vm_mid_lane #(.WIDTH(`VM_MID_WIDTH)) u_lane0 (
        .clk(clk),
        .en(en),
        .din_a(in0),
        .din_b(in1),
        .dout(lane0_q)
    );

    vm_mid_lane #(.WIDTH(`VM_MID_WIDTH)) u_lane1 (
        .clk(clk),
        .en(en),
        .din_a(in1),
        .din_b(in0),
        .dout(lane1_q)
    );

    vm_mid_router #(.WIDTH(`VM_MID_WIDTH)) u_router (
        .select(en),
        .x0(lane0_q),
        .x1(lane1_q),
        .y(out0)
    );
endmodule
