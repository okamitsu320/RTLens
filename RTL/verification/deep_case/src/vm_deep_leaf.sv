module vm_deep_leaf (
  input  logic [3:0] in_d,
  input  logic       in_v,
  output logic [3:0] out_d,
  output logic       out_v
);
  assign out_d = in_v ? {in_d[2:0], in_d[3]} : in_d;
  assign out_v = in_v;
endmodule
