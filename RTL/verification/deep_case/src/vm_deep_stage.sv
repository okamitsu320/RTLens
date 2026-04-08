module vm_deep_stage #(
  parameter int ID = 0
) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [3:0] in_d,
  input  logic       in_v,
  output logic [3:0] out_d,
  output logic       out_v
);
  logic [3:0] leaf_d;
  logic       leaf_v;

  vm_deep_leaf u_leaf (
    .in_d (in_d),
    .in_v (in_v),
    .out_d(leaf_d),
    .out_v(leaf_v)
  );

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      out_d <= '0;
      out_v <= 1'b0;
    end else begin
      out_d <= leaf_d ^ ID[3:0];
      out_v <= leaf_v;
    end
  end
endmodule
