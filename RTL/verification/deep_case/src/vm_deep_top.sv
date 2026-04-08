module vm_deep_top (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [3:0] in_d,
  input  logic       in_v,
  output logic [3:0] out_d,
  output logic       out_v
);
  logic [3:0] c0_d;
  logic       c0_v;
  logic [3:0] c1_d;
  logic       c1_v;

  vm_deep_cluster u_cluster0 (
    .clk  (clk),
    .rst_n(rst_n),
    .in_d (in_d),
    .in_v (in_v),
    .out_d(c0_d),
    .out_v(c0_v)
  );

  vm_deep_cluster u_cluster1 (
    .clk  (clk),
    .rst_n(rst_n),
    .in_d (c0_d),
    .in_v (c0_v),
    .out_d(c1_d),
    .out_v(c1_v)
  );

  assign out_d = c1_d ^ c0_d;
  assign out_v = c1_v & c0_v;
endmodule
