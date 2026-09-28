// order_table.v -- simple dual-port RAM (1 write, 1 registered read),
// read-old-data on same-address collision.  Infers block RAM.
// Entry layout: {valid[129], ref[128:65], side[64], price[63:32], shares[31:0]}
module order_table #(
    parameter AW = 12,
    parameter W  = 130
) (
    input  wire          clk,
    input  wire          we,
    input  wire [AW-1:0] waddr,
    input  wire [W-1:0]  wdata,
    input  wire [AW-1:0] raddr,
    output reg  [W-1:0]  rdata
);
    reg [W-1:0] mem [0:(1<<AW)-1];
    integer i;
    initial begin
        for (i = 0; i < (1<<AW); i = i + 1) mem[i] = {W{1'b0}};
        rdata = {W{1'b0}};
    end
    always @(posedge clk) begin
        if (we) mem[waddr] <= wdata;
        rdata <= mem[raddr];
    end
endmodule
