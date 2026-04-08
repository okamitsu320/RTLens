module simple_assign (
    input  [3:0] a, b,
    input        sel,
    output [3:0] y,
    output       eq
);
    assign y  = sel ? a : b;
    assign eq = (a == b);
endmodule
