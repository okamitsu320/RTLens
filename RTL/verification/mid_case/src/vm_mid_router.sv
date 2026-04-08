(* keep_hierarchy = "yes" *)
module vm_mid_router #(
    parameter int WIDTH = 8
) (
    input  logic             select,
    input  logic [WIDTH-1:0] x0,
    input  logic [WIDTH-1:0] x1,
    output logic [WIDTH-1:0] y
);
    assign y = select ? x1 : x0;
endmodule
