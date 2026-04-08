module counter #(parameter WIDTH = 8) (
    input              clk,
    input              rst_n,
    input              en,
    output [WIDTH-1:0] q
);
    logic [WIDTH-1:0] cnt;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) cnt <= '0;
        else if (en) cnt <= cnt + 1'b1;
    assign q = cnt;
endmodule
