module vm_min_top (
    input  logic [1:0] in_bus,
    input  logic       sel,
    output logic       out_main,
    output logic       out_alt
);
    logic stage_y;

    vm_min_stage u_stage (
        .in_bus(in_bus),
        .out_bit(stage_y)
    );

    assign out_main = sel ? stage_y : in_bus[0];
    assign out_alt  = ~stage_y;
endmodule
