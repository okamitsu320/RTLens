module vm_deep_cluster (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [3:0] in_d,
  input  logic       in_v,
  output logic [3:0] out_d,
  output logic       out_v
);
  logic [3:0] s0_d;
  logic       s0_v;

  vm_deep_stage #(.ID(1)) u_stage0 (
    .clk  (clk),
    .rst_n(rst_n),
    .in_d (in_d),
    .in_v (in_v),
    .out_d(s0_d),
    .out_v(s0_v)
  );

  vm_deep_stage #(.ID(2)) u_stage1 (
    .clk  (clk),
    .rst_n(rst_n),
    .in_d (s0_d),
    .in_v (s0_v),
    .out_d(out_d),
    .out_v(out_v)
  );
endmodule
