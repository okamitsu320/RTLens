`include "vm_mid_defs.svh"

(* keep_hierarchy = "yes" *)
module vm_mid_lane #(
    parameter int WIDTH = `VM_MID_WIDTH
) (
    input  logic             clk,
    input  logic             en,
    input  logic [WIDTH-1:0] din_a,
    input  logic [WIDTH-1:0] din_b,
    output logic [WIDTH-1:0] dout
);
    logic [WIDTH-1:0] alu_y;

    vm_mid_alu #(.WIDTH(WIDTH)) u_alu (
        .a(din_a),
        .b(din_b),
        .add_sub(en),
        .y(alu_y)
    );

    always_ff @(posedge clk) begin
        if (en) begin
            dout <= alu_y;
        end else begin
            dout <= din_a;
        end
    end
endmodule
