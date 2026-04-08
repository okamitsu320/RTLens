// Two-stage instruction pipeline (fetch → decode).
// Tests: multiple always_ff blocks, inter-stage register signal tracking,
//        clock/reset detection across multiple blocks.
module pipeline (
    input  logic        clk,
    input  logic        rst_n,
    input  logic [31:0] instr_in,
    input  logic        valid_in,
    output logic [31:0] instr_out,
    output logic        valid_out
);
    // Stage-1 pipeline registers
    logic [31:0] s1_instr;
    logic        s1_valid;

    // Stage-2 pipeline registers
    logic [31:0] s2_instr;
    logic        s2_valid;

    // Stage 1: latch inputs
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s1_instr <= 32'h0;
            s1_valid <= 1'b0;
        end else begin
            s1_instr <= instr_in;
            s1_valid <= valid_in;
        end
    end

    // Stage 2: latch stage-1 outputs
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s2_instr <= 32'h0;
            s2_valid <= 1'b0;
        end else begin
            s2_instr <= s1_instr;
            s2_valid <= s1_valid;
        end
    end

    assign instr_out = s2_instr;
    assign valid_out = s2_valid;
endmodule
