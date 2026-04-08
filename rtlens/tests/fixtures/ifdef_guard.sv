// Module with `ifdef / `else / `endif conditional compilation.
//
// Without -DUSE_FAST_PATH (default): the always_ff registered path is active.
// With    -DUSE_FAST_PATH          : the combinational pass-through is active.
//
// Tests: _preprocess_lines macro filtering, defined_macros parameter,
//        different signal/block sets depending on compilation flags.
module ifdef_guard (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [7:0] data_in,
    output logic [7:0] data_out
);
`ifdef USE_FAST_PATH
    // Combinational path (active only with -DUSE_FAST_PATH)
    assign data_out = data_in;
`else
    // Registered path (default, no macro required)
    logic [7:0] data_r;

    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) data_r <= 8'h0;
        else        data_r <= data_in;

    assign data_out = data_r;
`endif
endmodule
