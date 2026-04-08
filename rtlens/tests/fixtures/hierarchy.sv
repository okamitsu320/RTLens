module logic_gate (
    input  a,
    input  b,
    output y
);
    assign y = a & b;
endmodule

module hierarchy_top (
    input  clk, a, b, c,
    output out_ab, out_abc
);
    logic mid;
    logic_gate u0 (.a(a),   .b(b),   .y(mid));
    logic_gate u1 (.a(mid), .b(c),   .y(out_ab));
    assign out_abc = mid & out_ab;
endmodule
