// Module with a generate-for loop.
//
// Tests: parser behaviour with genvar / generate constructs.
// NOTE: the regex parser does not elaborate generate loops; the individual
//       bit-inverter assign statements inside the loop body are NOT extracted
//       as separate Assignment records.  This fixture documents that known
//       limitation and is used to verify that the parser does not crash or
//       misidentify module boundaries.
module generate_for #(parameter N = 4) (
    input  logic [N-1:0] in_vec,
    output logic [N-1:0] out_vec
);
    genvar i;
    generate
        for (i = 0; i < N; i = i + 1) begin : gen_bits
            assign out_vec[i] = ~in_vec[i];
        end
    endgenerate
endmodule
