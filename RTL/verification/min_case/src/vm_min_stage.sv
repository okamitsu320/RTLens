module vm_min_stage (
    input  logic [1:0] in_bus,
    output logic       out_bit
);
    vm_min_leaf u_leaf0 (
        .a(in_bus[0]),
        .b(in_bus[1]),
        .y(out_bit)
    );
endmodule
