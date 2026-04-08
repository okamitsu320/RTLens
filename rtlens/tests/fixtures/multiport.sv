module multiport #(
    parameter DATA_W = 16,
    parameter ADDR_W = 8
) (
    input                  clk,
    input                  rst_n,
    input  [ADDR_W-1:0]    addr,
    input  [DATA_W-1:0]    wdata,
    input                  we,
    output [DATA_W-1:0]    rdata,
    output                 valid
);
    logic [DATA_W-1:0] mem [0:(1<<ADDR_W)-1];
    logic [DATA_W-1:0] rdata_r;
    logic              valid_r;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_r <= 1'b0;
        end else begin
            if (we) mem[addr] <= wdata;
            rdata_r <= mem[addr];
            valid_r <= 1'b1;
        end
    end

    assign rdata = rdata_r;
    assign valid = valid_r;
endmodule
