`include "vm_mid_defs.svh"

(* keep_hierarchy = "yes" *)
module vm_mid_alu #(
    parameter int WIDTH = `VM_MID_WIDTH
) (
    input  logic [WIDTH-1:0] a,
    input  logic [WIDTH-1:0] b,
    input  logic             add_sub,
    output logic [WIDTH-1:0] y
);
    always_comb begin
        if (add_sub) begin
            y = a - b;
        end else begin
            y = a + b;
        end
    end
endmodule
