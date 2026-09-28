// sync_fifo.v -- small show-ahead FIFO (dout valid whenever !empty).
// A push while full (and not popping) is dropped and flagged on ovf.
module sync_fifo #(
    parameter W    = 8,
    parameter LOG2 = 3
) (
    input  wire          clk,
    input  wire          rst,
    input  wire          push,
    input  wire [W-1:0]  din,
    input  wire          pop,
    output wire [W-1:0]  dout,
    output wire          empty,
    output wire          full,
    output wire [LOG2:0] count,
    output wire          ovf
);
    reg [W-1:0]    mem [0:(1<<LOG2)-1];
    reg [LOG2-1:0] wp, rp;
    reg [LOG2:0]   cnt;
    wire do_pop  = pop && (cnt != 0);
    wire do_push = push && ((cnt != (1<<LOG2)) || do_pop);
    assign dout  = mem[rp];
    assign empty = (cnt == 0);
    assign full  = (cnt == (1<<LOG2));
    assign count = cnt;
    assign ovf   = push && !do_push;
    always @(posedge clk) begin
        if (rst) begin
            wp <= 0; rp <= 0; cnt <= 0;
        end else begin
            if (do_push) begin
                mem[wp] <= din;
                wp <= wp + 1'b1;
            end
            if (do_pop) rp <= rp + 1'b1;
            cnt <= cnt + (do_push ? 1'b1 : 1'b0) - (do_pop ? 1'b1 : 1'b0);
        end
    end
endmodule
