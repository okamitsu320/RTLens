module callable (
    input  [7:0] a, b,
    input  [1:0] op,
    output logic [7:0] result
);
    function automatic [7:0] alu_op(input [7:0] x, y, input [1:0] op);
        case (op)
            2'b00: alu_op = x + y;
            2'b01: alu_op = x - y;
            2'b10: alu_op = x & y;
            default: alu_op = x | y;
        endcase
    endfunction
    always_comb result = alu_op(a, b, op);
endmodule
