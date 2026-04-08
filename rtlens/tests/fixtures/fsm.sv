// Three-state FSM: IDLE → RUN → FINISH → IDLE
// Tests: always_ff state register, always_comb next-state logic,
//        enum-like typedef parameter, clock/reset tag detection.
module fsm (
    input  logic clk,
    input  logic rst_n,
    input  logic start,
    input  logic done,
    output logic busy,
    output logic ready
);
    typedef enum logic [1:0] {
        IDLE   = 2'b00,
        RUN    = 2'b01,
        FINISH = 2'b10
    } state_t;

    state_t state, next_state;

    // State register
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n)
            state <= IDLE;
        else
            state <= next_state;

    // Next-state + output logic
    always_comb begin
        next_state = state;
        busy  = 1'b0;
        ready = 1'b0;
        case (state)
            IDLE:   if (start) next_state = RUN;
            RUN:    begin busy = 1'b1; if (done) next_state = FINISH; end
            FINISH: begin ready = 1'b1; next_state = IDLE; end
            default: next_state = IDLE;
        endcase
    end
endmodule
